
import h5py
import pandas as pd
import numpy as np
import folium
from folium.plugins import PolyLineTextPath
import unicodedata
import json
import networkx as nx
import sys
from pathlib import Path
from datetime import datetime
import duckdb
from adopt_net0.result_management.read_results import extract_datasets_from_h5group

# Ensure UTF-8 output to terminal to avoid encoding errors with non-ASCII node names
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load storage/port node sets from combined_selected_manual_edit.xlsx
storage_nodes_set = set()
port_nodes_set = set()
try:
    df_nodes = pd.read_excel(
        Path(__file__).parent / "2_data_processed" / "intermediate_output" / "combined_selected_manual_edit.xlsx"
    )
    if "name_sanitized" in df_nodes.columns and "type" in df_nodes.columns:
        storage_nodes_set = set(
            df_nodes[df_nodes["type"].str.lower().str.contains("storage", na=False)]["name_sanitized"].astype(str).str.strip()
        )
        port_nodes_set = set(
            df_nodes[df_nodes["type"].str.lower().str.contains("port", na=False)]["name_sanitized"].astype(str).str.strip()
        )
except Exception as e:
    print(f"[WARN] Could not load storage/port node sets: {e}")

# ── Load iso2 map from database ──────────────────────────
db_path = Path(__file__).parent / "database.duckdb"
node_iso2_map = {}   # name_sanitized -> iso2
try:
    con = duckdb.connect(str(db_path), read_only=True)
    iso2_df = con.execute(
        "SELECT name_sanitized, iso2 FROM combined_selected_final WHERE name_sanitized IS NOT NULL AND iso2 IS NOT NULL"
    ).df()
    con.close()
    node_iso2_map = dict(zip(iso2_df["name_sanitized"].astype(str).str.strip(),
                             iso2_df["iso2"].astype(str).str.strip()))
except Exception as e:
    print(f"[WARN] Could not load iso2 map from database: {e}")

# Set the exact run file here (single-run mode).
h5_file = Path(r"C:\Users\dutka\MT\AdOpT-NET0_dw\2026_project\results\20260607043639-1_3M_SL10K_MG2_L0_NF0_CC0_LQ_80%\optimization_results.h5")
node_loc_file = Path(r"C:\Users\dutka\MT\AdOpT-NET0_dw\2026_project\3_model_inputs\NodeLocations.csv")
output_excel  = h5_file.parent / "results.xlsx"
output_map    = h5_file.parent / "network_map.html"

# ── SCENARIO CONFIGURATION (keep in sync with main.py) ──────────────────────────────────────
# Set SCENARIO to the same value used in main.py when this result was generated.
# Used to name the capacity-tracking JSON output.
SCENARIO = "Base"  # ← CHANGE THIS: "Conservative", "Base", or "Optimistic"

SCENARIO_CONFIG = {
    "Conservative": {"label": "Conservative_EarlyPhase",  "opex_var_storage_EUR_per_t": 75.8},
    "Base":         {"label": "Base_MidPhase",             "opex_var_storage_EUR_per_t": 50.6},
    "Optimistic":   {"label": "Optimistic_MaturePhase",    "opex_var_storage_EUR_per_t": 42.5},
}
_sc_result = SCENARIO_CONFIG[SCENARIO]

# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════

name_repair = {
    "BatÄ±Ã§im Bornova Cement Plant":
        "Batıçim Bornova Cement Plant",
    "Ä°DÃ‡ Izdemir Aliaga steel plant":
        "İDÇ Izdemir Aliaga steel plant",
    "CEMENTOS MOLINS INDUSTRIAL (SANT VICENÃ‡ DELS HORTS)":
        "CEMENTOS MOLINS INDUSTRIAL (SANT VICENÇ DELS HORTS)",
    "UnitÃ  Locale 3 - Impianto di Termovalorizzazione rifiuti non pericolosi":
        "Unità Locale 3 - Impianto di Termovalorizzazione rifiuti non pericolosi",
    "EVERÃ‰ SAS":          "ÉVERÉ SAS",
}

def normalize(s):
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    return unicodedata.normalize("NFKD", str(s)) \
                      .encode("ascii", errors="ignore") \
                      .decode("ascii").strip()

def repair_name(s):
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    s = str(s).strip()
    if s in name_repair:
        return name_repair[s]
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s

def decode(v):
    """Decode bytes to str, leave everything else as-is."""
    return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v

def parse_linestring_wkt(wkt_str):
    """Parse LINESTRING WKT into folium path format [(lat, lon), ...]."""
    if pd.isna(wkt_str):
        return None
    s = str(wkt_str).strip()
    if not s.upper().startswith("LINESTRING"):
        return None
    left = s.find("(")
    right = s.rfind(")")
    if left < 0 or right <= left:
        return None
    body = s[left + 1:right].strip()
    if not body:
        return None

    points = []
    for chunk in body.split(","):
        vals = [v for v in chunk.strip().split(" ") if v]
        if len(vals) < 2:
            return None
        try:
            lon = float(vals[0])
            lat = float(vals[1])
        except ValueError:
            return None
        points.append((lat, lon))

    return points if len(points) >= 2 else None

def load_ship_route_geometries(base_dir):
    """Load ship route WKT, prefer manual file when available and valid."""
    inter_dir = base_dir / "2_data_processed" / "intermediate_output"
    manual_path = inter_dir / "ship_routes_manual_edit.xlsx"
    default_path = inter_dir / "ship_routes.xlsx"

    route_map = {}
    loaded_from = []

    def add_from_file(path, prioritize=False):
        if not path.exists():
            return
        try:
            df = pd.read_excel(path)
        except Exception as exc:
            print(f"⚠️ Could not read ship route file: {path} ({exc})")
            return

        required = {"from_port", "to_port", "geometry_wkt"}
        if not required.issubset(df.columns):
            return

        work = df.copy()
        if "selection" in work.columns:
            sel = work["selection"].astype(str).str.strip().str.lower()
            work = work[sel == "yes"]

        added = 0
        for _, r in work.iterrows():
            geom = parse_linestring_wkt(r.get("geometry_wkt"))
            if geom is None:
                continue
            f = normalize(repair_name(r.get("from_port", "")))
            t = normalize(repair_name(r.get("to_port", "")))
            if not f or not t:
                continue
            key = (f, t)
            rev_key = (t, f)
            if prioritize or key not in route_map:
                route_map[key] = geom
                route_map[rev_key] = list(reversed(geom))
                added += 1

        loaded_from.append((path.name, added))

    add_from_file(default_path, prioritize=False)
    add_from_file(manual_path, prioritize=True)

    if loaded_from:
        msg = ", ".join([f"{name}: {count}" for name, count in loaded_from])
        print(f"Loaded ship geometries ({msg})")
   
    return route_map


def load_manual_pipeline_geometries(base_dir):
    """Load manual pipeline WKT geometry from pipeline_network_manual_edit.xlsx.
    Returns dict keyed by normalized (from_name, to_name) with reversed geometry too.
    Geometry is loaded for ANY row with a valid geometry_wkt, regardless of selection status.
    (The selection filter is intentionally omitted: geometry is only used when a route is
    already drawn as an active model arc, so extra entries in the map are harmless.)
    """
    inter_dir = base_dir / "2_data_processed" / "intermediate_output"
    manual_path = inter_dir / "pipeline_network_manual_edit.xlsx"

    route_map = {}
    if not manual_path.exists():
        return route_map

    try:
        df = pd.read_excel(manual_path)
    except Exception as exc:
        print(f"⚠️ Could not read manual pipeline file: {manual_path} ({exc})")
        return route_map

    required = {"from_name", "to_name", "geometry_wkt"}
    if not required.issubset(df.columns):
        print("[INFO] Manual pipeline geometry not used: missing one of from_name/to_name/geometry_wkt")
        return route_map

    added = 0
    for _, r in df.iterrows():
        geom = parse_linestring_wkt(r.get("geometry_wkt"))
        if geom is None:
            continue
        f = normalize(repair_name(str(r.get("from_name", ""))))
        t = normalize(repair_name(str(r.get("to_name", ""))))
        if not f or not t:
            continue

        route_map[(f, t)] = geom
        route_map[(t, f)] = list(reversed(geom))
        added += 1

    print(f"Loaded manual pipeline geometries ({manual_path.name}: {added} routes with WKT)")
    return route_map

def flatten_dict(raw):
    """Flatten nested dict into a single-column DataFrame keyed by tuple path."""
    if isinstance(raw, pd.DataFrame):
        return raw
    if not isinstance(raw, dict):
        return pd.DataFrame([raw])
    rows = []
    def _flatten(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _flatten(v, f"{prefix}{k} | " if prefix else f"{k} | ")
        elif isinstance(obj, (list, np.ndarray)):
            for i, v in enumerate(obj):
                rows.append({"key": f"{prefix}{i}", "value": decode(v)})
        else:
            rows.append({"key": prefix.rstrip(" | "), "value": decode(obj)})
    _flatten(raw)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("key")

# ══════════════════════════════════════════════════════════
# LOAD RAW DATA
# ══════════════════════════════════════════════════════════

with h5py.File(h5_file, "r") as f:
    raw_summary   = extract_datasets_from_h5group(f["summary"])
    raw_topology  = extract_datasets_from_h5group(f["topology"])
    raw_kmeans    = extract_datasets_from_h5group(f["k_means_specs"])
    raw_nodes     = extract_datasets_from_h5group(f["design"]["nodes"])
    raw_networks  = extract_datasets_from_h5group(f["design"]["networks"])
    raw_tec_op    = extract_datasets_from_h5group(f["operation"]["technology_operation"])

# Shared annualization factor for node-level KPIs
topology_cfg_path = Path(__file__).parent / "3_model_inputs" / "Topology.json"
hours_per_timestep_nodes = 1.0
try:
    with open(topology_cfg_path) as fh:
        topology_cfg = json.load(fh)
    res = str(topology_cfg.get("resolution", "1h")).strip().lower()
    if res.endswith("h"):
        hours_per_timestep_nodes = float(res[:-1])
except Exception:
    hours_per_timestep_nodes = 1.0

modelled_timesteps_nodes = 0
for _k, _v in raw_tec_op.items():
    arr_size = int(np.array(_v).size)
    if arr_size > 0:
        modelled_timesteps_nodes = arr_size
        break
modelled_hours_nodes = modelled_timesteps_nodes * hours_per_timestep_nodes if modelled_timesteps_nodes > 0 else 0.0
annualization_factor_nodes = (8760.0 / modelled_hours_nodes) if modelled_hours_nodes > 0 else 1.0

node_co2_captured_modelled = {}
for k, v in raw_tec_op.items():
    if not (isinstance(k, tuple) and len(k) == 4):
        continue
    _, node, _tec, var = k
    if var == "CO2captured_var_output_ccs":
        node_co2_captured_modelled[node] = node_co2_captured_modelled.get(node, 0.0) + float(np.array(v).sum())

# ── Parse nodes into wide DataFrame ──────────────────────
# Index = (period, node_name, technology, variable)
NODE_VARS = [
    "technology", "existing", "size", "size_ccs",
    "capex_tec", "capex_ccs", "capex_tot",
    "opex_fixed_tot", "opex_fixed_ccs", "opex_variable",
    "emissions_pos", "emissions_neg", "para_unitCAPEX",
]

node_records = {}
for idx, row in pd.DataFrame.from_dict(raw_nodes, orient="index").iterrows():
    if not isinstance(idx, tuple) or len(idx) < 4:
        continue
    _, node_name, technology, variable = idx
    if variable not in NODE_VARS:
        continue
    key = (node_name, technology)
    if key not in node_records:
        node_records[key] = {"node": node_name, "technology": technology}
    node_records[key][variable] = decode(row.iloc[0])

nodes_wide = pd.DataFrame(list(node_records.values()))

# Clean numeric columns
for col in ["size","size_ccs","capex_tec","capex_ccs","capex_tot",
            "opex_fixed_tot","opex_fixed_ccs","opex_variable",
            "emissions_pos","emissions_neg","para_unitCAPEX","existing"]:
    if col in nodes_wide.columns:
        nodes_wide[col] = pd.to_numeric(nodes_wide[col], errors="coerce")

# ── Parse networks into wide DataFrame ───────────────────
# Index = (period, network_type, arc_name, variable)
NET_VARS = [
    "fromNode", "toNode", "network", "size",
    "total_flow", "total_emissions",
    "capex", "opex_fixed", "opex_variable",
    "para_capex_gamma1", "para_capex_gamma2",
    "para_capex_gamma3", "para_capex_gamma4",
]

net_records = {}
for idx, row in pd.DataFrame.from_dict(raw_networks, orient="index").iterrows():
    if not isinstance(idx, tuple) or len(idx) < 4:
        continue
    _, network_type, arc_name, variable = idx
    if variable not in NET_VARS:
        continue
    key = (network_type, arc_name)
    if key not in net_records:
        net_records[key] = {"mode": network_type, "arc": arc_name}
    net_records[key][variable] = decode(row.iloc[0])

networks_wide = pd.DataFrame(list(net_records.values()))

# Repair node names
for col in ["fromNode", "toNode"]:
    if col in networks_wide.columns:
        networks_wide[col] = networks_wide[col].apply(repair_name)

# Clean numeric columns
for col in ["size","total_flow","total_emissions","capex",
            "opex_fixed","opex_variable"]:
    if col in networks_wide.columns:
        networks_wide[col] = pd.to_numeric(networks_wide[col], errors="coerce")

# ── Filter to Pipeline + Ship only ───────────────────────
networks_wide = networks_wide[
    networks_wide["mode"].isin(["CO2_Pipeline", "CO2Ship"])
].copy()

# ── Active subsets ────────────────────────────────────────
active_arcs  = networks_wide[networks_wide["size"] > 0.01].copy()
inactive_arcs = networks_wide[networks_wide["size"] <= 0.01].copy()
active_nodes_set = set(active_arcs["fromNode"]) | set(active_arcs["toNode"])
all_nodes_set = set(nodes_wide["node"])
inactive_nodes_set = all_nodes_set - active_nodes_set
active_nodes = nodes_wide[nodes_wide["node"].isin(active_nodes_set)].copy()
inactive_nodes = nodes_wide[nodes_wide["node"].isin(inactive_nodes_set)].copy()

# Add role column
from_set = set(active_arcs["fromNode"])
to_set   = set(active_arcs["toNode"])
def get_role(n):
    in_from = n in from_set
    in_to   = n in to_set
    if in_from and in_to: return "transit"
    if in_from:           return "emitter"
    return "storage/sink"

active_nodes["role"] = active_nodes["node"].apply(get_role)
inactive_nodes["role"] = inactive_nodes["node"].apply(lambda _: "inactive")

# Identify sink-like nodes and perform active-network component sanity checks.
storage_nodes = set(
    nodes_wide.loc[
        nodes_wide["technology"].astype(str).str.contains("storage", case=False, na=False),
        "node",
    ].tolist()
)
sink_like_nodes = (to_set - from_set) | storage_nodes

# Directed graph: used to trace which storage node each emitter/transit ultimately feeds into.
_digraph = nx.DiGraph()
_digraph.add_edges_from(active_arcs[["fromNode", "toNode"]].itertuples(index=False, name=None))

def _final_storage(node):
    """Return the storage node(s) reachable downstream from this node in the active CO2 network."""
    if node in storage_nodes:
        return node
    try:
        reachable = nx.descendants(_digraph, node)
    except nx.NetworkXError:
        return ""
    destinations = sorted(reachable & storage_nodes)
    return " | ".join(destinations) if destinations else ""

active_graph = nx.Graph()
active_graph.add_nodes_from(active_nodes_set)
active_graph.add_edges_from(active_arcs[["fromNode", "toNode"]].itertuples(index=False, name=None))

component_rows = []
for comp_id, comp_nodes in enumerate(nx.connected_components(active_graph), start=1):
    comp_nodes = sorted(comp_nodes)
    comp_set = set(comp_nodes)
    comp_arcs = active_arcs[
        active_arcs["fromNode"].isin(comp_set) & active_arcs["toNode"].isin(comp_set)
    ]
    comp_sinks = sorted(comp_set & sink_like_nodes)
    comp_emitters = sorted(comp_set & from_set)
    component_rows.append({
        "component_id": comp_id,
        "n_nodes": len(comp_set),
        "n_arcs": len(comp_arcs),
        "has_sink": bool(comp_sinks),
        "sink_nodes": " | ".join(comp_sinks),
        "example_emitters": " | ".join(comp_emitters[:6]),
        "all_nodes": " | ".join(comp_nodes),
    })

components_out = pd.DataFrame(component_rows).sort_values("component_id").reset_index(drop=True)
components_out.index += 1

components_without_sink = components_out[components_out["has_sink"] == False]

# Reorder columns nicely
arc_cols = ["mode","fromNode","toNode","size","total_flow",
            "total_emissions","capex","opex_fixed","opex_variable",
            "para_capex_gamma1","para_capex_gamma2",
            "para_capex_gamma3","para_capex_gamma4"]
arc_cols = [c for c in arc_cols if c in networks_wide.columns]

node_cols = ["node","role","technology","existing","size","size_ccs",
             "capex_tec","capex_ccs","capex_tot",
             "opex_fixed_tot","opex_fixed_ccs","opex_variable",
             "emissions_pos","emissions_neg","para_unitCAPEX"]
node_cols = [c for c in node_cols if c in active_nodes.columns]

# ── Combined Node sheet (active + inactive, with status & iso2) ──
active_nodes["status"] = "active"
inactive_nodes["status"] = "inactive"
nodes_combined = pd.concat([active_nodes, inactive_nodes], ignore_index=True)
nodes_combined["iso2"] = nodes_combined["node"].map(
    lambda n: node_iso2_map.get(str(n).strip(), "")
)
nodes_combined["CO2_captured_t_modelled"] = nodes_combined["node"].map(
    lambda n: node_co2_captured_modelled.get(str(n), 0.0)
)
nodes_combined["CO2_captured_t_annualized"] = (
    pd.to_numeric(nodes_combined["CO2_captured_t_modelled"], errors="coerce").fillna(0.0)
    * annualization_factor_nodes
)
# Final storage: downstream geological storage node(s) this node ultimately sends CO2 to.
nodes_combined["final_storage"] = nodes_combined["node"].apply(_final_storage)

# capex_tot and opex_fixed_tot are already annualized in the H5 design output.
# opex_variable is the raw modelled-period total and must be scaled to annual basis.
nodes_combined["node_direct_cost_EUR"] = (
    pd.to_numeric(nodes_combined.get("capex_tot", 0.0), errors="coerce").fillna(0.0)
    + pd.to_numeric(nodes_combined.get("opex_fixed_tot", 0.0), errors="coerce").fillna(0.0)
    + pd.to_numeric(nodes_combined.get("opex_variable", 0.0), errors="coerce").fillna(0.0) * annualization_factor_nodes
)
# Capture-only excludes storage technologies and therefore isolates emitter-side capture cost.
nodes_combined["node_capture_only_cost_EUR"] = np.where(
    nodes_combined["node"].isin(storage_nodes),
    0.0,
    nodes_combined["node_direct_cost_EUR"],
)
nodes_combined["node_capture_only_cost_EUR_per_t"] = np.where(
    nodes_combined["CO2_captured_t_annualized"] > 0,
    nodes_combined["node_capture_only_cost_EUR"] / nodes_combined["CO2_captured_t_annualized"],
    np.nan,
)
nodes_combined = nodes_combined.rename(
    columns={"node_capture_only_cost_EUR_per_t": "node_capture_only_cost_EUR_per_t_annualized"}
)

combined_node_cols = ["node","status","iso2","role","final_storage","technology","existing","size","size_ccs",
                      "capex_tec","capex_ccs","capex_tot",
                      "opex_fixed_tot","opex_fixed_ccs","opex_variable",
                      "emissions_pos","emissions_neg","para_unitCAPEX",
                      "CO2_captured_t_modelled","CO2_captured_t_annualized",
                      "node_capture_only_cost_EUR","node_capture_only_cost_EUR_per_t_annualized"]
combined_node_cols = [c for c in combined_node_cols if c in nodes_combined.columns]
nodes_combined_out = nodes_combined[combined_node_cols].sort_values(
    ["status","role","node"]).reset_index(drop=True)
nodes_combined_out.index += 1

# ── Combined Arc sheet (active + inactive, with status & fromNode iso2) ──
active_arcs["status"] = "active"
inactive_arcs["status"] = "inactive"
arcs_combined = pd.concat([active_arcs, inactive_arcs], ignore_index=True)
arcs_combined["iso2"] = arcs_combined["fromNode"].map(
    lambda n: node_iso2_map.get(str(n).strip(), "")
)
combined_arc_cols = ["mode","status","fromNode","iso2","toNode","size","total_flow",
                     "total_emissions","capex","opex_fixed","opex_variable",
                     "para_capex_gamma1","para_capex_gamma2",
                     "para_capex_gamma3","para_capex_gamma4"]
combined_arc_cols = [c for c in combined_arc_cols if c in arcs_combined.columns]
arcs_combined_out = arcs_combined[combined_arc_cols].sort_values(
    ["mode","status","size"], ascending=[True,True,False]).reset_index(drop=True)
arcs_combined_out.index += 1

# Keep legacy subsets for the map / sanity checks
active_arcs_out  = active_arcs[arc_cols].sort_values(
    ["mode","size"], ascending=[True,False]).reset_index(drop=True)
active_arcs_out.index += 1

active_nodes_out = active_nodes[node_cols].sort_values(
    ["role","node"]).reset_index(drop=True)
active_nodes_out.index += 1

# ══════════════════════════════════════════════════════════
# PARAMETERS SHEET
# ══════════════════════════════════════════════════════════
def _build_parameters_df(h5_file_path: Path) -> pd.DataFrame:
    """Collect key model parameters from ConfigModel.json, network JSONs,
    CarbonCost.csv, and the H5 summary into a tidy DataFrame."""
    rows = []
    base = h5_file_path.parent.parent.parent  # 2026_project/
    period_dir = base / "3_model_inputs" / "period1"
    cfg = {}

    def _add_row(parameter, value, unit="", description="", source=""):
        rows.append(
            {
                "parameter": parameter,
                "value": value,
                "unit": unit,
                "description": description,
                "source": source,
            }
        )

    def _to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # ── ConfigModel.json ──────────────────────────────────
    cfg_path = base / "3_model_inputs" / "ConfigModel.json"
    try:
        with open(cfg_path) as fh:
            cfg = json.load(fh)
        def _cfg(section, key, unit="", description=""):
            val = cfg.get(section, {}).get(key, {}).get("value", "N/A")
            _add_row(f"{section}.{key}", val, unit, description, "ConfigModel.json")
        _cfg("optimization", "objective", description="Optimization objective")
        _cfg("optimization", "emission_limit", "t CO2", "Emission limit (if objective = costs_emissionlimit)")
        _cfg("optimization", "pareto_points", description="Number of Pareto points")
        _cfg("solveroptions", "solver", description="Solver")
        _cfg("solveroptions", "mipgap", description="MIP gap tolerance")
        _cfg("solveroptions", "timelim", "h", "Solver time limit")
        _cfg("solveroptions", "mipfocus", description="MIP focus")
        _cfg("solveroptions", "numericfocus", description="Numeric focus")
        _cfg("energybalance", "copperplate", description="Copper-plate energy balance (1=yes)")
        _cfg("economic", "global_discountrate", description="Global discount rate (-1 = per-technology)")
    except Exception as e:
        _add_row("ConfigModel", str(e), "", "Error loading", "ConfigModel.json")

    # ── Topology.json ─────────────────────────────────────
    topology_path = base / "3_model_inputs" / "Topology.json"
    modelled_timesteps = None
    hours_per_timestep = 1.0
    modelled_hours = None
    try:
        with open(topology_path) as fh:
            topology = json.load(fh)
        _add_row(
            "topology.start_date",
            topology.get("start_date", "N/A"),
            "",
            "Model start datetime from Topology.json",
            "Topology.json",
        )
        _add_row(
            "topology.end_date",
            topology.get("end_date", "N/A"),
            "",
            "Model end datetime from Topology.json",
            "Topology.json",
        )

        resolution = str(topology.get("resolution", "1h")).strip().lower()
        if resolution.endswith("h"):
            try:
                hours_per_timestep = float(resolution[:-1])
            except ValueError:
                hours_per_timestep = 1.0

        # Preferred source for timestamp count: operation results in H5.
        with h5py.File(h5_file_path, "r") as fh:
            raw_eb = extract_datasets_from_h5group(fh["operation"]["energy_balance"])
        for arr in raw_eb.values():
            arr_size = int(np.array(arr).size)
            if arr_size > 0:
                modelled_timesteps = arr_size
                break

        # Fallback to date range if operation data was not available.
        if modelled_timesteps is None:
            start = pd.to_datetime(topology.get("start_date"), errors="coerce")
            end = pd.to_datetime(topology.get("end_date"), errors="coerce")
            if pd.notna(start) and pd.notna(end) and hours_per_timestep > 0:
                total_hours = (end - start).total_seconds() / 3600.0
                if total_hours > 0:
                    modelled_timesteps = int(round(total_hours / hours_per_timestep))

        if modelled_timesteps is not None:
            modelled_hours = modelled_timesteps * hours_per_timestep

        _add_row(
            "operation.n_timestamps",
            modelled_timesteps if modelled_timesteps is not None else "N/A",
            "-",
            "Number of operation timesteps represented in H5",
            "optimization_results.h5",
        )
        _add_row(
            "operation.hours_per_timestep",
            hours_per_timestep,
            "h",
            "Hours represented by each timestep (from Topology.json resolution)",
            "Topology.json",
        )
        _add_row(
            "operation.modelled_hours",
            modelled_hours if modelled_hours is not None else "N/A",
            "h",
            "Total modelled operating hours (n_timestamps * hours_per_timestep)",
            "optimization_results.h5 + Topology.json",
        )
    except Exception as e:
        _add_row("Topology", str(e), "", "Error loading", "Topology.json")

    # ── Carbon cost ───────────────────────────────────────
    try:
        node_data_dir = period_dir / "node_data"
        cc_val = None
        for node_dir in sorted(node_data_dir.iterdir()):
            cc_csv = node_dir / "CarbonCost.csv"
            if cc_csv.exists():
                df_cc = pd.read_csv(cc_csv, sep=";")
                if "price" in df_cc.columns:
                    cc_val = pd.to_numeric(df_cc["price"], errors="coerce").dropna().iloc[0]
                    break
        _add_row(
            "carbon_price",
            cc_val if cc_val is not None else "N/A",
            "€/t CO2",
            "Carbon cost applied to all nodes",
            "CarbonCost.csv",
        )
    except Exception as e:
        _add_row("carbon_price", str(e), "€/t CO2", "Error loading", "CarbonCost.csv")

    # ── Storage constraints (size_max / injection_rate_max) ───────────────
    try:
        storage_jsons = sorted(
            (period_dir / "node_data").glob("*/technology_data/PermanentStorage_CO2_simple.json")
        )
        storage_rows = []
        for p in storage_jsons:
            node_name = p.parts[-3]
            with open(p) as fh:
                jd = json.load(fh)
            size_max = _to_float(jd.get("size_max"))
            inj_max = _to_float(jd.get("Flexibility", {}).get("injection_rate_max"))
            storage_rows.append((node_name, size_max, inj_max))

        size_max_sum = sum(v for _, v, _ in storage_rows if v is not None)
        inj_max_sum = sum(v for _, _, v in storage_rows if v is not None)

        _add_row(
            "storage.n_nodes",
            len(storage_rows),
            "-",
            "Number of storage nodes with PermanentStorage_CO2_simple.json",
            "period1/node_data/*/technology_data/PermanentStorage_CO2_simple.json",
        )
        _add_row(
            "storage.size_max_total",
            size_max_sum,
            "t CO2",
            "Sum of storage size_max across storage nodes (upper bound on injectable quantity in model horizon)",
            "PermanentStorage_CO2_simple.json",
        )
        _add_row(
            "storage.injection_rate_max_total",
            inj_max_sum,
            "t/h",
            "Sum of injection_rate_max across storage nodes",
            "PermanentStorage_CO2_simple.json",
        )

        # Add per-node visibility for auditability.
        for node_name, size_max, inj_max in storage_rows:
            _add_row(
                f"storage.{node_name}.size_max",
                size_max if size_max is not None else "N/A",
                "t CO2",
                "Storage size_max used to limit injectable quantity",
                "PermanentStorage_CO2_simple.json",
            )
            _add_row(
                f"storage.{node_name}.injection_rate_max",
                inj_max if inj_max is not None else "N/A",
                "t/h",
                "Maximum storage injection rate",
                "PermanentStorage_CO2_simple.json",
            )
    except Exception as e:
        _add_row("storage_constraints", str(e), "", "Error loading", "node_data storage json")

    # ── Emission limit + % reduction vs total emitter emissions ───────────
    try:
        objective = cfg.get("optimization", {}).get("objective", {}).get("value", "N/A") if cfg else "N/A"
        emission_limit = _to_float(
            cfg.get("optimization", {}).get("emission_limit", {}).get("value") if cfg else None
        )

        total_emission_tpa = None
        if db_path.exists():
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                total_emission_tpa = con.execute(
                    """
                    SELECT SUM(CAST(emission_TPA AS DOUBLE))
                    FROM combined_selected_final
                    WHERE lower(type) = 'emitter' AND selection = 'Yes'
                    """
                ).fetchone()[0]
                if total_emission_tpa is not None:
                    total_emission_tpa = float(total_emission_tpa)
            finally:
                con.close()

        _add_row(
            "emission_limit.active",
            "Yes" if str(objective) == "costs_emissionlimit" else "No",
            "-",
            "Whether net emission limit constraint is active (objective = costs_emissionlimit)",
            "ConfigModel.json",
        )
        _add_row(
            "emission_limit.value",
            emission_limit if emission_limit is not None else "N/A",
            "t CO2",
            "Configured emission_limit",
            "ConfigModel.json",
        )
        _add_row(
            "emitters.total_emission_TPA",
            total_emission_tpa if total_emission_tpa is not None else "N/A",
            "t CO2/y",
            "Total annual emissions from selected emitters (combined_selected_final)",
            "database.duckdb",
        )

        reduction_pct = None
        if (total_emission_tpa is not None) and (emission_limit is not None) and (total_emission_tpa > 0):
            reduction_pct = ((total_emission_tpa - emission_limit) / total_emission_tpa) * 100.0
        _add_row(
            "emission_limit.reduction_percent",
            reduction_pct if reduction_pct is not None else "N/A",
            "%",
            "((total_emission_from_emitters - emission_limit) / total_emission_from_emitters) * 100",
            "ConfigModel.json + database.duckdb",
        )
    except Exception as e:
        _add_row("emission_limit.reduction_percent", str(e), "%", "Error calculating", "ConfigModel.json + database.duckdb")

    # ── Network JSONs ─────────────────────────────────────
    for net_name in ["CO2_Pipeline", "CO2Ship"]:
        net_path = period_dir / "network_data" / f"{net_name}.json"
        try:
            with open(net_path) as fh:
                net = json.load(fh)
            eco = net.get("Economics", {})
            perf = net.get("Performance", {})
            prefix = net_name
            _add_row(f"{prefix}.loss", perf.get("loss", "N/A"), "fraction/km", "Transport loss per km", f"{net_name}.json")
            _add_row(f"{prefix}.size_max", net.get("size_max", "N/A"), "t/h", "Maximum network arc size", f"{net_name}.json")
            _add_row(f"{prefix}.capex_gamma1", eco.get("gamma1", "N/A"), "€", "CAPEX fixed term", f"{net_name}.json")
            _add_row(
                f"{prefix}.capex_gamma2",
                eco.get("gamma2", "N/A"),
                "€/(t/h) or €/(t/h/km)",
                "CAPEX capacity or distance-based term",
                f"{net_name}.json",
            )
            _add_row(f"{prefix}.OPEX_variable", eco.get("OPEX_variable", "N/A"), "€/t", "Variable OPEX per tonne transported", f"{net_name}.json")
            _add_row(f"{prefix}.OPEX_fixed", eco.get("OPEX_fixed", "N/A"), "% of CAPEX", "Fixed OPEX as fraction of CAPEX", f"{net_name}.json")
            _add_row(f"{prefix}.discount_rate", eco.get("discount_rate", "N/A"), "-", "Discount rate for annualization", f"{net_name}.json")
            _add_row(f"{prefix}.lifetime", eco.get("lifetime", "N/A"), "years", "Asset lifetime", f"{net_name}.json")
            _add_row(f"{prefix}.loss2emissions", perf.get("loss2emissions", "N/A"), "-", "Whether transport loss counts as CO2 emission", f"{net_name}.json")
        except Exception as e:
            _add_row(f"{net_name}", str(e), "", "Error loading", f"{net_name}.json")

    return pd.DataFrame(rows)[["parameter", "value", "unit", "description", "source"]]

parameters_df = _build_parameters_df(h5_file)

# ══════════════════════════════════════════════════════════
# CO2_CAPTURE SHEET
# ══════════════════════════════════════════════════════════
def _build_co2_capture_df(h5_path: Path, nodes_wide_df: pd.DataFrame,
                           networks_wide_df: pd.DataFrame,
                           summary_raw: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build overall, per-storage, per-component and per-emitter CO2 capture cost summaries."""
    # Identify storage nodes from design data
    stor_node_names = set(
        nodes_wide_df.loc[
            nodes_wide_df["technology"].astype(str).str.contains("storage", case=False, na=False),
            "node",
        ]
    )

    # Sum CO2 network_inflow per node from energy balance
    co2_inflow_per_node: dict[str, float] = {}
    with h5py.File(h5_path, "r") as fh:
        raw_eb = extract_datasets_from_h5group(fh["operation"]["energy_balance"])
        raw_tec_op = extract_datasets_from_h5group(fh["operation"]["technology_operation"])

    for k, v in raw_eb.items():
        if not (isinstance(k, tuple) and len(k) == 4):
            continue
        _, node, carrier, var = k
        if carrier == "CO2captured" and var == "network_inflow":
            arr_val = float(np.array(v).sum())
            if arr_val > 0:
                co2_inflow_per_node[node] = co2_inflow_per_node.get(node, 0.0) + arr_val

    # Restrict to storage nodes only
    stor_co2_inflow = {n: v for n, v in co2_inflow_per_node.items() if n in stor_node_names}
    total_co2_injected = sum(stor_co2_inflow.values())

    # Annualize CO2 denominator to match annualized costs in summary.
    # For weekly models (e.g., 168 x 1h), this avoids inflating €/t by ~52x.
    base = h5_path.parent.parent.parent
    topology_path = base / "3_model_inputs" / "Topology.json"
    hours_per_timestep = 1.0
    try:
        with open(topology_path) as fh:
            topology = json.load(fh)
        resolution = str(topology.get("resolution", "1h")).strip().lower()
        if resolution.endswith("h"):
            hours_per_timestep = float(resolution[:-1])
    except Exception:
        hours_per_timestep = 1.0

    modelled_timesteps = 0
    if stor_co2_inflow:
        for k, v in raw_eb.items():
            if not (isinstance(k, tuple) and len(k) == 4):
                continue
            _, node, carrier, var = k
            if node in stor_node_names and carrier == "CO2captured" and var == "network_inflow":
                modelled_timesteps = int(np.array(v).size)
                if modelled_timesteps > 0:
                    break
    if modelled_timesteps <= 0:
        # Fallback: infer from any energy-balance series.
        for v in raw_eb.values():
            arr_size = int(np.array(v).size)
            if arr_size > 0:
                modelled_timesteps = arr_size
                break

    modelled_hours = modelled_timesteps * hours_per_timestep if modelled_timesteps > 0 else 0.0
    annualization_factor = (8760.0 / modelled_hours) if modelled_hours > 0 else 1.0
    total_co2_injected_annualized = total_co2_injected * annualization_factor

    # Pull design costs for each storage node (summed across technologies)
    stor_design = (
        nodes_wide_df[nodes_wide_df["node"].isin(stor_node_names)]
        .groupby("node", as_index=False)
        .agg(
            capex_tot=("capex_tot", "sum"),
            opex_fixed_tot=("opex_fixed_tot", "sum"),
            opex_variable=("opex_variable", "sum"),
            injection_capacity=("size", "sum"),
        )
    )

    # Summary entry: total model cost from summary
    def _get_summary_val(key):
        for k in summary_raw:
            if isinstance(k, tuple) and k[0] == key:
                v = summary_raw[k]
                if isinstance(v, (list, np.ndarray)):
                    v = v[0] if len(v) > 0 else None
                return float(v) if not isinstance(v, (bytes, str)) else decode(v)
        return None

    total_cost = _get_summary_val("total_cost")
    emissions_net = _get_summary_val("emissions_net")
    carbon_cost = _get_summary_val("carbon_cost")
    carbon_revenue = _get_summary_val("carbon_revenue")
    cost_capex_tecs = _get_summary_val("cost_capex_tecs")
    cost_capex_netws = _get_summary_val("cost_capex_netws")
    cost_opex_tecs = _get_summary_val("cost_opex_tecs")
    cost_opex_netws = _get_summary_val("cost_opex_netws")
    cost_imports = _get_summary_val("cost_imports")
    cost_exports = _get_summary_val("cost_exports")
    violation_cost = _get_summary_val("violation_cost")

    # CAPEX annualization traceability / up-front reconstruction
    # annualized_cost = upfront_cost * annualization_factor
    # => implied_upfront = annualized_cost / annualization_factor
    def _annualize_factor(discount_rate: float, lifetime_years: float, year_fraction: float) -> float:
        if lifetime_years <= 0:
            return np.nan
        if abs(discount_rate) < 1e-12:
            return (1.0 / lifetime_years) * year_fraction
        return (discount_rate / (1 - (1 / (1 + discount_rate) ** lifetime_years))) * year_fraction

    global_discount_rate = -1.0
    config_path = base / "3_model_inputs" / "ConfigModel.json"
    try:
        with open(config_path) as fh:
            cfg = json.load(fh)
        global_discount_rate = float(cfg.get("economic", {}).get("global_discountrate", {}).get("value", -1))
    except Exception:
        global_discount_rate = -1.0

    year_fraction = modelled_hours / 8760.0 if modelled_hours > 0 else 1.0

    implied_upfront_netws = 0.0
    implied_upfront_tecs = 0.0
    implied_upfront_ccs = 0.0
    netw_af_rows = []
    tec_with_af = 0
    tec_without_af = 0

    # Networks: reconstruct from mode-specific lifetime/discount_rate.
    try:
        mode_cfg = {}
        for mode in ["CO2_Pipeline", "CO2Ship"]:
            p = base / "3_model_inputs" / "period1" / "network_data" / f"{mode}.json"
            if p.exists():
                with open(p) as fh:
                    jd = json.load(fh)
                eco = jd.get("Economics", {})
                dr_mode = float(eco.get("discount_rate", 0.1))
                lt_mode = float(eco.get("lifetime", 25))
                if global_discount_rate >= 0:
                    dr_mode = global_discount_rate
                af_mode = _annualize_factor(dr_mode, lt_mode, year_fraction)
                mode_cfg[mode] = {"discount_rate": dr_mode, "lifetime": lt_mode, "af": af_mode}

        for mode, mdf in networks_wide_df.groupby("mode"):
            if mode not in mode_cfg:
                continue
            capex_mode = float(pd.to_numeric(mdf["capex"], errors="coerce").fillna(0).sum())
            af_mode = mode_cfg[mode]["af"]
            if np.isfinite(af_mode) and af_mode > 0:
                implied_upfront_netws += capex_mode / af_mode
                netw_af_rows.append(
                    f"{mode}: AF={af_mode:.6f} (r={mode_cfg[mode]['discount_rate']:.4f}, n={mode_cfg[mode]['lifetime']:.0f}y), annualized CAPEX={capex_mode:,.0f}€"
                )
    except Exception:
        pass

    # Technologies: reconstruct from per-node technology JSON economics.
    # CCS CAPEX is reconstructed using MEA JSON in same node folder when present.
    node_data_root = base / "3_model_inputs" / "period1" / "node_data"
    for _, row in nodes_wide_df.iterrows():
        node = str(row.get("node", "")).strip()
        tec = str(row.get("technology", "")).strip()
        capex_tec_val = float(pd.to_numeric(pd.Series([row.get("capex_tec", 0.0)]), errors="coerce").fillna(0).iloc[0])
        capex_ccs_val = float(pd.to_numeric(pd.Series([row.get("capex_ccs", 0.0)]), errors="coerce").fillna(0).iloc[0])

        if capex_tec_val > 0 and node and tec:
            tec_json = node_data_root / node / "technology_data" / f"{tec}.json"
            if tec_json.exists():
                try:
                    with open(tec_json) as fh:
                        tjs = json.load(fh)
                    eco = tjs.get("Economics", {})
                    dr = float(eco.get("discount_rate", 0.1))
                    n = float(eco.get("lifetime", 25))
                    if global_discount_rate >= 0:
                        dr = global_discount_rate
                    af = _annualize_factor(dr, n, year_fraction)
                    if np.isfinite(af) and af > 0:
                        implied_upfront_tecs += capex_tec_val / af
                        tec_with_af += 1
                    else:
                        tec_without_af += 1
                except Exception:
                    tec_without_af += 1
            else:
                tec_without_af += 1

        if capex_ccs_val > 0 and node:
            try:
                td = node_data_root / node / "technology_data"
                ccs_files = sorted(list(td.glob("MEA*.json")) + list(td.glob("*CCS*.json")))
                if ccs_files:
                    with open(ccs_files[0]) as fh:
                        cjs = json.load(fh)
                    eco = cjs.get("Economics", {})
                    dr = float(eco.get("discount_rate", 0.1))
                    n = float(eco.get("lifetime", 25))
                    if global_discount_rate >= 0:
                        dr = global_discount_rate
                    af = _annualize_factor(dr, n, year_fraction)
                    if np.isfinite(af) and af > 0:
                        implied_upfront_ccs += capex_ccs_val / af
            except Exception:
                pass

    implied_upfront_capex_total = implied_upfront_netws + implied_upfront_tecs + implied_upfront_ccs
    annualized_capex_total = (cost_capex_tecs or 0.0) + (cost_capex_netws or 0.0)
    implied_effective_af = (annualized_capex_total / implied_upfront_capex_total) if implied_upfront_capex_total > 0 else None
    netw_af_trace = " | ".join(netw_af_rows) if netw_af_rows else "No network AF trace available"

    avg_cost_overall = (total_cost / total_co2_injected_annualized) if total_co2_injected_annualized > 0 else None

    # Carbon-policy-excluded system cost (policy-neutral):
    # total_cost = base_system_cost + carbon_cost - carbon_revenue
    # => base_system_cost = total_cost - carbon_cost + carbon_revenue
    carbon_cost_safe = carbon_cost if carbon_cost is not None else 0.0
    carbon_revenue_safe = carbon_revenue if carbon_revenue is not None else 0.0
    total_cost_excl_carbon = (total_cost - carbon_cost_safe + carbon_revenue_safe) if total_cost is not None else None
    avg_capture_cost_excl_carbon = (
        total_cost_excl_carbon / total_co2_injected_annualized
        if (total_cost_excl_carbon is not None and total_co2_injected_annualized > 0)
        else None
    )

    # In H5 design, storage opex_variable is modelled-period total and must be annualized.
    storage_direct_cost_system = float(
        pd.to_numeric(stor_design.get("capex_tot", 0.0), errors="coerce").fillna(0.0).sum()
        + pd.to_numeric(stor_design.get("opex_fixed_tot", 0.0), errors="coerce").fillna(0.0).sum()
        + pd.to_numeric(stor_design.get("opex_variable", 0.0), errors="coerce").fillna(0.0).sum() * annualization_factor
    )
    network_cost_system = float((cost_capex_netws or 0.0) + (cost_opex_netws or 0.0))
    tec_total_system = float((cost_capex_tecs or 0.0) + (cost_opex_tecs or 0.0))
    import_export_net_cost_system = float((cost_imports or 0.0) + (cost_exports or 0.0))
    # total_minus_lccs references lccs_mod_bu (computed in the bottom-up block below)
    # placeholder until bottom-up block runs; will be resolved inline in overall_rows
    ts_cost_per_t = (
        ts_cost_system / total_co2_injected_annualized if total_co2_injected_annualized > 0 else None
    )
    lccs_cost_per_t_excl_imports_exports = (
        lccs_cost_excl_imports_exports / total_co2_injected_annualized if total_co2_injected_annualized > 0 else None
    )
    import_export_net_cost_per_t = (
        import_export_net_cost_system / total_co2_injected_annualized if total_co2_injected_annualized > 0 else None
    )

    # Cross-check (if all summary components exist):
    # base_system_cost = cost_tecs + cost_netws + cost_imports + cost_exports + violation_cost
    can_crosscheck = all(v is not None for v in [
        cost_capex_tecs, cost_capex_netws, cost_opex_tecs, cost_opex_netws,
        cost_imports, cost_exports, violation_cost
    ])
    total_cost_excl_carbon_from_components = None
    if can_crosscheck:
        total_cost_excl_carbon_from_components = (
            cost_capex_tecs + cost_capex_netws + cost_opex_tecs + cost_opex_netws
            + cost_imports + cost_exports + violation_cost
        )

    # ── Bottom-up annualized system cost breakdown (consistent basis) ─────────────────────────
    # H5 design values: capex_tot and opex_fixed_tot are year_fraction-scaled (like CAPEX annualization).
    # opex_variable is the raw modelled-period sum and needs ×annualization_factor to become annual.
    # The summary cost_opex_tecs mixes both scales, making top-down capture/TS decomposition
    # unreliable. Use H5 node/arc design data directly for the breakdown.
    _all_n_capex    = float(pd.to_numeric(nodes_wide_df["capex_tot"],       errors="coerce").fillna(0).sum())
    _all_n_opxf     = float(pd.to_numeric(nodes_wide_df["opex_fixed_tot"],  errors="coerce").fillna(0).sum())
    _all_n_opxv_mod = float(pd.to_numeric(nodes_wide_df["opex_variable"],   errors="coerce").fillna(0).sum())
    _stor_capex     = float(pd.to_numeric(stor_design["capex_tot"],         errors="coerce").fillna(0).sum())
    _stor_opxf      = float(pd.to_numeric(stor_design["opex_fixed_tot"],    errors="coerce").fillna(0).sum())
    _stor_opxv_mod  = float(pd.to_numeric(stor_design["opex_variable"],     errors="coerce").fillna(0).sum())
    _all_a_capex    = float(pd.to_numeric(networks_wide_df["capex"],         errors="coerce").fillna(0).sum()) if not networks_wide_df.empty else 0.0
    _all_a_opxf     = float(pd.to_numeric(networks_wide_df["opex_fixed"],    errors="coerce").fillna(0).sum()) if not networks_wide_df.empty else 0.0
    _all_a_opxv_mod = float(pd.to_numeric(networks_wide_df["opex_variable"], errors="coerce").fillna(0).sum()) if not networks_wide_df.empty else 0.0

    # Modelled-period totals (as optimizer sees them; CAPEX already year_fraction-scaled)
    _all_nodes_cost_mod   = _all_n_capex + _all_n_opxf + _all_n_opxv_mod
    _stor_cost_mod        = _stor_capex  + _stor_opxf  + _stor_opxv_mod
    _netw_cost_mod        = _all_a_capex + _all_a_opxf + _all_a_opxv_mod
    capture_only_mod_bu   = _all_nodes_cost_mod - _stor_cost_mod
    ts_cost_mod_bu        = _stor_cost_mod + _netw_cost_mod
    lccs_mod_bu           = capture_only_mod_bu + ts_cost_mod_bu

    # Annualized (×8760/modelled_hours applied to opex_variable; CAPEX/opex_fixed unchanged)
    _all_nodes_cost_ann   = _all_n_capex + _all_n_opxf + _all_n_opxv_mod * annualization_factor
    # storage_direct_cost_system is already the annualized storage cost (computed above)
    _netw_cost_ann        = _all_a_capex + _all_a_opxf + _all_a_opxv_mod * annualization_factor
    capture_only_ann_bu   = _all_nodes_cost_ann - storage_direct_cost_system
    ts_cost_ann_bu        = storage_direct_cost_system + _netw_cost_ann
    lccs_ann_bu           = capture_only_ann_bu + ts_cost_ann_bu

    def _per_t(cost): return cost / total_co2_injected_annualized if total_co2_injected_annualized > 0 else None
    total_minus_lccs = (total_cost - lccs_mod_bu) if total_cost is not None else None

    overall_rows = [
        {"metric": "total_cost",           "value": total_cost,           "unit": "€",      "note": "Total system cost (CAPEX+OPEX+imports+carbon)"},
        {"metric": "total_cost_excl_carbon", "value": total_cost_excl_carbon, "unit": "€",    "note": "Policy-neutral system cost = total_cost - carbon_cost + carbon_revenue"},
        {"metric": "cost_capex_tecs",      "value": cost_capex_tecs,      "unit": "€",      "note": "Annualized CAPEX of technologies from summary"},
        {"metric": "cost_capex_netws",     "value": cost_capex_netws,     "unit": "€",      "note": "Annualized CAPEX of networks from summary"},
        {"metric": "cost_opex_tecs",       "value": cost_opex_tecs,       "unit": "€",      "note": "OPEX of technologies from summary (mixed scale: opex_fixed=year_fraction-scaled, opex_var=modelled-period)"},
        {"metric": "cost_opex_netws",      "value": cost_opex_netws,      "unit": "€",      "note": "OPEX of networks from summary (same mixed scale as cost_opex_tecs)"},
        # ── Bottom-up OPEX breakdown (nodes + networks, consistent from H5 design data) ──────
        {"metric": "bu_opex_fixed_nodes",             "value": _all_n_opxf,                              "unit": "€",      "note": "Bottom-up: sum of opex_fixed_tot across all nodes (already year_fraction-scaled in H5)"},
        {"metric": "bu_opex_fixed_networks",          "value": _all_a_opxf,                              "unit": "€",      "note": "Bottom-up: sum of opex_fixed across all network arcs (already year_fraction-scaled in H5)"},
        {"metric": "bu_opex_fixed_total",             "value": _all_n_opxf + _all_a_opxf,                "unit": "€",      "note": "bu_opex_fixed_nodes + bu_opex_fixed_networks"},
        {"metric": "bu_opex_variable_nodes_modelled", "value": _all_n_opxv_mod,                          "unit": "€",      "note": "Bottom-up: sum of opex_variable across all nodes — RAW modelled-period total (not annualized)"},
        {"metric": "bu_opex_variable_nodes_annualized","value": _all_n_opxv_mod * annualization_factor,  "unit": "€/y",    "note": "bu_opex_variable_nodes_modelled × annualization_factor"},
        {"metric": "bu_opex_variable_networks_modelled","value": _all_a_opxv_mod,                        "unit": "€",      "note": "Bottom-up: sum of opex_variable across all network arcs — RAW modelled-period total"},
        {"metric": "bu_opex_variable_networks_annualized","value": _all_a_opxv_mod * annualization_factor,"unit": "€/y",   "note": "bu_opex_variable_networks_modelled × annualization_factor"},
        {"metric": "bu_opex_variable_total_modelled", "value": _all_n_opxv_mod + _all_a_opxv_mod,        "unit": "€",      "note": "Total OPEX_variable (nodes + networks) over modelled period"},
        {"metric": "bu_opex_variable_total_annualized","value": (_all_n_opxv_mod + _all_a_opxv_mod) * annualization_factor, "unit": "€/y", "note": "Total OPEX_variable (nodes + networks) annualized"},
        {"metric": "cost_imports",         "value": cost_imports,         "unit": "€",      "note": "Import cost from summary"},
        {"metric": "cost_exports",         "value": cost_exports,         "unit": "€",      "note": "Export cost from summary"},
        {"metric": "violation_cost",       "value": violation_cost,       "unit": "€",      "note": "Energy balance violation cost from summary"},
        {"metric": "carbon_cost",          "value": carbon_cost,          "unit": "€",      "note": "Carbon cost component"},
        {"metric": "carbon_revenue",       "value": carbon_revenue,       "unit": "€",      "note": "Carbon revenue component"},
        {"metric": "total_cost_excl_carbon_components", "value": total_cost_excl_carbon_from_components, "unit": "€", "note": "Cross-check from components: capex+opex+imports+exports+violation (no carbon terms)"},
        {"metric": "emissions_net",        "value": emissions_net,        "unit": "t CO2",  "note": "Net system emissions"},
        {"metric": "modelled_timesteps",             "value": modelled_timesteps,            "unit": "-",      "note": "Number of timesteps represented in operation results"},
        {"metric": "hours_per_timestep",             "value": hours_per_timestep,            "unit": "h",      "note": "From Topology.json resolution (fallback=1h)"},
        {"metric": "modelled_hours",                 "value": modelled_hours,                "unit": "h",      "note": "modelled_timesteps * hours_per_timestep"},
        {"metric": "fraction_of_year_modelled",      "value": year_fraction,                 "unit": "-",      "note": "modelled_hours / 8760 (same concept used in model annualization)"},
        {"metric": "annualization_factor",           "value": annualization_factor,          "unit": "-",      "note": "8760 / (modelled_timesteps * hours_per_timestep)"},
        {"metric": "config_global_discountrate",     "value": global_discount_rate,          "unit": "-",      "note": "From ConfigModel.json; -1 means per-component discount rates"},
        {"metric": "implied_upfront_CAPEX_total",    "value": implied_upfront_capex_total,   "unit": "€ upfront", "note": "Reconstructed from annualized CAPEX using component AF (network exact by mode; technology/CCS by node JSON when available)"},
        {"metric": "implied_effective_capex_AF",     "value": implied_effective_af,          "unit": "-",      "note": "(cost_capex_tecs + cost_capex_netws) / implied_upfront_CAPEX_total"},
        {"metric": "trace_capex_AF_network_modes",   "value": netw_af_trace,                 "unit": "text",   "note": "Mode-level AF trace: AF, discount rate, lifetime, annualized CAPEX"},
        {"metric": "trace_capex_AF_technology_coverage", "value": f"nodes_with_AF={tec_with_af}, nodes_without_AF={tec_without_af}", "unit": "text", "note": "Coverage of node-level technology CAPEX reconstruction"},
        {"metric": "total_CO2_injected_modelled",    "value": total_co2_injected,            "unit": "t CO2",  "note": "Sum of CO2 injected at all storage nodes over modelled horizon"},
        {"metric": "total_CO2_injected_annualized",  "value": total_co2_injected_annualized, "unit": "t CO2/y","note": "Modelled CO2 injected scaled to annual basis"},
        {"metric": "avg_capture_cost",               "value": avg_cost_overall,              "unit": "€/t CO2","note": "total_cost / total_CO2_injected_annualized"},
        {"metric": "avg_capture_cost_excl_carbon",   "value": avg_capture_cost_excl_carbon,  "unit": "€/t CO2","note": "total_cost_excl_carbon / total_CO2_injected_annualized"},
        # ── Capture-only / T&S / LCCS breakdown — bottom-up from H5 node+arc design data ──────
        # "modelled_period" = sum over the modelled period as seen by optimizer (CAPEX year_fraction-scaled, OPEX_var raw)
        # "annualized"      = same but OPEX_variable scaled up to annual basis (×8760/modelled_hours)
        {"metric": "capture_only_cost_modelled_period",    "value": capture_only_mod_bu,       "unit": "€",      "note": "Bottom-up: all non-storage node costs over modelled period (CAPEX+OPEX_fixed year_fraction-scaled; OPEX_var raw)"},
        {"metric": "capture_only_cost_annualized",         "value": capture_only_ann_bu,       "unit": "€/y",    "note": "Bottom-up annualized: OPEX_variable scaled to annual basis"},
        {"metric": "capture_only_cost_per_t_annualized",   "value": _per_t(capture_only_ann_bu),"unit": "€/t CO2","note": "capture_only_cost_annualized / total_CO2_injected_annualized"},
        {"metric": "TS_cost_modelled_period",              "value": ts_cost_mod_bu,            "unit": "€",      "note": "Bottom-up: storage node costs + network arc costs over modelled period"},
        {"metric": "TS_cost_annualized",                   "value": ts_cost_ann_bu,            "unit": "€/y",    "note": "Bottom-up annualized: storage + network costs"},
        {"metric": "TS_cost_per_t_annualized",             "value": _per_t(ts_cost_ann_bu),    "unit": "€/t CO2","note": "TS_cost_annualized / total_CO2_injected_annualized"},
        {"metric": "LCCS_modelled_period",                 "value": lccs_mod_bu,               "unit": "€",      "note": "Bottom-up: capture + T&S costs over modelled period (excludes imports/exports/carbon)"},
        {"metric": "LCCS_annualized",                      "value": lccs_ann_bu,               "unit": "€/y",    "note": "Bottom-up annualized LCCS"},
        {"metric": "LCCS_per_t_annualized",                "value": _per_t(lccs_ann_bu),       "unit": "€/t CO2","note": "LCCS_annualized / total_CO2_injected_annualized"},
        {"metric": "import_export_net_cost_system",  "value": import_export_net_cost_system, "unit": "€",      "note": "cost_imports + cost_exports from summary"},
        {"metric": "import_export_net_cost_per_t_annualized", "value": import_export_net_cost_per_t, "unit": "€/t CO2", "note": "Annualized: (cost_imports + cost_exports) / total_CO2_injected_annualized"},
        {"metric": "total_cost_minus_LCCS",          "value": total_minus_lccs,              "unit": "€",      "note": "total_cost - LCCS_modelled_period (residual = imports/exports/carbon/violation)"},
        {"metric": "LCCS_share_of_total_cost",       "value": (lccs_mod_bu / total_cost if (total_cost is not None and total_cost != 0) else None), "unit": "-", "note": "LCCS_modelled_period / total_cost"},
        # full_chain is appended after comp_cost_df is built (see below)
    ]
    overall_df = pd.DataFrame(overall_rows)

    # Per-storage breakdown
    per_stor_rows = []
    for node in sorted(stor_co2_inflow.keys()):
        co2_t = stor_co2_inflow[node]
        design_row = stor_design[stor_design["node"] == node]
        capex_t = float(design_row["capex_tot"].values[0]) if not design_row.empty else 0.0
        opex_f  = float(design_row["opex_fixed_tot"].values[0]) if not design_row.empty else 0.0
        opex_v  = float(design_row["opex_variable"].values[0]) if not design_row.empty else 0.0
        inj_cap = float(design_row["injection_capacity"].values[0]) if not design_row.empty else 0.0
        # opex_variable from H5 design is modelled-period total; annualize it to match annualized CO2 denominator
        opex_v_annualized = opex_v * annualization_factor
        direct_cost = capex_t + opex_f + opex_v_annualized
        co2_t_annualized = co2_t * annualization_factor
        cost_per_t = direct_cost / co2_t_annualized if co2_t_annualized > 0 else None
        per_stor_rows.append({
            "node": node,
            "CO2_injected_t_modelled": co2_t,
            "CO2_injected_t_annualized": co2_t_annualized,
            "injection_capacity_tph": inj_cap,
            "capex_tot_EUR_annualized": capex_t,
            "opex_fixed_EUR_annualized": opex_f,
            "opex_variable_EUR_modelled": opex_v,
            "opex_variable_EUR_annualized": opex_v_annualized,
            "direct_cost_EUR_annualized": direct_cost,
            "direct_cost_per_tCO2_annualized": cost_per_t,
        })

    per_stor_df = pd.DataFrame(per_stor_rows).sort_values("CO2_injected_t_annualized", ascending=False).reset_index(drop=True)
    per_stor_df.index += 1

    # Connected-component full-chain capture cost and emitter allocation.
    active_transport = networks_wide_df[
        pd.to_numeric(networks_wide_df.get("size", 0.0), errors="coerce").fillna(0.0) > 0.01
    ].copy()
    if not active_transport.empty:
        for c in ["total_flow", "capex", "opex_fixed", "opex_variable"]:
            if c in active_transport.columns:
                active_transport[c] = pd.to_numeric(active_transport[c], errors="coerce").fillna(0.0)

    node_cost_df = (
        nodes_wide_df.groupby("node", as_index=False)
        .agg(
            capex_tot=("capex_tot", "sum"),
            opex_fixed_tot=("opex_fixed_tot", "sum"),
            opex_variable=("opex_variable", "sum"),
        )
    )
    # opex_variable from H5 design is modelled-period total; annualize before summing with already-annualized capex/opex_fixed
    node_cost_df["node_cost_total"] = (
        pd.to_numeric(node_cost_df["capex_tot"], errors="coerce").fillna(0.0)
        + pd.to_numeric(node_cost_df["opex_fixed_tot"], errors="coerce").fillna(0.0)
        + pd.to_numeric(node_cost_df["opex_variable"], errors="coerce").fillna(0.0) * annualization_factor
    )
    node_cost_map = dict(zip(node_cost_df["node"], node_cost_df["node_cost_total"]))
    # Separate breakdown maps for CAPEX / OPEX_fixed / OPEX_variable (annualized)
    node_capex_map      = dict(zip(node_cost_df["node"], pd.to_numeric(node_cost_df["capex_tot"],      errors="coerce").fillna(0.0)))
    node_opex_fixed_map = dict(zip(node_cost_df["node"], pd.to_numeric(node_cost_df["opex_fixed_tot"], errors="coerce").fillna(0.0)))
    node_opex_var_map   = dict(zip(node_cost_df["node"], pd.to_numeric(node_cost_df["opex_variable"],  errors="coerce").fillna(0.0) * annualization_factor))

    emitter_captured_modelled = {}
    for k, v in raw_tec_op.items():
        if not (isinstance(k, tuple) and len(k) == 4):
            continue
        _, node, _tec, var = k
        if var == "CO2captured_var_output_ccs":
            emitter_captured_modelled[node] = emitter_captured_modelled.get(node, 0.0) + float(np.array(v).sum())
    emitter_captured_annualized = {k: v * annualization_factor for k, v in emitter_captured_modelled.items()}

    emitter_transport_modelled = {}
    if not active_transport.empty:
        grouped_outflow = active_transport.groupby("fromNode", as_index=False)["total_flow"].sum()
        emitter_transport_modelled = dict(zip(grouped_outflow["fromNode"], grouped_outflow["total_flow"]))
    emitter_transport_annualized = {k: v * annualization_factor for k, v in emitter_transport_modelled.items()}

    component_rows = []
    emitter_alloc_rows = []
    if not active_transport.empty:
        comp_graph = nx.Graph()
        comp_graph.add_nodes_from(set(active_transport["fromNode"]) | set(active_transport["toNode"]))
        comp_graph.add_edges_from(active_transport[["fromNode", "toNode"]].itertuples(index=False, name=None))

        for comp_id, comp_nodes in enumerate(nx.connected_components(comp_graph), start=1):
            comp_nodes = sorted(comp_nodes)
            comp_set = set(comp_nodes)
            comp_arcs = active_transport[
                active_transport["fromNode"].isin(comp_set) & active_transport["toNode"].isin(comp_set)
            ]

            comp_node_cost = float(sum(node_cost_map.get(n, 0.0) for n in comp_set))
            comp_storage_cost = float(sum(node_cost_map.get(n, 0.0) for n in comp_set if n in stor_node_names))
            # Node-level OPEX breakdown (annualized)
            comp_node_capex       = float(sum(node_capex_map.get(n, 0.0)      for n in comp_set))
            comp_node_opex_fixed  = float(sum(node_opex_fixed_map.get(n, 0.0) for n in comp_set))
            comp_node_opex_var    = float(sum(node_opex_var_map.get(n, 0.0)   for n in comp_set))
            # Arc-level OPEX breakdown (capex/opex_fixed annualized in H5; opex_variable scaled here)
            comp_arc_capex      = float(pd.to_numeric(comp_arcs.get("capex",      0.0), errors="coerce").fillna(0.0).sum())
            comp_arc_opex_fixed = float(pd.to_numeric(comp_arcs.get("opex_fixed", 0.0), errors="coerce").fillna(0.0).sum())
            comp_arc_opex_var   = float(pd.to_numeric(comp_arcs.get("opex_variable", 0.0), errors="coerce").fillna(0.0).sum() * annualization_factor)
            comp_arc_cost = comp_arc_capex + comp_arc_opex_fixed + comp_arc_opex_var
            comp_total_cost = comp_node_cost + comp_arc_cost
            comp_capture_only_cost = comp_node_cost - comp_storage_cost
            comp_ts_cost = comp_storage_cost + comp_arc_cost

            comp_injected_modelled = float(sum(stor_co2_inflow.get(n, 0.0) for n in comp_set))
            comp_injected_annualized = comp_injected_modelled * annualization_factor
            comp_cost_per_t = (
                comp_total_cost / comp_injected_annualized if comp_injected_annualized > 0 else None
            )

            emitter_nodes = sorted(
                n for n in comp_nodes if emitter_transport_annualized.get(n, 0.0) > 0
            )
            comp_transported_total = float(sum(emitter_transport_annualized.get(n, 0.0) for n in emitter_nodes))
            comp_captured_total = float(sum(emitter_captured_annualized.get(n, 0.0) for n in emitter_nodes))

            allocation_basis = "transported_CO2_share"
            if comp_transported_total <= 0:
                allocation_basis = "captured_CO2_share_fallback"
                comp_transported_total = float(sum(emitter_captured_annualized.get(n, 0.0) for n in emitter_nodes))

            component_rows.append(
                {
                    "component_id": comp_id,
                    "n_nodes": len(comp_nodes),
                    "n_arcs": len(comp_arcs),
                    "nodes": " | ".join(comp_nodes),
                    "node_direct_cost_EUR": comp_node_cost,
                    "node_capex_EUR": comp_node_capex,
                    "node_opex_fixed_EUR": comp_node_opex_fixed,
                    "node_opex_variable_EUR_annualized": comp_node_opex_var,
                    "capture_only_cost_EUR": comp_capture_only_cost,
                    "TS_cost_EUR": comp_ts_cost,
                    "network_cost_EUR": comp_arc_cost,
                    "netw_capex_EUR": comp_arc_capex,
                    "netw_opex_fixed_EUR": comp_arc_opex_fixed,
                    "netw_opex_variable_EUR_annualized": comp_arc_opex_var,
                    "full_chain_cost_EUR": comp_total_cost,
                    "CO2_injected_t_modelled": comp_injected_modelled,
                    "CO2_injected_t_annualized": comp_injected_annualized,
                    "full_chain_capture_cost_EUR_per_t_annualized": comp_cost_per_t,
                    "capture_only_cost_EUR_per_t_annualized": (
                        comp_capture_only_cost / comp_injected_annualized if comp_injected_annualized > 0 else None
                    ),
                    "TS_cost_EUR_per_t_annualized": (
                        comp_ts_cost / comp_injected_annualized if comp_injected_annualized > 0 else None
                    ),
                    "allocation_basis": allocation_basis,
                }
            )

            for emitter in emitter_nodes:
                transported_ann = float(emitter_transport_annualized.get(emitter, 0.0))
                captured_ann = float(emitter_captured_annualized.get(emitter, 0.0))
                numerator = transported_ann if allocation_basis == "transported_CO2_share" else captured_ann
                share = (numerator / comp_transported_total) if comp_transported_total > 0 else None
                allocated_cost = (comp_total_cost * share) if share is not None else None
                allocated_per_t = (allocated_cost / transported_ann) if (allocated_cost is not None and transported_ann > 0) else None

                share_transport = (transported_ann / comp_transported_total) if comp_transported_total > 0 else None
                share_capture = (captured_ann / comp_captured_total) if comp_captured_total > 0 else None

                share_blended = None
                if (share_transport is not None) and (share_capture is not None):
                    share_blended = 0.5 * (share_transport + share_capture)
                elif share_transport is not None:
                    share_blended = share_transport
                elif share_capture is not None:
                    share_blended = share_capture

                allocated_cost_transport = (comp_total_cost * share_transport) if share_transport is not None else None
                allocated_cost_capture = (comp_total_cost * share_capture) if share_capture is not None else None
                allocated_cost_blended = (comp_total_cost * share_blended) if share_blended is not None else None

                allocated_per_t_transport = (
                    allocated_cost_transport / transported_ann
                    if (allocated_cost_transport is not None and transported_ann > 0)
                    else None
                )
                allocated_per_t_capture = (
                    allocated_cost_capture / transported_ann
                    if (allocated_cost_capture is not None and transported_ann > 0)
                    else None
                )
                allocated_per_t_blended = (
                    allocated_cost_blended / transported_ann
                    if (allocated_cost_blended is not None and transported_ann > 0)
                    else None
                )

                emitter_alloc_rows.append(
                    {
                        "component_id": comp_id,
                        "emitter_node": emitter,
                        "transported_CO2_t_annualized": transported_ann,
                        "captured_CO2_t_annualized": captured_ann,
                        "share_in_component": share,
                        "allocated_full_chain_cost_EUR": allocated_cost,
                        "allocated_cost_per_t_transported_EUR_per_t": allocated_per_t,
                        "allocation_basis": allocation_basis,
                        "share_transport": share_transport,
                        "share_capture": share_capture,
                        "share_blended_50_50": share_blended,
                        "allocated_cost_transport_share_EUR": allocated_cost_transport,
                        "allocated_cost_capture_share_EUR": allocated_cost_capture,
                        "allocated_cost_blended_50_50_EUR": allocated_cost_blended,
                        "allocated_cost_per_t_transport_share_EUR_per_t": allocated_per_t_transport,
                        "allocated_cost_per_t_capture_share_EUR_per_t": allocated_per_t_capture,
                        "allocated_cost_per_t_blended_50_50_EUR_per_t": allocated_per_t_blended,
                    }
                )

    comp_cost_df = pd.DataFrame(component_rows)
    if not comp_cost_df.empty:
        comp_cost_df = comp_cost_df.sort_values("component_id").reset_index(drop=True)
        comp_cost_df.index += 1

    # Append full-chain totals (bottom-up sum across all components) to the metrics table
    if not comp_cost_df.empty:
        full_chain_total = float(comp_cost_df["full_chain_cost_EUR"].sum())
        full_chain_total_per_t = (
            full_chain_total / total_co2_injected_annualized
            if total_co2_injected_annualized > 0 else None
        )
    else:
        full_chain_total, full_chain_total_per_t = None, None
    overall_df = pd.concat([overall_df, pd.DataFrame([
        {"metric": "full_chain_cost_total_EUR_annualized",
         "value": full_chain_total,
         "unit": "€/y",
         "note": "Bottom-up annualized: sum of full_chain_cost_EUR across all connected components; includes only nodes+arcs in active network (cross-check vs LCCS_annualized)"},
        {"metric": "full_chain_cost_total_per_t_annualized",
         "value": full_chain_total_per_t,
         "unit": "€/t CO2",
         "note": "full_chain_cost_total_EUR_annualized / total_CO2_injected_annualized"},
    ])], ignore_index=True)

    emitter_alloc_df = pd.DataFrame(emitter_alloc_rows)
    if not emitter_alloc_df.empty:
        emitter_alloc_df = emitter_alloc_df.sort_values(
            ["component_id", "allocated_full_chain_cost_EUR"], ascending=[True, False]
        ).reset_index(drop=True)
        emitter_alloc_df.index += 1

    return overall_df, per_stor_df, comp_cost_df, emitter_alloc_df

co2_capture_overall_df, co2_capture_per_stor_df, co2_capture_per_component_df, co2_capture_emitter_alloc_df = _build_co2_capture_df(
    h5_file, nodes_wide, networks_wide, raw_summary
)

# ── Save per-storage capacity tracking JSON for the next scenario run ──────────────────────
# Used by main.py step 11 (2045 and 2050) to deduct prior injections from geological capacity.
_cap_T_map = {}
if db_path.exists():
    try:
        _con = duckdb.connect(str(db_path), read_only=True)
        _cap_df = _con.execute(
            "SELECT name_sanitized, capacity_T FROM combined_selected_final "
            "WHERE type = 'storage' AND selection = 'Yes'"
        ).df()
        _con.close()
        _cap_T_map = {
            str(r["name_sanitized"]): float(r["capacity_T"])
            for _, r in _cap_df.iterrows()
            if pd.notna(r["capacity_T"])
        }
    except Exception as _e:
        print(f"[WARN] Could not read capacity_T from DB for tracking: {_e}")

_capacity_tracking = {}
for _, _row in co2_capture_per_stor_df.iterrows():
    _node = str(_row["node"])
    _capacity_tracking[_node] = {
        "CO2_injected_t_modelled":   float(_row["CO2_injected_t_modelled"]),
        "CO2_injected_t_annualized": float(_row["CO2_injected_t_annualized"]),
        "injection_capacity_tph":    float(_row["injection_capacity_tph"]),
        "opex_var_EUR_per_t":        _sc_result["opex_var_storage_EUR_per_t"],
        "geological_capacity_T":     _cap_T_map.get(_node),
    }

_af_val = None
_af_rows = co2_capture_overall_df.loc[co2_capture_overall_df["metric"] == "annualization_factor", "value"]
if not _af_rows.empty:
    try:
        _af_val = float(_af_rows.iloc[0])
    except (ValueError, TypeError):
        pass

_capacity_json_path = Path(__file__).parent / "results" / f"capacity_used_{SCENARIO}.json"
_capacity_json_path.parent.mkdir(parents=True, exist_ok=True)
with open(_capacity_json_path, "w", encoding="utf-8") as _fh:
    json.dump({
        "scenario":             SCENARIO,
        "label":                _sc_result["label"],
        "run_folder":           h5_file.parent.name,
        "annualization_factor": _af_val,
        "storage_nodes":        _capacity_tracking,
    }, _fh, indent=4)
print(f"Saved capacity tracking -> {_capacity_json_path}")

# ── Build Storage_Utilization DataFrame ──────────────────────────────────────────────────────
# Re-read storage JSONs for size_max / injection_rate_max / OPEX_variable as written by main.py.
_stor_json_params = {}
_stor_json_dir = h5_file.parent.parent.parent / "3_model_inputs" / "period1" / "node_data"
try:
    for _p in sorted(_stor_json_dir.glob("*/technology_data/PermanentStorage_CO2_simple.json")):
        _nn2 = _p.parts[-3]
        with open(_p) as _fj:
            _jd = json.load(_fj)
        _stor_json_params[_nn2] = {
            "size_max_t":             _jd.get("size_max"),
            "injection_rate_max_tph": _jd.get("Flexibility", {}).get("injection_rate_max"),
            "opex_var_EUR_per_t":     _jd.get("Economics", {}).get("OPEX_variable"),
        }
except Exception as _e2:
    print(f"[WARN] Could not read storage JSONs for utilization sheet: {_e2}")

# For each storage node, find which emitter/transit nodes feed into it via the directed graph.
_emitters_per_storage = {}
for _sn2 in storage_nodes:
    if _sn2 not in _digraph:
        _emitters_per_storage[_sn2] = []
        continue
    _upstream = nx.ancestors(_digraph, _sn2)
    # Only pure emitters (send CO2 but not a storage themselves)
    _emitters_per_storage[_sn2] = sorted(
        n for n in _upstream
        if n in from_set and n not in storage_nodes
    )

_stor_util_rows = []
for _, _sr in co2_capture_per_stor_df.iterrows():
    _nn2 = str(_sr["node"])
    _geo_cap2 = _cap_T_map.get(_nn2)
    _jp = _stor_json_params.get(_nn2, {})
    _size_max2  = _jp.get("size_max_t")
    _inj_max2   = _jp.get("injection_rate_max_tph")
    _opex_v2    = _jp.get("opex_var_EUR_per_t")
    _co2_mod2   = float(_sr["CO2_injected_t_modelled"])
    _co2_ann2   = float(_sr["CO2_injected_t_annualized"])
    _emits2     = _emitters_per_storage.get(_nn2, [])
    _stor_util_rows.append({
        "storage_node":                   _nn2,
        "geological_capacity_T":          _geo_cap2,
        "size_max_t":                     _size_max2,
        "injection_rate_max_tph":         _inj_max2,
        "opex_var_EUR_per_t":             _opex_v2,
        "CO2_injected_t_modelled":        _co2_mod2,
        "CO2_injected_t_annualized":      _co2_ann2,
        "pct_size_max_used":              (_co2_mod2 / _size_max2 * 100) if _size_max2 else None,
        "pct_geo_cap_annualized":         (_co2_ann2 / _geo_cap2 * 100) if _geo_cap2 else None,
        "remaining_geo_cap_t_annualized": (_geo_cap2 - _co2_ann2) if _geo_cap2 else None,
        "n_emitters_assigned":            len(_emits2),
        "emitters_assigned":              " | ".join(_emits2),
    })

storage_utilization_df = pd.DataFrame(_stor_util_rows).sort_values(
    "CO2_injected_t_annualized", ascending=False
).reset_index(drop=True)
storage_utilization_df.index += 1

# ══════════════════════════════════════════════════════════
# PRINT SANITY CHECK TABLES
# ══════════════════════════════════════════════════════════
pd.set_option("display.max_colwidth", 45)
pd.set_option("display.width", 220)

print("\n" + "="*90)
print("TABLE 1 — ACTIVE ARCS")
print("="*90)
print(active_arcs_out[["mode","fromNode","toNode","size","total_flow","capex"]].to_string())
print(f"\n  Total: {len(active_arcs_out)}  |  "
      f"Pipeline: {len(active_arcs_out[active_arcs_out['mode']=='CO2_Pipeline'])}  |  "
      f"Ship: {len(active_arcs_out[active_arcs_out['mode']=='CO2Ship'])}")

print("\n" + "="*90)
print("TABLE 2 — ACTIVE NODES")
print("="*90)
print(active_nodes_out[["node","role","technology","size","size_ccs",
                         "emissions_pos","emissions_neg"]].to_string())
print(f"\n  Total: {len(active_nodes_out)}  |  "
      f"Emitters: {len(active_nodes_out[active_nodes_out['role']=='emitter'])}  |  "
      f"Storage/sink: {len(active_nodes_out[active_nodes_out['role']=='storage/sink'])}  |  "
      f"Transit: {len(active_nodes_out[active_nodes_out['role']=='transit'])}")

print("\n" + "="*90)
print("TABLE 3 — ACTIVE COMPONENT SANITY")
print("="*90)
if components_out.empty:
    print("No active components found.")
else:
    print(components_out[["component_id","n_nodes","n_arcs","has_sink","sink_nodes"]].to_string(index=False))
    print(f"\n  Components without sink: {len(components_without_sink)}")

# ══════════════════════════════════════════════════════════
# EXPORT TO EXCEL — single file, multi-tab
# ══════════════════════════════════════════════════════════
print(f"\nWriting {output_excel} ...")

def _write_results_excel(target_path: Path):
    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:

        # summary
        df = flatten_dict(raw_summary)
        if not df.empty:
            df["value"] = df["value"].apply(decode)
            df.to_excel(writer, sheet_name="summary")
            print(f"  ✅ summary:       {df.shape}")

        # topology
        df = flatten_dict(raw_topology)
        if not df.empty:
            df["value"] = df["value"].apply(decode)
            df.to_excel(writer, sheet_name="topology")
            print(f"  ✅ topology:      {df.shape}")

        # k_means_specs
        df = flatten_dict(raw_kmeans)
        if not df.empty:
            df["value"] = df["value"].apply(decode)
            df.to_excel(writer, sheet_name="k_means_specs")
            print(f"  ✅ k_means_specs: {df.shape}")

        # Parameters
        parameters_df.to_excel(writer, sheet_name="Parameters", index=False)
        print(f"  ✅ Parameters:    {parameters_df.shape}")

        # Node (combined active + inactive)
        nodes_combined_out.to_excel(writer, sheet_name="Node")
        print(f"  ✅ Node:          {nodes_combined_out.shape}")

        # Arc (combined active + inactive)
        arcs_combined_out.to_excel(writer, sheet_name="Arc")
        print(f"  ✅ Arc:           {arcs_combined_out.shape}")

        # active component connectivity sanity
        components_out.to_excel(writer, sheet_name="active_components_sanity")
        print(f"  ✅ active_components_sanity: {components_out.shape}")

        # CO2_capture — overall summary + per-storage + per-component + emitter allocation
        co2_capture_overall_df.to_excel(writer, sheet_name="CO2_capture", index=False, startrow=0)
        gap_row = len(co2_capture_overall_df) + 3
        co2_capture_per_stor_df.to_excel(writer, sheet_name="CO2_capture", startrow=gap_row)
        gap_row += len(co2_capture_per_stor_df) + 3
        co2_capture_per_component_df.to_excel(writer, sheet_name="CO2_capture", startrow=gap_row)
        gap_row += len(co2_capture_per_component_df) + 3
        co2_capture_emitter_alloc_df.to_excel(writer, sheet_name="CO2_capture", startrow=gap_row)
        print(
            f"  ✅ CO2_capture:   overall={co2_capture_overall_df.shape}, "
            f"per_storage={co2_capture_per_stor_df.shape}, "
            f"per_component={co2_capture_per_component_df.shape}, "
            f"emitter_alloc={co2_capture_emitter_alloc_df.shape}"
        )

        # Storage_Utilization: per-storage capacity used / remaining + assigned emitters
        if not storage_utilization_df.empty:
            storage_utilization_df.to_excel(writer, sheet_name="Storage_Utilization")
            print(f"  ✅ Storage_Utilization: {storage_utilization_df.shape}")

        # ── Scenario comparison sheet (Conservative / Base / Optimistic) ───────────────────
        # Loads capacity_used_SCENARIO.json from all three sensitivity runs and puts them
        # side-by-side for easy comparison. Written once all three JSONs exist.
        _results_dir = Path(__file__).parent / "results"
        _scen_data = {}
        for _sn in ["Conservative", "Base", "Optimistic"]:
            _cap_file = _results_dir / f"capacity_used_{_sn}.json"
            if _cap_file.exists():
                with open(_cap_file, "r", encoding="utf-8") as _fh2:
                    _scen_data[_sn] = json.load(_fh2)

        if len(_scen_data) >= 1:
            _all_nodes_s = set()
            for _sd in _scen_data.values():
                _all_nodes_s.update(_sd.get("storage_nodes", {}).keys())

            _scen_rows = []
            for _node in sorted(_all_nodes_s):
                _row_s = {"storage_node": _node}
                _geo_cap_s = None
                for _sn2 in ["Conservative", "Base", "Optimistic"]:
                    if _sn2 not in _scen_data:
                        continue
                    _nd2 = _scen_data[_sn2].get("storage_nodes", {}).get(_node, {})
                    _co2_ann2 = float(_nd2.get("CO2_injected_t_annualized", 0.0))
                    _opex_v2  = float(_nd2.get("opex_var_EUR_per_t",
                                              SCENARIO_CONFIG[_sn2]["opex_var_storage_EUR_per_t"]))
                    if _geo_cap_s is None and _nd2.get("geological_capacity_T") is not None:
                        _geo_cap_s = float(_nd2["geological_capacity_T"])
                    _row_s[f"{_sn2}.CO2_injected_tpy"]        = _co2_ann2
                    _row_s[f"{_sn2}.storage_OPEX_EUR_per_yr"] = _co2_ann2 * _opex_v2
                    _row_s[f"{_sn2}.OPEX_var_EUR_per_t"]      = _opex_v2
                _row_s["geological_capacity_T"] = _geo_cap_s
                _scen_rows.append(_row_s)

            _scen_cmp_df = pd.DataFrame(_scen_rows)
            _scen_cmp_df.to_excel(writer, sheet_name="Scenario_Comparison", index=False)
            print(
                f"  ✅ Scenario_Comparison: {_scen_cmp_df.shape} "
                f"(scenarios present: {sorted(_scen_data.keys())})"
            )
        else:
            print("  ⏭ Scenario_Comparison: No capacity_used_SCENARIO.json found yet.")


actual_output_excel = output_excel
try:
    _write_results_excel(actual_output_excel)
except PermissionError:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    actual_output_excel = output_excel.parent / f"results_{ts}.xlsx"
    print(f"[WARN] {output_excel.name} is open/locked. Writing to {actual_output_excel.name} instead.")
    _write_results_excel(actual_output_excel)

print(f"\n✅ Excel saved → {actual_output_excel}")

# ══════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════

script_dir = Path(__file__).resolve().parent
ship_route_geom = load_ship_route_geometries(script_dir)
manual_pipeline_geom = load_manual_pipeline_geometries(script_dir)

# Load coordinates
nodes_df = pd.read_csv(node_loc_file, sep=";",
                       names=["node","lon","lat","alt"], header=0)
node_coords_orig = dict(zip(nodes_df["node"],
                            zip(nodes_df["lat"], nodes_df["lon"])))
node_coords_norm = {normalize(k): v for k, v in node_coords_orig.items()}

def lookup_coords(name):
    if name in node_coords_orig:
        return node_coords_orig[name]
    repaired = repair_name(name)
    if repaired in node_coords_orig:
        return node_coords_orig[repaired]
    return node_coords_norm.get(normalize(repaired))

# Build map

# Create map with no base tiles, then add custom tile layer with opacity
m = folium.Map(location=[38.0, 15.0], zoom_start=5, tiles=None)
folium.TileLayer(
    tiles="CartoDB positron",
    name="Base Map",
    control=False,
    opacity=0.75
).add_to(m)

network_colors = {"CO2_Pipeline": "#000000", "CO2Ship": "#1900ff"}

layers = {
        "CO2_Pipeline_active": folium.FeatureGroup(name="CO2_Pipeline (Active)", show=True),
        "CO2_Pipeline_inactive": folium.FeatureGroup(name="CO2_Pipeline (Inactive)", show=False),
        "CO2Ship_active": folium.FeatureGroup(name="CO2Ship (Active)", show=True),
        "CO2Ship_inactive": folium.FeatureGroup(name="CO2Ship (Inactive)", show=False),
}
for lyr in layers.values():
        m.add_child(lyr)

node_layer = folium.FeatureGroup(name="Nodes", show=True)
m.add_child(node_layer)

max_size_all = pd.to_numeric(networks_wide["size"], errors="coerce").max()
if pd.isna(max_size_all) or max_size_all <= 0:
        max_size_all = 1.0

missing  = []
arc_js_meta = []
arrow_js_meta = []

all_arcs_map = networks_wide.copy()
all_arcs_map["status"] = np.where(all_arcs_map["size"] > 0.01, "Active", "Inactive")

for _, row in all_arcs_map.iterrows():
    fn = row["fromNode"]
    tn = row["toNode"]
    fc = lookup_coords(fn)
    tc = lookup_coords(tn)
    if fc is None or tc is None:
        missing.append((fn, tn))
        continue

    route_locations = [fc, tc]
    if row["mode"] == "CO2Ship":
        ship_key = (normalize(repair_name(fn)), normalize(repair_name(tn)))
        route_locations = ship_route_geom.get(ship_key, route_locations)
    elif row["mode"] == "CO2_Pipeline":
        pipe_key = (normalize(repair_name(fn)), normalize(repair_name(tn)))
        route_locations = manual_pipeline_geom.get(pipe_key, route_locations)

    row_size = pd.to_numeric(row.get("size"), errors="coerce")
    row_flow = pd.to_numeric(row.get("total_flow"), errors="coerce")
    row_capex = pd.to_numeric(row.get("capex"), errors="coerce")
    row_size = 0.0 if pd.isna(row_size) else float(row_size)
    row_flow = 0.0 if pd.isna(row_flow) else float(row_flow)
    row_capex = 0.0 if pd.isna(row_capex) else float(row_capex)

    base_weight = max(1.2, (max(row_size, 0.0) / max_size_all) * 10)
    is_active = row["status"] == "Active"
    layer_key = f"{row['mode']}_{'active' if is_active else 'inactive'}"

    # Set pipeline opacity to 0.75, others unchanged
    if row["mode"] == "CO2_Pipeline":
        opacity_val = 0.75
    else:
        opacity_val = 0.85 if is_active else 0.45
    line = folium.PolyLine(
        locations=route_locations,
        color=network_colors.get(row["mode"], "gray") if is_active else "#191818",
        weight=base_weight,
        opacity=opacity_val,
        dash_array=None if is_active else "6, 8",
        tooltip=(
            f"<b>{row['mode']}</b><br>"
            f"<b>Status:</b> {row['status']}<br>"
            f"<b>From:</b> {fn}<br>"
            f"<b>To:</b> {tn}<br>"
            f"<b>Size:</b> {row_size:.2f} t/h<br>"
            f"<b>Total flow:</b> {row_flow:,.0f} t<br>"
            f"<b>CAPEX:</b> €{row_capex:,.0f}"
        ),
    )
    line.add_to(layers[layer_key])
    arc_js_meta.append({"id": line.get_name(), "baseWeight": base_weight})

    if is_active:
        line_color = network_colors.get(row["mode"], "gray")
        arrow = PolyLineTextPath(
            line,
            "    \u25ba    ",
            repeat=True,
            offset=0,
            weight=3,
            color=line_color,
            attributes={"fill": line_color, "font-weight": "bold", "font-size": "14"},
        )
        arrow.add_to(layers[layer_key])
        arrow_js_meta.append({"id": arrow.get_name(), "text": "    \u25ba    "})

if missing:
    print(f"\n⚠️  Missing coordinates for {len(missing)} arc(s) — not in NodeLocations.csv:")
    for fn, tn in missing:
        print(f"   '{fn}' → '{tn}'")

active_nodes_norm = {normalize(n) for n in active_nodes_set}

node_emission_series = (
        nodes_wide.groupby("node", as_index=False)["emissions_pos"]
        .sum(min_count=1)
        .fillna(0.0)
)
node_emission_map_orig = {
        normalize(repair_name(row["node"])): float(max(row["emissions_pos"], 0.0))
        for _, row in node_emission_series.iterrows()
}
max_emission = max(node_emission_map_orig.values()) if node_emission_map_orig else 0.0
if max_emission <= 0:
        max_emission = 1.0

node_js_meta = []


for node_name, (lat, lon) in node_coords_orig.items():
    is_active = (node_name in active_nodes_set) or (normalize(node_name) in active_nodes_norm)
    emission_val = node_emission_map_orig.get(normalize(repair_name(node_name)), 0.0)
    emission_radius = 3.0 + (emission_val / max_emission) * 9.0
    equal_radius = 6.0

    # Node type detection (by name_sanitized)
    node_name_norm = normalize(repair_name(node_name))
    marker = None
    if node_name_norm in storage_nodes_set:
        # Storage: blue circle
        marker = folium.CircleMarker(
            location=[lat, lon],
            radius=emission_radius,
            color="#0074D9",
            fill=True,
            fill_color="#0074D9",
            fill_opacity=0.85,
            tooltip=f"<b>{node_name}</b><br><b>Type:</b> Storage<br>"
                    f"<b>Status:</b> {'Active' if is_active else 'Inactive'}<br>"
                    f"<b>Emission:</b> {emission_val:,.2f} t/h"
        )
    elif node_name_norm in port_nodes_set:
        # Port: green triangle
        marker = folium.RegularPolygonMarker(
            location=[lat, lon],
            number_of_sides=3,
            radius=emission_radius,
            color="#2ECC40",
            fill=True,
            fill_color="#2ECC40",
            fill_opacity=0.85,
            rotation=0,
            tooltip=f"<b>{node_name}</b><br><b>Type:</b> Port<br>"
                    f"<b>Status:</b> {'Active' if is_active else 'Inactive'}<br>"
                    f"<b>Emission:</b> {emission_val:,.2f} t/h"
        )
    else:
        # Default: red/gray circle
        marker = folium.CircleMarker(
            location=[lat, lon],
            radius=emission_radius,
            color="#d62728" if is_active else "#3A3939",
            fill=True,
            fill_color="#d62728" if is_active else "#3A3939",
            fill_opacity=0.85,
            tooltip=f"<b>{node_name}</b><br>"
                    f"<b>Status:</b> {'Active' if is_active else 'Inactive'}<br>"
                    f"<b>Emission:</b> {emission_val:,.2f} t/h"
        )
    marker.add_to(node_layer)
    node_js_meta.append({
        "id": marker.get_name(),
        "equalRadius": equal_radius,
        "emissionRadius": emission_radius,
    })

legend_html = """
<div style="position:fixed; bottom:30px; left:30px; z-index:1000;
         background:white; padding:15px; border-radius:8px;
         border:1px solid #ccc; font-size:13px; line-height:2;">
        <b>CO2 Transport Route</b><br>
        <span style="color:#000000; font-size:18px;">━━</span> Pipeline (Active)<br>
        <span style="color:#191818; font-size:18px;">- - -</span> Pipeline (Inactive)<br>
        <span style="color:#1900ff; font-size:18px;">━━</span> Ship (Active)<br>
        <span style="color:#8a8a8a; font-size:18px;">- - -</span> Ship (Inactive)<br>
    <hr style="margin:6px 0">
    <b>Node</b><br>
        <span style="color:#d62728;">●</span> Active Node<br>
        <span style="color:#3A3939;">●</span> Inactive Node<br>
        <span style="color:#0074D9;">●</span> Storage Node<br>
        <span style="color:#2ECC40;">▲</span> Port Node<br>
    <hr style="margin:6px 0">
        <i style="font-size:11px">Arc thickness follows capacity. Controls are on top-right.</i>
</div>
"""

control_html = """
<div id="map-control-panel" style="position:fixed; top:30px; right:30px; z-index:1000;
         background:white; padding:12px 14px; border-radius:8px; border:1px solid #bbb;
         font-size:12px; min-width:220px; box-shadow:0 2px 8px rgba(0,0,0,0.15);">
    <div style="font-weight:700; margin-bottom:8px;">Map Controls</div>
    <label for="arcScaleSlider" style="display:block;">Arc scale</label>
    <input id="arcScaleSlider" type="range" min="0.2" max="3" step="0.1" value="1" style="width:100%;">
    <div id="arcScaleValue" style="font-size:11px; margin-bottom:8px;">1.0x</div>

    <label for="nodeScaleSlider" style="display:block;">Node scale</label>
    <input id="nodeScaleSlider" type="range" min="0.2" max="3" step="0.1" value="1" style="width:100%;">
    <div id="nodeScaleValue" style="font-size:11px; margin-bottom:8px;">1.0x</div>

    <label for="nodeSizeMode" style="display:block;">Node size mode</label>
    <select id="nodeSizeMode" style="width:100%; padding:2px;">
        <option value="emission" selected>Based on emission</option>
        <option value="equal">Equal size</option>
    </select>
    <hr style="margin:8px 0">
    <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
        <input type="checkbox" id="showArrows" checked>
        Show direction arrows
    </label>
</div>
"""

m.get_root().html.add_child(folium.Element(legend_html))
m.get_root().html.add_child(folium.Element(control_html))

map_var = m.get_name()
arc_js = json.dumps(arc_js_meta)
node_js = json.dumps(node_js_meta)
arrow_js = json.dumps(arrow_js_meta)

m_script = f"""
<script>
(function() {{
    var mapObj = {map_var};
    var arcMeta = {arc_js};
    var nodeMeta = {node_js};
    var arrowMeta = {arrow_js};

    function applyArcScale() {{
        var scale = parseFloat(document.getElementById('arcScaleSlider').value || '1');
        document.getElementById('arcScaleValue').textContent = scale.toFixed(1) + 'x';
        arcMeta.forEach(function(item) {{
            var layer = window[item.id];
            if (layer && layer.setStyle) {{
                layer.setStyle({{weight: Math.max(0.8, item.baseWeight * scale)}});
            }}
        }});
    }}

    function applyNodeScale() {{
        var scale = parseFloat(document.getElementById('nodeScaleSlider').value || '1');
        var mode = document.getElementById('nodeSizeMode').value;
        document.getElementById('nodeScaleValue').textContent = scale.toFixed(1) + 'x';
        nodeMeta.forEach(function(item) {{
            var layer = window[item.id];
            if (!layer || !layer.setRadius) return;
            var base = (mode === 'equal') ? item.equalRadius : item.emissionRadius;
            layer.setRadius(Math.max(1.0, base * scale));
        }});
    }}

    function applyArrowToggle() {{
        var show = document.getElementById('showArrows').checked;
        arrowMeta.forEach(function(item) {{
            var layer = window[item.id];
            if (!layer || !layer.setText) return;
            layer.setText(show ? item.text : null);
        }});
    }}

    function initControls() {{
        var arcSlider = document.getElementById('arcScaleSlider');
        var nodeSlider = document.getElementById('nodeScaleSlider');
        var nodeMode = document.getElementById('nodeSizeMode');
        var arrowToggle = document.getElementById('showArrows');
        if (!arcSlider || !nodeSlider || !nodeMode) return;
        arcSlider.addEventListener('input', applyArcScale);
        nodeSlider.addEventListener('input', applyNodeScale);
        nodeMode.addEventListener('change', applyNodeScale);
        if (arrowToggle) arrowToggle.addEventListener('change', applyArrowToggle);
        // Always update values on load
        applyArcScale();
        applyNodeScale();
        applyArrowToggle();
        // Also update value displays immediately
        document.getElementById('arcScaleValue').textContent = arcSlider.value + 'x';
        document.getElementById('nodeScaleValue').textContent = nodeSlider.value + 'x';
    }}

    mapObj.whenReady(initControls);
}})();
</script>
"""
m.get_root().html.add_child(folium.Element(m_script))
folium.LayerControl(collapsed=False).add_to(m)
m.save(str(output_map))
print(f"✅ Map saved → {output_map}")
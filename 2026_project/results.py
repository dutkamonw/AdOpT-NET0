####### This script reads the optimization_results.h5 file and generates an Excel summary and a Folium map #######

############################################################################################
import h5py
import pandas as pd
import numpy as np
import folium
import os
import unicodedata
import json
import networkx as nx
import sys
from pathlib import Path
from datetime import datetime
import ast
import duckdb

# Using adopt_net0's function to extract datasets from H5 groups
from adopt_net0.result_management.read_results import extract_datasets_from_h5group

# Custom function to canonicalize node/technology names for consistent Arc/Node joins
from user_defined_function import canonicalize_name

#############################################################################################




# Ensure UTF-8 output to terminal to avoid encoding errors with non-ASCII node names
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


###############  SCENARIO SETTING #######################################################
# The scenario is set in main.py, which writes scenario_state.json. result.py reads
# Read SCENARIO from main.py's scenario_state.json if available; otherwise default to "scenario_1".
if "SCENARIO" in os.environ:
    SCENARIO = os.environ["SCENARIO"]
else:
    SCENARIO = "scenario_1"

# Name of column corresponding to the scenario selection in combined_selected_manual_edit.xlsx.
SCENARIO_COLUMNS = {
    "scenario_1": {"selection_col": "selection",   "capacity_col": "capacity_T"},
    "scenario_2": {"selection_col": "selection_2", "capacity_col": "capacity_T_2"}
}
SELECTION_COL = SCENARIO_COLUMNS[SCENARIO]["selection_col"]
CAPACITY_COL = SCENARIO_COLUMNS[SCENARIO]["capacity_col"]

# Corresponding to the scenario selected in main.py. This allows result.py to adapt to the scenario without requiring command-line arguments.
_scenario_state_path = Path(__file__).parent / "scenario_state.json"

try:
    with open(_scenario_state_path, "r", encoding="utf-8") as _sf:
        _state = json.load(_sf)
    _sc = _state.get("scenario")
    if _sc in SCENARIO_COLUMNS:
        SCENARIO = _sc
    # Prefer explicit columns from the state file; fall back to the scenario map.
    SELECTION_COL = _state.get("selection_col", SCENARIO_COLUMNS[SCENARIO]["selection_col"])
    CAPACITY_COL = _state.get("capacity_col", SCENARIO_COLUMNS[SCENARIO]["capacity_col"])
except Exception as _e:
    print(f"[WARN] Could not read scenario_state.json ({_e}); defaulting to '{SCENARIO}'.")
print(f"result.py scenario: {SCENARIO} (selection_col='{SELECTION_COL}', capacity_col='{CAPACITY_COL}')")


########################################################################################################################





################################# LOAD DATA FROM DATABASE.DUCKDB ########################################################

# Load storage/port node sets from combined_selected_manual_edit.xlsx
# If the file has the scenario selection column, only include nodes where it == 'Yes'. Otherwise fall back to the original selection column, then to all rows.
storage_nodes_set = set()
port_nodes_set = set()
try:
    df_nodes = pd.read_excel(
        Path(__file__).parent / "2_data_processed" / "intermediate_output" / "combined_selected_manual_edit.xlsx"
    )
    if "name_sanitized" in df_nodes.columns and "type" in df_nodes.columns:
        # Apply strict scenario filter to avoid accidental cross-scenario fallbacks.
        if SELECTION_COL not in df_nodes.columns:
            raise KeyError(
                f"Missing required scenario selection column '{SELECTION_COL}' in combined_selected_manual_edit.xlsx"
            )
        sel_mask = df_nodes[SELECTION_COL].astype(str).str.strip().str.lower() == "yes"
        df_sel = df_nodes[sel_mask]
        storage_nodes_set = set(
            df_sel[df_sel["type"].str.lower().str.contains("storage", na=False)]["name_sanitized"].astype(
                str).str.strip()
        )
        port_nodes_set = set(
            df_sel[df_sel["type"].str.lower().str.contains("port", na=False)]["name_sanitized"].astype(str).str.strip()
        )
except Exception as e:
    print(f"[WARN] Could not load storage/port node sets: {e}")



# Load iso2 map from database 
db_path = Path(__file__).parent / "database.duckdb"
node_iso2_map = {}  # name_sanitized -> iso2
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



######################################### PREPARE OUTPUT PATHS ########################################################

# Set result file in the same folder as h5 file.
PROJECT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT_DIR / "results"


# Find the newest h5 file, this is especially useful for loop_model.py's run
def find_newest_h5(results_dir: Path) -> Path | None:
    if not results_dir.exists():
        return None
    h5_files = list(results_dir.glob("**/optimization_results.h5"))
    if not h5_files:
        return None
    return max(h5_files, key=lambda p: p.stat().st_mtime)


_env_h5 = os.environ.get("ADOPT_H5_FILE")

# Single-result mode only.
# loop_model.py passes ADOPT_H5_FILE for each scenario after renaming the result folder.
# If ADOPT_H5_FILE is not provided, fall back to the newest optimization_results.h5 under results/.
if _env_h5:
    h5_file = Path(_env_h5)
else:
    _newest = find_newest_h5(RESULTS_DIR)
    if _newest is None:
        raise FileNotFoundError(f"No optimization_results.h5 found under {RESULTS_DIR}")
    h5_file = _newest

node_loc_file = PROJECT_DIR / "3_model_inputs" / "NodeLocations.csv"
input_dir = PROJECT_DIR / "3_model_inputs"
output_excel = h5_file.parent / "results.xlsx"
output_map = h5_file.parent / "network_map.html"

print(f"Reading H5 file: {h5_file}")
print(f"Excel output:   {output_excel}")
print(f"Map output:     {output_map}")
print(f"Input folder:   {input_dir}")

if not h5_file.exists():
    raise FileNotFoundError(f"optimization_results.h5 not found: {h5_file}")



##########################################   HELPERS  #############################################

def normalize(s):
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="replace")
    return unicodedata.normalize("NFKD", str(s)) \
        .encode("ascii", errors="ignore") \
        .decode("ascii").strip()


def repair_name(s):
    # Keep wrapper name for local call sites; delegate to shared project logic.
    return canonicalize_name(s)


def decode(v):
    """Decode bytes and byte-literal strings to plain str."""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("b'") and s.endswith("'")) or (s.startswith('b"') and s.endswith('"')):
            try:
                lit = ast.literal_eval(s)
                if isinstance(lit, bytes):
                    return lit.decode("utf-8", errors="replace")
            except Exception:
                pass
    return v


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


def sanitize_tooltip_text(value):
    """Sanitize text embedded into Folium tooltip template literals."""
    s = str(value)
    return (
        s.replace("\\", "\\\\")
        .replace("`", "'")
        .replace("${", "$\\{")
        .replace("</", "<\\/")
    )


def orient_route_locations(route_locations, start_coords, end_coords):
    """Ensure a route runs from start_coords to end_coords when the geometry is reversed."""
    if not route_locations or len(route_locations) < 2 or start_coords is None or end_coords is None:
        return route_locations

    def squared_distance(point, target):
        return (point[0] - target[0]) ** 2 + (point[1] - target[1]) ** 2

    start_d = squared_distance(route_locations[0], start_coords) + squared_distance(route_locations[-1], end_coords)
    reversed_d = squared_distance(route_locations[-1], start_coords) + squared_distance(route_locations[0], end_coords)
    if reversed_d < start_d:
        return list(reversed(route_locations))
    return route_locations


def _web_mercator_y(lat: float) -> float:
    """Approximate Web Mercator Y coordinate for map-screen angle calculation."""
    # Clamp latitude to the valid Web Mercator range to avoid infinities.
    lat = max(min(float(lat), 85.05112878), -85.05112878)
    rad = np.radians(lat)
    return float(np.log(np.tan(np.pi / 4.0 + rad / 2.0)))


def add_direction_arrow(layer, route_locations, color, is_active):
    """Add a direction arrow aligned with the displayed route geometry (fromNode -> toNode).

    Folium/Leaflet displays routes in Web Mercator screen space, where x follows longitude
    and screen y is inverted. A simple atan2(d_lat, d_lon) can therefore point slightly
    away from the line, especially for long Mediterranean arcs. This uses the same basic
    screen-space logic as the rendered map.
    """
    if not route_locations or len(route_locations) < 2:
        return

    # follows the local line direction on curved manual/ship geometries.
    n = len(route_locations)
    start_idx = max(0, min(n - 2, int(round(0.65 * (n - 1))) - 1))
    segment = None
    for offset in range(n - 1):
        i = (start_idx + offset) % (n - 1)
        p0 = route_locations[i]
        p1 = route_locations[i + 1]
        lat0, lon0 = p0
        lat1, lon1 = p1
        dx = float(np.radians(lon1)) - float(np.radians(lon0))
        dy_screen = -(_web_mercator_y(lat1) - _web_mercator_y(lat0))
        if abs(dx) >= 1e-12 or abs(dy_screen) >= 1e-12:
            segment = (p0, p1, dx, dy_screen)
            break

    if segment is None:
        return

    p0, p1, dx, dy_screen = segment
    lat0, lon0 = p0
    lat1, lon1 = p1

    # CSS rotate() is clockwise from the positive x-axis; using screen-space dy gives
    # 0=east/right, 90=south/down, -90=north/up, matching the visual line direction.
    angle_deg = float(np.degrees(np.arctan2(dy_screen, dx)))
    mid_lat = (float(lat0) + float(lat1)) / 2.0
    mid_lon = (float(lon0) + float(lon1)) / 2.0

    font_size = 14 if is_active else 12
    opacity = 0.95 if is_active else 0.6
    arrow_html = (
        f'<div style="transform: rotate({angle_deg:.1f}deg); transform-origin: center center; '
        f'color:{color}; font-size:{font_size}px; opacity:{opacity}; '
        f'line-height:1; font-weight:bold; width:{font_size}px; height:{font_size}px; '
        f'text-align:center;">&#8594;</div>'
    )

    folium.Marker(
        location=[mid_lat, mid_lon],
        icon=folium.DivIcon(
            html=arrow_html,
            class_name="route-dir-arrow",
            icon_size=(font_size, font_size),
            icon_anchor=(font_size / 2, font_size / 2),
        ),
    ).add_to(layer)


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
            print(f" Could not read ship route file: {path} ({exc})")
            return

        required = {"from_port", "to_port", "geometry_wkt"}
        if not required.issubset(df.columns):
            return

        work = df.copy()
        if SELECTION_COL in work.columns:
            sel = work[SELECTION_COL].astype(str).str.strip().str.lower()
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
        print(f" Could not read manual pipeline file: {manual_path} ({exc})")
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








################################## LOAD DATA FROM H5 FILE ########################################################

with h5py.File(h5_file, "r") as f:
    raw_summary = extract_datasets_from_h5group(f["summary"])
    raw_kmeans = extract_datasets_from_h5group(f["k_means_specs"])
    raw_nodes = extract_datasets_from_h5group(f["design"]["nodes"])
    raw_networks = extract_datasets_from_h5group(f["design"]["networks"])
    raw_tec_op = extract_datasets_from_h5group(f["operation"]["technology_operation"])
    raw_eb = extract_datasets_from_h5group(f["operation"]["energy_balance"])

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


def _sum_product(a, b) -> float:
    """Safe sum(a*b) for arrays/scalars with shape mismatch fallback."""
    aa = np.array(a, dtype=float)
    bb = np.array(b, dtype=float)
    if aa.shape != bb.shape:
        n = int(min(aa.size, bb.size))
        if n <= 0:
            return 0.0
        return float(np.sum(aa.reshape(-1)[:n] * bb.reshape(-1)[:n]))
    return float(np.sum(aa * bb))


# Per-node import cost from operation energy balance:
# cost_import_node = sum_t,car(import * import_price)
node_import_cost_modelled = {}
# Per-node transport-related import cost proxy:
# network_cost_import_node = sum_t,car(network_consumption * import_price)
node_network_import_cost_modelled = {}

for k, v in raw_eb.items():
    if not (isinstance(k, tuple) and len(k) == 4):
        continue
    period, node, carrier, var = k
    if var not in ("import", "network_consumption"):
        continue
    price_key = (period, node, carrier, "import_price")
    if price_key not in raw_eb:
        continue
    amount = _sum_product(v, raw_eb[price_key])
    if var == "import":
        node_import_cost_modelled[node] = node_import_cost_modelled.get(node, 0.0) + amount
    else:
        node_network_import_cost_modelled[node] = (
                node_network_import_cost_modelled.get(node, 0.0) + amount
        )

node_import_cost_annualized = {
    n: c * annualization_factor_nodes for n, c in node_import_cost_modelled.items()
}
node_network_import_cost_annualized = {
    n: c * annualization_factor_nodes for n, c in node_network_import_cost_modelled.items()
}



######################################### PARSE NODES ########################################################

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

# Normalize problematic names for reliable Arc/Node joins.
for col in ["node", "technology"]:
    if col in nodes_wide.columns:
        nodes_wide[col] = nodes_wide[col].apply(lambda x: repair_name(decode(x)))

# Clean numeric columns
for col in ["size", "size_ccs", "capex_tec", "capex_ccs", "capex_tot",
            "opex_fixed_tot", "opex_fixed_ccs", "opex_variable",
            "emissions_pos", "emissions_neg", "para_unitCAPEX", "existing"]:
    if col in nodes_wide.columns:
        nodes_wide[col] = pd.to_numeric(nodes_wide[col], errors="coerce")





################################ PARSE NETWORKS ########################################################

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
        networks_wide[col] = networks_wide[col].apply(lambda x: repair_name(decode(x)))

# Clean numeric columns
for col in ["size", "total_flow", "total_emissions", "capex",
            "opex_fixed", "opex_variable"]:
    if col in networks_wide.columns:
        networks_wide[col] = pd.to_numeric(networks_wide[col], errors="coerce")


# Filter to Pipeline + Ship only
networks_wide = networks_wide[
    networks_wide["mode"].isin(["CO2_Pipeline", "CO2Ship"])
].copy()





##########################  ACTIVE VS INACTIVE NODES AND ARCS ########################################################

active_arcs = networks_wide[networks_wide["size"] > 0.01].copy()
inactive_arcs = networks_wide[networks_wide["size"] <= 0.01].copy()
active_nodes_set = set(active_arcs["fromNode"]) | set(active_arcs["toNode"])
all_nodes_set = set(nodes_wide["node"])
inactive_nodes_set = all_nodes_set - active_nodes_set
active_nodes = nodes_wide[nodes_wide["node"].isin(active_nodes_set)].copy()
inactive_nodes = nodes_wide[nodes_wide["node"].isin(inactive_nodes_set)].copy()

# Add role column
from_set = set(active_arcs["fromNode"])
to_set = set(active_arcs["toNode"])


def get_role(n):
    in_from = n in from_set
    in_to = n in to_set
    if in_from and in_to: return "transit"
    if in_from:           return "emitter"
    return "storage/sink"


active_nodes["role"] = active_nodes["node"].apply(get_role)
inactive_nodes["role"] = inactive_nodes["node"].apply(lambda _: "inactive")

# Identify storage nodes and perform active-network component sanity checks.
storage_nodes = set(
    nodes_wide.loc[
        nodes_wide["technology"].astype(str).str.contains("storage", case=False, na=False),
        "node",
    ].tolist()
)
terminal_nodes = (to_set - from_set)

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
    comp_storage = sorted(comp_set & storage_nodes)
    comp_terminal = sorted(comp_set & terminal_nodes)
    comp_emitters = sorted(comp_set & from_set)
    component_rows.append({
        "component_id": comp_id,
        "n_nodes": len(comp_set),
        "n_arcs": len(comp_arcs),
        "has_storage_sink": bool(comp_storage),
        "storage_sink_nodes": " | ".join(comp_storage),
        "has_terminal_node": bool(comp_terminal),
        "terminal_nodes": " | ".join(comp_terminal),
        "example_emitters": " | ".join(comp_emitters[:6]),
        "all_nodes": " | ".join(comp_nodes),
    })

components_out = pd.DataFrame(component_rows).sort_values("component_id").reset_index(drop=True)
components_out.index += 1

components_without_sink = components_out[components_out["has_storage_sink"] == False]

#  Add upfront CAPEX and transparency columns for arcs. Build annualization factors by mode from network JSONs
base_dir = PROJECT_DIR  # 2026_project root
mode_af_map = {}
try:
    for mode in ["CO2_Pipeline", "CO2Ship"]:
        p = base_dir / "3_model_inputs" / "period1" / "network_data" / f"{mode}.json"
        if p.exists():
            with open(p) as fh:
                jd = json.load(fh)
            eco = jd.get("Economics", {})
            dr = float(eco.get("discount_rate", 0.1))
            lt = float(eco.get("lifetime", 25))
            # Use global discount rate if defined
            try:
                cfg_p = base_dir / "3_model_inputs" / "ConfigModel.json"
                with open(cfg_p) as cfh:
                    cfg = json.load(cfh)
                global_dr = cfg.get("economic", {}).get("global_discountrate", {}).get("value", -1)
                if global_dr >= 0:
                    dr = global_dr
            except Exception:
                pass
            # Annualization factor: r / (1 - (1/(1+r)^n)) * year_fraction
            _yf = (1.0 / annualization_factor_nodes) if annualization_factor_nodes > 0 else 1.0
            if dr == 0:
                af = (1.0 / lt) * _yf
            else:
                af = (dr / (1 - (1 / (1 + dr) ** lt))) * _yf
            mode_af_map[mode] = af
except Exception as e:
    print(f"[WARN] Could not load network annualization factors: {e}")


# Add upfront_capex_EUR to arcs (capex shown is annualized; compute upfront)
def compute_upfront_capex(row):
    try:
        if pd.isna(row["capex"]) or pd.isna(row["mode"]):
            return None
        af = mode_af_map.get(str(row["mode"]).strip())
        if af is None or af <= 0:
            return None
        return float(row["capex"]) / af
    except Exception:
        return None


networks_wide["upfront_capex_EUR"] = networks_wide.apply(compute_upfront_capex, axis=1)
active_arcs["upfront_capex_EUR"] = active_arcs.apply(compute_upfront_capex, axis=1)
inactive_arcs["upfront_capex_EUR"] = inactive_arcs.apply(compute_upfront_capex, axis=1)

# Reorder columns nicely
arc_cols = ["mode", "fromNode", "toNode", "size", "total_flow",
            "total_emissions", "upfront_capex_EUR", "capex", "opex_fixed", "opex_variable",
            "para_capex_gamma1", "para_capex_gamma2",
            "para_capex_gamma3", "para_capex_gamma4"]
arc_cols = [c for c in arc_cols if c in networks_wide.columns]

node_cols = ["node", "role", "technology", "existing", "size", "size_ccs",
             "capex_tec", "capex_ccs", "capex_tot",
             "opex_fixed_tot", "opex_fixed_ccs", "opex_variable",
             "emissions_pos", "emissions_neg", "para_unitCAPEX"]
node_cols = [c for c in node_cols if c in active_nodes.columns]

# Combined Node sheet (active + inactive, with status & iso2)
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
nodes_combined["cost_import_EUR_modelled"] = nodes_combined["node"].map(
    lambda n: node_import_cost_modelled.get(str(n), 0.0)
)
nodes_combined["cost_import"] = nodes_combined["node"].map(
    lambda n: node_import_cost_annualized.get(str(n), 0.0)
)
nodes_combined["network_cost_import_EUR_annualized"] = nodes_combined["node"].map(
    lambda n: node_network_import_cost_annualized.get(str(n), 0.0)
)
# Final storage: downstream geological storage node(s) this node ultimately sends CO2 to.
nodes_combined["final_storage"] = nodes_combined["node"].apply(_final_storage)

# For nodes: capex_tot is already annualized in H5; compute implied upfront using global AF as proxy
# (Note: individual technology economics may vary, this is approximate)
nodes_combined["upfront_capex_tot_approx_EUR"] = np.where(
    (pd.to_numeric(nodes_combined["capex_tot"], errors="coerce") > 0) & (annualization_factor_nodes > 0),
    pd.to_numeric(nodes_combined["capex_tot"], errors="coerce") / annualization_factor_nodes,
    None
)

# capex_tot and opex_fixed_tot are already annualized in the H5 design output.
# opex_variable is the raw modelled-period total and must be scaled to annual basis.
nodes_combined["node_direct_cost_EUR"] = (
        pd.to_numeric(nodes_combined.get("capex_tot", 0.0), errors="coerce").fillna(0.0)
        + pd.to_numeric(nodes_combined.get("opex_fixed_tot", 0.0), errors="coerce").fillna(0.0)
        + pd.to_numeric(nodes_combined.get("opex_variable", 0.0), errors="coerce").fillna(
    0.0) * annualization_factor_nodes
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

combined_node_cols = ["node", "status", "iso2", "role", "final_storage", "technology", "existing", "size", "size_ccs",
                      "upfront_capex_tot_approx_EUR", "capex_tec", "capex_ccs", "capex_tot",
                      "opex_fixed_tot", "opex_fixed_ccs", "opex_variable",
                      "emissions_pos", "emissions_neg", "para_unitCAPEX",
                      "cost_import", "cost_import_EUR_modelled", "network_cost_import_EUR_annualized",
                      "CO2_captured_t_modelled", "CO2_captured_t_annualized",
                      "node_capture_only_cost_EUR", "node_capture_only_cost_EUR_per_t_annualized"]
combined_node_cols = [c for c in combined_node_cols if c in nodes_combined.columns]
nodes_combined_out = nodes_combined[combined_node_cols].sort_values(
    ["status", "role", "node"]).reset_index(drop=True)
nodes_combined_out.index += 1

#  Combined Arc sheet (active + inactive, with status & fromNode iso2) 
active_arcs["status"] = "active"
inactive_arcs["status"] = "inactive"
arcs_combined = pd.concat([active_arcs, inactive_arcs], ignore_index=True)
arcs_combined["iso2"] = arcs_combined["fromNode"].map(
    lambda n: node_iso2_map.get(str(n).strip(), "")
)
combined_arc_cols = ["mode", "status", "fromNode", "iso2", "toNode", "size", "total_flow",
                     "total_emissions", "upfront_capex_EUR", "capex", "opex_fixed", "opex_variable",
                     "para_capex_gamma1", "para_capex_gamma2",
                     "para_capex_gamma3", "para_capex_gamma4"]
combined_arc_cols = [c for c in combined_arc_cols if c in arcs_combined.columns]
arcs_combined_out = arcs_combined[combined_arc_cols].sort_values(
    ["mode", "status", "size"], ascending=[True, True, False]).reset_index(drop=True)
arcs_combined_out.index += 1

# Keep legacy subsets for the map / sanity checks
active_arcs_out = active_arcs[arc_cols].sort_values(
    ["mode", "size"], ascending=[True, False]).reset_index(drop=True)
active_arcs_out.index += 1

active_nodes_out = active_nodes[node_cols].sort_values(
    ["role", "node"]).reset_index(drop=True)
active_nodes_out.index += 1


#################################  PARAMETERS SHEET  ################################################

def _build_parameters_df(h5_file_path: Path) -> pd.DataFrame:
    """Collect key model parameters from ConfigModel.json, network JSONs,
    CarbonCost.csv, and the H5 summary into a tidy DataFrame."""
    rows = []
    base = PROJECT_DIR  # 2026_project root
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
                    f"""
                    SELECT SUM(CAST(emission_TPA AS DOUBLE))
                    FROM combined_selected_final
                    WHERE lower(type) = 'emitter' AND {SELECTION_COL} = 'Yes'
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
        _add_row("emission_limit.reduction_percent", str(e), "%", "Error calculating",
                 "ConfigModel.json + database.duckdb")

    # ── Network JSONs ─────────────────────────────────────
    for net_name in ["CO2_Pipeline", "CO2Ship"]:
        net_path = period_dir / "network_data" / f"{net_name}.json"
        try:
            with open(net_path) as fh:
                net = json.load(fh)
            eco = net.get("Economics", {})
            perf = net.get("Performance", {})
            prefix = net_name
            _add_row(f"{prefix}.loss", perf.get("loss", "N/A"), "fraction/km", "Transport loss per km",
                     f"{net_name}.json")
            _add_row(f"{prefix}.size_max", net.get("size_max", "N/A"), "t/h", "Maximum network arc size",
                     f"{net_name}.json")
            _add_row(f"{prefix}.capex_gamma1", eco.get("gamma1", "N/A"), "€", "CAPEX fixed term", f"{net_name}.json")
            _add_row(
                f"{prefix}.capex_gamma2",
                eco.get("gamma2", "N/A"),
                "€/(t/h) or €/(t/h/km)",
                "CAPEX capacity or distance-based term",
                f"{net_name}.json",
            )
            _add_row(f"{prefix}.OPEX_variable", eco.get("OPEX_variable", "N/A"), "€/t",
                     "Variable OPEX per tonne transported", f"{net_name}.json")
            _add_row(f"{prefix}.OPEX_fixed", eco.get("OPEX_fixed", "N/A"), "% of CAPEX",
                     "Fixed OPEX as fraction of CAPEX", f"{net_name}.json")
            _add_row(f"{prefix}.discount_rate", eco.get("discount_rate", "N/A"), "-", "Discount rate for annualization",
                     f"{net_name}.json")
            _add_row(f"{prefix}.lifetime", eco.get("lifetime", "N/A"), "years", "Asset lifetime", f"{net_name}.json")
            _add_row(f"{prefix}.loss2emissions", perf.get("loss2emissions", "N/A"), "-",
                     "Whether transport loss counts as CO2 emission", f"{net_name}.json")
        except Exception as e:
            _add_row(f"{net_name}", str(e), "", "Error loading", f"{net_name}.json")

    return pd.DataFrame(rows)[["parameter", "value", "unit", "description", "source"]]


parameters_df = _build_parameters_df(h5_file)



##################################  CO2_CAPTURE SHEET ###################################################

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
    base = PROJECT_DIR
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
    # Fraction of one year represented by the modelled operating horizon.
    # This is the reciprocal concept of annualization_factor and is kept as
    # a reported metric because CAPEX/fixed OPEX in the H5 design outputs are
    # already scaled to this modelled fraction of a year.
    year_fraction = (modelled_hours / 8760.0) if modelled_hours > 0 else 1.0
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

    # Carbon-policy-excluded system cost (policy-neutral):
    # total_cost = base_system_cost + carbon_cost - carbon_revenue
    # => base_system_cost = total_cost - carbon_cost + carbon_revenue
    carbon_cost_safe = carbon_cost if carbon_cost is not None else 0.0
    carbon_revenue_safe = carbon_revenue if carbon_revenue is not None else 0.0
    total_cost_excl_carbon = (total_cost - carbon_cost_safe + carbon_revenue_safe) if total_cost is not None else None

    # In H5 design, storage opex_variable is modelled-period total and must be annualized.
    storage_direct_cost_system = float(
        pd.to_numeric(stor_design.get("capex_tot", 0.0), errors="coerce").fillna(0.0).sum()
        + pd.to_numeric(stor_design.get("opex_fixed_tot", 0.0), errors="coerce").fillna(0.0).sum()
        + pd.to_numeric(stor_design.get("opex_variable", 0.0), errors="coerce").fillna(0.0).sum() * annualization_factor
    )
    import_export_net_cost_system = float((cost_imports or 0.0) + (cost_exports or 0.0))
    import_export_net_cost_per_t = (
        import_export_net_cost_system / total_co2_injected_annualized if total_co2_injected_annualized > 0 else None
    )


    # ── Bottom-up annualized system cost breakdown (consistent basis) ─────────────────────────
    # H5 design values: capex_tot and opex_fixed_tot are year_fraction-scaled (like CAPEX annualization).
    # opex_variable is the raw modelled-period sum and needs ×annualization_factor to become annual.
    # The summary cost_opex_tecs mixes both scales, making top-down capture/TS decomposition
    _all_n_capex = float(pd.to_numeric(nodes_wide_df["capex_tot"], errors="coerce").fillna(0).sum())
    _all_n_opxf = float(pd.to_numeric(nodes_wide_df["opex_fixed_tot"], errors="coerce").fillna(0).sum())
    _all_n_opxv_mod = float(pd.to_numeric(nodes_wide_df["opex_variable"], errors="coerce").fillna(0).sum())
    _stor_capex = float(pd.to_numeric(stor_design["capex_tot"], errors="coerce").fillna(0).sum())
    _stor_opxf = float(pd.to_numeric(stor_design["opex_fixed_tot"], errors="coerce").fillna(0).sum())
    _stor_opxv_mod = float(pd.to_numeric(stor_design["opex_variable"], errors="coerce").fillna(0).sum())
    _all_a_capex = float(pd.to_numeric(networks_wide_df["capex"], errors="coerce").fillna(
        0).sum()) if not networks_wide_df.empty else 0.0
    _all_a_opxf = float(pd.to_numeric(networks_wide_df["opex_fixed"], errors="coerce").fillna(
        0).sum()) if not networks_wide_df.empty else 0.0
    _all_a_opxv_mod = float(pd.to_numeric(networks_wide_df["opex_variable"], errors="coerce").fillna(
        0).sum()) if not networks_wide_df.empty else 0.0


    # Modelled-period totals (as optimizer sees them; CAPEX already year_fraction-scaled)
    _all_nodes_cost_mod = _all_n_capex + _all_n_opxf + _all_n_opxv_mod
    _stor_cost_mod = _stor_capex + _stor_opxf + _stor_opxv_mod
    _netw_cost_mod = _all_a_capex + _all_a_opxf + _all_a_opxv_mod
    capture_only_mod_bu = _all_nodes_cost_mod - _stor_cost_mod
    ts_cost_mod_bu = _stor_cost_mod + _netw_cost_mod
    lccs_mod_bu = capture_only_mod_bu + ts_cost_mod_bu

    # Annualized (×8760/modelled_hours applied to opex_variable; CAPEX/opex_fixed unchanged)
    _all_nodes_cost_ann = _all_n_capex + _all_n_opxf + _all_n_opxv_mod * annualization_factor
    # storage_direct_cost_system is already the annualized storage cost (computed above)
    _netw_cost_ann = _all_a_capex + _all_a_opxf + _all_a_opxv_mod * annualization_factor
    capture_only_ann_bu = _all_nodes_cost_ann - storage_direct_cost_system
    transport_only_ann_bu = _netw_cost_ann
    storage_only_ann_bu = storage_direct_cost_system
    ts_cost_ann_bu = storage_direct_cost_system + _netw_cost_ann
    lccs_ann_bu = capture_only_ann_bu + ts_cost_ann_bu

    # Annualized import/export costs (summary values are modelled-period totals).
    imports_ann = float((cost_imports or 0.0) * annualization_factor)
    exports_ann = float((cost_exports or 0.0) * annualization_factor)
    import_export_net_ann = imports_ann + exports_ann

    # Per-node import costs computed locally from raw_eb (already loaded above).
    # node_import_cost: sum(import * import_price) per node
    # node_network_import_cost: sum(network_consumption * import_price) per node
    _local_node_import_cost: dict[str, float] = {}
    _local_node_net_import_cost: dict[str, float] = {}
    for _k, _v in raw_eb.items():
        if not (isinstance(_k, tuple) and len(_k) == 4):
            continue
        _period, _node, _carrier, _var = _k
        if _var not in ("import", "network_consumption"):
            continue
        _price_key = (_period, _node, _carrier, "import_price")
        if _price_key not in raw_eb:
            continue
        _amount = float(np.sum(np.array(_v, dtype=float) * np.array(raw_eb[_price_key], dtype=float)))
        if _var == "import":
            _local_node_import_cost[_node] = _local_node_import_cost.get(_node, 0.0) + _amount
        else:
            _local_node_net_import_cost[_node] = _local_node_net_import_cost.get(_node, 0.0) + _amount
    _local_node_import_cost_ann = {n: c * annualization_factor for n, c in _local_node_import_cost.items()}
    _local_node_net_import_cost_ann = {n: c * annualization_factor for n, c in _local_node_net_import_cost.items()}

    # active_transport is defined later; compute decomposition after it is built (see below).

    def _per_t(cost):
        return cost / total_co2_injected_annualized if total_co2_injected_annualized > 0 else None


    overall_rows = [
        {"metric": "total_cost", "value": total_cost, "unit": "€",
         "note": "Total system cost (CAPEX+OPEX+imports+carbon)"},
        {"metric": "total_cost_excl_carbon", "value": total_cost_excl_carbon, "unit": "€",
         "note": "Policy-neutral system cost = total_cost - carbon_cost + carbon_revenue"},
        {"metric": "cost_capex_tecs", "value": cost_capex_tecs, "unit": "€",
         "note": "Annualized CAPEX of technologies from summary"},
        {"metric": "cost_capex_netws", "value": cost_capex_netws, "unit": "€",
         "note": "Annualized CAPEX of networks from summary"},
        {"metric": "cost_opex_tecs", "value": cost_opex_tecs, "unit": "€",
         "note": "OPEX of technologies from summary (mixed scale: opex_fixed=year_fraction-scaled, opex_var=modelled-period)"},
        {"metric": "cost_opex_netws", "value": cost_opex_netws, "unit": "€",
         "note": "OPEX of networks from summary (same mixed scale as cost_opex_tecs)"},
        # ── Bottom-up OPEX breakdown (nodes + networks, consistent from H5 design data) ──────
        {"metric": "bu_opex_fixed_nodes", "value": _all_n_opxf, "unit": "€",
         "note": "Bottom-up: sum of opex_fixed_tot across all nodes (already year_fraction-scaled in H5)"},
        {"metric": "bu_opex_fixed_networks", "value": _all_a_opxf, "unit": "€",
         "note": "Bottom-up: sum of opex_fixed across all network arcs (already year_fraction-scaled in H5)"},
        {"metric": "bu_opex_fixed_total", "value": _all_n_opxf + _all_a_opxf, "unit": "€",
         "note": "bu_opex_fixed_nodes + bu_opex_fixed_networks"},
        {"metric": "bu_opex_variable_nodes_modelled", "value": _all_n_opxv_mod, "unit": "€",
         "note": "Bottom-up: sum of opex_variable across all nodes — RAW modelled-period total (not annualized)"},
        {"metric": "bu_opex_variable_nodes_annualized", "value": _all_n_opxv_mod * annualization_factor, "unit": "€/y",
         "note": "bu_opex_variable_nodes_modelled × annualization_factor"},
        {"metric": "bu_opex_variable_networks_modelled", "value": _all_a_opxv_mod, "unit": "€",
         "note": "Bottom-up: sum of opex_variable across all network arcs — RAW modelled-period total"},
        {"metric": "bu_opex_variable_networks_annualized", "value": _all_a_opxv_mod * annualization_factor,
         "unit": "€/y", "note": "bu_opex_variable_networks_modelled × annualization_factor"},
        {"metric": "bu_opex_variable_total_modelled", "value": _all_n_opxv_mod + _all_a_opxv_mod, "unit": "€",
         "note": "Total OPEX_variable (nodes + networks) over modelled period"},
        {"metric": "bu_opex_variable_total_annualized",
         "value": (_all_n_opxv_mod + _all_a_opxv_mod) * annualization_factor, "unit": "€/y",
         "note": "Total OPEX_variable (nodes + networks) annualized"},
        {"metric": "cost_imports", "value": cost_imports, "unit": "€", "note": "Import cost from summary"},
        {"metric": "cost_exports", "value": cost_exports, "unit": "€", "note": "Export cost from summary"},
        {"metric": "violation_cost", "value": violation_cost, "unit": "€",
         "note": "Energy balance violation cost from summary"},
        {"metric": "carbon_cost", "value": carbon_cost, "unit": "€", "note": "Carbon cost component"},
        {"metric": "carbon_revenue", "value": carbon_revenue, "unit": "€", "note": "Carbon revenue component"},
        {"metric": "emissions_net", "value": emissions_net, "unit": "t CO2", "note": "Net system emissions"},
        {"metric": "modelled_timesteps", "value": modelled_timesteps, "unit": "-",
         "note": "Number of timesteps represented in operation results"},
        {"metric": "hours_per_timestep", "value": hours_per_timestep, "unit": "h",
         "note": "From Topology.json resolution (fallback=1h)"},
        {"metric": "modelled_hours", "value": modelled_hours, "unit": "h",
         "note": "modelled_timesteps * hours_per_timestep"},
        {"metric": "fraction_of_year_modelled", "value": year_fraction, "unit": "-",
         "note": "modelled_hours / 8760 (same concept used in model annualization)"},
        {"metric": "annualization_factor", "value": annualization_factor, "unit": "-",
         "note": "8760 / (modelled_timesteps * hours_per_timestep)"},
        {"metric": "total_CO2_injected_modelled", "value": total_co2_injected, "unit": "t CO2",
         "note": "Sum of CO2 injected at all storage nodes over modelled horizon"},
        {"metric": "total_CO2_injected_annualized", "value": total_co2_injected_annualized, "unit": "t CO2/y",
         "note": "Modelled CO2 injected scaled to annual basis"},
        # ── Capture-only / T&S / LCCS breakdown — bottom-up from H5 node+arc design data ──────
        # "modelled_period" = sum over the modelled period as seen by optimizer (CAPEX year_fraction-scaled, OPEX_var raw)
        # "annualized"      = same but OPEX_variable scaled up to annual basis (×8760/modelled_hours)
        {"metric": "LCCS_modelled_period", "value": lccs_mod_bu, "unit": "€",
         "note": "Bottom-up: capture + T&S costs over modelled period (excludes imports/exports/carbon)"},
        {"metric": "LCCS_annualized", "value": lccs_ann_bu, "unit": "€/y", "note": "Bottom-up annualized LCCS"},
        {"metric": "LCCS_per_t_annualized", "value": _per_t(lccs_ann_bu), "unit": "€/t CO2",
         "note": "LCCS_annualized / total_CO2_injected_annualized"},
        {"metric": "import_export_net_cost_system", "value": import_export_net_cost_system, "unit": "€",
         "note": "cost_imports + cost_exports from summary (raw modelled-period total)"},
        {"metric": "import_export_net_cost_system_annualized",
         "value": import_export_net_cost_system * annualization_factor, "unit": "€/y",
         "note": "(cost_imports + cost_exports) × annualization_factor — annualized to annual basis"},
        {"metric": "import_export_net_cost_per_t_annualized", "value": import_export_net_cost_per_t, "unit": "€/t CO2",
         "note": "Annualized: (cost_imports + cost_exports) / total_CO2_injected_annualized"},
        # ── UPFRONT CAPEX TRANSPARENCY ──────────────────────────────────────────────────────
        # full_chain is appended after comp_cost_df is built (see below)
    ]
    overall_df = pd.DataFrame(overall_rows)

    # Per-storage breakdown
    per_stor_rows = []
    for node in sorted(stor_co2_inflow.keys()):
        co2_t = stor_co2_inflow[node]
        design_row = stor_design[stor_design["node"] == node]
        capex_t = float(design_row["capex_tot"].values[0]) if not design_row.empty else 0.0
        opex_f = float(design_row["opex_fixed_tot"].values[0]) if not design_row.empty else 0.0
        opex_v = float(design_row["opex_variable"].values[0]) if not design_row.empty else 0.0
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

    per_stor_df = pd.DataFrame(per_stor_rows).sort_values("CO2_injected_t_annualized", ascending=False).reset_index(
        drop=True)
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
    node_capex_map = dict(
        zip(node_cost_df["node"], pd.to_numeric(node_cost_df["capex_tot"], errors="coerce").fillna(0.0)))
    node_opex_fixed_map = dict(
        zip(node_cost_df["node"], pd.to_numeric(node_cost_df["opex_fixed_tot"], errors="coerce").fillna(0.0)))
    node_opex_var_map = dict(zip(node_cost_df["node"],
                                 pd.to_numeric(node_cost_df["opex_variable"], errors="coerce").fillna(
                                     0.0) * annualization_factor))

    emitter_captured_modelled = {}
    for k, v in raw_tec_op.items():
        if not (isinstance(k, tuple) and len(k) == 4):
            continue
        _, node, _tec, var = k
        if var == "CO2captured_var_output_ccs":
            emitter_captured_modelled[node] = emitter_captured_modelled.get(node, 0.0) + float(np.array(v).sum())
    emitter_captured_annualized = {k: v * annualization_factor for k, v in emitter_captured_modelled.items()}
    total_co2_captured_modelled = float(sum(emitter_captured_modelled.values()))
    total_co2_captured_annualized = total_co2_captured_modelled * annualization_factor

    # Direct import-cost decomposition (requires both active_transport and emitter_captured_annualized).
    _at_nodes = set()
    if not active_transport.empty:
        _at_nodes = set(active_transport["fromNode"].astype(str)) | set(active_transport["toNode"].astype(str))
    _emitter_nodes_set = {
        str(n)
        for n, c in emitter_captured_annualized.items()
        if float(c) > 0 and str(n) in _at_nodes and str(n) not in stor_node_names
    }
    _storage_nodes_set = {str(n) for n in stor_node_names}

    capture_import_cost_direct_ann = float(
        sum(_local_node_import_cost_ann.get(n, 0.0) for n in _emitter_nodes_set)
    )
    transport_import_cost_direct_ann = float(
        sum(_local_node_net_import_cost_ann.get(n, 0.0) for n in _at_nodes)
    )
    storage_import_cost_direct_ann = float(
        sum(_local_node_import_cost_ann.get(n, 0.0) for n in _storage_nodes_set)
    )

    capture_only_incl_imports_ann = capture_only_ann_bu + capture_import_cost_direct_ann
    transport_only_incl_imports_ann = transport_only_ann_bu + transport_import_cost_direct_ann
    storage_only_incl_imports_ann = storage_only_ann_bu + storage_import_cost_direct_ann

    overall_df = pd.concat([overall_df, pd.DataFrame([
        {"metric": "capture_only_incl_imports_EUR_annualized", "value": capture_only_incl_imports_ann, "unit": "€/y",
         "note": "capture_only_cost_annualized + capture_import_cost_direct_EUR_annualized"},
        {"metric": "capture_only_incl_imports_per_t_annualized", "value": _per_t(capture_only_incl_imports_ann),
         "unit": "€/t CO2", "note": "capture_only_incl_imports_EUR_annualized / total_CO2_injected_annualized"},
        {"metric": "transport_only_incl_imports_EUR_annualized", "value": transport_only_incl_imports_ann,
         "unit": "€/y", "note": "transport_only_cost_annualized + transport_import_cost_direct_EUR_annualized"},
        {"metric": "transport_only_incl_imports_per_t_annualized", "value": _per_t(transport_only_incl_imports_ann),
         "unit": "€/t CO2", "note": "transport_only_incl_imports_EUR_annualized / total_CO2_injected_annualized"},
        {"metric": "storage_only_incl_imports_EUR_annualized", "value": storage_only_incl_imports_ann, "unit": "€/y",
         "note": "storage_only_cost_annualized + storage_import_cost_direct_EUR_annualized"},
        {"metric": "storage_only_incl_imports_per_t_annualized", "value": _per_t(storage_only_incl_imports_ann),
         "unit": "€/t CO2", "note": "storage_only_incl_imports_EUR_annualized / total_CO2_injected_annualized"},
    ])], ignore_index=True)

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
            comp_node_capex = float(sum(node_capex_map.get(n, 0.0) for n in comp_set))
            comp_node_opex_fixed = float(sum(node_opex_fixed_map.get(n, 0.0) for n in comp_set))
            comp_node_opex_var = float(sum(node_opex_var_map.get(n, 0.0) for n in comp_set))
            # Arc-level OPEX breakdown (capex/opex_fixed annualized in H5; opex_variable scaled here)
            comp_arc_capex = float(pd.to_numeric(comp_arcs.get("capex", 0.0), errors="coerce").fillna(0.0).sum())
            comp_arc_opex_fixed = float(
                pd.to_numeric(comp_arcs.get("opex_fixed", 0.0), errors="coerce").fillna(0.0).sum())
            comp_arc_opex_var = float(pd.to_numeric(comp_arcs.get("opex_variable", 0.0), errors="coerce").fillna(
                0.0).sum() * annualization_factor)
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

            # ── Per-network (per-component) LCOC incl. imports, denominated in captured CO2 ──
            # Mirrors the bottom-up capture/transport/storage direct-import decomposition used
            # for the system-wide breakdown_sum_incl_imports metrics, but restricted to the nodes
            # of this connected component so each network gets its own import-inclusive LCOC.
            comp_emitter_nodes_for_import = {
                n for n in comp_set
                if emitter_captured_annualized.get(n, 0.0) > 0 and n not in stor_node_names
            }
            comp_capture_import_ann = float(
                sum(_local_node_import_cost_ann.get(n, 0.0) for n in comp_emitter_nodes_for_import)
            )
            comp_transport_import_ann = float(
                sum(_local_node_net_import_cost_ann.get(n, 0.0) for n in comp_set)
            )
            comp_storage_import_ann = float(
                sum(_local_node_import_cost_ann.get(n, 0.0) for n in comp_set if n in stor_node_names)
            )
            comp_import_total_ann = comp_capture_import_ann + comp_transport_import_ann + comp_storage_import_ann
            comp_full_chain_incl_imports_ann = comp_total_cost + comp_import_total_ann
            comp_captured_total_all = float(sum(emitter_captured_annualized.get(n, 0.0) for n in comp_set))
            comp_lcoc_full_chain_incl_imports_per_t_captured_annualized = (
                comp_full_chain_incl_imports_ann / comp_captured_total_all
                if comp_captured_total_all > 0 else None
            )

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
                    "CO2_captured_t_annualized": comp_captured_total_all,
                    "full_chain_incl_imports_EUR_annualized": comp_full_chain_incl_imports_ann,
                    "lcoc_full_chain_incl_imports_per_t_captured_annualized": comp_lcoc_full_chain_incl_imports_per_t_captured_annualized,
                }
            )

            for emitter in emitter_nodes:
                transported_ann = float(emitter_transport_annualized.get(emitter, 0.0))
                captured_ann = float(emitter_captured_annualized.get(emitter, 0.0))
                numerator = transported_ann if allocation_basis == "transported_CO2_share" else captured_ann
                share = (numerator / comp_transported_total) if comp_transported_total > 0 else None
                allocated_cost = (comp_total_cost * share) if share is not None else None
                allocated_per_t = (allocated_cost / transported_ann) if (
                            allocated_cost is not None and transported_ann > 0) else None

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

    # imports/exports from summary are raw modelled-period totals → annualize before adding
    full_chain_incl_imports = (
        (full_chain_total + imports_ann + exports_ann) if full_chain_total is not None else None
    )
    full_chain_incl_imports_per_t = (
        full_chain_incl_imports / total_co2_injected_annualized
        if (full_chain_incl_imports is not None and total_co2_injected_annualized > 0) else None
    )
    lcoc_full_chain_incl_imports_per_t_captured = (
        full_chain_incl_imports / total_co2_captured_annualized
        if (full_chain_incl_imports is not None and total_co2_captured_annualized > 0) else None
    )

    overall_df = pd.concat([overall_df, pd.DataFrame([
        {"metric": "full_chain_cost_total_EUR_annualized",
         "value": full_chain_total,
         "unit": "€/y",
         "note": "Bottom-up annualized: sum of full_chain_cost_EUR across all connected components; includes only nodes+arcs in active network (cross-check vs LCCS_annualized)"},
        {"metric": "full_chain_cost_total_per_t_annualized",
         "value": full_chain_total_per_t,
         "unit": "€/t CO2",
         "note": "full_chain_cost_total_EUR_annualized / total_CO2_injected_annualized"},
        {"metric": "full_chain_incl_imports_EUR_annualized",
         "value": full_chain_incl_imports,
         "unit": "€/y",
         "note": "full_chain_cost_total + (cost_imports + cost_exports) × annualization_factor; raw import/export costs from summary annualized to match node+arc costs"},
        {"metric": "full_chain_incl_imports_per_t_annualized",
         "value": full_chain_incl_imports_per_t,
         "unit": "€/t CO2",
         "note": "full_chain_incl_imports_EUR_annualized / total_CO2_injected_annualized"},
        {"metric": "total_CO2_captured_modelled",
         "value": total_co2_captured_modelled,
         "unit": "t CO2",
         "note": "Sum of captured CO2 from all CCS technologies over modelled horizon"},
        {"metric": "total_CO2_captured_annualized",
         "value": total_co2_captured_annualized,
         "unit": "t CO2/y",
         "note": "total_CO2_captured_modelled scaled to annual basis"},
        {"metric": "lcoc_full_chain_incl_imports_per_t_captured_annualized",
         "value": lcoc_full_chain_incl_imports_per_t_captured,
         "unit": "€/t CO2",
         "note": "full_chain_incl_imports_EUR_annualized / total_CO2_captured_annualized (LCOC-style denominator uses captured CO2)"},
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

# Thesis-ready CO2_capture rows. Use a keep-list rather than a drop-list so
# newly added debug/audit metrics do not silently clutter the output workbook.
CO2_CAPTURE_KEEP_METRICS = [
    "total_cost",
    "total_cost_excl_carbon",
    "cost_capex_tecs",
    "cost_capex_netws",
    "cost_opex_tecs",
    "cost_opex_netws",
    "bu_opex_fixed_nodes",
    "bu_opex_fixed_networks",
    "bu_opex_fixed_total",
    "bu_opex_variable_nodes_modelled",
    "bu_opex_variable_nodes_annualized",
    "bu_opex_variable_networks_modelled",
    "bu_opex_variable_networks_annualized",
    "bu_opex_variable_total_modelled",
    "bu_opex_variable_total_annualized",
    "cost_imports",
    "cost_exports",
    "violation_cost",
    "carbon_cost",
    "carbon_revenue",
    "emissions_net",
    "modelled_timesteps",
    "hours_per_timestep",
    "modelled_hours",
    "fraction_of_year_modelled",
    "annualization_factor",
    "total_CO2_injected_modelled",
    "total_CO2_injected_annualized",
    "LCCS_modelled_period",
    "LCCS_annualized",
    "LCCS_per_t_annualized",
    "import_export_net_cost_system",
    "import_export_net_cost_system_annualized",
    "import_export_net_cost_per_t_annualized",
    "capture_only_incl_imports_EUR_annualized",
    "capture_only_incl_imports_per_t_annualized",
    "transport_only_incl_imports_EUR_annualized",
    "transport_only_incl_imports_per_t_annualized",
    "storage_only_incl_imports_EUR_annualized",
    "storage_only_incl_imports_per_t_annualized",
    "full_chain_cost_total_EUR_annualized",
    "full_chain_cost_total_per_t_annualized",
    "full_chain_incl_imports_EUR_annualized",
    "full_chain_incl_imports_per_t_annualized",
    "total_CO2_captured_modelled",
    "total_CO2_captured_annualized",
    "lcoc_full_chain_incl_imports_per_t_captured_annualized",
]


def _filter_co2_capture_overall(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only thesis-facing metrics and preserve the order in CO2_CAPTURE_KEEP_METRICS."""
    if df.empty or "metric" not in df.columns:
        return df
    metric_order = {metric: i for i, metric in enumerate(CO2_CAPTURE_KEEP_METRICS)}
    out = df[df["metric"].astype(str).str.strip().isin(metric_order)].copy()
    out["_metric_order"] = out["metric"].map(metric_order)
    out = out.sort_values("_metric_order").drop(columns="_metric_order").reset_index(drop=True)
    print(f"CO2_capture cleanup: kept {len(out)} thesis-facing metric row(s); removed {len(df) - len(out)} debug/audit row(s).")
    return out


co2_capture_overall_df = _filter_co2_capture_overall(co2_capture_overall_df)

# ── Storage geological capacity map ─────────────────────────────────────────────────────────
# Used only for the Storage_Utilization sheet. If DB table/columns are unavailable,
# capacity_T_2 stays blank instead of stopping export.
_cap_T_map = {}
if db_path.exists():
    try:
        _con = duckdb.connect(str(db_path), read_only=True)
        try:
            # Read the scenario capacity column and alias it to the stable internal
            # name 'capacity_T_2' so downstream columns/labels remain unchanged.
            _cap_df = _con.execute(
                f"""
                SELECT name_sanitized, {CAPACITY_COL} AS capacity_T_2
                FROM combined_selected_final
                WHERE name_sanitized IS NOT NULL
                  AND {CAPACITY_COL} IS NOT NULL
                """
            ).df()
        except Exception:
            _cap_df = _con.execute(
                """
                SELECT name_sanitized, geo_capacity_ton
                FROM combined_selected_final
                WHERE name_sanitized IS NOT NULL
                  AND geo_capacity_ton IS NOT NULL
                """
            ).df().rename(columns={"geo_capacity_ton": "capacity_T_2"})
        finally:
            _con.close()

        if not _cap_df.empty:
            _cap_df["name_sanitized"] = _cap_df["name_sanitized"].astype(str).str.strip()
            _cap_df["capacity_T_2"] = pd.to_numeric(
                _cap_df["capacity_T_2"], errors="coerce"
            )
            _cap_df = _cap_df.dropna(subset=["capacity_T_2"])
            _cap_T_map = dict(zip(_cap_df["name_sanitized"], _cap_df["capacity_T_2"]))
    except Exception as _e:
        print(f"[WARN] Could not load geological capacity map from DB: {_e}")

# ── Build Storage_Utilization DataFrame ──────────────────────────────────────────────────────
# Re-read storage JSONs for size_max / injection_rate_max / OPEX_variable as written by main.py.
_stor_json_params = {}
_stor_json_dir = PROJECT_DIR / "3_model_inputs" / "period1" / "node_data"
try:
    for _p in sorted(_stor_json_dir.glob("*/technology_data/PermanentStorage_CO2_simple.json")):
        _nn2 = _p.parts[-3]
        with open(_p) as _fj:
            _jd = json.load(_fj)
        _stor_json_params[_nn2] = {
            "size_max_t": _jd.get("size_max"),
            "injection_rate_max_tph": _jd.get("Flexibility", {}).get("injection_rate_max"),
            "opex_var_EUR_per_t": _jd.get("Economics", {}).get("OPEX_variable"),
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
# Reuse the exact annualization_factor that produced CO2_injected_t_annualized in
# co2_capture_per_stor_df (sourced from the CO2_capture overall summary), so the new
# size_max_t_annualized column is computed on the same basis as the rest of this table.
_af_row_su = co2_capture_overall_df.loc[
    co2_capture_overall_df["metric"] == "annualization_factor", "value"
]
_annualization_factor_su = float(_af_row_su.iloc[0]) if not _af_row_su.empty else 1.0

for _, _sr in co2_capture_per_stor_df.iterrows():
    _nn2 = str(_sr["node"])
    _geo_cap2 = _cap_T_map.get(_nn2)
    _jp = _stor_json_params.get(_nn2, {})
    _size_max2 = pd.to_numeric(_jp.get("size_max_t"), errors="coerce")
    _size_max2 = None if pd.isna(_size_max2) else float(_size_max2)
    _inj_max2 = pd.to_numeric(_jp.get("injection_rate_max_tph"), errors="coerce")
    _inj_max2 = None if pd.isna(_inj_max2) else float(_inj_max2)
    _opex_v2 = pd.to_numeric(_jp.get("opex_var_EUR_per_t"), errors="coerce")
    _opex_v2 = None if pd.isna(_opex_v2) else float(_opex_v2)
    _co2_mod2 = float(_sr["CO2_injected_t_modelled"])
    _co2_ann2 = float(_sr["CO2_injected_t_annualized"])
    _emits2 = _emitters_per_storage.get(_nn2, [])
    _size_max_ann2 = (
        _size_max2 * _annualization_factor_su if _size_max2 is not None else None
    )
    _stor_util_rows.append({
        "storage_node": _nn2,
        "capacity_T_2": _geo_cap2,
        "size_max_t": _size_max2,
        "size_max_t_annualized": _size_max_ann2,
        "injection_rate_max_tph": _inj_max2,
        "opex_var_EUR_per_t": _opex_v2,
        "CO2_injected_t_modelled": _co2_mod2,
        "CO2_injected_t_annualized": _co2_ann2,
        "pct_size_max_used": (_co2_mod2 / _size_max2 * 100) if _size_max2 else None,
        "pct_capacity_T_2_annualized": (_co2_ann2 / _geo_cap2 * 100) if _geo_cap2 else None,
        "remaining_capacity_T_2_annualized": (_geo_cap2 - _co2_ann2) if _geo_cap2 else None,
        "n_emitters_assigned": len(_emits2),
        "emitters_assigned": " | ".join(_emits2),
    })

storage_utilization_df = pd.DataFrame(_stor_util_rows).sort_values(
    "CO2_injected_t_annualized", ascending=False
).reset_index(drop=True)
storage_utilization_df.index += 1

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
            print(f"   summary:       {df.shape}")


        # k_means_specs
        df = flatten_dict(raw_kmeans)
        if not df.empty:
            df["value"] = df["value"].apply(decode)
            df.to_excel(writer, sheet_name="k_means_specs")
            print(f"   k_means_specs: {df.shape}")

        # Parameters
        parameters_df.to_excel(writer, sheet_name="Parameters", index=False)
        print(f"   Parameters:    {parameters_df.shape}")

        # Node (combined active + inactive)
        nodes_combined_out.to_excel(writer, sheet_name="Node")
        print(f"   Node:          {nodes_combined_out.shape}")

        # Arc (combined active + inactive)
        arcs_combined_out.to_excel(writer, sheet_name="Arc")
        print(f"   Arc:           {arcs_combined_out.shape}")

        # active component connectivity sanity
        components_out.to_excel(writer, sheet_name="active_components_sanity")

        # CO2_capture — overall summary + per-storage + per-component + emitter allocation
        co2_capture_overall_df.to_excel(writer, sheet_name="CO2_capture", index=False, startrow=0)
        gap_row = len(co2_capture_overall_df) + 3
        co2_capture_per_stor_df.to_excel(writer, sheet_name="CO2_capture", startrow=gap_row)
        gap_row += len(co2_capture_per_stor_df) + 3
        co2_capture_per_component_df.to_excel(writer, sheet_name="CO2_capture", startrow=gap_row)

        # Storage_Utilization: per-storage capacity used / remaining + assigned emitters
        if not storage_utilization_df.empty:
            storage_utilization_df.to_excel(writer, sheet_name="Storage_Utilization")


actual_output_excel = output_excel
try:
    _write_results_excel(actual_output_excel)
except PermissionError:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    actual_output_excel = output_excel.parent / f"results_{ts}.xlsx"
    print(f"[WARN] {output_excel.name} is open/locked. Writing to {actual_output_excel.name} instead.")
    _write_results_excel(actual_output_excel)

print(f"\n Excel saved → {actual_output_excel}")

#################################################################################################################





###########################################  MAP HTML OUTPUT  ###################################################

script_dir = Path(__file__).resolve().parent
ship_route_geom = load_ship_route_geometries(script_dir)
manual_pipeline_geom = load_manual_pipeline_geometries(script_dir)

# Load coordinates
nodes_df = pd.read_csv(node_loc_file, sep=";",
                       names=["node", "lon", "lat", "alt"], header=0)
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

missing = []
arc_js_meta = []

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

    route_locations = orient_route_locations(route_locations, fc, tc)

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
            f"<b>{sanitize_tooltip_text(row['mode'])}</b><br>"
            f"<b>Status:</b> {sanitize_tooltip_text(row['status'])}<br>"
            f"<b>From:</b> {sanitize_tooltip_text(fn)}<br>"
            f"<b>To:</b> {sanitize_tooltip_text(tn)}<br>"
            f"<b>Size:</b> {row_size:.2f} t/h<br>"
            f"<b>Total flow:</b> {row_flow:,.0f} t<br>"
            f"<b>CAPEX:</b> €{row_capex:,.0f}"
        ),
    )
    line.add_to(layers[layer_key])
    arc_js_meta.append({"id": line.get_name(), "baseWeight": base_weight})

    line_color = network_colors.get(row["mode"], "gray") if is_active else "#555555"
    add_direction_arrow(layers[layer_key], route_locations, line_color, is_active)

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

# Lookup of storage hover stats (capacity_T_2, size_max, CO2 injected) built earlier from
# _stor_util_rows, keyed the same way node names are normalized elsewhere on this map.
stor_hover_map = {
    normalize(repair_name(_row["storage_node"])): _row
    for _row in _stor_util_rows
}


def _fmt_num(value, unit=""):
    """Format a hover-tooltip number, falling back to 'N/A' for missing values."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "N/A"
    try:
        return f"{float(value):,.2f}{(' ' + unit) if unit else ''}"
    except (TypeError, ValueError):
        return "N/A"


node_js_meta = []


def _register_node_marker(marker_obj, equal_radius_val, emission_radius_val):
    """Add a node marker to the map and record its sizing metadata."""
    marker_obj.add_to(node_layer)
    node_js_meta.append({
        "id": marker_obj.get_name(),
        "equalRadius": equal_radius_val,
        "emissionRadius": emission_radius_val,
    })


for node_name, (lat, lon) in node_coords_orig.items():
    is_active = (node_name in active_nodes_set) or (normalize(node_name) in active_nodes_norm)
    emission_val = node_emission_map_orig.get(normalize(repair_name(node_name)), 0.0)
    emission_radius = 3.0 + (emission_val / max_emission) * 9.0
    equal_radius = 6.0

    # Node type detection (by name_sanitized)
    node_name_norm = normalize(repair_name(node_name))
    is_storage_node = node_name_norm in storage_nodes_set
    is_port_node = node_name_norm in port_nodes_set
    is_emitter_role = emission_val > 0

    if is_storage_node:
        # Storage: blue circle, with capacity/size_max/injected-CO2 hover details
        stor_info = stor_hover_map.get(node_name_norm, {})
        storage_tooltip = (
            f"<b>{sanitize_tooltip_text(node_name)}</b><br><b>Type:</b> Storage<br>"
            f"<b>Status:</b> {'Active' if is_active else 'Inactive'}<br>"
            f"<b>Emission:</b> {emission_val:,.2f} t/h<br>"
            f"<b>Total capacity (capacity_T_2):</b> {_fmt_num(stor_info.get('capacity_T_2'), 't CO2')}<br>"
            f"<b>Size max:</b> {_fmt_num(stor_info.get('size_max_t'), 't')}<br>"
            f"<b>CO2 injected (modelled):</b> {_fmt_num(stor_info.get('CO2_injected_t_modelled'), 't')}<br>"
            f"<b>CO2 injected (annualized):</b> {_fmt_num(stor_info.get('CO2_injected_t_annualized'), 't/y')}"
        )
        marker = folium.CircleMarker(
            location=[lat, lon],
            radius=emission_radius,
            color="#0074D9",
            fill=True,
            fill_color="#0074D9",
            fill_opacity=0.85,
            tooltip=storage_tooltip,
        )
        _register_node_marker(marker, equal_radius, emission_radius)

    elif is_port_node and is_emitter_role:
        # Node is both a port and an active emitter: draw the emitter circle (sized by
        # emission, colored by status) first, then layer a smaller green port marker
        # in front of it (added after, so it renders on top) to show both roles at once.
        combined_tooltip = (
            f"<b>{sanitize_tooltip_text(node_name)}</b><br><b>Type:</b> Port + Emitter<br>"
            f"<b>Status:</b> {'Active' if is_active else 'Inactive'}<br>"
            f"<b>Emission:</b> {emission_val:,.2f} t/h"
        )
        emitter_part = folium.CircleMarker(
            location=[lat, lon],
            radius=emission_radius,
            color="#d62728" if is_active else "#3A3939",
            fill=True,
            fill_color="#d62728" if is_active else "#3A3939",
            fill_opacity=0.85,
            tooltip=combined_tooltip,
        )
        _register_node_marker(emitter_part, equal_radius, emission_radius)

        port_radius = max(3.0, emission_radius * 0.45)
        port_part = folium.CircleMarker(
            location=[lat, lon],
            radius=port_radius,
            color="#2ECC40",
            weight=2,
            fill=True,
            fill_color="#2ECC40",
            fill_opacity=0.95,
            tooltip=combined_tooltip,
        )
        _register_node_marker(port_part, equal_radius, port_radius)

    elif is_port_node:
        # Port: green circle (plugin-free to keep map robust across browsers)
        marker = folium.CircleMarker(
            location=[lat, lon],
            radius=emission_radius,
            color="#2ECC40",
            fill=True,
            fill_color="#2ECC40",
            fill_opacity=0.85,
            tooltip=f"<b>{sanitize_tooltip_text(node_name)}</b><br><b>Type:</b> Port<br>"
                    f"<b>Status:</b> {'Active' if is_active else 'Inactive'}<br>"
                    f"<b>Emission:</b> {emission_val:,.2f} t/h"
        )
        _register_node_marker(marker, equal_radius, emission_radius)

    else:
        # Default: red/gray circle (emitter or unclassified node)
        marker = folium.CircleMarker(
            location=[lat, lon],
            radius=emission_radius,
            color="#d62728" if is_active else "#3A3939",
            fill=True,
            fill_color="#d62728" if is_active else "#3A3939",
            fill_opacity=0.85,
            tooltip=f"<b>{sanitize_tooltip_text(node_name)}</b><br>"
                    f"<b>Status:</b> {'Active' if is_active else 'Inactive'}<br>"
                    f"<b>Emission:</b> {emission_val:,.2f} t/h"
        )
        _register_node_marker(marker, equal_radius, emission_radius)

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
        <span style="color:#d62728;">●</span><span style="color:#2ECC40; margin-left:-9px;">●</span> Port + Emitter (port shown in front)<br>
</div>
"""

m.get_root().html.add_child(folium.Element(legend_html))

map_var = m.get_name()
arc_js = json.dumps(arc_js_meta)
node_js = json.dumps(node_js_meta)

folium.LayerControl(collapsed=False).add_to(m)
m.save(str(output_map))
print(f" Map saved → {output_map}")
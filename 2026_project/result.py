
import h5py
import pandas as pd
import numpy as np
import folium
import unicodedata
import json
import networkx as nx
from pathlib import Path
from adopt_net0.result_management.read_results import extract_datasets_from_h5group

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

h5_file       = Path(r"C:\Users\dutka\MT\AdOpT-NET0_dw\2026_project\results\20260520033640-1\optimization_results.h5")
node_loc_file = Path(r"C:\Users\dutka\MT\AdOpT-NET0_dw\2026_project\3_model_inputs\NodeLocations.csv")
output_excel  = h5_file.parent / "results.xlsx"
output_map    = h5_file.parent / "network_map.html"

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
    "HESTAMBIENTE S.R.L":  "HERAMBIENTE S.R.L.",
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
arc_cols = [c for c in arc_cols if c in active_arcs.columns]

node_cols = ["node","role","technology","existing","size","size_ccs",
             "capex_tec","capex_ccs","capex_tot",
             "opex_fixed_tot","opex_fixed_ccs","opex_variable",
             "emissions_pos","emissions_neg","para_unitCAPEX"]
node_cols = [c for c in node_cols if c in active_nodes.columns]

active_arcs_out  = active_arcs[arc_cols].sort_values(
    ["mode","size"], ascending=[True,False]).reset_index(drop=True)
active_arcs_out.index += 1

inactive_arcs_out = inactive_arcs[arc_cols].sort_values(
    ["mode","size"], ascending=[True,False]).reset_index(drop=True)
inactive_arcs_out.index += 1

active_nodes_out = active_nodes[node_cols].sort_values(
    ["role","node"]).reset_index(drop=True)
active_nodes_out.index += 1

inactive_nodes_out = inactive_nodes[node_cols].sort_values(
    ["role","node"]).reset_index(drop=True)
inactive_nodes_out.index += 1

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

with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:

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

    # active_arcs
    active_arcs_out.to_excel(writer, sheet_name="active_arcs")
    print(f"  ✅ active_arcs:   {active_arcs_out.shape}")

    # inactive_arcs
    inactive_arcs_out.to_excel(writer, sheet_name="inactive_arcs")
    print(f"  ✅ inactive_arcs: {inactive_arcs_out.shape}")

    # active_nodes
    active_nodes_out.to_excel(writer, sheet_name="active_nodes")
    print(f"  ✅ active_nodes:  {active_nodes_out.shape}")

    # inactive_nodes
    inactive_nodes_out.to_excel(writer, sheet_name="inactive_nodes")
    print(f"  ✅ inactive_nodes:{inactive_nodes_out.shape}")

    # active component connectivity sanity
    components_out.to_excel(writer, sheet_name="active_components_sanity")
    print(f"  ✅ active_components_sanity: {components_out.shape}")

print(f"\n✅ Excel saved → {output_excel}")

# ══════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════

script_dir = Path(__file__).resolve().parent
ship_route_geom = load_ship_route_geometries(script_dir)

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
</div>
"""

m.get_root().html.add_child(folium.Element(legend_html))
m.get_root().html.add_child(folium.Element(control_html))

map_var = m.get_name()
arc_js = json.dumps(arc_js_meta)
node_js = json.dumps(node_js_meta)

m_script = f"""
<script>
(function() {{
    var mapObj = {map_var};
    var arcMeta = {arc_js};
    var nodeMeta = {node_js};

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

    function initControls() {{
        var arcSlider = document.getElementById('arcScaleSlider');
        var nodeSlider = document.getElementById('nodeScaleSlider');
        var nodeMode = document.getElementById('nodeSizeMode');
        if (!arcSlider || !nodeSlider || !nodeMode) return;
        arcSlider.addEventListener('input', applyArcScale);
        nodeSlider.addEventListener('input', applyNodeScale);
        nodeMode.addEventListener('change', applyNodeScale);
        // Always update values on load
        applyArcScale();
        applyNodeScale();
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
import h5py
import pandas as pd
import numpy as np
import folium
import unicodedata
from pathlib import Path
from adopt_net0.result_management.read_results import extract_datasets_from_h5group

h5_file       = Path(r"C:\Users\0898341\PycharmProjects\2026_project\results\20260518134819-1\optimization_results.h5")
node_loc_file = Path(r"C:\Users\0898341\PycharmProjects\2026_project\3_model_inputs\NodeLocations.csv")
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
active_nodes_set = set(active_arcs["fromNode"]) | set(active_arcs["toNode"])
active_nodes = nodes_wide[nodes_wide["node"].isin(active_nodes_set)].copy()

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

active_nodes_out = active_nodes[node_cols].sort_values(
    ["role","node"]).reset_index(drop=True)
active_nodes_out.index += 1

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

# ══════════════════════════════════════════════════════════
# EXPORT TO EXCEL — single file, 5 tabs
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

    # active_nodes
    active_nodes_out.to_excel(writer, sheet_name="active_nodes")
    print(f"  ✅ active_nodes:  {active_nodes_out.shape}")

print(f"\n✅ Excel saved → {output_excel}")

# ══════════════════════════════════════════════════════════
# MAP
# ══════════════════════════════════════════════════════════

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
m = folium.Map(location=[38.0, 15.0], zoom_start=5,
               tiles="CartoDB positron")

network_colors = {"CO2_Pipeline": "#1f77b4", "CO2Ship": "#d62728"}

layers = {}
for net in ["CO2_Pipeline", "CO2Ship"]:
    layers[net] = folium.FeatureGroup(name=net, show=True)
    m.add_child(layers[net])

node_layer = folium.FeatureGroup(name="Nodes", show=True)
m.add_child(node_layer)

max_size = active_arcs_out["size"].max() or 1
missing  = []

for _, row in active_arcs_out.iterrows():
    fn = row["fromNode"]
    tn = row["toNode"]
    fc = lookup_coords(fn)
    tc = lookup_coords(tn)
    if fc is None or tc is None:
        missing.append((fn, tn))
        continue
    folium.PolyLine(
        locations=[fc, tc],
        color=network_colors.get(row["mode"], "gray"),
        weight=max(1.5, (row["size"] / max_size) * 10),
        opacity=0.8,
        tooltip=(
            f"<b>{row['mode']}</b><br>"
            f"<b>From:</b> {fn}<br>"
            f"<b>To:</b> {tn}<br>"
            f"<b>Size:</b> {row['size']:.2f} t/h<br>"
            f"<b>Total flow:</b> {row['total_flow']:,.0f} t<br>"
            f"<b>CAPEX:</b> €{row['capex']:,.0f}"
        )
    ).add_to(layers[row["mode"]])

if missing:
    print(f"\n⚠️  Missing coordinates for {len(missing)} arc(s) — not in NodeLocations.csv:")
    for fn, tn in missing:
        print(f"   '{fn}' → '{tn}'")

active_nodes_norm = {normalize(n) for n in active_nodes_set}

for node_name, (lat, lon) in node_coords_orig.items():
    is_active = (node_name in active_nodes_set) or \
                (normalize(node_name) in active_nodes_norm)
    folium.CircleMarker(
        location=[lat, lon],
        radius=7 if is_active else 3,
        color="#d62728" if is_active else "#aaaaaa",
        fill=True,
        fill_color="#d62728" if is_active else "#cccccc",
        fill_opacity=0.85,
        tooltip=f"<b>{node_name}</b><br>"
                f"{'🔴 Active' if is_active else '⚪ Inactive'}"
    ).add_to(node_layer)

legend_html = """
<div style="position:fixed; bottom:30px; left:30px; z-index:1000;
     background:white; padding:15px; border-radius:8px;
     border:1px solid #ccc; font-size:13px; line-height:2;">
  <b>CO₂ Transport Route</b><br>
  <span style="color:#1f77b4; font-size:18px;">━━</span> CO₂ Pipeline<br>
  <span style="color:#d62728; font-size:18px;">━━</span> CO₂ Ship<br>
  <hr style="margin:6px 0">
  <b>Node</b><br>
  <span style="color:#d62728;">●</span> Active<br>
  <span style="color:#aaa;">●</span> Inactive<br>
  <hr style="margin:6px 0">
  <i style="font-size:11px">Line thickness ∝ capacity (t/h)</i>
</div>
"""
m.get_root().html.add_child(folium.Element(legend_html))
folium.LayerControl(collapsed=False).add_to(m)
m.save(str(output_map))
print(f"✅ Map saved → {output_map}")
import duckdb
import pandas as pd
import pydeck as pdk
import streamlit as st
from pathlib import Path

import shapely.wkt
from shapely.geometry import LineString


st.set_page_config(layout="wide", page_title="CO2 Network Dashboard")

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "database.duckdb"

SUBSECTOR_COLORS = {
    "cement": [255, 99, 71, 220],      # Tomato
    "steel": [0, 191, 255, 220],              # Deep Sky Blue
    "waste": [138, 43, 226, 220],                # Blue Violet
    "unknown": [169, 169, 169, 220]              # Dark Gray
}


EDGE_COLORS = {
    "emitter_to_port": [25, 156, 2, 180],
    "emitter_to_emitter": [0, 0, 0, 170],
    "emitter_to_alternative": [255, 54, 235, 170],
    "emitter_to_terminal": [5, 174, 240, 170],
    "terminal_to_storage": [8, 37, 255, 170],
}

ADDITIONAL_PIPELINE_COLOR = [8, 37, 255, 170]

SHIP_ROUTE_COLOR = [8, 37, 255, 170]
MANUAL_PIPELINE_COLOR = [0, 0, 0, 170]
MANUAL_PIPELINE_DIRECTION_COLORS = {
    "oneway": [34, 139, 34, 190],
    "twoway": [30, 144, 255, 190],
    "reverse": [255, 140, 0, 190],
}

XIAO_COLOR = [255, 165, 0, 200]


def _format_value(value):
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return str(value)


def _build_hover_text(df: pd.DataFrame, excluded_cols: set[str]) -> pd.Series:
    cols = [c for c in df.columns if c not in excluded_cols]

    def _row_to_text(row):
        parts = []
        for col in cols:
            val = _format_value(row[col])
            if val == "":
                continue
            parts.append(f"<b>{col}</b>: {val}")
        return "<br/>".join(parts)

    return df.apply(_row_to_text, axis=1)


def _clean_coords(df: pd.DataFrame, lat_col: str, lon_col: str) -> pd.DataFrame:
    out = df.copy()
    out[lat_col] = pd.to_numeric(out[lat_col], errors="coerce")
    out[lon_col] = pd.to_numeric(out[lon_col], errors="coerce")
    out = out.dropna(subset=[lat_col, lon_col])
    out = out[(out[lat_col].between(-90, 90)) & (out[lon_col].between(-180, 180))]
    return out


def _parse_linestring_wkt(wkt_str):
    """Parse LINESTRING WKT to pydeck path format [[lon, lat], ...]."""
    try:
        if pd.isna(wkt_str):
            return None
        geom = shapely.wkt.loads(str(wkt_str))
        if isinstance(geom, LineString):
            return [[coord[0], coord[1]] for coord in geom.coords]
    except Exception:
        return None
    return None


@st.cache_data
def load_data(db_mtime_ns: int):
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    pipeline = con.execute("SELECT * FROM pipeline_network").fetchdf()
    combined = con.execute("SELECT * FROM combined_selected_final").fetchdf()

    try:
        pipeline_additional = con.execute("SELECT * FROM pipeline_network_additional").fetchdf()
    except Exception:
        pipeline_additional = pd.DataFrame()

    con.close()

    type_norm = combined["type"].astype(str).str.strip().str.lower()
    emitters = combined[type_norm == "emitter"].copy()
    ports = combined[type_norm == "port"].copy()
    if "selection_2" in ports.columns:
        sel_norm = ports["selection_2"].astype(str).str.strip().str.lower()
        ports = ports[sel_norm == "yes"].copy()   
    #if "selection" in ports.columns:
    #    sel_norm = ports["selection"].astype(str).str.strip().str.lower()
    #    ports = ports[sel_norm == "yes"].copy()
    storage = combined[type_norm == "storage"].copy()

    return pipeline, emitters, ports, storage, pipeline_additional


@st.cache_data
def load_manual_data(ship_routes_mtime_ns: int, manual_pipeline_mtime_ns: int):
    ship_routes_path = PROJECT_DIR / "2_data_processed" / "intermediate_output" / "ship_routes_manual_edit.xlsx"
    manual_pipeline_path = PROJECT_DIR / "2_data_processed" / "intermediate_output" / "pipeline_network_manual_edit.xlsx"

    # Load ship routes if file exists, else empty
    if ship_routes_path.exists():
        ship_df = pd.read_excel(ship_routes_path)
        if "selection_2" in ship_df.columns:
            ship_df = ship_df[
                ship_df["selection_2"].astype(str).str.strip().str.lower() == "yes"
            ].copy()
        #if "selection" in ship_df.columns:
        #    ship_df = ship_df[
        #        ship_df["selection"].astype(str).str.strip().str.lower() == "yes"
        #    ].copy()
    else:
        ship_df = pd.DataFrame()

    if not ship_df.empty:
        ship_df["path"] = ship_df["geometry_wkt"].apply(_parse_linestring_wkt)
        ship_df = ship_df.dropna(subset=["path"])

    # Load manual pipelines if file exists
    if manual_pipeline_path.exists():
        manual_df = pd.read_excel(manual_pipeline_path)

        # Prefer manual geometry from geometry_wkt; fallback to straight line if absent.
        manual_df["path"] = None
        if "geometry_wkt" in manual_df.columns:
            manual_df["path"] = manual_df["geometry_wkt"].apply(_parse_linestring_wkt)

        fallback_mask = manual_df["path"].isna()
        if fallback_mask.any():
            fallback = manual_df.loc[fallback_mask].copy()
            fallback = _clean_coords(fallback, "from_latitude", "from_longitude")
            fallback = _clean_coords(fallback, "to_latitude", "to_longitude")
            fallback["path"] = fallback.apply(
                lambda r: [[r["from_longitude"], r["from_latitude"]],
                           [r["to_longitude"], r["to_latitude"]]],
                axis=1,
            )
            manual_df.loc[fallback.index, "path"] = fallback["path"]

        manual_df = manual_df.dropna(subset=["path"]).copy()
        manual_df["route_source"] = manual_df["geometry_wkt"].apply(
            lambda v: "manual_polyline" if pd.notna(v) and str(v).strip() else "straight_line"
        ) if "geometry_wkt" in manual_df.columns else "straight_line"
    else:
        manual_df = pd.DataFrame()

    return ship_df, manual_df


@st.cache_data
def load_xiao_networks():
    xiao_greece_path = PROJECT_DIR / "0_data_exploration" / "export" / "xiao" / "node_metrics_greece_edges.xlsx"
    xiao_italy_path = PROJECT_DIR / "0_data_exploration" / "export" / "xiao" / "node_metrics_italy_edges.xlsx"

    greece_df = pd.read_excel(xiao_greece_path) if xiao_greece_path.exists() else pd.DataFrame()
    italy_df = pd.read_excel(xiao_italy_path) if xiao_italy_path.exists() else pd.DataFrame()

    combined_xiao = pd.concat([greece_df, italy_df], ignore_index=True) if not (greece_df.empty and italy_df.empty) else pd.DataFrame()
    return combined_xiao


def prepare_xiao_nodes(xiao_df: pd.DataFrame, node_size: float) -> tuple[pd.DataFrame, pdk.Layer]:
    if xiao_df.empty:
        return pd.DataFrame(), pdk.Layer("ScatterplotLayer", pd.DataFrame())

    from_nodes = xiao_df[['from_name', 'from_latitude', 'from_longitude']].copy()
    to_nodes = xiao_df[['to_name', 'to_latitude', 'to_longitude']].copy()
    from_nodes.columns = ['node_name', 'latitude', 'longitude']
    to_nodes.columns = ['node_name', 'latitude', 'longitude']

    all_nodes = pd.concat([from_nodes, to_nodes], ignore_index=True)
    all_nodes = all_nodes.drop_duplicates(subset=['node_name']).reset_index(drop=True)
    all_nodes = _clean_coords(all_nodes, "latitude", "longitude")

    if all_nodes.empty:
        return pd.DataFrame(), pdk.Layer("ScatterplotLayer", pd.DataFrame())

    all_nodes["color"] = [XIAO_COLOR] * len(all_nodes)
    all_nodes["radius"] = node_size

    excluded = {"latitude", "longitude", "color", "radius"}
    all_nodes["hover_text"] = _build_hover_text(all_nodes, excluded)

    layer = pdk.Layer(
        "ScatterplotLayer",
        all_nodes,
        get_position="[longitude, latitude]",
        get_fill_color="color",
        get_line_color=[0, 0, 0, 100],
        line_width_min_pixels=1,
        get_radius="radius",
        pickable=True,
        auto_highlight=True,
    )
    return all_nodes, layer


def prepare_xiao_pipeline_layer(xiao_df: pd.DataFrame) -> tuple[pd.DataFrame, pdk.Layer]:
    if xiao_df.empty:
        return pd.DataFrame(), pdk.Layer("PathLayer", pd.DataFrame())

    x = xiao_df.copy()
    x["from_latitude"] = pd.to_numeric(x["from_latitude"], errors="coerce")
    x["from_longitude"] = pd.to_numeric(x["from_longitude"], errors="coerce")
    x["to_latitude"] = pd.to_numeric(x["to_latitude"], errors="coerce")
    x["to_longitude"] = pd.to_numeric(x["to_longitude"], errors="coerce")
    x = x.dropna(subset=["from_latitude", "from_longitude", "to_latitude", "to_longitude"])

    x["path"] = x.apply(
        lambda r: [[r["from_longitude"], r["from_latitude"]],
                   [r["to_longitude"], r["to_latitude"]]],
        axis=1,
    )
    x["color"] = [XIAO_COLOR] * len(x)

    excluded = {"from_latitude", "from_longitude", "to_latitude", "to_longitude", "path", "color"}
    x["hover_text"] = _build_hover_text(x, excluded)

    layer = pdk.Layer(
        "PathLayer",
        x,
        get_path="path",
        get_color="color",
        get_width=3,
        width_min_pixels=1,
        pickable=True,
        auto_highlight=True,
    )
    return x, layer


def prepare_pipeline_layer(pipeline_df: pd.DataFrame, color_by_edge_type: bool = True) -> tuple[pd.DataFrame, pdk.Layer]:
    p = _clean_coords(pipeline_df, "from_latitude", "from_longitude")
    p = _clean_coords(p, "to_latitude", "to_longitude")

    p["path"] = p.apply(
        lambda r: [[r["from_longitude"], r["from_latitude"]],
                   [r["to_longitude"], r["to_latitude"]]],
        axis=1,
    )
    if color_by_edge_type:
        p["color"] = p["edge_type"].map(EDGE_COLORS).apply(
            lambda v: v if isinstance(v, list) else [100, 100, 100, 160]
        )
    else:
        p["color"] = [[0, 0, 0, 170]] * len(p)
        p["edge_type"] = "Pipeline"

    excluded = {"from_latitude", "from_longitude", "to_latitude", "to_longitude", "path", "color"}
    p["hover_text"] = _build_hover_text(p, excluded)

    layer = pdk.Layer(
        "PathLayer",
        p,
        get_path="path",
        get_color="color",
        get_width=4,
        width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )
    return p, layer


def prepare_additional_pipeline_layer(pipeline_df: pd.DataFrame) -> tuple[pd.DataFrame, pdk.Layer]:
    if pipeline_df.empty:
        return pd.DataFrame(), pdk.Layer("PathLayer", pd.DataFrame())

    p = _clean_coords(pipeline_df, "from_latitude", "from_longitude")
    p = _clean_coords(p, "to_latitude", "to_longitude")

    p["path"] = p.apply(
        lambda r: [[r["from_longitude"], r["from_latitude"]],
                   [r["to_longitude"], r["to_latitude"]]],
        axis=1,
    )
    p["color"] = [ADDITIONAL_PIPELINE_COLOR] * len(p)

    excluded = {"from_latitude", "from_longitude", "to_latitude", "to_longitude", "path", "color"}
    p["hover_text"] = _build_hover_text(p, excluded)

    layer = pdk.Layer(
        "PathLayer",
        p,
        get_path="path",
        get_color="color",
        get_width=4,
        width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )
    return p, layer


def prepare_ship_route_layer(ship_df: pd.DataFrame) -> tuple[pd.DataFrame, pdk.Layer]:
    if ship_df.empty:
        return pd.DataFrame(), pdk.Layer("PathLayer", pd.DataFrame())

    routes = ship_df.copy()
    routes["color"] = [SHIP_ROUTE_COLOR] * len(routes)

    excluded = {"geometry_wkt", "path", "color"}
    routes["hover_text"] = _build_hover_text(routes, excluded)

    layer = pdk.Layer(
        "PathLayer",
        routes,
        get_path="path",
        get_color="color",
        get_width=3,
        width_min_pixels=1,
        pickable=True,
        auto_highlight=True,
    )
    return routes, layer


def prepare_manual_pipeline_layer(manual_df: pd.DataFrame, color_by_direction: bool = False) -> tuple[pd.DataFrame, pdk.Layer]:
    if manual_df.empty:
        return pd.DataFrame(), pdk.Layer("PathLayer", pd.DataFrame())

    m = manual_df.copy()
    if color_by_direction and "direction" in m.columns:
        m["direction_norm"] = m["direction"].astype(str).str.strip().str.lower()
        m["color"] = m["direction_norm"].map(MANUAL_PIPELINE_DIRECTION_COLORS).apply(
            lambda v: v if isinstance(v, list) else MANUAL_PIPELINE_COLOR
        )
    else:
        m["color"] = [MANUAL_PIPELINE_COLOR] * len(m)

    excluded = {"path", "color"}
    m["hover_text"] = _build_hover_text(m, excluded)

    layer = pdk.Layer(
        "PathLayer",
        m,
        get_path="path",
        get_color="color",
        get_width=4,
        width_min_pixels=2,
        pickable=True,
        auto_highlight=True,
    )
    return m, layer


@st.cache_data
def load_distance_matrix():
    matrix_path = PROJECT_DIR / "2_data_processed" / "network_topology_prep" / "CO2_Pipeline" / "distance.csv"
    if matrix_path.exists():
        try:
            df = pd.read_csv(matrix_path, index_col="NODE", sep=";")
            st.success(f"Loaded distance matrix: {df.shape[0]} x {df.shape[1]}")
            return df
        except Exception as e:
            st.warning(f"Cannot load distance matrix: {e}")
            return None
    else:
        st.warning(f"Distance matrix not found at: {matrix_path}")
        return None


# ---------- FIXED distance matrix layer function ----------
def prepare_distance_matrix_layer(dist_matrix, threshold_km, opacity, line_width,
                                  show_matrix,  # new parameter
                                  emitters_clean=None, ports_clean=None,
                                  storage_clean=None, manual_pipelines_clean=None,
                                  pipeline_clean=None):
    if dist_matrix is None or not show_matrix:
        return pd.DataFrame(), pdk.Layer("PathLayer", pd.DataFrame())

    node_coords = {}

    # Build coordinate dictionary from all available sources.
    # For emitters/ports/storage the node identifier may be "name_sanitized" or "name".
    sources = [
        (emitters_clean, ["name_sanitized", "name"], "longitude", "latitude"),
        (ports_clean,    ["name_sanitized", "name"], "longitude", "latitude"),
        (storage_clean,  ["name_sanitized", "name"], "longitude", "latitude"),
        (manual_pipelines_clean, ["from_name"], "from_longitude", "from_latitude"),
        (manual_pipelines_clean, ["to_name"],   "to_longitude",   "to_latitude"),
        (pipeline_clean, ["from_name"], "from_longitude", "from_latitude"),
        (pipeline_clean, ["to_name"],   "to_longitude",   "to_latitude"),
    ]

    for df, name_cols, lon_col, lat_col in sources:
        if df is None or df.empty:
            continue
        # Use the first name column that actually exists in the DataFrame
        name_col = next((c for c in name_cols if c in df.columns), None)
        if name_col is None:
            continue
        for _, row in df.iterrows():
            name = row.get(name_col)
            lon  = row.get(lon_col)
            lat  = row.get(lat_col)
            if pd.notna(name) and pd.notna(lon) and pd.notna(lat):
                node_coords[str(name).strip()] = [float(lon), float(lat)]

    edges_list = []
    nodes = dist_matrix.index.tolist()

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            # Check both triangles so directional edges stored in either half are shown
            d_ij = dist_matrix.iloc[i, j]
            d_ji = dist_matrix.iloc[j, i]
            # Use the non-zero value; if both non-zero, use the smaller
            candidates = [v for v in [d_ij, d_ji] if pd.notna(v) and v > 0]
            if not candidates:
                continue
            dist = min(candidates)
            if dist > threshold_km:
                continue
            n1 = str(nodes[i]).strip()
            n2 = str(nodes[j]).strip()
            if n1 in node_coords and n2 in node_coords:
                edges_list.append({
                    "path": [node_coords[n1], node_coords[n2]],
                    "distance_km": float(dist),
                    "color": [255, 0, 0, 102]
                })

    if not edges_list:
        st.info("No connections within threshold for Distance Matrix")
        return pd.DataFrame(), pdk.Layer("PathLayer", pd.DataFrame())

    df_edges = pd.DataFrame(edges_list)

    layer = pdk.Layer(
        "PathLayer",
        df_edges,
        get_path="path",
        get_color="color",
        get_width=line_width,
        width_min_pixels=8,
        pickable=True,
        auto_highlight=True,
    )
    return df_edges, layer


def prepare_point_layer(
    df: pd.DataFrame,
    color: list[int],
    radius_col: str = None,
    base_radius: int = 20000,
    scale_factor: float = 1.0,
    color_by_subsector: bool = False,
) -> tuple[pd.DataFrame, pdk.Layer]:

    points = _clean_coords(df, "latitude", "longitude")

    # NEW
    if color_by_subsector and "subsector" in points.columns:

        def get_subsector_color(val):
            if pd.isna(val):
                return SUBSECTOR_COLORS["other"]

            val_norm = str(val).strip().lower()

            for key, c in SUBSECTOR_COLORS.items():
                if key in val_norm:
                    return c

            return SUBSECTOR_COLORS["other"]

        points["color"] = points["subsector"].apply(get_subsector_color)

    else:
        points["color"] = [color] * len(points)

    if radius_col and radius_col in points.columns:
        points["radius"] = pd.to_numeric(points[radius_col], errors="coerce").fillna(0) * scale_factor
    else:
        points["radius"] = base_radius

    excluded = {"latitude", "longitude", "color", "radius"}
    points["hover_text"] = _build_hover_text(points, excluded)

    layer = pdk.Layer(
        "ScatterplotLayer",
        points,
        get_position="[longitude, latitude]",
        get_fill_color="color",
        get_line_color=[0, 0, 0, 180],
        line_width_min_pixels=1,
        get_radius="radius",
        pickable=True,
        auto_highlight=True,
    )

    return points, layer


def prepare_port_layer(df: pd.DataFrame, color: list[int], size: float) -> tuple[pd.DataFrame, pdk.Layer]:
    ports = _clean_coords(df, "latitude", "longitude")

    triangles = []
    for _, row in ports.iterrows():
        lon, lat = row["longitude"], row["latitude"]
        offset = size / 111000

        triangle = {
            "polygon": [
                [lon, lat + offset],
                [lon - offset * 0.866, lat - offset * 0.5],
                [lon + offset * 0.866, lat - offset * 0.5],
            ],
            "color": color,
        }
        for col in ports.columns:
            if col not in ["latitude", "longitude"]:
                triangle[col] = row[col]
        triangles.append(triangle)

    triangle_df = pd.DataFrame(triangles)

    excluded = {"polygon", "color"}
    triangle_df["hover_text"] = _build_hover_text(triangle_df, excluded)

    layer = pdk.Layer(
        "PolygonLayer",
        triangle_df,
        get_polygon="polygon",
        get_fill_color="color",
        get_line_color=[0, 0, 0, 200],
        line_width_min_pixels=1,
        pickable=True,
        auto_highlight=True,
    )
    return ports, layer


def prepare_storage_label_layer(storage_df: pd.DataFrame) -> pdk.Layer:
    storage = _clean_coords(storage_df, "latitude", "longitude")

    if "capacity_T_2" in storage.columns:
        storage["capacity_label"] = pd.to_numeric(storage["capacity_T_2"], errors="coerce") / 1_000_000
        storage["capacity_label"] = storage["capacity_label"].fillna(0).map(lambda x: f"{x:.1f} MtCO2")


    #if "capacity_T" in storage.columns:
    #    storage["capacity_label"] = pd.to_numeric(storage["capacity_T"], errors="coerce") / 1_000_000
    #    storage["capacity_label"] = storage["capacity_label"].fillna(0).map(lambda x: f"{x:.1f} MtCO2")
    
    else:
        storage["capacity_label"] = "0.0 MtCO2"

    return pdk.Layer(
        "TextLayer",
        storage,
        get_position="[longitude, latitude]",
        get_text="capacity_label",
        get_size=20,
        get_color=[255, 0, 0, 255],
        get_angle=0,
        get_text_anchor="start",
        get_alignment_baseline="center",
        get_pixel_offset=[25, 0],
        pickable=False,
    )


# ==================== MAIN APP ====================
st.title("CCS Network")
st.caption(f"Data source: {DB_PATH}")

# Sidebar controls
st.sidebar.subheader("Emitter:")
emitter_selection = st.sidebar.selectbox("Selection", options=["All", "Yes", "No"], index=0)
emitter_color_mode = st.sidebar.selectbox(
    "Emitter color mode",
    options=["Single Color", "Subsector"],
    index=0
)

emitter_size_mode = st.sidebar.selectbox("Emitter size based on", options=["None", "emission_TPA"], index=0)



if emitter_size_mode == "emission_TPA":
    emitter_scale = st.sidebar.slider("Emitter Scale (x emission_TPA)", 0.0, 0.04, 0.01, 0.001)
    emitter_base_radius = 20000
else:
    emitter_scale = 1.0
    emitter_base_radius = st.sidebar.slider("Emitter Point Size (None mode)", 2000, 60000, 20000, 1000)

# Move Manual Pipelines selection dropdown here
st.sidebar.markdown("**Manual Pipelines:**")
manual_pipeline_selection = st.sidebar.selectbox(
    "Manual Pipelines Selection",
    options=["All", "Yes"],
    index=0,
    key="manual_pipeline_selection",
)


st.sidebar.markdown("**Storage:**")

storage_size_mode = st.sidebar.selectbox("Storage size based on", options=["None", "capacity_T_2"], index=0)
if storage_size_mode == "capacity_T_2":
    storage_scale = st.sidebar.slider("Storage Scale (x capacity_T_2)", 0.0001, 0.0005, 0.0001, 0.0001)
    storage_base_radius = 20000


#storage_size_mode = st.sidebar.selectbox("Storage size based on", options=["None", "capacity_T"], index=0)
#if storage_size_mode == "capacity_T":
#    storage_scale = st.sidebar.slider("Storage Scale (x capacity_T)", 0.0001, 0.0005, 0.0001, 0.0001)
#    storage_base_radius = 20000

else:
    storage_scale = 1.0
    storage_base_radius = st.sidebar.slider("Storage Point Size (None mode)", 2000, 60000, 20000, 1000)

st.sidebar.markdown("**Port:**")
port_size = st.sidebar.slider("Port Triangle Size (meters)", 500, 20000, 5000, 500)

st.sidebar.subheader("Show/Hide Layers")
show_pipeline = st.sidebar.checkbox("Pipeline Network", value=True)
pipeline_color_by_type = st.sidebar.checkbox("Pipeline color by edge type", value=True)
show_additional_pipeline = st.sidebar.checkbox("Additional Pipeline Network", value=True)
show_emitters = st.sidebar.checkbox("Emitters", value=True)
show_ports = st.sidebar.checkbox("Ports", value=True)
show_storage = st.sidebar.checkbox("Storage", value=True)

show_ship_routes = st.sidebar.checkbox("Manual Ship Routes", value=True)

show_manual_pipelines = st.sidebar.checkbox("Manual Pipelines", value=True)
manual_pipeline_color_by_direction = st.sidebar.checkbox("Manual Pipelines color by direction", value=False)

st.sidebar.subheader("Distance Matrix")
show_distance_matrix = st.sidebar.checkbox("Show Distance Matrix Overlay", value=False)
if show_distance_matrix:
    matrix_threshold = st.sidebar.slider("Show connections shorter than (km)", 5, 5000, 5000, 50)
    matrix_opacity = st.sidebar.slider("Matrix Line Opacity", 30, 120, 102, 5)
    matrix_width = st.sidebar.slider("Matrix Line Width", 1.0, 8.0, 4.0, 0.5)
else:
    matrix_threshold = 5000
    matrix_opacity = 102
    matrix_width = 4.0

st.sidebar.subheader("Xiao's Network")
show_xiao_network = st.sidebar.checkbox("Show Xiao's Network (Greece & Italy)", value=False)
if show_xiao_network:
    xiao_node_size = st.sidebar.slider("Xiao's Node Size (meters)", 2000, 40000, 8000, 1000)
else:
    xiao_node_size = 8000

# Load all data
db_mtime_ns = DB_PATH.stat().st_mtime_ns if DB_PATH.exists() else 0
ship_routes_path = PROJECT_DIR / "2_data_processed" / "intermediate_output" / "ship_routes_manual_edit.xlsx"
manual_pipeline_path = PROJECT_DIR / "2_data_processed" / "intermediate_output" / "pipeline_network_manual_edit.xlsx"
ship_routes_mtime_ns = ship_routes_path.stat().st_mtime_ns if ship_routes_path.exists() else 0
manual_pipeline_mtime_ns = manual_pipeline_path.stat().st_mtime_ns if manual_pipeline_path.exists() else 0

pipeline_df, emitters_df, ports_df, storage_df, pipeline_additional_df = load_data(db_mtime_ns)
ship_routes_df, manual_pipelines_df = load_manual_data(ship_routes_mtime_ns, manual_pipeline_mtime_ns)
# Filter manual pipelines based on dropdown

if manual_pipeline_selection == "Yes" and "selection_2" in manual_pipelines_df.columns:
    manual_pipelines_df = manual_pipelines_df[
        manual_pipelines_df["selection_2"].astype(str).str.strip().str.lower() == "yes"
    ].copy()
#if manual_pipeline_selection == "Yes" and "selection" in manual_pipelines_df.columns:
#    manual_pipelines_df = manual_pipelines_df[
#        manual_pipelines_df["selection"].astype(str).str.strip().str.lower() == "yes"
#    ].copy()

xiao_network_df = load_xiao_networks() if show_xiao_network else pd.DataFrame()

# Filter emitters based on selection

if emitter_selection == "Yes":
    emitters_df = emitters_df[
        emitters_df["selection_2"].astype(str).str.strip().str.lower() == "yes"
    ].copy()
elif emitter_selection == "No":
    emitters_df = emitters_df[
        emitters_df["selection_2"].astype(str).str.strip().str.lower() == "no"
    ].copy()



#if emitter_selection == "Yes":
#    emitters_df = emitters_df[
#        emitters_df["selection"].astype(str).str.strip().str.lower() == "yes"
#    ].copy()
#elif emitter_selection == "No":
#    emitters_df = emitters_df[
#        emitters_df["selection"].astype(str).str.strip().str.lower() == "no"
#    ].copy()

# Prepare pipeline layers
pipeline_clean, pipeline_layer = prepare_pipeline_layer(pipeline_df, color_by_edge_type=pipeline_color_by_type)
additional_pipeline_clean, additional_pipeline_layer = prepare_additional_pipeline_layer(pipeline_additional_df)
ship_routes_clean, ship_routes_layer = prepare_ship_route_layer(ship_routes_df)
manual_pipelines_clean, manual_pipelines_layer = prepare_manual_pipeline_layer(
    manual_pipelines_df, color_by_direction=manual_pipeline_color_by_direction
)

# Load distance matrix
dist_matrix = load_distance_matrix()

# ---------- IMPORTANT: prepare point layers FIRST ----------
emitter_radius_col = "emission_TPA" if emitter_size_mode == "emission_TPA" else None
emitters_clean, emitters_layer = prepare_point_layer(
    emitters_df,
    [214, 39, 40, 220],
    radius_col=emitter_radius_col,
    base_radius=emitter_base_radius,
    scale_factor=emitter_scale,
    color_by_subsector=(emitter_color_mode == "Subsector"),
)

ports_clean, ports_layer = prepare_port_layer(ports_df, [16, 207, 48, 220], port_size)

storage_radius_col = "capacity_T_2" if storage_size_mode == "capacity_T_2" else None
#storage_radius_col = "capacity_T" if storage_size_mode == "capacity_T" else None

storage_clean, storage_layer = prepare_point_layer(
    storage_df, [20, 31, 240, 210],
    radius_col=storage_radius_col, base_radius=storage_base_radius, scale_factor=storage_scale
)
storage_text_layer = prepare_storage_label_layer(storage_df)

# ---------- NOW create distance matrix layer (all needed variables exist) ----------
dist_matrix_clean, dist_matrix_layer = prepare_distance_matrix_layer(
    dist_matrix, matrix_threshold, matrix_opacity, matrix_width,
    show_distance_matrix,  # pass the checkbox value
    emitters_clean=emitters_clean,
    ports_clean=ports_clean,
    storage_clean=storage_clean,
    manual_pipelines_clean=manual_pipelines_clean,
    pipeline_clean=pipeline_clean
)

# Prepare Xiao's layers if enabled
if show_xiao_network and not xiao_network_df.empty:
    xiao_pipeline_clean, xiao_pipeline_layer = prepare_xiao_pipeline_layer(xiao_network_df)
    xiao_nodes_clean, xiao_nodes_layer = prepare_xiao_nodes(xiao_network_df, xiao_node_size)
else:
    xiao_pipeline_clean = pd.DataFrame()
    xiao_nodes_clean = pd.DataFrame()
    xiao_pipeline_layer = pdk.Layer("PathLayer", pd.DataFrame())
    xiao_nodes_layer = pdk.Layer("ScatterplotLayer", pd.DataFrame())

# Build layers list
layers = []
if show_pipeline:
    layers.append(pipeline_layer)
if show_additional_pipeline and not additional_pipeline_clean.empty:
    layers.append(additional_pipeline_layer)
if show_ship_routes and not ship_routes_clean.empty:
    layers.append(ship_routes_layer)
if show_manual_pipelines and not manual_pipelines_clean.empty:
    layers.append(manual_pipelines_layer)
if show_xiao_network and not xiao_network_df.empty:
    layers.append(xiao_pipeline_layer)
    layers.append(xiao_nodes_layer)
if show_emitters:
    layers.append(emitters_layer)
if show_ports:
    layers.append(ports_layer)
if show_storage:
    layers.append(storage_layer)
    layers.append(storage_text_layer)
if show_distance_matrix and not dist_matrix_clean.empty:
    layers.append(dist_matrix_layer)

# Compute map center
all_lats = pd.concat(
    [
        emitters_clean["latitude"],
        ports_clean["latitude"],
        storage_clean["latitude"],
        pipeline_clean["from_latitude"],
        pipeline_clean["to_latitude"],
        ship_routes_clean["path"].apply(lambda pts: [p[1] for p in pts]).explode() if not ship_routes_clean.empty else pd.Series(),
        manual_pipelines_clean["path"].apply(lambda pts: [p[1] for p in pts]).explode() if not manual_pipelines_clean.empty else pd.Series(),
    ],
    ignore_index=True,
)
all_lons = pd.concat(
    [
        emitters_clean["longitude"],
        ports_clean["longitude"],
        storage_clean["longitude"],
        pipeline_clean["from_longitude"],
        pipeline_clean["to_longitude"],
        ship_routes_clean["path"].apply(lambda pts: [p[0] for p in pts]).explode() if not ship_routes_clean.empty else pd.Series(),
        manual_pipelines_clean["path"].apply(lambda pts: [p[0] for p in pts]).explode() if not manual_pipelines_clean.empty else pd.Series(),
    ],
    ignore_index=True,
)

if all_lats.empty or all_lons.empty:
    center_lat, center_lon = 45.0, 10.0
else:
    center_lat = float(all_lats.mean())
    center_lon = float(all_lons.mean())

view_state = pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=4, pitch=0)

tooltip = {"html": "{hover_text}", "style": {"backgroundColor": "#111111", "color": "#f5f5f5", "fontSize": "12px"}}

deck = pdk.Deck(
    map_style="light",
    initial_view_state=view_state,
    layers=layers,
    tooltip=tooltip,
    height="1400",
)

# Legend

# --- HORIZONTAL LEGEND ABOVE MAP ---
legend_items = []
if emitter_color_mode == "Subsector":
    legend_items.extend([
        '<span style="display:inline-block; width:12px; height:12px; border-radius:50%; background-color:rgba(255, 99, 71, 0.9); border:1px solid #000; margin-right:6px;"></span>Cement',
        '<span style="display:inline-block; width:12px; height:12px; border-radius:50%; background-color:rgba(0, 191, 255, 0.9); border:1px solid #000; margin-right:6px;"></span>Steel',
        '<span style="display:inline-block; width:12px; height:12px; border-radius:50%; background-color:rgba(138, 43, 226, 0.9); border:1px solid #000; margin-right:6px;"></span>Waste',
        '<span style="display:inline-block; width:12px; height:12px; border-radius:50%; background-color:rgba(169, 169, 169, 0.9); border:1px solid #000; margin-right:6px;"></span>Unknown',
    ])
else:
    legend_items.append('<span style="display:inline-block; width:12px; height:12px; border-radius:50%; background-color:rgba(214,39,40,0.9); border:1px solid #000; margin-right:6px;"></span>Emitters')

legend_items.append('<span style="display:inline-block; width:0; height:0; border-left:7px solid transparent; border-right:7px solid transparent; border-bottom:12px solid rgba(16,207,48,0.9); margin-right:6px; vertical-align:middle;"></span>Ports')
legend_items.append('<span style="display:inline-block; width:12px; height:12px; border-radius:50%; background-color:rgba(20,31,240,0.9); border:1px solid #000; margin-right:6px;"></span>Storage')

if pipeline_color_by_type:
    legend_items.extend([
        '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(25,156,2,0.9); margin-right:6px;"></span>Emitter to Port',
        '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(0,0,0,0.9); margin-right:6px;"></span>Emitter to Emitter',
        '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(255,54,235,0.9); margin-right:6px;"></span>Emitter to Alternative',
        '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(5,174,240,0.9); margin-right:6px;"></span>Emitter to Terminal',
        '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(8,37,255,0.9); margin-right:6px;"></span>Terminal to Storage',
    ])
else:
    legend_items.append('<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(0,0,0,0.9); margin-right:6px;"></span>Pipeline')

if show_additional_pipeline and not additional_pipeline_clean.empty:
    legend_items.append('<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(8,37,255,0.9); margin-right:6px;"></span>Additional Pipeline')
if show_ship_routes and not ship_routes_clean.empty:
    legend_items.append('<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(8,37,255,0.9); margin-right:6px;"></span>Ship Routes')
if show_manual_pipelines and not manual_pipelines_clean.empty:
    if manual_pipeline_color_by_direction:
        legend_items.extend([
            '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(34,139,34,0.9); margin-right:6px;"></span>Pipeline: oneway',
            '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(30,144,255,0.9); margin-right:6px;"></span>Pipeline: twoway',
            '<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(255,140,0,0.9); margin-right:6px;"></span>Pipeline: reverse',
        ])
    else:
        legend_items.append('<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(0,0,0,0.9); margin-right:6px;"></span>Manual Pipeline')
if show_xiao_network and not xiao_network_df.empty:
    legend_items.append('<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(255,165,0,0.9); margin-right:6px;"></span>Xiao\'s Pipeline')
    legend_items.append('<span style="display:inline-block; width:12px; height:12px; border-radius:50%; background-color:rgba(255,165,0,0.9); margin-right:6px;"></span>Xiao\'s Nodes')
if show_distance_matrix and not dist_matrix_clean.empty:
    legend_items.append('<span style="display:inline-block; width:22px; height:0; border-top:3px solid rgba(180,180,180,0.8); margin-right:6px;"></span>Distance Matrix')

legend_html = '<div style="display:flex; flex-wrap:wrap; gap:18px; align-items:center; margin-bottom:18px;">' + \
    ''.join(f'<div style="display:flex; align-items:center; gap:4px;">{item}</div>' for item in legend_items) + '</div>'

st.markdown("#### Legend", unsafe_allow_html=True)
st.markdown(legend_html, unsafe_allow_html=True)

# --- INCREASE MAP HEIGHT ---
deck = pdk.Deck(
    map_style="light",
    initial_view_state=view_state,
    layers=layers,
    tooltip=tooltip,
    height=6000,  # Increased height
)

st.pydeck_chart(deck, use_container_width=True)

st.markdown("### Counts")
col1, col2, col3, col4, col5, col6, col7, col8 = st.columns(8)
col1.metric("Emitters", f"{len(emitters_clean):,}")
col2.metric("Ports", f"{len(ports_clean):,}")
col4.metric("Storage", f"{len(storage_clean):,}")
total_points = len(emitters_clean) + len(ports_clean) + len(storage_clean)
col3.metric("Total Points", f"{total_points:,}")
col5.metric("Pipeline", f"{len(pipeline_clean):,}")
col6.metric("Ship Routes", f"{len(ship_routes_clean):,}" if not ship_routes_clean.empty else "N/A")
col7.metric("Manual Pipelines", f"{len(manual_pipelines_clean):,}" if not manual_pipelines_clean.empty else "N/A")
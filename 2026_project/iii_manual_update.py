
# This script is for updating nessary processed data when there are manual edits on the selected nodes and transportation network. It includes the following steps:
# 1) Load the manually edited excel files for selected nodes and transportation network, add a
# 2) Recreate matrices for ship routes and pipeline network for network topology preparation
# 3) Update Topology.json with the new list of nodes and carriers



#######################################################################################################

import json
import pandas as pd
import duckdb
from pathlib import Path
from user_defined_function import canonicalize_name, create_gamma_matrix_v3, create_matrix

##################################################################################################

## Set up all paths relative to this script's location
script_dir = Path(__file__).resolve().parent


## ------ Define file paths ------

# General paths
db_path = script_dir / 'database.duckdb'
output_path_data_processed = script_dir / '2_data_processed'
path_model_input = script_dir / '3_model_inputs'


# Manual edited files
updated_nodes = output_path_data_processed / "intermediate_output" / "combined_selected_manual_edit.xlsx"
updated_pipeline_network = output_path_data_processed / "intermediate_output" / "pipeline_network_manual_edit.xlsx"
updated_ship_routes = output_path_data_processed / "intermediate_output" / "ship_routes_manual_edit.xlsx"


#########################################################################################

def manual_update():
    
################# 1) Load and prepare data for processing ##############################
    
    ###  To add a column 'selected_emitter' in pipeline excel file for helping selecting pipeline routes
    # Load data
    df_combined_selected = pd.read_excel(updated_nodes)
    df_pipeline_selected = pd.read_excel( updated_pipeline_network)
    # 'Yes' if from_name or to_name matches selected name_sanitized from combined_selected file.
    selected_names = {
        canonicalize_name(n)
        for n in df_combined_selected.loc[
            df_combined_selected["selection"].fillna("") == "Yes", "name_sanitized"
        ].dropna().astype(str)
    }
    df_pipeline_selected["selected_emitter"] = df_pipeline_selected.apply(
        lambda row: "Yes"
        if canonicalize_name(row.get("from_name", "")) in selected_names
        or canonicalize_name(row.get("to_name", "")) in selected_names
        else "No",
        axis=1,
    )

    # Canonicalize names in edited files before writing to DB/matrices.
    for col in ["name_sanitized", "from_name", "to_name", "from_port", "to_port"]:
        if col in df_combined_selected.columns:
            df_combined_selected[col] = df_combined_selected[col].apply(canonicalize_name)
        if col in df_pipeline_selected.columns:
            df_pipeline_selected[col] = df_pipeline_selected[col].apply(canonicalize_name)

    df_ship_routes = pd.read_excel(updated_ship_routes)
    for col in ["from_port", "to_port"]:
        if col in df_ship_routes.columns:
            df_ship_routes[col] = df_ship_routes[col].apply(canonicalize_name)

    ###  Helper function to fill missing values (if any) in 'distance_km' column with user_defined_function.distance
    from user_defined_function import distance
    
    df_pipeline_selected["distance_km"] = df_pipeline_selected.apply(
        lambda row: distance(row["from_latitude"], row["from_longitude"], row["to_latitude"], row["to_longitude"])
        if pd.isna(row["distance_km"])
        else row["distance_km"],
        axis=1,
    )


    ###  Overwrite the same input file with the new column.
    df_combined_selected.to_excel(updated_nodes, index=False)
    df_pipeline_selected.to_excel(updated_pipeline_network, index=False)
    df_ship_routes.to_excel(updated_ship_routes, index=False)

    ### To store the updated excel files into database.duckdb for later use (before filtering)
    # Load Excel files from following dictionary to database.duckdb
    update_network = {
        "combined_selected_final": df_combined_selected,
        "pipeline_network_final": df_pipeline_selected,
        "ship_routes_final": df_ship_routes,
    }

    con = duckdb.connect(str(db_path))
    for table_name, df in update_network.items():
        con.register("df_input", df)
        con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM df_input")
    con.close()
    print("Manual update: Updated nodes and transportation network have been stored in database")


################## 2) Create matrices for ship routes and pipeline network for network topology preparation ##############################

    ### ----- Create distance & connection matrices for ship_routes_final ------
    table_name = 'ship_routes_final'  # new table name
    col_start = 'from_port'
    col_end = 'to_port'
    value = 'distance_km'
    output_path_dist = output_path_data_processed / 'network_topology_prep' / 'CO2Ship' / 'distance.csv'
    
    # Distance matrix
    matrix = create_matrix(table_name, col_start, col_end, value, output_path_dist)
    print(f"Manual update: Updated distance matrix for '{table_name}'")
    print("-------------------------------------")

    # Connection matrix (binary adjacency: 1 if route exists, else 0)
    matrix_binary = (matrix > 0).astype(int)
    output_path_conn = output_path_data_processed / 'network_topology_prep' / 'CO2Ship' / 'connection.csv'
    matrix_binary.to_csv(output_path_conn, index_label='BINARY', encoding='utf-8', sep=';')
    print(f"Manual update: Updated matrices for {table_name}")


    # ---- Create gamma matrix for Ship ----
    create_gamma_matrix_v3(
        cost_model_type    = "ship",
        table_name         ="combined_selected_final",  # new table (to get emission data)
        distance_matrix    = output_path_data_processed / "network_topology_prep" / "CO2Ship" / "distance.csv",
        discount_rate      = 0.10,      # Generic number
        financial_year_out = 2025,
        output_path        = output_path_data_processed / "network_topology_prep" / "CO2Ship",
    )
    print(f"Manual update: Updated gamma matrices for '{table_name}'")
    print("---------------------")

    



    ### --- Create distance & connection matrices for pipeline_network_final 
    table_name = 'pipeline_network_final'   # new table name
    col_start = 'from_name'
    col_end = 'to_name'
    value = 'distance_km'
    output_path_dist = output_path_data_processed / 'network_topology_prep' / 'CO2_Pipeline' / 'distance.csv'
    
    # Distance matrix
    matrix = create_matrix(table_name, col_start, col_end, value, output_path_dist)
    print(f"Manual update: Updated distance matrix for '{table_name}'")

    # Connection matrix
    matrix_binary = (matrix > 0).astype(int)
    output_path_conn = output_path_data_processed / 'network_topology_prep' / 'CO2_Pipeline' / 'connection.csv'
    matrix_binary.to_csv(output_path_conn, index_label='BINARY', encoding='utf-8', sep=';')
    print(f"Manual update: Updated connection matrix for '{table_name}'")

   
    # ---- Create gamma matrix for pipeline ----
    create_gamma_matrix_v3(
        cost_model_type    = "pipeline",
        table_name         ="combined_selected_final",  # new table (to get emission data)
        distance_matrix    = output_path_data_processed / "network_topology_prep" / "CO2_Pipeline" / "distance.csv",
        discount_rate      = 0.10,              # Generic number
        financial_year_out = 2025,
        output_path        = output_path_data_processed / "network_topology_prep" / "CO2_Pipeline",
    )
    print(f"Manual update: Updated gamma matrices for '{table_name}'")
    print("---------------------")


    
    print(f"Manual update: Updated matrices for {table_name}")
    
    print("="*100)




##################### 3) Update Topology.json #############################
    
    # Get data from combined_selected table in database.duckdb
    con = duckdb.connect(str(db_path))
    try:
        node_name = con.execute("SELECT DISTINCT name_sanitized AS name FROM combined_selected_final WHERE selection = 'Yes'").fetchall()
    except:
        node_name = con.execute("SELECT DISTINCT name_sanitized AS name FROM combined_selected").fetchall()
    try:
        subsector = con.execute("SELECT DISTINCT subsector FROM combined_selected_final WHERE selection = 'Yes'").fetchall()
    except:
        subsector = con.execute("SELECT DISTINCT subsector FROM combined_selected").fetchall()
    con.close()

    # Extract names from tuples to a list of node names
    node_name_list = sorted({canonicalize_name(name[0]) for name in node_name if name and name[0] is not None})

    # List of carriers to be included in the model (must match with the carriers defined in the model)
    carrier_list = ["electricity", "heat", "CO2captured"]

    # Add subsectors_list into carriers_list, drop null values if there are any
    subsector_list = [subsector[0] for subsector in subsector if subsector[0] is not None]
    carrier_list.extend(subsector_list)

    # ---- Update Topology.json
    with open(path_model_input / "Topology.json", "r") as json_file:
        topology = json.load(json_file)

    topology["nodes"] = node_name_list
    topology["carriers"] = carrier_list

    with open(path_model_input / "Topology.json", "w") as json_file:
        json.dump(topology, json_file, indent=4)
 
    print("Manual update: Updated Topology.json")
    print("---------------------")



def main():
    manual_update()
    con = duckdb.connect(str(db_path))
    tables = con.execute("SHOW TABLES").fetchall()
    print("Tables in DB:", tables)
    con.close()

if __name__ == "__main__":                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     
    main()


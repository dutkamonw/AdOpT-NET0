
##############################################################################################################
# This script is for data processing, which includes:

# Network_topology_prep
# 1) Build an N x N distance matrix, connection matrix and gamma matrix from pipeline
# 2) Build an N x N distance matrix, connection matrix and gamma matrix from ship

# Update Topology.json for create node folder directory (created template from 0_initialize_AdOpT-NET0.py)
# 3) Update Topology.json to set nodes, carriers, and investment periods based on the data in database.duckdb


# Copy existing technology and network data from Adopt-net0 database for model input
# 4) Copy technology JSON files from Adopt-net0 database to a destination folder for model input
# 5) Copy network data JSON files from Adopt-net0 database to a destination folder for model input

# Create new technology JSON files for emitters based on user input in an excel file
# 6) Create emitter_technology JSON files based on user input in excel file

# Calculate import gas price for heat.csv 
# 7) Calculate import gas price for heat.csv based on electricity price and COP of High Temp Heat Pump (HTHP) and store the result in database.duckdb

##################################################################################################

import json
from pathlib import Path
import duckdb
import sys


# Ensure imports resolve to the local repository package first (not site-packages).
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from user_defined_function import (
    canonicalize_name,
    create_gamma_matrix,
    create_matrix,
    copy_technology_from_db,
    copy_network_data_from_db,
    create_emitter_technology,
)


##################################################################################################

## Set up all paths relative to this script's location
script_dir = Path(__file__).resolve().parent

## ------ Define file paths ------

output_path_data_processed = script_dir / '2_data_processed'
path_model_input = script_dir / '3_model_inputs'
db_path = script_dir / 'database.duckdb'    

# Create data_processed folder if it doesn't exist
output_path_data_processed.mkdir(parents=True, exist_ok=True)


####################################################################################################

def data_processing():


################## 1) Build an N x N distance matrix and connection matrix from pipeline_network.distance_km ###################

    # ----- Create distance matrix for pipeline -----
    table_name='pipeline_network'
    col_start='from_name'
    col_end='to_name'
    value='distance_km'
    output_path = output_path_data_processed / 'network_topology_prep' / 'CO2_Pipeline' / 'distance.csv'
   
    output_path.parent.mkdir(parents=True, exist_ok=True)   # Create network_topology_prep folder if it doesn't exist

    matrix = create_matrix(table_name, col_start, col_end, value, output_path)
    print(f"Created distance matrix for '{table_name}'")
    print("---------------------")

    # ---- Create connection matrix for pipeline -----
    # binary adjacency: 1 if route exists, else 0
    matrix_binary = (matrix > 0).astype(int)
    output_path = output_path_data_processed / 'network_topology_prep' / 'CO2_Pipeline' / 'connection.csv'
    matrix_binary.to_csv(output_path, index_label='BINARY', encoding='utf-8', sep=';')
    print(f"Created connection matrix for '{table_name}'")
    print("---------------------")

    # ---- Create gamma matrix for pipeline ----
    create_gamma_matrix(
        cost_model_type    = "pipeline",
        table_name         ="combined_selected",  # To get emission data 
        distance_matrix    = output_path_data_processed / "network_topology_prep" / "CO2_Pipeline" / "distance.csv",
        discount_rate      = 0.10,              # Generic number
        financial_year_out = 2025,
        output_path        = output_path_data_processed / "network_topology_prep" / "CO2_Pipeline",
    )
    print(f"Created gamma matrices for '{table_name}'")
    print("---------------------")




################ 2) Build an N x N matrix from ship_routes.distance_km #####################
    # The union of all ports from `from_port` and `to_port`

    # ----- Create distance & connection matrices for ship -----
    table_name='ship_routes'
    col_start='from_port'
    col_end='to_port'
    value='distance_km'
    output_path = output_path_data_processed / 'network_topology_prep' / 'CO2Ship' / 'distance.csv'
    output_path.parent.mkdir(parents=True, exist_ok=True)   # Create network_topology_prep folder if it doesn't exist

    # Distance matrix
    matrix = create_matrix(table_name, col_start, col_end, value, output_path)
    print(f"Created distance matrix for '{table_name}'")
    print("---------------------")


    # Connection matrix (binary adjacency: 1 if route exists, else 0)
    matrix_binary = (matrix > 0).astype(int)
    output_path = output_path_data_processed / 'network_topology_prep' / 'CO2Ship' / 'connection.csv'
    matrix_binary.to_csv(output_path, index_label='BINARY', encoding='utf-8', sep=';')
    print(f"Created connection matrix for '{table_name}'")
    print("---------------------")


    # ---- Create gamma matrix for Ship ----
    create_gamma_matrix(
        cost_model_type    = "ship",
        table_name         ="combined_selected",  # To get emission data for massflow bounds
        distance_matrix    = output_path_data_processed / "network_topology_prep" / "CO2Ship" / "distance.csv",
        discount_rate      = 0.10,      # Generic number
        financial_year_out = 2025,
        output_path        = output_path_data_processed / "network_topology_prep" / "CO2Ship",
    )
    print(f"Created gamma matrices for '{table_name}'")
    print("---------------------")


##################### 3) Update Topology.json  #############################
    
    # Get data from combined_selected table in database.duckdb
    con = duckdb.connect(str(db_path))
    node_name = con.execute("SELECT DISTINCT name_sanitized AS name FROM combined_selected").fetchall()
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
    topology["investment_periods"] = ["period1"]
    topology["start_date"] =  "2041-01-01 00:00"
    topology["end_date"] = "2041-12-31 23:00"   # Test 1 year
    topology["resolution"] = "1h"

    with open(path_model_input / "Topology.json", "w") as json_file:
        json.dump(topology, json_file, indent=4)
    print("Updated Topology.json")
    print("---------------------")



########################## 4) Copy technology JSON files from Adopt-net0 database to a destination folder ##########################
    
    # Define the list of technology to be copied from the Adopt-net0 database.
    technology_list = [
        "MEA_large",
        "MEA_medium",
        "MEA_small",
        "PermanentStorage_CO2_simple"
    ]
    output_path = output_path_data_processed / "technology_data_prep"

    # Run function
    copy_technology_from_db(technology_list, output_path)
    print(f"Copied technology JSON files from Adopt-net0 database to {output_path} ")
    print("---------------------")


######################### 4 (b) Update global PermanentStorage_CO2_simple.json ##########################

    with open(output_path / "PermanentStorage_CO2_simple.json", "r") as json_file:
        storage = json.load(json_file)
    storage["Flexibility"]["comment"] = "determines the flexibility of the power capacity compared to the energy capacity, injection in t/h"
    storage["Flexibility"]["injection_capacity_is_decision_var"] = 1  # To allow the model to decide the injection capacity
    
    with open(output_path / "PermanentStorage_CO2_simple.json", "w") as json_file:
        json.dump(storage, json_file, indent=4)
    print("Updated PermanentStorage_CO2_simple.json")
    print("---------------------")


########################## 5) Copy network data JSON files from Adopt-net0 database ##########################
    
    # network_data_list should match with the network names defined in Networks.json
    network_data_list = [
        "CO2_Pipeline",
        "CO2Ship"
    ]

    output_path = output_path_data_processed / "network_data_prep"

    # Run function
    copy_network_data_from_db(network_data_list, output_path)
    print("---------------------")



########################## 5) (b) Update CO2_Pipeline.json #####################################
    with open(output_path / "CO2_Pipeline.json", "r") as json_file:
        pipeline = json.load(json_file)
    
    pipeline["capex_defined_per_arc"] = 1       # To enable capex for individual arc
    pipeline["size_max"] = 10000                # Test
    pipeline["size_max_defined_per_arc"] = 0    # To disable max size for individual arc, and use the global max size instead
    pipeline["Performance"]["loss"] = 0         # Set a default loss value
    pipeline["Performance"]["bidirectional_network"] = 0   # 0:Not allow flow in both direction, 1:To allow flow in both directions
    #pipeline["Performance"]["bidirectional_network_precise"] = 1   # 1:Not allow flow in both directions at the same time

    with open(output_path / "CO2_Pipeline.json", "w") as json_file:
        json.dump(pipeline, json_file, indent=4)


########################## 5) (c) Update CO2Ship.json #####################################
    with open(output_path / "CO2Ship.json", "r") as json_file:
        ship = json.load(json_file)
    
    ship["capex_defined_per_arc"] = 1       # To enable capex for individual arc
    ship["size_max"] = 10000                # Test
    ship["size_max_defined_per_arc"] = 0    # To disable max size for individual arc, and use the global max size instead
    ship["Performance"]["loss"] = 0         # Set a default loss value
    ship["Performance"]["energyconsumption"]["electricity"]["cons_model"] = 1
    ship["Performance"]["energyconsumption"]["electricity"]["k_flow"] = 0.009   # Set k_flow from Roussanaly et al., 2021 (DOI: 10.3390/en14185635)
    ship["Performance"]["energyconsumption"]["electricity"]["k_flowDistance"] = 0
    for key in ["p", "c", "T", "eta", "gam", "LHV"]:
        ship["Performance"]["energyconsumption"]["electricity"].pop(key, None)
    
    with open(output_path / "CO2Ship.json", "w") as json_file:
        json.dump(ship, json_file, indent=4)


########################## 6) Create emitter_technology JSON files based on user input in excel file ##############################
    # Define the input excel file path and output folder path for the generated technology JSON files.
    input_path_excel = script_dir / '1_raw' / 'technology_emitter.xlsx'
    output_path = output_path_data_processed / "technology_data_prep"

    # Run function
    create_emitter_technology(input_path_excel, output_path)

    # Normalize Economics keys in the generated emitter files so they match adopt_net0 expectations.
    for emitter_file in output_path.glob("emitter_*.json"):
        with open(emitter_file, "r") as json_file:
            emitter_data = json.load(json_file)

        economics = emitter_data.get("Economics", {})
        if "capex_model" in economics:
            economics["CAPEX_model"] = economics.pop("capex_model")
        if "unit_capex" in economics:
            economics["unit_CAPEX"] = economics.pop("unit_capex")
        if "opex_variable" in economics:
            economics["OPEX_variable"] = economics.pop("opex_variable")
        if "opex_fixed" in economics:
            economics["OPEX_fixed"] = economics.pop("opex_fixed")

        emitter_data["Economics"] = economics

        with open(emitter_file, "w") as json_file:
            json.dump(emitter_data, json_file, indent=4)

    print("---------------------")


############################## 7) Calculate import gas price for heat.csv ####################################################
    # Assuming the gas price = electricity price / COP, where COP is Coefficient of Performance  for High Temp Heat Pump (HTHP)
    # COP = 2.67 (Cremona et. al, 2025)
    con = duckdb.connect(str(db_path))
    electricity_price = con.execute("SELECT * FROM electricity_price_yearly ").fetchdf()

    # Calculate import gas price and store in a new column 'avg_price_EUR/MWhth' in the electricity_price dataframe 
    gas_price = electricity_price.copy()
    gas_price['avg_price_EUR/MWhth'] = gas_price['avg_price_EUR/MWhe'] / 2.67   # COP for High Temp Heat Pump (HTHP) from Cremona et. al, 2025

    # Store the calculated gas price back to duckdb
    con.register("gas_price_yearly", gas_price)
    con.execute("CREATE OR REPLACE TABLE gas_price_yearly AS SELECT * FROM gas_price_yearly")
    con.close()

def main():
    data_processing()

if __name__ == "__main__":                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     
    main()


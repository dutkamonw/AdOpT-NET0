#####################################################################################################################
# This script is the main script to run all steps from raw data to running the model.
# !!! NEED to identify directory and which steps to run, see the section "IDENTIFY WHICH STEPS TO RUN" below !!!

####### This script includes the following steps: #######

# Intialize the AdOpT-NET0 template if needed (create the folder structure and template)
# 1) Run adopt functions to initialize the template

# Data preparation and processing
# 2) Run i_etl_raw_to_db.py (extract, transform, and load raw data into duckdb database, and create combined_selected nodes dataframe)
# 3) Run ii_data_processing.py (create model inputs e,g, network matrices, update topology.json, node location, and technology JSON files)

# Create subfolders for all nodes based on Topology.json
# 4) Run adopt.create_node_folder_directory(), to create node folder directory based on the updated Topology.json file

# Update global model configuration
# 5) Update NodeLocations.csv for model input
# 6) Update ConfigModel.json to set optimization objective and solver options
# 7) Update period1/Networks.json to set new network data

# Copy & Assign topology and network data from data_processed to model input folder
# 8) Copy all network data and topology files 
# 9) Assign technologies to nodes based on node type and update emitter JSON files

# Assign data to each nodes
# 10) Assign carrier data(emitters), electricity/heat limit for every nodes, (emitter data by using emission_TPH = Demand (as model requirement), assign electricity price data to each nodes based on country, and assign gas price for heat.
# 11) Update storage injection rate
# 12) Apply carbon price to all nodes (Updating CarbonCost.csv)

# Run the model
# 13) Run the optimization model and generate results

############################################################################################################################

import json
from pathlib import Path
import adopt_net0 as adopt
import shutil
import duckdb
import pandas as pd
from user_defined_function import assign_technologies_to_nodes, copy_all_files, create_matrix, create_node_location
from i_etl_raw_to_db import etl_raw_to_db
from ii_data_processing import data_processing
from iii_manual_update import manual_update

############################### [ !!IMPORTANT!!] DEFINE PATHS ################################################################################################################

# Use the folder where this script is located
script_dir = Path(__file__).parent

# Define path for adopt-net0 model input folder(to be created)
path_model_input = script_dir / "3_model_inputs"    # Path that created template for model input data (will be updated with processed data and used as model input)
path_model_input.mkdir(parents=True, exist_ok=True)
result_path = script_dir / "results"
result_path.mkdir(parents=True, exist_ok=True)

# Database
db_path = script_dir / "database.duckdb"

############################## [!!IMPORTANT!!]  IDENTIFY WHICH STEPS TO RUN ########################################################################################################

# Change to 'True' if you need to (re)run OR 'False' to skip the step
initialize = False # Step 1) initialize the adopt-net0 template [!!IMPORTANT!!] This study has added 2025 PPI data into "producer_price_index_euro.csv" file
raw_prep = False # Step 2) ETL raw data to database
data_process = False # Step 3) data processing (create matrix, update Topology.json, prepare technology and network data)

manual_update_network = True  # Optional step (if there is a manual update on node selection and transportation routes))

building_node_folder = False # Step 4) create node folders based on Topology.json
prepare_inputs = False # Step 5) to 12) Formating inputs from update global model configuration,  copy processed files, and assign data to each nodes

run_model = False # Step 13) run the optimization model


############################## RUN ALL MODEL INPUT PREPARATION STEPS ##############################################################################################

################## 1) Intialize the AdOpT-NET0 template if needed (create the folder structure and template)  ######################################################
print(f"Initialize adopt-net0 template is {initialize}")

if initialize:
     # Create optimization templates in the inputs folder
    adopt.create_optimization_templates(path_model_input)
    # Create input data folder template in the inputs folder
    adopt.create_input_data_folder_template(path_model_input)
    print("Initializing the template: Completed")
    # Replace the default producer price index data with the updated 2025 PPI data
    ppi_src = script_dir / "1_raw" / "producer_price_index_euro.csv"
    ppi_workspace = script_dir.parent / "adopt_net0" / "database" / "data" / "producer_price_index_euro.csv"
    shutil.copy2(ppi_src, ppi_workspace)
    # Also copy to the installed site-packages version (imported when running from 2026_project/)
    import adopt_net0 as _adopt_net0_pkg
    ppi_installed = Path(_adopt_net0_pkg.__file__).parent / "database" / "data" / "producer_price_index_euro.csv"
    if ppi_installed.resolve() != ppi_workspace.resolve():
        shutil.copy2(ppi_src, ppi_installed)
        print(f"  PPI also copied to installed package: {ppi_installed}")

else:
    print("Skipped initializing the template")

print("="*100)


################## 2) Run raw data preparation steps (run: i_etl_raw_to_db.py)  ###############################
print(f"Raw data preparation is {raw_prep}")

if raw_prep:
    etl_raw_to_db()
    print("ETL raw data to database: Completed")

else:
    print("Skipped raw data preparation")

print("="*100)




################## 3) Run data processing steps (run: ii_data_processing.py)  ###############################
print(f"Data processing is {data_process}")

if data_process:
    data_processing()
    print("Processing input data: Completed")

else:
    print("Skipped data processing")

print("="*100)


################ Optional step: If there is a manual update on node selection and transportation routes,  ###############################

if manual_update_network:
    manual_update()
    print("Manual update: Completed")
else:
    print("No manual update")

################## 4)  Create input data folder template in the inputs folder ####################################
print(f"Building node folder is {building_node_folder}")
if building_node_folder:
    adopt.create_input_data_folder_template(path_model_input)
    # Delete node1, node2 folders created by the template
    for node in ["node1", "node2"]:
        folder = path_model_input / "period1" / "node_data" / node
        if folder.exists():
            shutil.rmtree(folder)
    print("Created node folders: Completed")

else:
    print("Skipped creating node folder directory")

print("="*100)




############################   Preparation all inputs in model_input folder  #####################################################################
print(f"Preparation inputs is {prepare_inputs}")

if prepare_inputs:

    ################## 5) Update NodeLocations.csv for model input (Query the database to get unique node locations from combined_selected table in database.duckdb) #######################
    # Define parameter
    altitude = 10  # assign altitude value to all nodes

    # Run function
    create_node_location(altitude, path_model_input)
    print("Updated NodeLocation.csv: Completed")
    print("-"*60)

    #################### 6) Update ConfigModel.json  ##########################
    with open(path_model_input / "ConfigModel.json", "r") as json_file:
        configuration = json.load(json_file)

    # Set optimization objective (select from existing options in ConfigModel.json)
     #configuration["optimization"]["objective"]["value"] = "emissions_minC"  # find the minimum cost system at minimum emissions (minimizes net emissions in the first step and cost as a second step)
    configuration["optimization"]["objective"]["value"] = "costs_emissionlimit"  # find the minimum cost system that meets a specified emission limit
    configuration["optimization"]["emission_limit"]["value"] = 66998708.72*0.5*0.5/52          # Test 6 wmonths reduction 80% from total emission from all emitters in 1 year
    #configuration["optimization"]["objective"]["value"] = "costs"

    # Set value to define MIP gap for the optimization solver
    configuration["solveroptions"]["mipgap"]["value"] = 0.02  # typically 1%-5% for large problems, lower for more accuracy but longer solve time

    configuration["solveroptions"]["numericfocus"]["value"] = 0 # 0 (default) to 3 (most aggressive) for better numerical stability, especially important for large-scale problems with wide-ranging cost coefficients

    # Set result path
    configuration['reporting']['save_summary_path']['value'] = str(result_path)
    configuration['reporting']['save_path']['value'] = str(result_path)
    
    with open(path_model_input / "ConfigModel.json", "w") as json_file:
        json.dump(configuration, json_file, indent=4)

    print("Updated ConfigModel.json: Completed")
    print("-"*60)

    ########################## 7) Update period1/Networks.json  ##########################
    with open(path_model_input / "period1" / "Networks.json", "r") as json_file:
        networks = json.load(json_file)

    networks["new"] = ["CO2_Pipeline","CO2Ship"]

    with open(path_model_input / "period1" / "Networks.json", "w") as json_file:
        json.dump(networks, json_file, indent=4)
    print("Updated Networks.json: Completed")
    print("-"*60)

    ####################### 8) Copy network data and technology data ##########################

    ########################   Network data ##########################
    # Copy network data JSON files from data_processed to model input
    input_path = script_dir / "2_data_processed" / "network_data_prep"
    output_path = path_model_input / "period1" / "network_data"
    copy_all_files(input_path, output_path)

    ########################   Network topology ##########################
    network_topology_folder = path_model_input / "period1" / "network_topology"

    # Delete all template files in the network_topology folder
    for folder in ["new", "existing"]:
        for file in (network_topology_folder / folder).glob("*.csv"):
            file.unlink()

    # Copy network topology files from data_processed to model_input (assume "new" folder only)
    for transpot_mode in ["CO2_Pipeline", "CO2Ship"]:  # List corresponding to Networks.json
        input_path = script_dir / "2_data_processed" / "network_topology_prep" / transpot_mode
        output_path = network_topology_folder  / "new" / transpot_mode
        copy_all_files(input_path, output_path)
    print("Copying network data and topology data: Completed")


    ########################## 9) Assign technologies to nodes based on node type and update emitter JSON files  ##########################

    input_path = script_dir / "2_data_processed" / "technology_data_prep"
    output_path = path_model_input
    assign_technologies_to_nodes(input_path, output_path)
    print("Assigning technologies to nodes and updating emitter JSON files: Completed")
    
    
    ########################## 10) Assign carrier data #########################
    
    ### Default electricity/heat limits  
    adopt.fill_carrier_data(
        folder_path=path_model_input,
        value_or_data=1000,              # MWe limit for electricity import, can be adjusted based on the context of the case study
        columns=["Import limit"],
        carriers=["electricity"],
        investment_periods=["period1"],
    )


    ### For heat, do not fill for starage
    con = duckdb.connect(str(db_path))
    try:
        heat_nodes = con.execute("SELECT DISTINCT name_sanitized AS name FROM combined_selected_final WHERE type != 'storage' AND selection = 'Yes' ").df()
    except:
        heat_nodes = con.execute("SELECT DISTINCT name_sanitized AS name FROM combined_selected WHERE type != 'storage' ").df()
    con.close()

    adopt.fill_carrier_data(
        folder_path=path_model_input,
        value_or_data=2000,             # MWth limit for heat import, can be adjusted based on the context of the case study
        columns=["Import limit"],
        carriers=["heat"],
        investment_periods=["period1"],
    )

    ### Emission for each emitters
    #  For each emitter node, assign the emission_TPH value as 'Demand' for the corresponding subsector as carrier 
    con = duckdb.connect(str(db_path))
    try:
        df = con.execute("SELECT DISTINCT name_sanitized AS name, subsector, emission_TPH FROM combined_selected_final WHERE type = 'emitter' AND selection = 'Yes'").df()
    except:
        df = con.execute("SELECT DISTINCT name_sanitized AS name, subsector, emission_TPH FROM combined_selected WHERE type = 'emitter'").df()
    con.close()
    
    for _, row in df.iterrows():
        adopt.fill_carrier_data(
            folder_path= path_model_input,
            value_or_data=row["emission_TPH"],  # Average from annual emission, and using it as 'Demand' for each emitter node
            columns=["Demand"],                 # Adopt NET0 model requires 'Demand' data to compute the emission. Tn this study, there is no demand data but emission data, so we use emission_TPH as demand for the model input. 
            carriers=[row["subsector"]],        # Use subsector as carrier, e.g. steel, cement, waste, etc
            nodes=[row["name"]],
            investment_periods=["period1"]
        )

    ### Fill emission factor = 1 for all emitters (as the model will compute the emission based on the demand which is represented by emission_TPH)
    for _, row in df.iterrows():
        adopt.fill_carrier_data(
            folder_path= path_model_input,
            value_or_data=1, 
            columns=["Emission factor"],                 
            carriers=[row["subsector"]],        
            nodes=[row["name"]],
            investment_periods=["period1"]
        )
    


    ### Electricity and heat price based on country (if applicable)
    con = duckdb.connect(str(db_path))
    df_price = con.execute("SELECT * FROM electricity_price_yearly").df()
    # Assign price to 'emitter' and 'port', except 'storage'
    try:
        df_nodes = con.execute("SELECT DISTINCT name_sanitized AS name, iso2 FROM combined_selected_final WHERE type != 'storage' AND selection = 'Yes' ").df()
    except:
        df_nodes = con.execute("SELECT DISTINCT name_sanitized AS name, iso2 FROM combined_selected WHERE type != 'storage' ").df()
    con.close()

    # Assign price for each node by merging nodes with iso2
    df = df_nodes.merge(df_price, on="iso2", how="left")
    df = df.dropna(subset=["avg_price_EUR/MWhe"])

    # Export the merged dataframe to an excel file for checking
    df.to_excel(script_dir / "2_data_processed" / "electricity_price_assigned.xlsx", index=False)

    # For each node, assign the average electricity price (EUR/MWhe) for the carrier 'electricity' if the node's country iso2 matches with the iso2 in the electricity price table
    for _, row in df.iterrows():
        adopt.fill_carrier_data(
            folder_path= path_model_input,
            value_or_data=row["avg_price_EUR/MWhe"],  # Average electricity price (EUR/MWhe)
            columns=["Import price"],          # Create a new column "Import price" for electricity carrier
            carriers=["electricity"],   # Assign to electricity carrier
            nodes=[row["name"]],       # Assign to the node
            investment_periods=["period1"]
        )
    
    ### Import gas price for heat
    con = duckdb.connect(str(db_path))
    df_heat = con.execute("SELECT * FROM gas_price_yearly").df()
    con.close()

    # Assign price for each node by merging nodes with iso2
    df_heat = df_nodes.merge(df_heat, on="iso2", how="left")
    df_heat = df_heat.dropna(subset=["avg_price_EUR/MWhth"])

    # Export the merged dataframe to an excel file for checking
    df_heat.to_excel(script_dir / "2_data_processed" / "heat_price_assigned.xlsx", index=False)

    # For each node, assign the average gas price (EUR/MWhth) for the carrier 'heat' if the node's country iso2 matches with the iso2 in the gas price table
    for _, row in df_heat.iterrows():
        adopt.fill_carrier_data(
            folder_path= path_model_input,
            value_or_data=row["avg_price_EUR/MWhth"],  # Average gas price (EUR/MWhth)
            columns=["Import price"],          # Create a new column "Import price" for heat carrier
            carriers=["heat"],   # Assign to heat carrier
            nodes=[row["name"]],       # Assign to the node
            investment_periods=["period1"]
        )

    print("Filled carrier data for all nodes: Completed")

    ######################### 11) Update storage injection rate  #########################
    
    con = duckdb.connect(str(db_path))
    try:
        storage_df = con.execute(""" SELECT name_sanitized AS node_name, capacity_T FROM combined_selected_final WHERE type = 'storage' AND selection = 'Yes' """).fetchdf()
    except:
        storage_df = con.execute(""" SELECT name_sanitized AS node_name, capacity_T FROM combined_selected WHERE type = 'storage'""").fetchdf()
    con.close()

    if not storage_df.empty:
        for _, row in storage_df.iterrows():
            if pd.notna(row['capacity_T']):
                capacity = float(row['capacity_T'])


            # Assuming the storage can be fully charged or discharged within 25 years in tonne per hour (T/h)
            injection_rate = capacity / (25 * 365 * 24)  # Convert to T/h

            # Cap size_max at what is physically achievable within 1 year (the model horizon: 8760 h).
            # The geological capacity often exceeds what can be injected in 1 year, leaving a large
            # but unreachable upper bound that causes Gurobi numerical scaling warnings.
            # min(capacity, injection_rate * 8760) keeps the bound tight and consistent.
            size_max = min(capacity, injection_rate * 8760*0.5) # Test 6 months

            json_path = path_model_input / "period1" / "node_data" / row['node_name'] / "technology_data" / "PermanentStorage_CO2_simple.json"
        
            if json_path.exists():
                with open(json_path, 'r') as f:
                    data = json.load(f)
                data["size_max"] = round(size_max, 2)   # size_max = max achievable storage in model horizon
                data["Flexibility"]["injection_rate_max"] = round(injection_rate, 4)
                with open(json_path, 'w') as f:
                    json.dump(data, f, indent=4)
    
    print(f"Updated storage injection rate for {len(storage_df)} storage nodes: Completed")



############################# 12) Apply carbon price ################################
    # Extract and apply in one go
    with open(path_model_input / "Topology.json", 'r', encoding='utf-8') as f:
        node_names = json.load(f)["nodes"]

    carbon_price = 0  # Assume
    success = 0

    for node in node_names:
        path = Path(path_model_input / f"period1/node_data/{node}/CarbonCost.csv")
        df = pd.read_csv(path, sep=";")
        df["price"] = carbon_price
        df.to_csv(path, index=False, sep=";") # Save the updated carbon cost back to the same path
        success += 1

    print(f"Applied carbon price to {success} nodes: Completed")

else:
    print("Skipped preparing model input data")

print("="*100)

############################ 13) Run the optimization model  ###############################

if run_model:
    # Run the optimization model
    print("Running the optimization model...")
   
    m = adopt.ModelHub()
    m.read_data(path_model_input)
    print("Reading data: Completed")
   

    m.quick_solve()
    print("Model run: Completed")



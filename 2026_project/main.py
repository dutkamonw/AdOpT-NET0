#####################################################################################################################
# This script is the main script to run all steps from raw data to running the model.
# !!! NEED to identify directory and which steps to run, see the section "IDENTIFY WHICH STEPS TO RUN" below !!!
# Firstly, define the path, then identify which steps to run, and finally run the steps in order.


####### This script includes the following steps: #######

# Intialize the AdOpT-NET0 template if needed (create the folder structure and template)
# 1) Run adopt functions to initialize the template (this includes update PPI data)

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
import os
from pathlib import Path
import adopt_net0 as adopt
import shutil
import duckdb
import pandas as pd
from user_defined_function import assign_technologies_to_nodes, copy_all_files, create_node_location
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
data_process = True # Step 3) data processing (create matrix, update Topology.json, prepare technology and network data)

manual_update_network = True  # Optional step (if there is a manual update on node selection and transportation routes))

building_node_folder = True # Step 4) create node folders based on Topology.json
prepare_inputs = True # Step 5) to 12) Formating inputs from update global model configuration,  copy processed files, and assign data to each nodes

run_model = False # Step 13) run the optimization model


############################### SCENARIO CONFIGURATION ########################################

# objective
#objective = "costs_emissionlimit"  # find the minimum cost system that meets a specified emission limit
objective = "emissions_minC"      # find the minimum cost system at minimum emissions (minimizes net emissions in the first step and cost as a second step)

# scenario_1: excl.Ravenna & Prinos
# scenario_2: incl. Ravenna & Prinos
scenario = "scenario_2"

############################### PARAMETERS SETTING ########################################

# The injection rate is set as a fraction of geological storage capacity per year
percentage_injection = 0.04             # fixed at 4 % of geological capacity per year (Base mid-case)
opex_var_storage_EUR_per_t = 61.6       # 55.4-61.6-86.2 EUR/t relevelised from 42.5-50.6-75.8 EUR/tCO2 based on Ravenna levelised storage cost, Italian goverment report: "Analisi degli aspetti tecnici, economici e normativi funzionali allo sviluppo della filiera CCUS" [Analysis of technical, economic, and regulatory aspects functional to the development of the CCUS supply chain] (2025)

# Reduction target for costs_emissionlimit objective (Default 0.70 = 70% reduction from BAU emissions)
# loop_model.py can override this via env var CCS_REDUCTION_TARGET when objective == costs_emissionlimit.
ccs_reduction_target  = float(os.environ.get("CCS_REDUCTION_TARGET", 0.70))



############################### HELPER #######################################################################
# Helper dictionary to map scenario to the corresponding selection and capacity columns in the database.
SCENARIO_COLUMNS = {
    "scenario_1": {"selection_col": "selection",   "capacity_col": "capacity_T"},
    "scenario_2": {"selection_col": "selection_2", "capacity_col": "capacity_T_2"},
}
if scenario not in SCENARIO_COLUMNS:
    raise ValueError(f"Unknown scenario '{scenario}'. Choose one of {list(SCENARIO_COLUMNS)}.")
selection_col = SCENARIO_COLUMNS[scenario]["selection_col"]
capacity_col  = SCENARIO_COLUMNS[scenario]["capacity_col"]
print(f"Scenario: {scenario}  (selection_col='{selection_col}', capacity_col='{capacity_col}')")


def run_final_query(con, sql: str, purpose: str):
    """Execute a scenario-aware query on combined_selected_final or fail with context."""
    try:
        return con.execute(sql)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read scenario-aware data for {purpose}. "
            f"Expected table 'combined_selected_final' with columns '{selection_col}' and '{capacity_col}'. "
            f"Original error: {exc}"
        ) from exc


# Persist the active scenario so result.py (and loop_model.py) use the exact same
# scenario/columns as this run, without duplicating the setting elsewhere.
with open(script_dir / "scenario_state.json", "w", encoding="utf-8") as _sf:
    json.dump(
        {"scenario": scenario, "selection_col": selection_col, "capacity_col": capacity_col},
        _sf,
        indent=4,
    )

# Safety: the loop is intended only for sweeping reduction targets under costs_emissionlimit.
if os.environ.get("RUN_LOOP_MODE", "False").strip().lower() == "true" and objective != "costs_emissionlimit":
    raise ValueError(
        "loop_model.py is intended for objective='costs_emissionlimit' only. "
        "Please set objective='costs_emissionlimit' in main.py before running the loop."
    )









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
    manual_update(selection_col=selection_col)
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

# Auto-compute fraction of year modelled from Topology.json start/end dates. This is used to scale emission_limit and storage size_max consistently.
with open(path_model_input / "Topology.json", 'r', encoding='utf-8') as _topo_f:
    _topo = json.load(_topo_f)
_topo_start = pd.Timestamp(_topo["start_date"])
_topo_end   = pd.Timestamp(_topo["end_date"])
_modelled_hours = (_topo_end - _topo_start).total_seconds() / 3600 + 1  # +1 to include last hour
fraction_of_year_modelled = _modelled_hours / 8760
print(f"Topology period: {_topo_start.date()} → {_topo_end.date()} | {_modelled_hours:.0f} h | fraction={fraction_of_year_modelled:.4f}")

if prepare_inputs:

    ################## 5) Update NodeLocations.csv for model input (Query the database to get unique node locations from combined_selected table in database.duckdb) #######################
    # Define parameter
    altitude = 10  # assign altitude value to all nodes

    # Get annual_total_emission from total emission_tpa of all emitters in combined_selected table in database.duckdb
    con = duckdb.connect(str(db_path))
    annual_total_emission = run_final_query(
        con,
        f"SELECT SUM(emission_TPA) AS annual_total_emission FROM combined_selected_final "
        f"WHERE type = 'emitter' AND {selection_col} = 'Yes'",
        "annual_total_emission",
    ).fetchone()[0]
    con.close()

    print(f"Annual total emission from database: {annual_total_emission:.2f} tCO2 per year")

    # Run function
    create_node_location(altitude, path_model_input, selection_col=selection_col)
    print("Updated NodeLocation.csv: Completed")
    print("-"*60)

    #################### 6) Update ConfigModel.json  ##########################
    with open(path_model_input / "ConfigModel.json", "r") as json_file:
        configuration = json.load(json_file)

    # Set optimization objective (select from existing options in ConfigModel.json)
    if objective == "emissions_minC":
        configuration["optimization"]["objective"]["value"] = "emissions_minC"  # find the minimum cost system at minimum emissions (minimizes net emissions in the first step and cost as a second step)
        print("Optimization objective set to 'emissions_minC'")
    
    if objective == "costs_emissionlimit":
        configuration["optimization"]["objective"]["value"] = "costs_emissionlimit"  # find the minimum cost system that meets a specified emission limit
        print("Optimization objective set to 'costs_emissionlimit'")
        # emission_limit: annual total emission × fraction of year modelled × target reduction (e.g. 0.2 = 80% reduction)
        # fraction_of_year_modelled is auto-derived from Topology.json start_date/end_date above.
        emission_limit_value = annual_total_emission * fraction_of_year_modelled * (1-ccs_reduction_target)
        configuration["optimization"]["emission_limit"]["value"] = emission_limit_value
        emissions_in_horizon = annual_total_emission * fraction_of_year_modelled
        required_capture = emissions_in_horizon - emission_limit_value
        print(
            f"Emission target summary: emissions_in_horizon={emissions_in_horizon:.2f} tCO2, "
            f"emission_limit={emission_limit_value:.2f} tCO2, required_capture={required_capture:.2f} tCO2"
        )

    # Set value to define MIP gap for the optimization solver
    configuration["solveroptions"]["mipgap"]["value"] = 0.01  # typically 1%-5% for large problems, lower for more accuracy but longer solve time

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

    # Scenario 2: zero CAPEX for terminal_to_storage arcs directly connecting to Ravenna and Prinos.
    # These storage sites are treated as having existing injection infrastructure, so no new pipeline
    # CAPEX is charged on the terminal→storage segment. We zero the columns for those nodes in the
    # per-arc gamma matrices (gamma1, gamma2, gamma3) in the copied model-input files.
    if scenario == "scenario_2":
        zero_capex_storage_nodes = ["Ravenna", "Prinos"]
        pipeline_topo_path = network_topology_folder / "new" / "CO2_Pipeline"
        for gamma_file in ["gamma1.csv", "gamma2.csv", "gamma3.csv"]:
            gf = pipeline_topo_path / gamma_file
            if gf.exists():
                df_g = pd.read_csv(gf, sep=";", index_col=0)
                for node in zero_capex_storage_nodes:
                    if node in df_g.columns:
                        df_g[node] = 0.0
                df_g.to_csv(gf, sep=";", float_format="%.4f", index_label="NODE")
        print(f"Scenario 2: zeroed terminal_to_storage CAPEX for {zero_capex_storage_nodes} in gamma matrices.")

    ########################## 9) Assign technologies to nodes based on node type and update emitter JSON files  ##########################

    input_path = script_dir / "2_data_processed" / "technology_data_prep"
    output_path = path_model_input
    assign_technologies_to_nodes(input_path, output_path, selection_col=selection_col)
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
    heat_nodes = run_final_query(
        con,
        f"SELECT DISTINCT name_sanitized AS name FROM combined_selected_final WHERE type != 'storage' AND {selection_col} = 'Yes' ",
        "heat node selection",
    ).df()
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
    df = run_final_query(
        con,
        f"SELECT DISTINCT name_sanitized AS name, subsector, emission_TPH FROM combined_selected_final WHERE type = 'emitter' AND {selection_col} = 'Yes'",
        "emitter rows",
    ).df()
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
    df_nodes = run_final_query(
        con,
        f"SELECT DISTINCT name_sanitized AS name, iso2 FROM combined_selected_final WHERE type != 'storage' AND {selection_col} = 'Yes' ",
        "node iso2 selection",
    ).df()
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

    ######################### 11) Update storage injection rate & OPEX_var (scenario-aware) ########

    con = duckdb.connect(str(db_path))
    storage_df = run_final_query(
        con,
        f""" SELECT name_sanitized AS node_name, {capacity_col} AS capacity FROM combined_selected_final WHERE type = 'storage' AND {selection_col} = 'Yes' """,
        "storage capacities",
    ).fetchdf()
    con.close()

    # For each storage node, compute the injection rate and size_max based on the capacity and percentage_injection, and update the corresponding PermanentStorage_CO2_simple.json file.
    if not storage_df.empty:
        total_size_max = 0.0
        for _, row in storage_df.iterrows():
            if pd.isna(row['capacity']):
                continue
            capacity = float(row['capacity'])
            node_name = str(row['node_name'])

            # Injection rate = fixed fraction of geological capacity (t/h), same across all scenarios
            injection_rate = capacity * percentage_injection / 8760

            # size_max caps injectable CO2 over the modelled horizon (physically achievable upper bound).
            # Uses fraction_of_year_modelled so it updates automatically when Topology dates change.
            size_max = min(capacity, injection_rate * 8760 * fraction_of_year_modelled)
            total_size_max += size_max

            json_path = (path_model_input / "period1" / "node_data" / node_name
                         / "technology_data" / "PermanentStorage_CO2_simple.json")

            if json_path.exists():
                with open(json_path, 'r') as f:
                    data = json.load(f)
                data["size_max"] = round(size_max, 2)
                data["Flexibility"]["injection_rate_max"] = round(injection_rate, 4)
                # Set levelised storage cost for this scenario (amortised CAPEX + OPEX per tCO2)
                data["Economics"]["OPEX_variable"] = opex_var_storage_EUR_per_t
                with open(json_path, 'w') as f:
                    json.dump(data, f, indent=4)

        if objective == "costs_emissionlimit":
            storage_margin = total_size_max - required_capture
            print(
                f"Storage feasibility check: max_injectable={total_size_max:.2f} tCO2, "
                f"required_capture={required_capture:.2f} tCO2, margin={storage_margin:.2f} tCO2"
            )
            if storage_margin < 0:
                print(
                    "WARNING: Storage capacity limit < Required capture. Model can become infeasible even before considering transport connectivity/losses."
                )

    print(
        f"Updated PermanentStorage_CO2_simple JSON files"
        f"for {len(storage_df)} storage nodes: Completed"
    )



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



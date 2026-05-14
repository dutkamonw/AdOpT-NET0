##############################################################################################################
# This script is for data preparation (preprocessing + processsing), which includes:
# 1) Run ETL functions to extract, transform, and load (ETL) raw data into duck
# 2) Run data selection functions to filter the data based on defined parameters
# 3) Combine all selected data into one table for easier querying and analysis  
# 4) Create ship routes data using SCgraph
# 5) Create pipeline network based on selected emitters and ports using straight line (havensine) and Prim's algorithm (for clustering)
# 6) Store electricity price data (hourly) into database.duckdb
#############################################################################################################

from pathlib import Path
from user_defined_function import create_pipeline_network, etl_eea, etl_climate_trace, combine_emitters, etl_co2_storage, etl_port, select_emitters, select_co2_storage, select_ports, combine_all_selected, create_ship_routes, create_pipeline_network, store_additional_electricity_price, store_electricity_price_hourly, store_electricity_price_yearly
import duckdb

################### Define file paths and filter parameters ###################################################

## Set up all paths relative to this script's location
script_dir = Path(__file__).resolve().parent

## ----- Define file path -----
raw_dir = script_dir / '1_raw'


file_path_eea = str(raw_dir / 'EEA_CO2.xlsx')         # raw excel file (query from Industrial_dataset_v_15_2025_12_15.DB)
file_path_climate_trace = str(raw_dir / 'climate_trace')   # folder with multiple raw csv files
file_path_co2_storage = str(raw_dir / 'storage.xlsx')           # manual create excel file
file_path_port = str(raw_dir / 'port.csv')                      # editted csv file from World Port Index
file_path_area = str(raw_dir / 'Area_Boundary.geojson')         # manual create geojson file
#file_path_electricity_price_hourly = str(raw_dir / 'whole_sale_electricity_price_all_countries.csv')  # Ember whole sale electricity price
file_path_electricity_price_yearly = str(raw_dir / 'electricity_price_Eurostat.xlsx')  # Eurostat electricity price (yearly)


db_path = script_dir / 'database.duckdb'            # database file to store the data
output_path_intermediate_result = str(script_dir / '2_data_processed' / 'intermediate_output')    #  export for manual checking


# Create intermediate_output folder if it doesn't exist
Path(output_path_intermediate_result).mkdir(parents=True, exist_ok=True)

## ----- Define filter parameters -----
selected_subsectors = ['steel', 'cement', 'waste']
emission_cutoff = 200000   # tCO2 per year
storage_cutoff = 15000000  # tCO2 


################################## Run function #############################################################

def etl_raw_to_db():

    ##########   1)  Run ETL functions  ####################
    etl_eea(file_path_eea)
    etl_climate_trace(file_path_climate_trace)
    etl_port(file_path_port)
    etl_co2_storage(file_path_co2_storage)

    ########### 2) Run data selection functions ################
    combine_emitters()
    select_emitters(file_path_area, emission_cutoff, selected_subsectors)
    select_co2_storage(file_path_area, storage_cutoff)
    select_ports()

    ################ 3) Combine all selected data into one table ################
    combined_selected = combine_all_selected(output_path_intermediate_result)
    # Export combined_selected to excel for manual checking
    combined_selected.to_excel(Path(output_path_intermediate_result) / 'combined_selected.xlsx', index=False)

    ################ 4)  Create ship routes data using SCGraph ################
    create_ship_routes(output_path_intermediate_result)

    ################ 5) Create pipeline network based on selected emitters and ports using straight line (havensine) and Prim's algorithm (for clustering) ################
    create_pipeline_network(output_path_intermediate_result)

    ################ 6) Store electricity price data (hourly) from EuroStat into database.duckdb ################
    sheet_name_list = ['Sheet 43', 'Sheet 52', 'Sheet 61']
    column = '2025-S1'
    store_electricity_price_yearly(file_path_electricity_price_yearly, sheet_name_list, column)

    ################ 7) Store additional electricity price data from manual created excel file into database.duckdb ################
    additional_price_file = str(raw_dir / 'electricity_price_manual_addition.xlsx')
    default_country_iso2 = 'EG'  # Use Egypt's price to fill missing values
    store_additional_electricity_price(additional_price_file, default_country_iso2)


# Print the table name from database.duckdb
    con = duckdb.connect(str(db_path))
    print("Created tables in duckdb:")
    tables = con.execute("SHOW TABLES").fetchall()
    print(tables)
    con.close()


def main():
    etl_raw_to_db()

if __name__ == "__main__":
    main()

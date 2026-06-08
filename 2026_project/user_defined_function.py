########### This files contains user-defined functions, including: ##########

### Helper: ###
# 0. Sanitize name function to remove/repace invalid characters for Windows file/folder names

### The functions are called in 1_data_preprocessing.py: ###
# 1. ETL pipeline for loading all cleaned data into duckdb database (EEA, Climate TRACE, CO2 storage, port)
# 2. Data selection for 2026 project case study based on defined parameters (query from database.duckdb and filter in python)
# 3. Combines all selected emitters, storage, and ports data into a single table
# 4. (a) Create shipping route based on selected ports by SCGraph's marnet geograph API
# 4. (b) Sorting out routes based on unwanted destination ports
# 5. (a) Calculate straight line distance based on haversine formula (the greatest circle distance)
# 5. (b) Create pipeline network based on selected emitters and ports using straight line (haversine) and Prim's algorithm (for clustering)
# 5. (c) Create additional pipeline routes among emitters
# 11. (a) Store electricity price data (hourly) from EMBER into database.duckdb
# 11. (b) Store electricity price data (annual) from Eurostat into database.duckdb

### The functions are called in 2_data_processing.py for model input: ###
# 6. (a) Create N x N matrix from database table for distance
# 6. (b) Create matrix for gamma using CO2_Pipeline_CostModel (Default: gamma3 & gamma 4 == 0)
# 8. Copy technology JSON files from adopt-net0 database
# 9. Create technology JSON files for emitters based on Excel config (if needed, not in current scope)
# 10. Copy network data JSON files from adopt-net0 database
# 12. Aggregate electricity price data from hourly to yearly

### The functions are called in main.py
# 7. Create NodeLocations.csv from database
# 13. Copy all network data and topology files from data_processed folder to model input folder for model input



################################################################################

import csv
from importlib.resources import path
import pandas as pd
import numpy as np
import duckdb
from pyproj import Proj
import glob
import datetime as dt
import pycountry
import geopandas as gpd
from pathlib import Path
from shapely.geometry import LineString
from scgraph import GeoGraph

marnet_geograph = GeoGraph.load_geograph("marnet")
import json
import re
import shutil
import unicodedata

import networkx as nx
from adopt_net0.database.components.networks import CO2_Pipeline_CostModel
from co2_ship_cost_model_V1 import CO2_Ship_Dedicated_CostModel




##################################################################################

# Database path relative to this module's location
DB_PATH = str(Path(__file__).resolve().parent / 'database.duckdb')


def _resolve_financial_year_for_inflation(requested_year: int) -> int:
    """Return a year available in producer_price_index_euro.csv (fallback to latest <= requested)."""
    ppi_path = Path(__file__).resolve().parent.parent / "adopt_net0" / "database" / "data" / "producer_price_index_euro.csv"
    ppi = pd.read_csv(ppi_path)
    years = sorted(pd.to_datetime(ppi["TIME_PERIOD"]).dt.year.unique().tolist())
    if requested_year in years:
        return int(requested_year)

    valid = [y for y in years if y <= requested_year]
    if valid:
        fallback = int(valid[-1])
    else:
        fallback = int(years[0])

    print(
        f"Requested financial_year_out={requested_year} not in PPI dataset; "
        f"using {fallback} for currency/inflation conversion."
    )
    return fallback


############# 0. Sanitize name function  #########################################

def sanitize_name(name):
    """
    Sanitize name for Windows folder creation.
    """
    if pd.isna(name) or name is None:
        return 'Unnamed_Node'

    name_str = canonicalize_name(name)
    
    # Replace invalid chars
    sanitized = re.sub(r'[\\/:*?"<>|]', '_', name_str)
    
    # Clean trailing dots/spaces
    sanitized = sanitized.rstrip(' .')
    
    # Truncate long names
    return sanitized[:200] if sanitized else 'Unnamed_Node'


def canonicalize_name(name):
    """Canonicalize node names to avoid mojibake duplicates in matrices and topology."""
    if pd.isna(name) or name is None:
        return ""

    s = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
    s = unicodedata.normalize("NFC", s).replace("\u00a0", " ").strip()

    # Remove trailing punctuation artifacts frequently present in source datasets.
    s = s.rstrip(". ")

    # Common mojibake fixes seen in this project.
    explicit_fixes = {
        "BatÄ±Ã§im Bornova Cement Plant": "Batıçim Bornova Cement Plant",
        "Ä°DÃ‡ Izdemir Aliaga steel plant": "İDÇ Izdemir Aliaga steel plant",
        "CEMENTOS MOLINS INDUSTRIAL (SANT VICENÃ‡ DELS HORTS)": "CEMENTOS MOLINS INDUSTRIAL (SANT VICENÇ DELS HORTS)",
        "UnitÃ  Locale 3 - Impianto di Termovalorizzazione rifiuti non pericolosi": "Unità Locale 3 - Impianto di Termovalorizzazione rifiuti non pericolosi",
        "UnitÃ  Locale 3 - Impianto di Termovalorizzazione rifiuti non pericolosi": "Unità Locale 3 - Impianto di Termovalorizzazione rifiuti non pericolosi",
        "EVERÃ‰ SAS": "ÉVERÉ SAS",
    }
    if s in explicit_fixes:
        return explicit_fixes[s]

    # Heuristic repair for latin1-decoded UTF-8 text.
    if any(token in s for token in ("Ã", "Ä", "Å", "Â", "â")):
        try:
            repaired = s.encode("latin-1").decode("utf-8")
            repaired = unicodedata.normalize("NFC", repaired).replace("\u00a0", " ").strip().rstrip(". ")
            if repaired:
                return repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    return s


############ 1. ETL pipeline for loading all cleaned data into duckdb database (EEA, Climate TRACE, CO2 storage, port) ################

## ----- Create mapping function to standardise subsector name (only interested sectors) -----
def map_subsector(code):
    """ Map source's subsector code to subsector category: steel, cement, waste, or None.
    
    Parameters:
    code (str): subsector code to be mapped to subsector category.

    """
    if pd.isna(code):
        return None
    code_str = str(code)
    # Refineries: 1(a), 'oil-and-gas-refining'
    if code_str.startswith('1(a)') or code_str.startswith('oil-and-gas-refining'):
        return 'refineries'
    # Gasification: 1(b)
    elif code_str.startswith('1(b)'):
        return 'gasification'
    # Steel: 2(a), 2(b), 2(c)*, 2(d), 'iron-and-steel'
    elif code_str.startswith('2(a)') or code_str.startswith('2(b)') or code_str.startswith('2(c)') or code_str.startswith('2(d)') or code_str.startswith('iron-and-steel'):
        return 'steel'
    # Cement: 3(c)*, 'cement'
    elif code_str.startswith('3(c)') or code_str.startswith('cement'):
        return 'cement'
    # Petrochemical: 4(a), 'petrochemical'
    elif code_str.startswith('4(a)') or code_str.startswith('petrochemical-steam-cracking'):
        return 'petrochemical'
    # Waste: 5(b)
    elif code_str.startswith('5(b)'):
        return 'waste'
    else:
        return None

## ----- Create mapping function to standardise country name to iso2 -----
def map_iso3_to_iso2(code):
    """Map ISO-3 country code to ISO-2, returning None for invalid/missing codes.
    
    Parameters:
    code (str): ISO-3 country code to be mapped to ISO-2.
    
    """
    if pd.isna(code):
        return None
    country = pycountry.countries.get(alpha_3=str(code).strip().upper())
    return country.alpha_2 if country else None


def map_country_to_iso2(country_value):
    """Map full country name (or ISO code) to ISO-2, returning None for invalid/missing values.
    
    Parameters:
    country_value (str): country name or code to be mapped to ISO-2.

    """
    if pd.isna(country_value):
        return None

    value = str(country_value).strip()
    if not value:
        return None

    # Normalize input to handle case differences and hidden spaces.
    value_norm = " ".join(value.replace("\u00a0", " ").split()).casefold()

    # Handle common names/variants that may not resolve reliably via pycountry.
    aliases = {
        'turkey': 'TR',
        'turkiye': 'TR',
        'türkiye': 'TR',
        'turkiye (turkey)': 'TR',
        'republic of turkey': 'TR',
    }
    alias_hit = aliases.get(value_norm)
    if alias_hit:
        return alias_hit

    # Already ISO-2
    if len(value) == 2 and value.isalpha():
        return value.upper()

    # ISO-3 input
    iso3_match = pycountry.countries.get(alpha_3=value.upper())
    if iso3_match:
        return iso3_match.alpha_2

    # Full country name (or common alias recognized by pycountry)
    try:
        return pycountry.countries.lookup(value).alpha_2
    except LookupError:
        return None


## ----- ETL for EEA data -----
def etl_eea(file_path_eea):
    """ETL process for EEA data: Extract, Transform, Load.
    
    Parameters:
    file_path_eea (str or Path): path to the EEA Excel file.
    """
    
    # Import EEA excel file in raw folder
    eea = pd.read_excel(file_path_eea)

    ### Transformation ###
    # Rename columns
    eea.rename(columns={'nameOfFeature': 'name','pointGeometryLat': 'latitude', 'pointGeometryLon': 'longitude', 'reportingYear' : 'year', 'countryCode': 'iso2'}, inplace=True)
    # Convert latitude, longitude, and emission to numeric
    eea['latitude'] = pd.to_numeric(eea['latitude'], errors='coerce')
    eea['longitude'] = pd.to_numeric(eea['longitude'], errors='coerce')
    eea['totalPollutantQuantityKg'] = pd.to_numeric(eea['totalPollutantQuantityKg'], errors='coerce')
    # Add emission column in tCO2 per year
    eea['emission_TPA'] = eea['totalPollutantQuantityKg'] / 1000
    # Add subsector column based on mainActivityCode
    eea['subsector'] = eea['mainActivityCode'].apply(map_subsector)
    # Add data_source column
    eea['data_source'] = 'eea'

    ### Store the data in database.duckdb, if exists, replace it ###
    con = duckdb.connect(DB_PATH)
    con.register('eea', eea)
    con.execute("CREATE OR REPLACE TABLE eea AS SELECT * FROM eea")
    con.close()

## ----- ETL for Climate TRACE data -----
def etl_climate_trace(file_path_climate_trace):
    """ETL process for Climate TRACE data: Extract, Transform, Load.
    
    Parameters:
    file_path_climate_trace (str or Path): path to the folder containing Climate TRACE CSV files.
    """
    # Import all Climate TRACE csv file in the folder
    climate_trace = glob.glob(file_path_climate_trace + "/*.csv")
    climate_trace = pd.concat((pd.read_csv(file, low_memory=False, encoding='utf-8') for file in climate_trace), ignore_index=True)
    
    ### Transformation ###
    # Rename columns
    climate_trace.rename(columns={'source_name' : 'name', 'subsector': 'source_subsector', 'lat': 'latitude', 'lon': 'longitude', 'emissions_quantity' : 'emission_TPA'}, inplace=True)
    # Convert latitude, longitude, and emission to numeric 
    climate_trace['latitude'] = pd.to_numeric(climate_trace['latitude'], errors='coerce')
    climate_trace['longitude'] = pd.to_numeric(climate_trace['longitude'], errors='coerce')
    climate_trace['emission_TPA'] = pd.to_numeric(climate_trace['emission_TPA'], errors='coerce')
    # Add 'iso2' column by converting 'iso3_country' to iso2
    climate_trace['iso2'] = climate_trace['iso3_country'].apply(map_iso3_to_iso2)
    # Create 'year' column based on 'start_time'
    climate_trace['year'] = pd.to_datetime(climate_trace['start_time'], errors='coerce').dt.year
    # Sum emission by 'name', 'year', 'latitude', and 'longitude' to combine multiple entries for the same source in the same year, if any
    climate_trace = climate_trace.groupby(['name', 'year', 'latitude', 'longitude'], as_index=False).agg({'emission_TPA': 'sum', 'source_subsector': 'first', 'iso2': 'first'})
    # Add subsector column based on source_subsector
    climate_trace['subsector'] = climate_trace['source_subsector'].apply(map_subsector)
    # Add data_source column
    climate_trace['data_source'] = 'climate_trace'

    ### Store the data in database.duckdb, if exists, replace it ###
    con = duckdb.connect(DB_PATH)
    con.register('climate_trace', climate_trace)
    con.execute("CREATE OR REPLACE TABLE climate_trace AS SELECT * FROM climate_trace")
    con.close()


## ----- Combine all emitter data into one table for study -----
def combine_emitters():
    """Combine emitter data from EEA and Climate TRACE"""
    con = duckdb.connect(DB_PATH)
    combined = con.execute("SELECT name, iso2, latitude, longitude, year, emission_TPA, subsector, data_source FROM eea UNION ALL SELECT name, iso2, latitude, longitude, year, emission_TPA, subsector, data_source FROM climate_trace").fetchdf()
    con.register('emitters_all', combined)
    con.execute("CREATE OR REPLACE TABLE emitters_all AS SELECT * FROM emitters_all")
    con.close()


## ----- ETL for CO2 Storage data -----
def etl_co2_storage(file_path_co2_storage):
    """ETL process for CO2 Storage data: Extract, Transform, Load.
    
    Parameters:
    file_path_co2_storage (str or Path): path to the CO2 storage Excel file.
    """
    
    # Import CO2 Storage excel file in raw folder
    co2_storage = pd.read_excel(file_path_co2_storage)
    
    ### Transformation ###
    # Convert x, y, and TOTAL_CAPACITY_BASE_MT to numeric
    co2_storage['x'] = pd.to_numeric(co2_storage['x'], errors='coerce')
    co2_storage['y'] = pd.to_numeric(co2_storage['y'], errors='coerce')
    co2_storage['TOTAL_CAPACITY_BASE_MT'] = pd.to_numeric(co2_storage['TOTAL_CAPACITY_BASE_MT'], errors='coerce')
    # Covert 'TOTAL_CAPACITY_BASE_MT' in MtCO2 to 'capacity' in tCO2
    co2_storage['capacity_T'] = co2_storage['TOTAL_CAPACITY_BASE_MT'] * 1000000
    # Convert x, y to latitude, longitude where needed
    mask = co2_storage['latitude'].isna() & co2_storage['EPSG'].notna() & co2_storage['x'].notna() & co2_storage['y'].notna()
    for idx in co2_storage[mask].index:
        try:
            proj = Proj(f"epsg:{int(co2_storage.loc[idx, 'EPSG'])}")
            lon, lat = proj(co2_storage.loc[idx, 'x'], co2_storage.loc[idx, 'y'], inverse=True)
            co2_storage.loc[idx, 'latitude'] = lat
            co2_storage.loc[idx, 'longitude'] = lon
        except:
            pass
    
    ### Store the data in database.duckdb, if exists, replace it ###
    con = duckdb.connect(DB_PATH)
    con.register('co2_storage', co2_storage)
    con.execute("CREATE OR REPLACE TABLE co2_storage AS SELECT * FROM co2_storage")
    con.close()

## ----- ETL for port data -----
def etl_port(file_path_port):
    """ETL process for port data: Extract, Transform, Load.
    
    Parameters:
    file_path_port (str or Path): path to the port CSV file.
    """
    # Import port excel file in raw folder
    port = pd.read_csv(file_path_port, encoding='utf-8')
    
    ### Transformation ###
    # Rename columns
    port.rename(columns={'Main Port Name': 'name', 'Latitude': 'latitude', 'Longitude': 'longitude'}, inplace=True)
    # Convert latitude and longitude to numeric
    port['latitude'] = pd.to_numeric(port['latitude'], errors='coerce')
    port['longitude'] = pd.to_numeric(port['longitude'], errors='coerce')

    ### Store the data in database.duckdb, if exists, replace it ###
    con = duckdb.connect(DB_PATH)
    con.register('port', port)
    con.execute("CREATE OR REPLACE TABLE port AS SELECT * FROM port")
    con.close()


##################   2. Data selection for case study #####################

## ----- Select emitters ------
def select_emitters(file_path_area, emission_cutoff, selected_subsectors):
    """Select emitters based on area, emission cutoff, and subsectors.
    
    Parameters:
    file_path_area (str or Path): path to the area geojson file for spatial filtering.
    emission_cutoff (float): minimum emission in tCO2 per year to be included.
    selected_subsectors (list of str): list of subsectors to be included, e.g. ['steel', 'cement', 'waste'].
    """
    # Import area from geojson
    area = gpd.read_file(file_path_area)
    # Get data from emitters table
    con = duckdb.connect(DB_PATH)
    emitters = con.execute("SELECT name, latitude, longitude, year, emission_TPA, subsector, data_source, iso2 FROM emitters_all").fetchdf()
    con.close()

    ### Filtering ###
    # Keep only selected subsectors
    emitters = emitters[emitters['subsector'].isin(selected_subsectors)]
    # Drop climate_trace data where iso2 is EU country
    eu_countries = ['AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU', 
                    'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE']
    emitters = emitters[~((emitters['data_source'] == 'climate_trace') & (emitters['iso2'].isin(eu_countries)))]
    # Drop data older than (current_year - 4) to keep only active data
    current_year = dt.datetime.now().year
    emitters = emitters[emitters['year'] > current_year - 4]
    # Keep only latest year for each name
    emitters = emitters.sort_values('year', ascending=False).groupby('name', as_index=False).first()
    # Clip data within area (spatial filter)
    emitters_gdf = gpd.GeoDataFrame(
        emitters, 
        geometry=gpd.points_from_xy(emitters['longitude'], emitters['latitude']),
        crs='EPSG:4326')
    # Ensure area has same CRS
    if area.crs != emitters_gdf.crs:
        area = area.to_crs(emitters_gdf.crs)
    # Clip data within area (spatial filter)
    emitters_selected = gpd.sjoin(emitters_gdf, area, how='inner', predicate='within')
    # Keep only emission >= emission_cutoff tCO2
    emitters_selected = emitters_selected[emitters_selected['emission_TPA'] >= emission_cutoff]
    # After final selection, drop iso2 == Tunisia (TN) and Algeria (DZ) as all storage sites are full
    emitters_selected = emitters_selected[~emitters_selected['iso2'].isin(['TN', 'DZ'])]

    ### Store the selected emitters data in database.duckdb, if exists, replace it ###
    # Drop geometry column (not supported by DuckDB)
    emitters_selected_df = emitters_selected.drop(columns=['geometry', 'index_right'], errors='ignore').copy()
    # Add point type column for downstream joins/exports
    emitters_selected_df['type'] = 'emitter'



    con = duckdb.connect(DB_PATH)
    con.register('emitters_selected', emitters_selected_df)
    con.execute("CREATE OR REPLACE TABLE emitters_selected AS SELECT * FROM emitters_selected")
    con.close()
    

## ----- Select co2_storage ------
def select_co2_storage(file_path_area, storage_cutoff):
    """Select CO2 storage sites based on area and capacity cutoff. Need to define 'group' for clustering in raw file
    
    Parameters:
    file_path_area (str or Path): path to the area geojson file for spatial filtering.
    storage_cutoff (float): minimum storage capacity in tCO2 to be included.
    """
    # Import area from geojson
    area = gpd.read_file(file_path_area)
    # Get data from co2_storage table 
    con = duckdb.connect(DB_PATH)
    co2_storage = con.execute("SELECT * FROM co2_storage").fetchdf()
    con.close()

    ### Filtering ###
    # Drop missing coordinate and capacity data
    co2_storage = co2_storage.dropna(subset=['latitude', 'longitude', 'capacity_T'])
    # Clip data within area (spatial filter)
    co2_storage_gdf = gpd.GeoDataFrame(
        co2_storage, 
        geometry=gpd.points_from_xy(co2_storage['longitude'], co2_storage['latitude']),
        crs='EPSG:4326')
    # Ensure area has same CRS
    if area.crs != co2_storage_gdf.crs:
        area = area.to_crs(co2_storage_gdf.crs)
    # Clip data within area (spatial filter)
    co2_storage_selected = gpd.sjoin(co2_storage_gdf, area, how='inner', predicate='within')
    # Keep only storage with capacity >= storage_cutoff
    co2_storage_selected = co2_storage_selected[co2_storage_selected['capacity_T'] >= storage_cutoff]
    # Sum capacity by 'group' and keep other data from the row with largest capacity
    co2_storage_selected = co2_storage_selected.sort_values('capacity_T', ascending=False)
    agg_dict = {col: 'first' for col in co2_storage_selected.columns if col != 'capacity_T'}
    agg_dict['capacity_T'] = 'sum'
    co2_storage_selected = co2_storage_selected.groupby('group', as_index=False).agg(agg_dict)
    
    ### Store the selected co2 storage data in database.duckdb, if exists, replace it ###
    # Drop geometry column (not supported by DuckDB)
    co2_storage_selected_df = co2_storage_selected.drop(columns=['geometry', 'index_right'], errors='ignore').copy()
    co2_storage_selected_df['type'] = 'storage'
    # Keep only necessary columns
    co2_storage_selected_df = co2_storage_selected_df[['group', 'name', 'iso2', 'latitude', 'longitude', 'capacity_T', 'type', 'data_source']]

    con = duckdb.connect(DB_PATH)
    con.register('co2_storage_selected', co2_storage_selected_df)
    con.execute("CREATE OR REPLACE TABLE co2_storage_selected AS SELECT * FROM co2_storage_selected")
    con.close() 

## ----- Select ports ------
def select_ports():
    """Select ports based on 'selected' column.
    """
    con = duckdb.connect(DB_PATH)
    port = con.execute("SELECT * FROM port").fetchdf()
    con.close()

    ### Filtering ###
    # Keep only ports where Selected == 'yes'
    port_selected = port[port['Selected'] == 'yes'].copy()
    # Rename columns
    port_selected.rename(columns={'Screening': 'screening', 'Country Code': 'country'}, inplace=True)
    # Change country field (full name in source file) to iso2
    port_selected['iso2'] = port_selected['country'].apply(map_country_to_iso2)
    # Safety fallback for known exact text in source files
    turkey_mask = port_selected['country'].astype(str).str.strip().str.casefold().eq('turkey')
    port_selected.loc[turkey_mask, 'iso2'] = 'TR'
    # Add column 'Type' == port
    port_selected['type'] = 'port'
    # Keep only necessary columns
    port_selected = port_selected[['name', 'screening', 'iso2', 'latitude', 'longitude', 'type']]

    ### Store the selected port data in database.duckdb, if exists, replace it ###
    con = duckdb.connect(DB_PATH)
    con.register('port_selected', port_selected)
    con.execute("CREATE OR REPLACE TABLE port_selected AS SELECT * FROM port_selected")
    con.close()


################## 3. Combines all seleted emiiters, storage, and ports data #####################
def combine_all_selected(output_path):
    """Combine all selected emitters, storage, and ports data into one table
    
    Parameters:
    output_path (str or Path): directory to save the combined_selected.xlsx for manual checking and database storage.
    """
    con = duckdb.connect(DB_PATH)
    query = """
    SELECT
        CAST(NULL AS VARCHAR) AS "group",
        name,
        iso2,
        latitude,
        longitude,
        emission_TPA,
        CAST(NULL AS DOUBLE) AS capacity_T,
        subsector,
        data_source,
        type,
        CAST(NULL AS VARCHAR) AS screening,
        year
    FROM emitters_selected
    UNION ALL
    SELECT
        "group",
        name,
        iso2,
        latitude,
        longitude,
        CAST(NULL AS DOUBLE) AS emission_TPA,
        capacity_T,
        CAST(NULL AS VARCHAR) AS subsector,
        data_source,
        type,
        CAST(NULL AS VARCHAR) AS screening,
        CAST(NULL AS BIGINT) AS year
    FROM co2_storage_selected
    UNION ALL
    SELECT
        CAST(NULL AS VARCHAR) AS "group",
        name,
        iso2,
        latitude,
        longitude,
        CAST(NULL AS DOUBLE) AS emission_TPA,
        CAST(NULL AS DOUBLE) AS capacity_T,
        CAST(NULL AS VARCHAR) AS subsector,
        CAST(NULL AS VARCHAR) AS data_source,
        type,
        screening,
        CAST(NULL AS BIGINT) AS year
    FROM port_selected
    """
    # Execute the query and get combined_selected dataframe
    combined_selected = con.execute(query).fetchdf()

    # Add a column 'name_sanitized' by removing/replacing invalid characters for Windows file/folder names
    combined_selected['name_sanitized'] = combined_selected['name'].apply(sanitize_name)
    
    # Add a column 'emission_TPH' by converting 'emission_TPA' from tCO2 per annum to tCO2 per hour (assuming 365 days per year (2040) and 24 hours per day)
    combined_selected['emission_TPH'] = combined_selected['emission_TPA'] / (365 * 24)

    # Add a column 'xiao_emitters' by matching 'name' from 'xiao_emitters.xlsx' and filling 'Yes' if matched, otherwise 'No'.
    # Only when file is existing
    if (Path(__file__).resolve().parent / '1_raw' / 'xiao' / 'xiao_emitters.xlsx').exists():
        xiao_emitters = pd.read_excel(Path(__file__).resolve().parent / '1_raw' / 'xiao' / 'xiao_emitters.xlsx')
        # Only 'type' == 'emitter'
        combined_selected['xiao_emitters'] = combined_selected.apply(lambda row: 'Yes' if row['type'] == 'emitter' and row['name'] in xiao_emitters['name'].values else 'No', axis=1)

    # Store the combined data in database.duckdb, if exists, replace it
    con.register('combined_selected', combined_selected)
    con.execute("CREATE OR REPLACE TABLE combined_selected AS SELECT * FROM combined_selected")
    con.close()

    # Export combined_selected to excel for manual checking (non-fatal if file is locked)
    try:
        combined_selected.to_excel(Path(output_path) / 'combined_selected.xlsx', index=False)
    except PermissionError:
        print("WARNING: Could not write combined_selected.xlsx (file may be open in Excel). Database was updated successfully.")
    
    return combined_selected

############# 4. (a) Create ship route data for selected ports #####################
def create_ship_routes(output_path):
    """
    Create ship route data for selected ports and save to Excel and database.
    Using SCGraph's marnet geograph API to get the shortest path and distance between ports.
    The ship routes data is stored in database.duckdb for model input and also exported to Excel for manual checking.

    Parameters:
    output_path (str or Path): directory to save the ship_routes.xlsx for manual checking and database storage.
    """
    con = duckdb.connect(DB_PATH)
    
    # Get selected ports
    ports_selected = con.execute("SELECT * FROM combined_selected WHERE type = 'port'").fetchdf()
    
    # Create sink ports
    sink_ports = (
    ports_selected[ports_selected['screening'] == 'sink'].reset_index(drop=True))
    
    # Create emitter ports
    emitter_ports = (
    ports_selected[ports_selected['screening'] != 'sink'].reset_index(drop=True))

    ### Create ship routes between emitter ports and sink ports using marnet geograph
    routes = []
    port_records = emitter_ports.to_dict('records')
    sink_records = sink_ports.to_dict('records')

    # Note: Depending on the number of ports, this nested loop could result in a large number of API calls to marnet_geograph.
    # Emitter to Sink terminal
    for port in port_records:
        for sink in sink_records:
            # Get shortest path and distance from port to sink using marnet_geograph
            result = marnet_geograph.get_shortest_path(
                origin_node={"latitude": port["latitude"], "longitude": port["longitude"]},
                destination_node={"latitude": sink["latitude"], "longitude": sink["longitude"]},
                output_units='km')
            # Extract coordinates and distance from the result
            coords = result["coordinate_path"]
            distance = result["length"]
            # Convert to LineString (lon, lat)
            line = LineString([(lon, lat) for lat, lon in coords])
            # Append route information to the list        
            routes.append({
                "from_port": port["name_sanitized"],
                "from_iso2": port["iso2"],
                "to_port": sink["name_sanitized"],
                "to_iso2": sink["iso2"],
                "distance_km": distance,
                "geometry": line})
    
    # Sink terminal to Sink terminal
    sink_records_list = sink_ports.to_dict('records')
    
    for sink_from in sink_records_list:
        for sink_to in sink_records_list:  
            if sink_from["name_sanitized"] == sink_to["name_sanitized"]:
                continue
            result = marnet_geograph.get_shortest_path(
                origin_node={"latitude": sink_from["latitude"], "longitude": sink_from["longitude"]},
                destination_node={"latitude": sink_to["latitude"], "longitude": sink_to["longitude"]},
                output_units='km')
            coords = result["coordinate_path"]
            distance = result["length"]
            line = LineString([(lon, lat) for lat, lon in coords])
            routes.append({
                "from_port": sink_from["name_sanitized"],
                "from_iso2": sink_from["iso2"],
                "to_port": sink_to["name_sanitized"],
                "to_iso2": sink_to["iso2"],
                "distance_km": distance,
                "geometry": line})


    ## Store the ship routes data in database.duckdb, if exists, replace it. Geometry is stored as WKT string for compatibility with DuckDB and potential use in mapping applications. The original LineString geometry is retained in the code for any future use that may require geometric operations before storage.
    # Store ship routes in single table with geometry as WKT for dual use (metrics + mapping)
    routes_df = pd.DataFrame(routes)
    routes_df.insert(0, 'route_id', range(1, len(routes_df) + 1))

    # Convert geometry to WKT string for database storage
    routes_df['geometry_wkt'] = routes_df['geometry'].apply(
        lambda geom: geom.wkt if geom is not None else None)
    
    # Prepare final table
    ship_routes = routes_df[['route_id', 'from_port', 'from_iso2', 'to_port', 'to_iso2', 'distance_km', 'geometry_wkt']]
    
    # Export to excel for manual checking
    ship_routes.to_excel(Path(output_path) / 'ship_routes.xlsx', index=False)
    
    # Load into database
    con.register('ship_routes', ship_routes)
    con.execute("CREATE OR REPLACE TABLE ship_routes AS SELECT * FROM ship_routes")
    con.close()


############# 5. Create pipeline network for selected emitters and ports using straight line distance #####################

## Function to calculate straight line distance based on haversine formula, which accounts for the curvature of the Earth. The distance is returned in kilometers.
def distance(lat1, lon1, lat2, lon2):
    """Calculate the great circle distance in kilometers between two points on the Earth specified in decimal degrees."""
    lat1, lon1 = np.radians(lat1), np.radians(lon1)
    lat2, lon2 = np.radians(lat2), np.radians(lon2)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    return 6371.0 * (2.0 * np.arcsin(np.sqrt(a)))  # kilometers


## Function to create pipeline network
def create_pipeline_network(output_path):
    """
    Build a CO2 pipeline network from combined_selected data.

    Parameters:
    - output_path (str or Path): directory to save the result

    Edge types produced:
      emitter_to_emitter   – MST edges connecting emitters within the same cluster
      emitter_to_port      – MST edges connecting an emitter directly to its cluster's loading port;
                             every loading port (screening != 'sink') has at least one such edge
      emitter_to_alternative – shortest cross-cluster emitter-emitter bridge, one per cluster pair
      emitter_to_terminal  – emitter → sink port (screening == 'sink'); added for an emitter only
                             when the terminal distance is shorter than its shortest other edge;
                             every terminal gets at least one such edge
      terminal_to_storage  – sink port → storage site (nearest terminal per storage)

    Clustering: each emitter is assigned to its nearest loading port; Prim's MST is then run on the
    combined set of {emitters in cluster} + {their loading port} so the MST naturally decides
    whether each emitter connects directly to the port or chains through another emitter.

    """
    con = duckdb.connect(DB_PATH)
    combined_selected = con.execute(
        'SELECT name_sanitized AS name, type, latitude, longitude, screening FROM "combined_selected"'
    ).fetchdf()

    emitter  = combined_selected[combined_selected["type"] == "emitter"].reset_index(drop=True)
    port_all = combined_selected[combined_selected["type"] == "port"].reset_index(drop=True)
    storage  = combined_selected[combined_selected["type"] == "storage"].reset_index(drop=True)

    # Loading ports (non-sink) vs. terminal ports (sink)
    port = port_all[port_all["screening"] != "sink"].reset_index(drop=True)
    sink = port_all[port_all["screening"] == "sink"].reset_index(drop=True)

    emit_lat = emitter["latitude"].to_numpy()
    emit_lon = emitter["longitude"].to_numpy()
    port_lat = port["latitude"].to_numpy() if len(port) > 0 else np.array([])
    port_lon = port["longitude"].to_numpy() if len(port) > 0 else np.array([])
    sink_lat = sink["latitude"].to_numpy() if len(sink) > 0 else np.array([])
    sink_lon = sink["longitude"].to_numpy() if len(sink) > 0 else np.array([])

    cols = ["edge_type", "from_name", "from_latitude", "from_longitude",
            "to_name", "to_latitude", "to_longitude", "distance_km"]

    #  Distance matrices 
    dist_ep = (distance(emit_lat[:, None], emit_lon[:, None], port_lat[None, :], port_lon[None, :])
               if len(port) > 0 else np.empty((len(emitter), 0)))
    dist_ee = distance(emit_lat[:, None], emit_lon[:, None], emit_lat[None, :], emit_lon[None, :])
    dist_es = (distance(emit_lat[:, None], emit_lon[:, None], sink_lat[None, :], sink_lon[None, :])
               if len(sink) > 0 else None)

    # Cluster assignment: each emitter → index of nearest loading port
    b = dist_ep.argmin(axis=1) if len(port) > 0 else np.zeros(len(emitter), dtype=int)

    ## -----  Prim's MST per cluster (emitters + their loading port) ----
    # Nodes 0..n_e-1 map to emit_indices; node n_e is the loading port.
    # Starting from the port ensures the MST is rooted there, so the edges
    # produced naturally represent CO2 flowing toward the port.
    mst_rows = []

    def _prim_cluster(emit_indices, p_idx):
        # emit_indices: indices of emitters in the cluster (referring to rows in emitter DataFrame)
        n_e = len(emit_indices)
        n   = n_e + 1
        # Prim's algorithm with adjacency matrix given by dist_ee for emitter-emitter edges and dist_ep for emitter-port edges
        in_tree  = np.zeros(n, dtype=bool)
        min_cost = np.full(n, np.inf)
        parent   = np.full(n, -1, dtype=int)
        min_cost[n_e] = 0.0  # root: loading port

        for _ in range(n):
            # pick cheapest not-yet-in-tree node
            u = -1; best = np.inf
            # Note: the port node (n_e) is included in this loop and can be picked when its min_cost is lowest. 
            # Subsequent emitters will then connect either to the port or to other emitters based on the MST logic.
            for v in range(n):
                if not in_tree[v] and min_cost[v] < best:
                    best, u = min_cost[v], v
            if u == -1:
                break
            in_tree[u] = True
            # add edge (parent[u], u) to MST result, if u is not the root
            if parent[u] != -1:
                pu = parent[u]
                if u < n_e and pu < n_e:
                    # emitter–emitter edge
                    i, j = emit_indices[u], emit_indices[pu]
                    mst_rows.append({
                        "edge_type":      "emitter_to_emitter",
                        "from_name":      emitter.iloc[i]["name"],
                        "from_latitude":  emit_lat[i], "from_longitude": emit_lon[i],
                        "to_name":        emitter.iloc[j]["name"],
                        "to_latitude":    emit_lat[j], "to_longitude":   emit_lon[j],
                        "distance_km":    dist_ee[i, j],
                    })
                else:
                    # emitter–port edge (one of u/pu is n_e, the other is an emitter)
                    ei = emit_indices[u] if u < n_e else emit_indices[pu]
                    mst_rows.append({
                        "edge_type":      "emitter_to_port",
                        "from_name":      emitter.iloc[ei]["name"],
                        "from_latitude":  emit_lat[ei], "from_longitude": emit_lon[ei],
                        "to_name":        port.iloc[p_idx]["name"],
                        "to_latitude":    port_lat[p_idx], "to_longitude":   port_lon[p_idx],
                        "distance_km":    dist_ep[ei, p_idx],
                    })

            # relax edges from u
            for v in range(n):
                # Skip if already in tree or if u and v are both the port node (no self-loop)
                if in_tree[v]:
                    continue
                # Determine cost of edge (u, v) based on whether u and v are emitters or the port
                if u < n_e and v < n_e:
                    cost = dist_ee[emit_indices[u], emit_indices[v]]
                # One of u/v is the port node (n_e) and the other is an emitter: cost from dist_ep
                elif u < n_e and v == n_e:
                    cost = dist_ep[emit_indices[u], p_idx]
                # The case of u == n_e and v == n_e is not valid (no self-loop on port)
                elif u == n_e and v < n_e:
                    cost = dist_ep[emit_indices[v], p_idx]
                else:
                    continue
                # Relax edge (u, v) if cost is lower
                if cost < min_cost[v]:
                    min_cost[v] = cost
                    parent[v] = u
    # Run Prim's MST for each cluster
    if len(port) > 0:
        for cid in range(len(port)):
            # Find emitters in this cluster (those whose nearest port is cid)
            members = np.where(b == cid)[0].tolist()
            # If no emitters are assigned to this port, connect the port directly to its closest emitter (even though it's not in the same cluster by the nearest-port rule).
            if len(members) == 0:
                # Port has no emitters cluster-assigned: connect its closest emitter directly
                e_idx = int(dist_ep[:, cid].argmin())
                mst_rows.append({
                    "edge_type":      "emitter_to_port",
                    "from_name":      emitter.iloc[e_idx]["name"],
                    "from_latitude":  emit_lat[e_idx], "from_longitude": emit_lon[e_idx],
                    "to_name":        port.iloc[cid]["name"],
                    "to_latitude":    port_lat[cid], "to_longitude":   port_lon[cid],
                    "distance_km":    dist_ep[e_idx, cid],
                })
            else:
                _prim_cluster(members, cid)

    mst_df             = pd.DataFrame(mst_rows, columns=cols) if mst_rows else pd.DataFrame(columns=cols)
    emitter_to_port    = mst_df[mst_df["edge_type"] == "emitter_to_port"].reset_index(drop=True)
    emitter_to_emitter = mst_df[mst_df["edge_type"] == "emitter_to_emitter"].reset_index(drop=True)

    # emitter_to_alternative: one bridge per cluster pair (shortest)
    alt_candidates = []
    seen_pairs = set()
    for i in range(len(emitter)):
        # Find emitters in different clusters (b) and calculate distance to them using dist_ee; pick the closest one as alternative edge candidate for this emitter.
        other = np.where(b != b[i])[0]
        if len(other) == 0:
            continue
        # Pick the closest emitter in a different cluster as alternative edge candidate
        j = int(other[dist_ee[i, other].argmin()])
        key = (min(i, j), max(i, j))
        # Add this pair as an alternative edge candidate if we haven't already added an alternative edge for this cluster pair.
        # The seen_pairs set ensures we only add one alternative edge per cluster pair, even if multiple emitters in the same cluster have the same closest emitter in the other cluster.
        if key not in seen_pairs:
            seen_pairs.add(key)
            alt_candidates.append({
                "edge_type":      "emitter_to_alternative",
                "from_name":      emitter.iloc[i]["name"],
                "from_latitude":  emit_lat[i], "from_longitude": emit_lon[i],
                "to_name":        emitter.iloc[j]["name"],
                "to_latitude":    emit_lat[j], "to_longitude":   emit_lon[j],
                "distance_km":    dist_ee[i, j],
                "_from_cluster":  int(b[i]),
                "_to_cluster":    int(b[j]),
            })
    # Among the candidate alternative edges, keep only the shortest one per cluster pair to avoid redundancy.
    if alt_candidates:
        alt_df = pd.DataFrame(alt_candidates)
        alt_df["_cluster_pair"] = alt_df.apply(
            lambda r: (min(r["_from_cluster"], r["_to_cluster"]),
                       max(r["_from_cluster"], r["_to_cluster"])), axis=1
        )
        alt_df = alt_df.loc[alt_df.groupby("_cluster_pair")["distance_km"].idxmin()]
        emitter_to_alt = alt_df[cols].reset_index(drop=True)
    else:
        emitter_to_alt = pd.DataFrame(columns=cols)

    # emitter_to_terminal: emitter → nearest sink port
    # Rule: add only when d(emitter→terminal) < emitter's shortest other edge.
    # Guarantee: every terminal (sink port) has at least one emitter_to_terminal.
    emitter_to_terminal = pd.DataFrame(columns=cols)
    if len(sink) > 0 and dist_es is not None:
        # Per-emitter minimum distance across all edges built so far
        all_other = pd.concat([emitter_to_port, emitter_to_emitter, emitter_to_alt], ignore_index=True)
        from_min = all_other.groupby("from_name")["distance_km"].min()
        to_min   = all_other.groupby("to_name")["distance_km"].min()
        # For each emitter, find the nearest terminal and compare the distance to that terminal with the emitter's shortest other edge.
        # If the terminal is closer, add an edge from the emitter to that terminal.
        term_rows = []
        for e_idx in range(len(emitter)):
            e_name = emitter.iloc[e_idx]["name"]
            s_idx  = int(dist_es[e_idx].argmin())
            d_term = dist_es[e_idx, s_idx]
            d_other = min(from_min.get(e_name, np.inf), to_min.get(e_name, np.inf))
            if d_term < d_other:
                term_rows.append({
                    "edge_type":      "emitter_to_terminal",
                    "from_name":      e_name,
                    "from_latitude":  emit_lat[e_idx], "from_longitude": emit_lon[e_idx],
                    "to_name":        sink.iloc[s_idx]["name"],
                    "to_latitude":    sink_lat[s_idx], "to_longitude":   sink_lon[s_idx],
                    "distance_km":    d_term,
                })
        emitter_to_terminal = pd.DataFrame(term_rows, columns=cols) if term_rows else pd.DataFrame(columns=cols)

        # Guarantee every terminal has at least one emitter_to_terminal
        covered = set(emitter_to_terminal["to_name"]) if not emitter_to_terminal.empty else set()
        for s_idx in range(len(sink)):
            if sink.iloc[s_idx]["name"] not in covered:
                e_idx = int(dist_es[:, s_idx].argmin())
                emitter_to_terminal = pd.concat([emitter_to_terminal, pd.DataFrame([{
                    "edge_type":      "emitter_to_terminal",
                    "from_name":      emitter.iloc[e_idx]["name"],
                    "from_latitude":  emit_lat[e_idx], "from_longitude": emit_lon[e_idx],
                    "to_name":        sink.iloc[s_idx]["name"],
                    "to_latitude":    sink_lat[s_idx], "to_longitude":   sink_lon[s_idx],
                    "distance_km":    dist_es[e_idx, s_idx],
                }])], ignore_index=True)

    # terminal_to_storage: nearest terminal → each storage site
    terminal_to_storage = pd.DataFrame(columns=cols)
    if len(sink) > 0 and len(storage) > 0:
        stor_lat  = storage["latitude"].to_numpy()
        stor_lon  = storage["longitude"].to_numpy()
        dist_stor = distance(stor_lat[:, None], stor_lon[:, None], sink_lat[None, :], sink_lon[None, :])
        ts = []
        for st_idx in range(len(storage)):
            sk_idx = int(dist_stor[st_idx].argmin())
            ts.append({
                "edge_type":      "terminal_to_storage",
                "from_name":      sink.iloc[sk_idx]["name"],
                "from_latitude":  sink_lat[sk_idx], "from_longitude": sink_lon[sk_idx],
                "to_name":        storage.iloc[st_idx]["name"],
                "to_latitude":    stor_lat[st_idx], "to_longitude":   stor_lon[st_idx],
                "distance_km":    dist_stor[st_idx, sk_idx],
            })
        terminal_to_storage = pd.DataFrame(ts, columns=cols)

    # Combine & save
    pipeline_network = pd.concat(
        [emitter_to_port, emitter_to_emitter, emitter_to_alt,
         emitter_to_terminal, terminal_to_storage],
        ignore_index=True
    )

    # Make sure all distance_km are numeric
    pipeline_network['distance_km'] = pd.to_numeric(pipeline_network['distance_km'], errors='coerce')

    # Fill 0 for 'from_name' == 'to_name'
    same_name_mask = pipeline_network["from_name"] == pipeline_network["to_name"]
    pipeline_network.loc[same_name_mask, "distance_km"] = 0
    # Export to excel for manual checking
    pipeline_network.to_excel(Path(output_path) / 'pipeline_network.xlsx', index=False)

    con.register('pipeline_network', pipeline_network)
    con.execute("CREATE OR REPLACE TABLE pipeline_network AS SELECT * FROM pipeline_network")
    con.close()




############# 6. Create N x N matrix from database table #####################
def create_matrix(table_name, col_start, col_end, value, output_path):
    """ 
    Create a square matrix (return as DataFrame) from a database table.

    Parameters:
    table_name (str): Name of the database table to query.
    col_start (str): Column name for the origin node (row index in the matrix).
    col_end (str): Column name for the destination node (column index in the matrix).
    value (str): Column name for the distance value. If a cell is blank/NaN, the
                 great-circle distance is calculated automatically using the
                 'distance()' function (requires latitude/longitude columns in the table).
    output_path (str or Path): Path to save the resulting CSV file.

    Auto-detected columns (optional):
    - 'selection': if present, only rows where selection == 'Yes' are used.
    - 'direction': if present, controls how the matrix is filled:
        * 'oneway'  -> fill [col_start -> col_end] only
        * 'reverse' -> fill [col_end -> col_start] only
        * 'twoway'  -> fill both [col_start -> col_end] and [col_end -> col_start]
      If the column is absent, all edges are treated as oneway (default).
    - 'distance': if coordinate columns present
      and the distance value is blank/NaN, the great-circle distance is computed
      automatically via the distance() function.
    """
    
    
    ### Prepare edge data 
    # Load data from database
    query = f'''
    SELECT *,
       "{col_start}" AS node_start,
       "{col_end}" AS node_end,
       "{value}" AS cell_value
    FROM "{table_name}"
    '''
    with duckdb.connect(DB_PATH) as con:
        data = con.execute(query).fetchdf()
    
    # if 'selection' column exists, filter to keep only rows where selection == 'Yes'
    if 'selection' in data.columns:
        data = data[data['selection'].astype(str).str.strip() == 'Yes'].copy()

    # Canonicalize node names before creating matrix to prevent duplicate mojibake variants.
    data['node_start'] = data['node_start'].apply(canonicalize_name)
    data['node_end'] = data['node_end'].apply(canonicalize_name)

    data = data.dropna(subset=['node_start', 'node_end'])
    data = data[(data['node_start'] != '') & (data['node_end'] != '')].copy()
    data['cell_value'] = pd.to_numeric(data['cell_value'], errors='coerce')
    
    ### Calculate missing distances using coordinates if available
    missing = data['cell_value'].isna()
    coord_cols = {'latitude_from', 'longitude_from', 'latitude_to', 'longitude_to'} 
    if missing.any() and coord_cols.issubset(set(data.columns)):
        data.loc[missing, 'cell_value'] = data.loc[missing].apply(
            lambda r: distance(r['latitude_from'], r['longitude_from'],
                               r['latitude_to'], r['longitude_to']), axis=1)
    
    ### Build matrix
    nodes = sorted(set(data['node_start']).union(data['node_end']))
    matrix = pd.DataFrame(np.nan, index=nodes, columns=nodes, dtype=float)
    np.fill_diagonal(matrix.values, 0.0) # Fill diagonal with 0.0 for same-node distances
    
    # Fill matrix with direction handling
    has_dir = 'direction' in data.columns
    dir_map = {'oneway': [(0,1)], 'reverse': [(1,0)], 'twoway': [(0,1), (1,0)]}
    
    # Iterate through rows and fill the matrix according to the specified direction. For duplicate edges, keep the smallest positive distance.
    for row in data.itertuples(index=False):
        src, dst = row.node_start, row.node_end
        val = pd.to_numeric(row.cell_value, errors='coerce')
        # Skip missing values so they do not overwrite a valid edge with NaN.
        if pd.isna(val):
            continue
        val = float(val)
        dir_key = str(row.direction).strip().lower() if has_dir else 'oneway'
        
        for i, j in dir_map.get(dir_key, [(0,1)]):
            r_name, c_name = [src, dst][i], [src, dst][j]
            current = matrix.at[r_name, c_name]
            # For duplicate edges, keep the smallest positive distance.
            if pd.isna(current):
                matrix.at[r_name, c_name] = val
            elif val > 0 and val < current:
                matrix.at[r_name, c_name] = val

    ### Save and return
    matrix = matrix.fillna(0.0)
    matrix = matrix.astype(float)
    matrix.index = matrix.columns = matrix.index.astype(str)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    matrix.to_csv(output_path, index_label="NODE", encoding='utf-8', sep=';')
    
    return matrix




########################## 6. (b) Create matrix for gamma ##############################

# Version 1:  massflow bounds of pipeline arcs are based on the min/max emission of the connected group, while massflow bounds of ship arcs are based on the ship capacity.
def create_gamma_matrix_v1(
    cost_model_type:    str,      # "pipeline" or "ship"
    table_name:         str,      # database table to query for emission data
    distance_matrix:    Path,
    discount_rate:      float,
    financial_year_out: int,
    output_path:        Path,
    connection_matrix:  Path = None,   # required for pipeline; optional for ship since ship routes are all pairwise
    ):                                              
    """
    Runs CO2_Pipeline_CostModel or CO2_Ship_Dedicated_CostModel (depending on the cost_model_type) for every connected arc at its real distance.
    Massflow range = min/max emission of the connencted group of arcs
    where "connected group" means nodes reachable via the connection matrix.

    Parameters:
    cost_model_type (str): Type of cost model to use ("pipeline" or "ship").
    table_name (str): Name of the database table to query for emission data.
    distance_matrix (Path): Path to the CSV file containing the distance matrix between nodes.
    discount_rate (float): Discount rate to use in the cost model calculations.
    financial_year_out (int): The financial year to which the cost model outputs should be discounted
    output_path (str or Path): Path to save the resulting gamma matrices as CSV files.
    connection_matrix (Path, optional): Path to the CSV file containing the connection matrix for pipelines. Required if cost_model_type is "pipeline"; ignored if cost_model_type is "ship".

    Output
    gamma1, gamma2, gamma3, gamma4 matrices as CSV files
    """
   
 
    # 1. Load emitters from database
    with duckdb.connect(DB_PATH) as con:
        emitters = con.execute(f"""
            SELECT name_sanitized, emission_TPH
            FROM {table_name}
            WHERE type = 'emitter'
        """).fetchdf()

    emitters["emission_TPH"] = pd.to_numeric(emitters["emission_TPH"], errors="coerce")
    emitters = emitters.dropna(subset=["emission_TPH"])
    emission_dict = dict(zip(emitters["name_sanitized"], emitters["emission_TPH"]))

    # 2. Load distance matrix
    df_dist = pd.read_csv(Path(distance_matrix), index_col=0, sep=";")
    df_dist.index   = df_dist.index.astype(str)
    df_dist.columns = df_dist.columns.astype(str)
    nodes = df_dist.index.tolist()

    # 3. Prepare arc filter and massflow bounds 
    # 3.1 For pipelines
    if cost_model_type == "pipeline":
        # Load connection matrix for identifying connected components.
        df_conn = pd.read_csv(Path(connection_matrix), index_col=0, sep=";")
        df_conn.index   = df_conn.index.astype(str)
        df_conn.columns = df_conn.columns.astype(str)

        # Massflow bounds per connected component for pipeline network
        G = nx.from_pandas_adjacency(df_conn)  # undirected graph where edges represent connectivity (not distance)
        component_limits = {}
        
        # Find connected components and calculate min/max emissions for each component to set massflow bounds.
        for component in nx.connected_components(G):
            
            # Get emissions (tonnes/hour) for nodes in this component that are emitters
            emissions_tph = [emission_dict[node] for node in component if node in emission_dict]
            # If there are emitters in this component, calculate min and max emissions to set massflow bounds
            if emissions_tph:
                min_tph = min(emissions_tph)
                sum_tph = sum(emissions_tph)
                # Convert to kg/s (1 t/h = 1/3.6 kg/s)
                min_kg_per_s = min_tph / 3.6
                max_kg_per_s = sum_tph / 3.6
            else:
                # No emitters in this component
                min_kg_per_s = 0.0
                max_kg_per_s = 0.0
        
            # Store limits using frozenset as key (immutable for dict)
            component_limits[frozenset(component)] = (min_kg_per_s, max_kg_per_s)

    # 3.2 For ships, all arcs are pairwise.
    # Min = 0
    # Max = ship capacity 
    elif cost_model_type == "ship":
        global_min_kg_s = 0.0
        global_max_kg_s = 7004.21 * 1000 / 3600  # Northern Lights ship capacity (Northern Light Annual Report, 2025) in kg/s (7004.21 t / 3.6 = kg/s)

    else:
        raise ValueError(f"Invalid cost_model_type: {cost_model_type}. Must be 'pipeline' or 'ship'.")
    
    # Ensure conversion year exists in PPI dataset to avoid NaN currency conversion.
    financial_year_out_resolved = _resolve_financial_year_for_inflation(financial_year_out)

    # 4. Instantiate cost model 
    
    if cost_model_type == "pipeline":
        model = CO2_Pipeline_CostModel("CO2_Pipeline")

    else:
        model = CO2_Ship_Dedicated_CostModel("CO2Ship")

    # 5. Initialise output gamma matrices 
    gamma_matrices = {
        g: pd.DataFrame(0.0, index=nodes, columns=nodes)
        for g in ["gamma1", "gamma2", "gamma3", "gamma4"]
    }

    # 6. Compute gamma1 and gamma2 for each active arc 
    for node_from in nodes:
        for node_to in nodes:

            # Skip if not connected (pipeline) or no distance
            if cost_model_type == "pipeline" and df_conn.loc[node_from, node_to] != 1:
                continue
            
            # Get distance; skip if missing or non-positive
            dist = pd.to_numeric(df_dist.at[node_from, node_to], errors="coerce")
            if pd.isna(dist) or dist <= 0.0:
                continue

            # Massflow bounds for this arc
            if cost_model_type == "pipeline":
                for comp, (mn, mx) in component_limits.items():
                    if node_from in comp and node_to in comp:
                        min_flow = mn
                        max_flow = mx
                        break
                else:
                    continue  # arc not in any component
            else:
                min_flow, max_flow = global_min_kg_s, global_max_kg_s

            # Build options and run model
            if cost_model_type == "pipeline":
                options = {
                    "length_km":             dist,
                    "massflow_min_kg_per_s": min_flow,
                    "massflow_max_kg_per_s": max_flow,
                    "discount_rate":         discount_rate,
                    "financial_year_out":    financial_year_out_resolved,
                    "currency_out":          "EUR",
                    "terrain":               "Onshore",
                }
            else:
                options = {
                    "distance_km":           dist,
                    "massflow_min_kg_per_s": min_flow,
                    "massflow_max_kg_per_s": max_flow,
                    "discount_rate":         discount_rate,
                    "financial_year_out":    financial_year_out_resolved,
                    "currency_out":          "EUR",
                }

            model.calculate_indicators(options)

            gamma_matrices["gamma1"].at[node_from, node_to] = model.financial_indicators["gamma1"]
            gamma_matrices["gamma2"].at[node_from, node_to] = model.financial_indicators["gamma2"]
            
            # Pipeline distance-dependent OPEX: 13,000 EUR/km/year (from Ravenna case in Report: Analisi degli aspetti tecnici, economici e normativi funzionali allo sviluppo della filiera CCUS" [Analysis of technical, economic and regulatory aspects functional to the development of the CCUS supply chain], 2025).
            # AdOpT-NET0 multiplies all gamma values by annualization_factor before passing them to the optimizer. So gamma3 must be supplied as an upfront-equivalent

            if cost_model_type == "pipeline":
                _r = discount_rate
                _L = model.json_data["Economics"]["lifetime"]  # read directly from CO2_Pipeline.json
                _af = _r * (1 + _r) ** _L / ((1 + _r) ** _L - 1)
                _opex_per_km_per_yr = 13_000  # EUR/km/year
                gamma_matrices["gamma3"].at[node_from, node_to] = (_opex_per_km_per_yr / _af) * dist
            
            # gamma4 remains 0.0

    # 7. Export CSVs
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    for name, df in gamma_matrices.items():
        df.to_csv(output_path / f"{name}.csv", sep=";", float_format="%.4f", index_label="NODE")

    return gamma_matrices


# Version 2: set massflow bounds for all arcs (both pipeline and ship) based on the min/max emission of the connected group.
def create_gamma_matrix(
    cost_model_type:    str,       # "pipeline" or "ship"
    table_name:         str,       # database table for emission data
    distance_matrix:    Path,      # For pipeline or ship
    discount_rate:      float,
    financial_year_out: int,
    output_path:        Path,
):
    """
    Compute per-arc gamma matrices for CO2_Pipeline or CO2_Ship.

    Pipeline arcs : connection_matrix == 1
    Ship arcs     : distance_matrix > 0  (port-to-port only)

    Massflow bounds (both modes):
      Derived from connected components in connection_matrix (CO2_Pipeline/connection.csv).
      For pipeline → bounds apply to each emitter-containing component.
      For ship     → bounds apply to the port's component
                     (emitters feeding into that port on land).
                     max_flow = sum(emitter emissions in component) → drives n_shipments.
    """

    # Ensure conversion year exists in PPI dataset to avoid NaN currency conversion.
    financial_year_out_resolved = _resolve_financial_year_for_inflation(financial_year_out)

    # 1. Load emitters
    with duckdb.connect(DB_PATH) as con:
        emitters = con.execute(f"""
            SELECT name_sanitized, emission_TPH
            FROM {table_name}
            WHERE type = 'emitter'
        """).fetchdf()
    emitters["emission_TPH"] = pd.to_numeric(emitters["emission_TPH"], errors="coerce")
    emitters = emitters[emitters["emission_TPH"] > 0].dropna(subset=["emission_TPH"])
    emission_dict = dict(zip(emitters["name_sanitized"], emitters["emission_TPH"]))

    # 2. Load distance matrix (pipeline or ship)
    df_dist = pd.read_csv(Path(distance_matrix), index_col=0, sep=";")
    df_dist.index   = df_dist.index.astype(str)
    df_dist.columns = df_dist.columns.astype(str)
    nodes = df_dist.index.tolist()

    # 3. Load connection matrix (always CO2_Pipeline/connection.csv)
    df_conn = pd.read_csv(Path(__file__).parent / "2_data_processed" / "network_topology_prep" / "CO2_Pipeline" / "connection.csv", index_col=0, sep=";")
    df_conn.index   = df_conn.index.astype(str)
    df_conn.columns = df_conn.columns.astype(str)

    # 4. Connected components → massflow bounds per component
    # This applies to both pipeline and ship modes, as the ship mode also have emission from onshore emitters which are connected to the ports via pipeline arcs.
    G = nx.from_pandas_adjacency(df_conn)   # Function from_pandas_adjacency treats nonzero entries as edges; since connection matrix is binary, this gives us the connectivity graph.  
    
    # For each connected component, find the emitters in that component and calculate the min/max emissions to set massflow bounds.
    # Minimum massflow the pipeline cost model can handle: velocity must be >= vRange_min (0.5 m/s) through the smallest NPS pipe (id ≈ 0.029 m, liquid CO2 density ≈ 850 kg/m³).
    # 0.5 kg/s gives v ≈ 0.8 m/s through the smallest pipe → safely above 0.5 m/s.
    min_kg_per_s_limit = 0.5

    component_limits = {}  
    for component in nx.connected_components(G):
        emissions_tph = [emission_dict[n] for n in component if n in emission_dict]
        if emissions_tph:
            max_kg_per_s = sum(emissions_tph) / 3.6
            min_kg_per_s = min(emissions_tph) / 3.6

        else:
            min_kg_per_s = max_kg_per_s = 0.0
        component_limits[frozenset(component)] = (min_kg_per_s, max_kg_per_s)

    # 5. Instantiate cost model
    if cost_model_type == "pipeline":
        model = CO2_Pipeline_CostModel("CO2_Pipeline")
    elif cost_model_type == "ship":
        model = CO2_Ship_Dedicated_CostModel("CO2Ship")
    else:
        raise ValueError(f"cost_model_type must be 'pipeline' or 'ship', got '{cost_model_type}'")

    # 6. Initialise output gamma matrices
    gamma_matrices = {
        g: pd.DataFrame(0.0, index=nodes, columns=nodes)
        for g in ["gamma1", "gamma2", "gamma3", "gamma4"]
    }

    n_calculated = 0
    n_skipped_nonfinite = 0

    # 7. Compute gamma per arc
    for node_from in nodes:
        for node_to in nodes:

            # Arc filter
            if cost_model_type == "pipeline":
                # Must be explicitly connected in CO2_Pipeline/connection.csv
                if node_from not in df_conn.index or node_to not in df_conn.columns:
                    continue
                if df_conn.loc[node_from, node_to] != 1:
                    continue
            # ship: arc exists if distance > 0 in CO2Ship/distance.csv (checked below)

            dist = pd.to_numeric(df_dist.at[node_from, node_to], errors="coerce")
            if pd.isna(dist) or dist <= 0.0:
                continue

            # Massflow bounds from pipeline-connected component of node_from
            # Pipeline: emitters in the component feeding this arc
            # Ship:     emitters on land feeding into the source port
            for comp, (mn, mx) in component_limits.items():
                if node_from in comp:
                    min_flow, max_flow = mn, mx
                    break
            else:
                continue  # skip 'node_from' not found in any component

            if max_flow < min_kg_per_s_limit:
                continue  # flow too small for pipeline cost model (velocity would be below vRange_min=0.5 m/s)

            # Guard against degenerate/invalid ranges before running OLS-based models.
            if min_flow >= max_flow:
                min_flow = max(min_kg_per_s_limit, 0.5 * max_flow)
            if min_flow >= max_flow:
                continue

            # Build options
            if cost_model_type == "pipeline":
                options = {
                    "length_km":             dist,
                    "massflow_min_kg_per_s": min_flow,
                    "massflow_max_kg_per_s": max_flow,
                    "discount_rate":         discount_rate,
                    "financial_year_out":    financial_year_out_resolved,
                    "currency_out":          "EUR",
                    "terrain":               "Onshore",
                }
            else:
                options = {
                    "distance_km":           dist,
                    "massflow_min_kg_per_s": min_flow,   # min single emitter in component
                    "massflow_max_kg_per_s": max_flow,   # sum of all emitters in component
                    "discount_rate":         discount_rate,
                    "financial_year_out":    financial_year_out_resolved,
                    "currency_out":          "EUR",
                    # c_ship_EUR_per_ship, c_land_EUR → use defaults from model
                }

            # Run model and store gamma values
            model.calculate_indicators(options)
            g1 = pd.to_numeric(model.financial_indicators.get("gamma1"), errors="coerce")
            g2 = pd.to_numeric(model.financial_indicators.get("gamma2"), errors="coerce")
            if pd.isna(g1) or pd.isna(g2):
                n_skipped_nonfinite += 1
                continue

            gamma_matrices["gamma1"].at[node_from, node_to] = float(g1)
            gamma_matrices["gamma2"].at[node_from, node_to] = float(g2)
            n_calculated += 1
            # gamma3 & gamma4 remain 0.0

    # 8. Export CSVs
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    for name, df in gamma_matrices.items():
        df.to_csv(output_path / f"{name}.csv", sep=";", float_format="%.4f", index_label="NODE")

    print(
        f"Gamma matrix ({cost_model_type}) computed arcs: {n_calculated}, "
        f"skipped non-finite arcs: {n_skipped_nonfinite}"
    )

    return gamma_matrices


#################### 7. Create NodeLocations.csv ########################
def create_node_location(altitude, path_model_input):
    """Create NodeLocations.csv for a given node type (emitter, port, storage) with specified altitude. 
    The node names are sanitized to be safe for file/folder naming and are used as the index in the output CSV.
    The CSV is formatted with ';' as the separator and includes columns for longitude (lon), latitude (lat), and altitude (alt).
    
    Parameters:
    type (str): The type of nodes to include in the output (e.g., 'emitter', 'port', 'storage').
    altitude (float): The altitude value to assign to all nodes in the output.
    path_model_input (str or Path): The directory where the NodeLocations.csv file will be saved.
    
    """

    con = duckdb.connect(DB_PATH)
    
    try:
        # Try to query combined_selected_final first
        nodes = con.execute("SELECT name_sanitized AS name, longitude, latitude FROM combined_selected_final WHERE selection ='Yes'").fetchdf()
    except duckdb.CatalogException:
        # If table doesn't exist, fall back to combined_selected
        nodes = con.execute("SELECT name_sanitized AS name, longitude, latitude FROM combined_selected").fetchdf()
    
    con.close()

    node_locations = nodes[['name', 'longitude', 'latitude']].copy()
    node_locations.rename(columns={'longitude': 'lon', 'latitude': 'lat'}, inplace=True)
    node_locations['alt'] = altitude
    
    # Handle duplicates by keeping the row with valid (non-NULL) coordinates.
    # If all duplicates have the same coordinates, just keep the first.
    node_locations = node_locations.drop_duplicates(subset=['name'], keep='first')
    
    node_locations = node_locations.set_index('name')

    output_path = path_model_input / 'NodeLocations.csv'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Match expected model format: ;lon;lat;alt with node names in first column.
    # QUOTE_NONNUMERIC wraps string index values (node names) in quotes so names containing commas are not mis-split by comma-aware viewers (e.g. Excel).
    node_locations.to_csv(output_path, sep=';', index=True, index_label='',
                          encoding='utf-8', quoting=csv.QUOTE_NONNUMERIC)







############# 8. Copy technology JSON files from adopt-net0 database  #####################
def copy_technology_from_db(technology_list, output_path):
    """
    Copy technology JSON files from the adopt_net0 database to a destination folder.
    
    Parameters:
    technology_list (list) : list of names of technologies to copy. The name must match the JSON filename (without .json extension).
    output_path (str or Path): Folder where the JSON files will be copied to.
    """

    # The adopt_net0 template library is expected to be located at:
    template_root = Path(__file__).resolve().parent.parent / 'adopt_net0' / 'database' / 'templates' / 'technology_data'
    output_path = Path(output_path)
    # Create the output directory if it doesn't exist
    output_path.mkdir(parents=True, exist_ok=True)

    # Build a lookup of available template files by their filename.
    available = {p.stem: p for p in template_root.rglob('*.json')}

    # For each technology name in the input list, check if a corresponding JSON file exists in the template library and copy it to the output folder.
    for name in technology_list:
        if name in available:
            src = available[name]
            shutil.copy2(src, output_path / src.name)
            print(f"Copied: {src.name}  -->  {output_path}")







############# 9. Create emitter technology JSON files for industrial sectors #####################
def create_emitter_technology(input_path, output_path):
    """Create emitter technology JSON files from Excel config.
    
    Parameters:
    input_path (str or Path): Path to the Excel file containing technology configurations. Expected columns
    output_path (str or Path): Folder where the generated JSON files will be saved.
    
    """
    
    destination_folder = Path(output_path)
    df = pd.read_excel(input_path).dropna(how="all")

    # Iterate through each row of the DataFrame and create a JSON file for each technology configuration.
    # The JSON structure is based on the expected format for technology data in the adopt_net0.
    for row in df.to_dict("records"):

        data = {
            "tec_type": row["tec_type"],
            "comment": "This file is auto-generated from user input in excel file.",
            "size_min": 0,
            "size_max": row["size_max"],
            "size_is_int": 0,   
            "size_based_on": "output",
            "decommission": 0,
            "Economics": {
                "CAPEX_model": 1,
                "unit_CAPEX": 0,
                "OPEX_variable": 0,
                "OPEX_fixed": 0,
                "discount_rate": row["discount_rate"],
                "lifetime": int(row["lifetime"]),
                "decommission_cost": 0,
            },
            "Performance": {
                "performance_function_type": 1,
                "main_output_carrier": row["main_output_carrier"],
                "output_carrier": [row["output_carrier"]],
                "output_ratios": {},
                "emission_factor": row["emission_factor"],
                "min_part_load": 0,
                "ccs": {
                    "possible": 1,
                    "co2_concentration": row["co2_concentration"],
                    "ccs_type": "MEA_large",  # Default, will be changed after creating node folder
                },
                "ramping_rate": -1,
                "standby_power": -1,
                "min_uptime": -1,
                "min_downtime": -1,
                "SU_time": -1,
                "SD_time": -1,
                "SU_load": -1,
                "SD_load": -1,
                "max_startups": -1,
            },
            "Units": {
                "size": "t/h",
                "output_carrier": {"CO2captured": "t/h"},
            },
        }

        filename = Path(row["filename"]).with_suffix(".json")
        output_file = destination_folder / filename

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        print(f"Created: {output_file}")





############# 10. Copy network data JSON files from adopt_net0 database #####################
def copy_network_data_from_db(network_data_list, output_path):
    """
    Copy network data JSON files from the adopt_net0 database to a destination folder.
    
    Parameters:
    network_data_list (list): list of names of network data files to copy. The name must match the JSON filename (without .json extension).
    output_path (str or Path): Folder where the JSON files will be copied to.
    """

    # The adopt_net0 template library is expected to be located at:
    template_root = Path(__file__).resolve().parent.parent / 'adopt_net0' / 'database' / 'templates' / 'network_data'
    output_path = Path(output_path)
    # Create the output directory if it doesn't exist
    output_path.mkdir(parents=True, exist_ok=True)

    # Build a lookup of available template files by their filename.
    available = {p.stem: p for p in template_root.rglob('*.json')}

    # For each network data name in the input list, check if a corresponding JSON file exists in the template library and copy it to the output folder.
    for name in network_data_list:
        if name in available:
            src = available[name]
            shutil.copy2(src, output_path / src.name)
            print(f"Copied: {src.name}  -->  {output_path}")



################## 11. (a) Store (Whole sale) electricity price raw data from EMBER into database.duckdb #####################
def store_electricity_price_hourly(input_path):
    """
    Load electricity price data from CSV, add iso2 column, and store in database.duckDB as 'electricity_price_houly'.
    
    Parameters:
    input_path (str or Path): Path to the input CSV file containing electricity price data.

    """
    df = pd.read_csv(input_path, encoding='utf-8')

    # Add iso2 column using map_country_to_iso2 from 'Country' column
    df['iso2'] = df['Country'].apply(map_country_to_iso2)

    # Add 'Year' column extracted from 'Datetime (UTC)' (format: 1/1/2015  12:00:00 AM)
    df['Year'] = pd.to_datetime(df['Datetime (UTC)'], errors='coerce').dt.year
   
    
    # Store in database.duckDB
    con = duckdb.connect(DB_PATH)
    con.register('electricity_price_houly', df)
    con.execute("CREATE OR REPLACE TABLE electricity_price_houly AS SELECT * FROM electricity_price_houly")
    con.close()


#################### 11. (b) Store (End-user) annual electricity price raw data from Eurostat into database.duckdb #####################

def store_electricity_price_yearly(input_path, sheet_name_list, column):
    """
    Load electricity price data from Excel file, compute average 2025-S1 price per country, convert country names to iso2 codes, and store in database.duckDB as 'electricity_price_yearly'.
    Convert EUR per kWh to EUR per MWhe by multiplying by 1000.

    Parameters:
    input_path (str or Path): Path to the input Excel file containing electricity price data in multiple sheets.
    sheet_name_list (list): List of sheet names to process.
    column (str): The column name to compute the average for.
    """

    dfs = []
    for sheet in sheet_name_list:
        raw = pd.read_excel(input_path, sheet_name=sheet, header=None)

        mask = raw.iloc[:, 0].astype(str).str.contains("GEO", na=False)
        if not mask.any():
            continue
        geo_row = mask.idxmax()
        time_row = geo_row - 1  # row with period labels like '2025-S1'

        # Build column names: col 0 from geo_row ("GEO (Labels)"), rest from time_row
        col_names = raw.iloc[time_row].astype(str).str.strip().tolist()
        col_names[0] = "country"   # <-- override col 0 with "country" directly

        # Data starts after geo_row
        df = raw.iloc[geo_row + 1:].copy()
        df.columns = col_names

        if column not in df.columns:
            print(f"[WARN] Missing column '{column}' in {sheet}, found: {df.columns.tolist()}")
            continue

        temp = df[["country", column]].copy()
        temp[column] = pd.to_numeric(temp[column], errors="coerce")
        temp = temp.dropna(subset=["country", column])
        temp = temp[temp["country"].astype(str).str.strip() != ""]
        dfs.append(temp)

    df_final = (
        pd.concat(dfs, ignore_index=True)
        .groupby("country", as_index=False)[column]
        .mean()
        .assign(iso2=lambda d: d["country"].apply(map_country_to_iso2))
        .dropna(subset=["iso2"])
        .rename(columns={column: "avg_price_EUR/MWhe"})
        [["iso2", "avg_price_EUR/MWhe"]]
    )

    df_final["avg_price_EUR/MWhe"] = df_final["avg_price_EUR/MWhe"] * 1000  # Convert from EUR/kWh to EUR/MWhe

    con = duckdb.connect(DB_PATH)
    con.register("df_final", df_final)
    con.execute("CREATE OR REPLACE TABLE electricity_price_yearly AS SELECT * FROM df_final")
    con.close()

################### 11. (c) Store additional electricity price data from manual excel files into database.duckdb #####################
def store_additional_electricity_price(input_path, default_country_iso2=None):
    """
    Compute avg_price_EUR/MWhe from price + exchange rate
    Optionally fill missing prices using another country's avg_price_EUR/MWhe

    Parameters:
    excel_path (str or Path) Path to the Excel file.
    default_country_iso2 (str or None) : Fill iso2, to use its value to fill missing 'price' values.
    
    """

    df = pd.read_excel(Path(input_path))

    # Clean numeric columns
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["exchange_rate_to_EUR"] = pd.to_numeric(df["exchange_rate_to_EUR"], errors="coerce")

    # Compute avg_price_EUR/MWhe where possible, by converting local currency per kWh to EUR per MWhe
    df["avg_price_EUR/MWhe"] = df["price"] * df["exchange_rate_to_EUR"] * 1000  # Convert from local_currency/kWh to EUR/MWhe (×1000 kWh/MWh)

    # Fill missing prices if requested, using the price of the specified default country in iso2
    if default_country_iso2 is not None:
        default_price = df.loc[df["iso2"] == default_country_iso2, "avg_price_EUR/MWhe"].iloc[0]
        df["avg_price_EUR/MWhe"] = df["avg_price_EUR/MWhe"].fillna(default_price)
    
    # Keep only relevant columns for DB insert
    out_df = df[["iso2", "avg_price_EUR/MWhe"]].copy()

    # Open DuckDB and append
    con = duckdb.connect(DB_PATH)

    # Append data
    con.register("out_df_view", out_df)
    con.execute(f"INSERT INTO electricity_price_yearly SELECT * FROM out_df_view")
    con.unregister("out_df_view")
    con.close()

    return df



#################### 12. Aggregate yearly average per country  #######################################
def aggregate_electricity_price_yearly(year, output_path):
    """
    Aggregate average electricity price per year per country for a given year,
    filter by iso2 in combined_selected, store as 'electricity_price_yearly' in database.duckDB,
    export as CSV, and return the dataframe.

    Parameters:
    year (int): The year for which to aggregate electricity price data.
    output_path (str or Path): The directory where the resulting CSV file will be saved (for manual checking).
    """

    # Query to select electricity price data for the specified year and filter by iso2 in combined_selected
    con = duckdb.connect(DB_PATH)
    df = con.execute(f"SELECT * FROM electricity_price_houly WHERE Year = {year}").fetchdf()
    
    # Getting the list of iso2 codes from combined_selected to filter the electricity price data
    combined_selected = con.execute("SELECT DISTINCT iso2 FROM combined_selected WHERE iso2 IS NOT NULL").fetchdf()
    iso2_list = set(combined_selected['iso2'].dropna().unique())

    # Filter by iso2 in combined_selected
    df = df[df['iso2'].isin(iso2_list)]

    # Group by iso2 and compute average price per year per country
    electricity_price_yearly = df.groupby(['iso2', 'Year'], as_index=False).agg({'Price (EUR/MWhe)': 'mean'}).rename(columns={'Price (EUR/MWhe)': 'Average Price (EUR/MWhe)'})

    # Store in database.duckDB
    con.register('electricity_price_yearly', electricity_price_yearly)
    con.execute("CREATE OR REPLACE TABLE electricity_price_yearly AS SELECT * FROM electricity_price_yearly")
    con.close()

    # Export as CSV
    output_path = Path(output_path) / 'electricity_price_yearly.csv'
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    electricity_price_yearly.to_csv(output_path, index=False)

    return electricity_price_yearly



######################## 13. Copy all files from one folder to another #########################
def copy_all_files(input_path, output_path):
    """
    Copy all files from the source folder to the destination folder.

    Parameters:
    input_path (str or Path): The source directory containing files to be copied.
    output_path (str or Path): The destination directory where files will be copied to.
    """
    source_folder = Path(input_path)
    destination_folder = Path(output_path)
    destination_folder.mkdir(parents=True, exist_ok=True)

    for item in source_folder.iterdir():
        if item.is_file():
            shutil.copy2(item, destination_folder / item.name)
            print(f"Copied: {item.name}  -->  {destination_folder}")



########################## 14. Assign techologies to nodes (config Technologies.json, copy technology JSON files, and update emitter JSONs) ##########################

def assign_technologies_to_nodes(input_path, output_path):
    """
    For each node in the topology, assign technologies based on its type (emitter, storage, or other).
    - Emitters: assign emitter technology and copy matching MEA CCS file for lookup
    - Storage: PermanentStorage_CO2_simple technology.
    - Port: No technologies are assigned.
    The function also copies the necessary technology JSON files from input_path to subfolders in out_put_path

    Parameters:
    input_path (str or Path): The directory containing the Topology.json file and technology preparation files.
    output_path (str or Path): Model input directory
    """
    
    ### Load emitters and storage from combined_selected 
    con = duckdb.connect(DB_PATH)
    # Nested dictionary of emitters
    try:
        emitters = con.execute(""" SELECT name_sanitized AS name, subsector, emission_TPH FROM combined_selected_final WHERE type = 'emitter' AND selection ='Yes' """).df().set_index('name').to_dict('index')
    except duckdb.CatalogException:
        emitters = con.execute(""" SELECT name_sanitized AS name, subsector, emission_TPH FROM combined_selected WHERE type = 'emitter'""").df().set_index('name').to_dict('index')
    # Set of storage site names
    try:
        storage = set(con.execute(""" SELECT name_sanitized AS name FROM combined_selected_final  WHERE type = 'storage' AND selection ='Yes' """).df()['name'].tolist())
    except duckdb.CatalogException:
        storage = set(con.execute(""" SELECT name_sanitized AS name FROM combined_selected  WHERE type = 'storage' """).df()['name'].tolist())

    con.close()

    ### Load MEA size_min as cutoff for MEA technology assignment
    # Path for MEA JSON files
    mea_files = {
        "MEA_large":  input_path / "MEA_large.json",
        "MEA_medium": input_path / "MEA_medium.json",
        "MEA_small":  input_path / "MEA_small.json"
        }
    
    # Build a dictionary of MEA technology names to their size_min values
    mea_cutoff = {}
    for name, path in mea_files.items():
        with open(path) as f:
            mea_json = json.load(f)
            mea_cutoff[name] = mea_json["size_min"]

    # Sort MEA technologies by size_min to facilitate assignment based on flue gas TPH thresholds (smallest to largest)
    mea_order = sorted(mea_cutoff.items(), key=lambda x: x[1])

    # Get topology to know the list of nodes to process
    with open( output_path / "Topology.json") as f:
        topology = json.load(f)

    ### This is not necessary but to tracking the number of copied files and assigned nodes
    copied_tech_files = set()
    total_files_copied = 0
    emitter_count = 0
    storage_count = 0

    # Iterate through each node in the topology and assign technologies based on its type (emitter and storage).
    for node_name in topology["nodes"]:
        
        # node folder
        node_folder = output_path / "period1" / "node_data" / node_name
        # technology_data subfolder
        tech_folder = node_folder / "technology_data"

        technologies = []
        technologies_to_copy = []
        
        # For storage
        if node_name in storage:
            technologies = ["PermanentStorage_CO2_simple"]
            technologies_to_copy = list(technologies)
            storage_count += 1 # Tracking

        # For emitters
        elif node_name in emitters:
            # Get emission and subsectordata from the nested dictionary for each node 
            emitter_data = emitters[node_name]
            emission_tph = emitter_data["emission_TPH"]
            subsector = emitter_data["subsector"]

            # Loop emitter JSON to get co2_concentration
            with open(input_path / f"emitter_{subsector}.json") as f:
                emitter_json = json.load(f)
            co2_conc = emitter_json["Performance"]["ccs"]["co2_concentration"]

            # Calculate flue gas
            if co2_conc > 0:
                flue_gas_tph = emission_tph / co2_conc
            else:
                flue_gas_tph = 0

            # Assign MEA technology based on flue gasS\\
            chosen_mea = "MEA_small"  # Default
            # Tuple unpacking to get technology name and its size_min
            for mea_name, size_min in mea_order:
                if flue_gas_tph >= size_min:
                    chosen_mea = mea_name

            # Define technologies (emitter & MEA) for each emitter
            emitter_tech = f"emitter_{subsector}"
            technologies = [emitter_tech]
            technologies_to_copy = [emitter_tech, chosen_mea]
            
            emitter_count += 1 # Tracking

        # Copy all technologies and track unique files
        for tech in technologies_to_copy:
            source = input_path / f"{tech}.json"
            destination = tech_folder / f"{tech}.json"
            shutil.copy2(source, destination)

            # Tracking
            copied_tech_files.add(tech)
            total_files_copied += 1

            # Update emitter JSON with chosen MEA
            if tech.startswith("emitter"):
                with open(destination) as f:
                    data = json.load(f)
                economics = data.get("Economics", {})
                if "capex_model" in economics:
                    economics["CAPEX_model"] = economics.pop("capex_model")
                if "unit_capex" in economics:
                    economics["unit_CAPEX"] = economics.pop("unit_capex")
                if "opex_variable" in economics:
                    economics["OPEX_variable"] = economics.pop("opex_variable")
                if "opex_fixed" in economics:
                    economics["OPEX_fixed"] = economics.pop("opex_fixed")
                data["Economics"] = economics
                data["Performance"]["ccs"]["ccs_type"] = chosen_mea
                with open(destination, "w") as f:
                    json.dump(data, f, indent=4)
        
        # Create manifest (all technologies as "new")
        manifest = {
            "existing": {},
            "new": technologies
        }
        
        with open(node_folder / "Technologies.json", "w") as f:
            json.dump(manifest, f, indent=4)

    print(f"Copied {total_files_copied} technology files")
    print(f"Total assigned {emitter_count + storage_count} nodes | Emitters: {emitter_count} | Storage: {storage_count}")
    print(f"List: {sorted(copied_tech_files)}")


def k_flow_calculation(file, cost_model):
    """
    Calculate the k-flow values using technical_indicators from the CO2_Pipeline_CostModel and update the input JSON file with the calculated k_flow values.
    
    Parameters:
    file (str or Path): Path to the input JSON file that contains the data to be updated with k_flow values.
    cost_model (str): The name of the cost model to use for calculating technical indicators,
    
    """
    from adopt_net0.database.components.networks import CO2_Pipeline_CostModel
    model = CO2_Pipeline_CostModel(cost_model)

    with open(file) as f:
        data = json.load(f)
        data["Performance"]["energyconsumption"]["electricity"]["k_flow"] = model.technical_indicators["energyconsumption"]
    with open(file, "w") as f:
        json.dump(data, f, indent=4)



    
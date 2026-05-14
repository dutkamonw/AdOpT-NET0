####################################################################################


# This script customizes the cost model for dedicated CO2 shipping, based on Oeuvray et al. (2024).
# 1) A class `CO2_Ship_Dedicated_CostModel` that inherits from `DataComponent_CostModel` and implements the cost calculation logic for dedicated CO2 shipping.
# 2) A function `_calculate_capex` based on the main logic of Dedicated Ship from Oeuvray et al. (2024), which calculates CAPEX using fleet sizing (number of ships).
# 3) A function `calculate_indicators` fits the gamma parameters using a grid of mass flow rates and distances, and updates the financial and technical indicators. 


######################################################################################

import numpy as np
import pandas as pd
from adopt_net0.components.technologies.utilities import fit_linear_function
from adopt_net0.database.data_component import DataComponent_CostModel
from adopt_net0.database.utilities import convert_currency

#######################################################################################


###  Create a cost model class for dedicated CO2 shipping
class CO2_Ship_Dedicated_CostModel(DataComponent_CostModel):
    """
    Dedicated CO2 Ship Cost Model based on Oeuvray et al. (2024)
    To calculate CAPEX per arc (using distance matrices).
    Fix d, sweep S → 1D OLS → gamma1_ij, gamma2_ij  (gamma3=gamma4=0).

    """

    # Initialize with default parameters and options
    def __init__(self, tec_name="CO2Ship"):
        super().__init__(tec_name)

        # Default parameters (to be set by caller or overridden in options)
        self.financial_year_in = 2025
        self.currency_in = "EUR"

        # Set default option
        self.default_options.update({

            ### ---- OLS Grid for fitting gamma parameters ----
            "massflow_min_kg_per_s": 5.0,           # Dummy values for grid fitting, cover emission range of interest
            "massflow_max_kg_per_s": 200.0,         # Dummy values for grid fitting, cover emission range of interest
            "massflow_evaluation_points": 12,       # Dummy values for grid fitting


            ##### ---- Ship parameters ----
            ##  From 2025 Norther Light Annual Report    
            "ship_capacity_t": 7004.21,             # Northern Pioneer Capacity (7500 m3) * LCO2 density (-35 degC, 19 bar --> 1098.7 kg/m3 from NIST) * Utilization factor (0.85) (Footnote Table A.1)                      
            ###  To be set by caller in EUR 2025
            "I_c_EUR": None,                        # Cost per carrier (EUR/carrier) 
            "c_st_EUR_per_t": None,                 # Cost of intermediate storage (EUR/t)
            "c_l_EUR_per_t": None,                  # Cost of loading (EUR/t)
            

            ##### ---- OPEX parameters -----
            "opex_fixed_fraction": 0.02,            # Maintenance cost 2% of CAPEX (from ZEP Report, page 24)
            "opex_var_EUR_per_t": 0,                # Neglected, because it depends on several factors (e.g., fuel type, fuel price, ship efficiency, crew) and is expected to be small. 


            ##### ---- Operational parameters -----
            ## From Oeuvray et al. (2024)
            "operating_hours_per_a": 8400,           # Hour per year (Ref. Table A.1 Dedicated Ship
            "ship_speed_km_per_h": 27.8,             # 15 knots (1 knot = 1.852 km/h) from https://ecolog.earth/ccus/ (Retrieved 07 May 2026))
            "port_time_h": 24.0,                     # Loading + Unloading from Table A.4 Dedicated Ship (same as ZEP)           
           

            ##### ----- Financial parameters -----
            "lifetime": 25,                         # Default operational lifetime of the ship (years) from Northern Light operational lifetime (2025 Northern Light Annual Report)
            "discount_rate": 0.1,                   # Default from AdOpT-NET0
            "financial_year_out": 2025,             # Default output costs in this financial year (for currency conversion), can be overridden by caller
            "currency_out": "EUR",                  # Output costs in this currency (for currency conversion
  
        })

    def _set_options(self, options: dict):
        """Store all options into self.options — mirrors CO2_Pipeline_CostModel."""
        # Parent handles: currency_out, financial_year_out, discount_rate
        super()._set_options(options)
            # Store distance_km (required for per-arc mode)
        if "distance_km" in options:
            self.options["distance_km"] = options["distance_km"]
        # Store all keys from default_options (use provided value or fall back to default)
        for key in self.default_options:
            self._set_option_value(key, options)


    ##### Capital Recovery Factor (CRF) to convert upfront CAPEX to annual equivalent, used in the loading station cost component (Equation 10 in Oeuvray et al. (2024))
    def _crf(self) -> float:
        """Capital Recovery Factor (converts upfront CAPEX to annual equivalent)."""
        r = self.options["discount_rate"]
        n = self.options["lifetime"]
        return r * (1 + r)**n / ((1 + r)**n - 1)

    ##### Calculate CAPEX using the main logic of Dedicated Ship from Oeuvray et al. (2024)
    def _calculate_capex(self, emission_tpa: float, distance_km: float) -> float:
        """
        Calculate CAPEX based on Oeuvray et al. (2024) using fleet sizing (number of ships).
        Returns total CAPEX in EUR 2025.

        CAPEX components:
        1. Intermediate storage = Capacity (t) * Cost_of_intermediate_storage (EUR/t)  (Equation 8)
        2. Loading station =  Mass_flow_CO2_transport (t/year) * Cost_of_loading (EUR/t) / CRF (Equation 10)
        3. Carrier = Number_of_carriers * Cost per carrier (EUR/carrier)  (Equation 11)
        
        Parameters:
        - c_st_EUR_per_t: Cost of intermediate storage (EUR/t)
        - c_l_EUR_per_t: Cost of loading (EUR/t)
        - I_c_EUR: Cost per carrier (EUR/carrier)

        """

        # Unpack options for easier access
        o = self.options
        crf = self._crf()

        #########  CAPEX from Equation 8, 10, 11 in Oeuvray et al. (2024) #########
        ### 1. Intermediate storage = Capacity (t) * Cost_of_intermediate_storage (EUR/t)  (Equation 8)
        capex_storage = o["c_st_EUR_per_t"] * o["ship_capacity_t"]


        ### 2. Loading station =  Mass_flow_CO2_transport (t/year) * Cost_of_loading (EUR/t) / CRF (Equation 10)
        capex_loading = o["c_l_EUR_per_t"] * emission_tpa / crf
        

        ### 3. Carrier = Number_of_carriers * Cost per carrier (EUR/carrier)  (Equation 11)
        n_shipments = emission_tpa / o["ship_capacity_t"]       # Number of shipments per year (Equation 2 in Oeuvray et al. (2024))
        round_trip_h = (2 * distance_km / o["ship_speed_km_per_h"]) + o["port_time_h"]  # Round trip duration (hours)
        n_carriers = n_shipments * round_trip_h / o["operating_hours_per_a"]    # Number of carriers (Equation 3 in Oeuvray et al. (2024))
        capex_carrier = o["I_c_EUR"] * n_carriers

        # Total CAPEX = 1 + 2 + 3
        capex = capex_storage + capex_loading + capex_carrier

        return capex

    ###### Fit gamma parameters using a grid of mass flow rates and distances, and update financial and technical indicators
    def calculate_indicators(self, options: dict = None, financial_year_out: int = 2025) -> dict:
        """
        Per-arc 1S OLA: fix distance_km, sweep S → 1D OLS.

        Fit: CAPEX = gamma1_ij + gamma2_ij * S    (gamma3 = gamma4 = 0)

        Reasoning:
        - distance_km is fixed per arc → as we have distance matrices, we can calculate a specific gamma1 and gamma2 for each arc.
        - If 2D OLS, columns "d" and "Sd" would be collinear with "intercept" and "S" and cause a rank-deficient matrix
        - So, using 1D OLS, we avoid this issue as CO2_Pipeline_CostModel (1D OLS, d fixed per arc)

        Requires options["distance_km"]

        Parameters for grid:
        - S_tph_range: from massflow_min_kg_per_s to massflow_max_kg_per_s, converted to t/h, with massflow_evaluation_points points in between.

        """

        # Unpack options and set defaults
        if options:
            self._set_options(options)
        o = self.options

        if "distance_km" not in o:
            raise KeyError("'distance_km' required.")

        # Create grid of S values (mass flow rates) for fitting, convert from kg/s to t/h
        S_tph_range = np.linspace(
            o["massflow_min_kg_per_s"] * 3.6,
            o["massflow_max_kg_per_s"] * 3.6,
            o["massflow_evaluation_points"]
        )

        #### ---- 1D OLS Grid ------
        # For each S in the grid, calculate CAPEX using the _calculate_capex function, keeping distance_km fixed.
        rows = [
            # 1.0 is added as an intercept term for the OLS regression later, to allow fitting a non-zero intercept (γ1) in addition to the slope (γ2)
            {"intercept": 1.0, "S": S,
             "capex": self._calculate_capex(S * o["operating_hours_per_a"], o["distance_km"])}
            for S in S_tph_range     
        ]

        # Fit linear function to the grid data to extract γ1 (intercept) and γ2 (slope)
        df     = pd.DataFrame(rows)
        coeffs = fit_linear_function(df[["intercept", "S"]].values, df["capex"].values)
        g1, g2 = coeffs


        conv = lambda v: convert_currency(
            v, 
            self.financial_year_in,
            o.get("financial_year_out", financial_year_out),
            self.currency_in,
            o.get("currency_out", "EUR")
        )

        self.financial_indicators.update({
            "gamma1":        conv(g1),
            "gamma2":        conv(g2),
            "gamma3":        0.0,   # γ3=0: d fixed per arc, baked into γ1/γ2
            "gamma4":        0.0,   # γ4=0: d fixed per arc, baked into γ1/γ2
            "opex_fixed":    o["opex_fixed_fraction"],
            "opex_variable": conv(o["opex_var_EUR_per_t"]),
            "lifetime":      o["lifetime"],
        })

        self.json_data.setdefault("Economics", {}).update(self.financial_indicators)
        return self.financial_indicators
import h5py
import pandas as pd
from pathlib import Path
from adopt_net0.result_management.read_results import extract_datasets_from_h5group

h5_file = Path(r"C:\Users\0898341\PycharmProjects\2026_project\results\20260518030842-1\optimization_results.h5")

with h5py.File(h5_file, "r") as f:
    raw_nodes    = extract_datasets_from_h5group(f["design"]["nodes"])
    raw_networks = extract_datasets_from_h5group(f["design"]["networks"])

nodes_df    = pd.DataFrame.from_dict(raw_nodes,    orient="index")
networks_df = pd.DataFrame.from_dict(raw_networks, orient="index")

# Print first 40 index tuples for nodes
print("=== NODE VARIABLES (first 40) ===")
for idx in list(nodes_df.index)[:40]:
    print(idx)

print("\n=== NETWORK VARIABLES (first 20) ===")
for idx in list(networks_df.index)[:20]:
    print(idx)
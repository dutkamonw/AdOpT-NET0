from pathlib import Path
import os
import sys
import time
import shutil
import subprocess

PROJECT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT_DIR / "results"

# Sweep only reduction targets (R10, R20, ..., R100).
# Scenario and objective remain defined in main.py.
TARGETS = range(10, 101, 10)


def find_h5_files():
    return set(RESULTS_DIR.glob("*/optimization_results.h5"))


def get_new_h5(before, start_time):
    after = find_h5_files()
    new_files = list(after - before)

    if new_files:
        return max(new_files, key=lambda p: p.stat().st_mtime)

    # fallback: newest H5 modified after this run started
    candidates = [
        p for p in after
        if p.stat().st_mtime >= start_time - 2
    ]

    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


def make_unique_folder(folder):
    if not folder.exists():
        return folder

    i = 2
    while True:
        candidate = folder.with_name(f"{folder.name}_{i}")
        if not candidate.exists():
            return candidate
        i += 1


for pct in TARGETS:
    label = f"R{pct}"
    target = pct / 100

    print("=" * 80)
    print(f"Running {label}: ccs_reduction_target = {target}")
    print("=" * 80)

    env = os.environ.copy()
    env["CCS_REDUCTION_TARGET"] = str(target)
    env["RUN_LOOP_MODE"] = "True"

    before = find_h5_files()
    start_time = time.time()

    # 1) Run main.py
    subprocess.run(
        [sys.executable, "main.py"],
        cwd=PROJECT_DIR,
        env=env,
        check=True
    )

    # 2) Find newly created optimization_results.h5
    new_h5 = get_new_h5(before, start_time)

    if new_h5 is None:
        raise RuntimeError(f"No new optimization_results.h5 found for {label}")

    # 3) Rename result folder by adding _R10, _R20, ...
    old_folder = new_h5.parent
    new_folder = old_folder.with_name(f"{old_folder.name}_{label}")
    new_folder = make_unique_folder(new_folder)

    shutil.move(str(old_folder), str(new_folder))

    renamed_h5 = new_folder / "optimization_results.h5"

    print(f"Result folder renamed to: {new_folder.name}")

    # 4) Run result.py using this h5 file
    env["ADOPT_H5_FILE"] = str(renamed_h5)

    subprocess.run(
        [sys.executable, "result.py"],
        cwd=PROJECT_DIR,
        env=env,
        check=True
    )

    print(f"Completed {label}")
    print(f"Exported result to: {new_folder}")

print("=" * 80)
print("All scenarios completed.")
print("=" * 80)
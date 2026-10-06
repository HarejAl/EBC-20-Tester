#!/usr/bin/env python3
"""Edit the settings below, then click VS Code's Run Python File button."""

import os

from ecm_characterization.characterize_hppc import (
    CharacterizationConfig, capacity_from_cell_json, characterize)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESAMPLED_HPPC_DIR = os.path.join(SCRIPT_DIR, "results", "hppc", "resampled")

# --------------------------- EDIT THESE SETTINGS ---------------------------

# Completed HPPC logger CSV (the resampled one, for evenly-spaced fitting).
# Leave None to pick it with a file dialog instead of typing a path.
INPUT_HPPC_CSV = None

# Use a config/cells JSON, or set CELL_JSON=None and provide CAPACITY_AH.
CELL_JSON = os.path.join(SCRIPT_DIR, "config", "cells", "LP604765.json")
CAPACITY_AH = 2.357

# Fraction from 0 to 1 at the first valid CSV sample.
INITIAL_SOC = 1.0

# Pulses far from this measured duration are flagged or rejected.
EXPECTED_PULSE_DURATION_S = 60.0

# Absolute current above which a sample belongs to a pulse.
CURRENT_THRESHOLD_A = 1.5

# Seconds after STOP used to fit V(t) = OCV + sum_i A_i*exp(-t/tau_i).
# Matches a 600s (10 min) rest, leaving a margin before the next pulse.
RELAXATION_FITTING_WINDOW_S = (1.0, 550.0)

# Width as a fraction (0.05 = 5% SOC).
SOC_BIN_WIDTH = 0.03

# Number of RC branches to fit (1-4). 2 resolves a fast + a slow relaxation;
# beyond 2 is fragile unless the rest is long and clean (see the printed
# warning). Requires enough real relaxation data to identify each branch --
# a 600s rest comfortably supports 2, a short rest (e.g. 120s) usually won't.
NUM_RC_BRANCHES = 2
MIN_ADJACENT_TAU_RATIO = 2.5

# Optional pseudo_ocv.json from runPseudoOcvVSCode.py/pseudo_ocv.py. When set,
# overrides CAPACITY_AH/CELL_JSON above with the measured capacity and
# anchors each pulse's SOC/OCV to the measured curve instead of pure
# coulomb-counting. None auto-picks the newest one under results/; set to
# False to skip pseudo-OCV anchoring entirely (plain coulomb-counting).
PSEUDO_OCV_JSON = None

# Derived CSV, JSON, and SVG files are written here. None places them next
# to the input CSV's own protocol folder (e.g. results/hppc/ecm_characterization
# for anything under results/hppc/...), matching the existing convention.
OUTPUT_FOLDER = None

# ---------------------------------------------------------------------------


def pick_file(initial_dir: str, title: str) -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title=title,
        initialdir=initial_dir if os.path.isdir(initial_dir) else SCRIPT_DIR,
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
    root.destroy()
    return path or None


def resolve_input_csv() -> str:
    if INPUT_HPPC_CSV:
        return INPUT_HPPC_CSV
    picked = pick_file(RESAMPLED_HPPC_DIR, "Select a resampled HPPC CSV to characterize")
    if not picked:
        raise SystemExit("No HPPC CSV selected.")
    return picked


def find_latest(filename: str) -> str | None:
    results_dir = os.path.join(SCRIPT_DIR, "results")
    candidates = []
    for folder, _, files in os.walk(results_dir):
        if filename in files:
            path = os.path.join(folder, filename)
            candidates.append((os.path.getmtime(path), path))
    return max(candidates)[1] if candidates else None


def resolve_pseudo_ocv_json() -> str | None:
    if PSEUDO_OCV_JSON is False:
        return None
    if PSEUDO_OCV_JSON:
        return PSEUDO_OCV_JSON
    return find_latest("pseudo_ocv.json")


def resolve_output_folder(input_csv: str) -> str:
    if OUTPUT_FOLDER:
        return OUTPUT_FOLDER
    # .../results/<protocol>/resampled/run_resampled_1s.csv -> .../results/<protocol>/ecm_characterization
    protocol_dir = os.path.dirname(input_csv)
    if os.path.basename(protocol_dir).casefold() == "resampled":
        protocol_dir = os.path.dirname(protocol_dir)
    return os.path.join(protocol_dir, "ecm_characterization")


def main() -> None:
    if CAPACITY_AH is None and not CELL_JSON:
        raise ValueError("Set CELL_JSON or CAPACITY_AH")
    input_csv = resolve_input_csv()
    output_folder = resolve_output_folder(input_csv)
    pseudo_ocv_json = resolve_pseudo_ocv_json()
    capacity = (float(CAPACITY_AH) if CAPACITY_AH is not None
                else capacity_from_cell_json(CELL_JSON))
    result = characterize(CharacterizationConfig(
        input_csv=input_csv,
        output_dir=output_folder,
        capacity_ah=capacity,
        initial_soc=INITIAL_SOC,
        expected_pulse_s=EXPECTED_PULSE_DURATION_S,
        current_threshold_a=CURRENT_THRESHOLD_A,
        relaxation_window_s=RELAXATION_FITTING_WINDOW_S,
        soc_bin_width=SOC_BIN_WIDTH,
        num_rc_branches=NUM_RC_BRANCHES,
        min_adjacent_tau_ratio=MIN_ADJACENT_TAU_RATIO,
        pseudo_ocv_json=pseudo_ocv_json,
    ))
    print("Characterization complete")
    print(f"  input: {input_csv}")
    if pseudo_ocv_json:
        print(f"  pseudo-OCV curve: {pseudo_ocv_json}")
    print(f"  pulse quality: {result['pulse_quality_counts']}")
    print(f"  output folder: {os.path.abspath(output_folder)}")


if __name__ == "__main__":
    main()

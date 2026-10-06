#!/usr/bin/env python3
"""Edit the settings below, then click VS Code's Run Python File button."""

import os

from ecm_characterization.validate_profile import ValidationConfig, validate

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")

# --------------------------- EDIT THESE SETTINGS ---------------------------

# Completed logger CSV from a run separate from the HPPC used to fit the ECM.
# Leave None to pick it with a file dialog instead of typing a path.
PROFILE_CSV = None

# ecm_parameters.json produced by runCharacterizationVSCode.py. Leave None to
# auto-use the most recently generated one anywhere under results/.
ECM_JSON = None

# Fraction from 0 to 1 at the first valid CSV sample of PROFILE_CSV. This
# depends on the specific run you pick, so it's not auto-detected -- check
# that run's starting voltage against the ECM's OCV curve if unsure.
INITIAL_SOC = 1.0

# "auto" picks the ECM direction matching the profile's dominant current
# sign, falling back (with a note) if that direction has no lookup rows.
# Force "charge" or "discharge" to pin it explicitly.
DIRECTION = "auto"

# None reuses the capacity_ah recorded in ECM_JSON's configuration.
CAPACITY_AH = None

# Absolute current below which a sample counts as rest (only matters when
# the profile CSV lacks a signed_current_a column).
CURRENT_THRESHOLD_A = 0.05

# Derived CSV, JSON, and SVG files are written here. None places them next
# to the profile's own protocol folder, e.g. results/<protocol>/ecm_validation.
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


def find_latest(filename: str) -> str | None:
    candidates = []
    for folder, _, files in os.walk(RESULTS_DIR):
        if filename in files:
            path = os.path.join(folder, filename)
            candidates.append((os.path.getmtime(path), path))
    return max(candidates)[1] if candidates else None


def resolve_profile_csv() -> str:
    if PROFILE_CSV:
        return PROFILE_CSV
    picked = pick_file(RESULTS_DIR, "Select a recorded voltage/current profile to validate")
    if not picked:
        raise SystemExit("No profile CSV selected.")
    return picked


def resolve_ecm_json() -> str:
    if ECM_JSON:
        return ECM_JSON
    latest = find_latest("ecm_parameters.json")
    if not latest:
        raise SystemExit(
            "No ecm_parameters.json found under results/. Run "
            "runCharacterizationVSCode.py first.")
    return latest


def resolve_output_folder(profile_csv: str) -> str:
    if OUTPUT_FOLDER:
        return OUTPUT_FOLDER
    # .../results/<protocol>/resampled/run_resampled_1s.csv -> .../results/<protocol>/ecm_validation
    protocol_dir = os.path.dirname(profile_csv)
    if os.path.basename(protocol_dir).casefold() == "resampled":
        protocol_dir = os.path.dirname(protocol_dir)
    return os.path.join(protocol_dir, "ecm_validation")


def main() -> None:
    profile_csv = resolve_profile_csv()
    ecm_json = resolve_ecm_json()
    output_folder = resolve_output_folder(profile_csv)
    result = validate(ValidationConfig(
        profile_csv=profile_csv,
        ecm_json=ecm_json,
        output_dir=output_folder,
        initial_soc=INITIAL_SOC,
        direction=DIRECTION,
        capacity_ah=CAPACITY_AH,
        current_threshold_a=CURRENT_THRESHOLD_A,
    ))
    if result["direction_note"]:
        print(f"NOTE: {result['direction_note']}")
    print("Validation complete")
    print(f"  profile: {profile_csv}")
    print(f"  ecm: {ecm_json}")
    print(f"  metrics: {result['metrics']}")
    print(f"  output folder: {os.path.abspath(output_folder)}")


if __name__ == "__main__":
    main()

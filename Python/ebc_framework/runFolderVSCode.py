#!/usr/bin/env python3
"""Run every JSON test in a config/tests subfolder, in filename order.

Edit the settings below, then click VS Code's Run Python File button.
Each test gets its own CSV, metadata, and checkpoint files. Between tests the
EBC is stopped and disconnected, and this script waits for the configured
rest interval before opening the next test.
"""

import json
import os
import time
from dataclasses import dataclass

from launchTest import TESTS_DIR, main as run_one_test


# --------------------------- EDIT THESE SETTINGS ---------------------------

# Subfolder inside config/tests. "paper" selects config/tests/paper.
TEST_SUBFOLDER = "paper"

# Tests and rests to run, in the exact order wanted. Test names may omit the
# .json suffix. Add {"rest_min": 20} anywhere for an explicit 20-minute rest.
# Reorder, omit, or repeat tests and rests as needed.
# Set to None to run every JSON in the subfolder in filename order instead.
TEST_ORDER = [
    # "01_pseudo_ocv_discharge",
    # {"rest_min": 10},
    # "01b_pseudo_ocv_charge",
    # {"rest_min": 10},
    # "02a_cc_discharge_0p5C",
    
    "02c_cc_discharge_2C",
    "02b_cc_discharge_1C",
    "03a_pulse_train_precharge",
    "03b_pulse_train",
    # "04a_validation_prep_80pct",
    # "04b_validation_profile",
    # Optional finishing phases can be inserted wherever needed:
    # "slow_finish_charge",
    # "slow_finish_discharge",
]

# Cell ID from config/cells. It overrides/ supplies the cell in every JSON.
CELL = "LP604765"

# Real tester settings.
PORT = "COM5"

# Default rest between adjacent tests when TEST_ORDER does not contain an
# explicit rest between them. No extra rest is added beside an explicit rest
# or after the final test. Set to 0 to use only rests written in TEST_ORDER.
REST_BETWEEN_TESTS_MIN = 20.0

# True: each test writes CSV + metadata + checkpoint under results/.
# False: samples are only printed in the terminal (nothing saved, no resume).
SAVE_DATA = True

# A live-plot window is opened for every test when True (needs SAVE_DATA). Close old plot
# windows yourself; they are separate processes and otherwise remain open.
LIVE_PLOT = False

# Optional filename/stem at which to begin, e.g. "03a_pulse_train_precharge".
# Earlier files are skipped. Keep None to start with the first JSON.
START_AT = None

# Optional names/stems to omit. Example: the reverse pseudo-OCV charge is
# normally LFP-only, so an NMC sequence may include it here.
EXCLUDE_TESTS = set()

# Print the complete plan and require typing RUN before controlling hardware.
REQUIRE_CONFIRMATION = True

# Stop the complete sequence if a protocol reports a safety stop.
STOP_ON_SAFETY_EVENT = True

# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestStep:
    minutes: float


def normalized_name(value: str) -> str:
    """Compare a JSON filename and a file stem equivalently."""
    return os.path.splitext(os.path.basename(str(value)))[0].casefold()


def discover_tests() -> list[str | RestStep]:
    """Resolve TEST_ORDER into an ordered sequence of tests and rests."""
    folder = os.path.abspath(os.path.join(TESTS_DIR, TEST_SUBFOLDER))
    tests_root = os.path.abspath(TESTS_DIR)
    try:
        inside_tests = os.path.commonpath([folder, tests_root]) == tests_root
    except ValueError:
        inside_tests = False
    if not inside_tests:
        raise ValueError("TEST_SUBFOLDER must stay inside config/tests")
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"test subfolder not found: {folder}")

    available_paths = [
        os.path.join(folder, name)
        for name in sorted(os.listdir(folder), key=str.casefold)
        if name.lower().endswith(".json")
    ]
    available = {normalized_name(path): path for path in available_paths}

    if TEST_ORDER is None:
        steps: list[str | RestStep] = list(available_paths)
    else:
        if not TEST_ORDER:
            raise ValueError("TEST_ORDER is empty")
        steps = []
        for requested in TEST_ORDER:
            if isinstance(requested, dict):
                if set(requested) != {"rest_min"}:
                    raise ValueError(
                        "rest entries must look like {'rest_min': 20}")
                minutes = float(requested["rest_min"])
                if minutes < 0:
                    raise ValueError("rest_min cannot be negative")
                steps.append(RestStep(minutes))
                continue
            if not isinstance(requested, str):
                raise ValueError(
                    "TEST_ORDER entries must be test names or rest mappings")
            key = normalized_name(requested)
            if key not in available:
                raise ValueError(
                    f"TEST_ORDER test not found in {folder}: {requested}")
            # Repeated entries are intentional and execute repeatedly.
            steps.append(available[key])

    excluded = {normalized_name(value) for value in EXCLUDE_TESTS}
    steps = [step for step in steps
             if isinstance(step, RestStep)
             or normalized_name(step) not in excluded]
    if not any(isinstance(step, str) for step in steps):
        raise ValueError("no tests remain after applying order/exclusions")

    if START_AT:
        target = normalized_name(START_AT)
        matches = [index for index, step in enumerate(steps)
                   if isinstance(step, str)
                   and normalized_name(step) == target]
        if not matches:
            raise ValueError(f"START_AT test not found: {START_AT}")
        steps = steps[matches[0]:]
    return steps


def describe_test(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            definition = json.load(fh)
        return f"{definition.get('name', os.path.basename(path))} " \
               f"[{definition.get('type', 'unknown')}]"
    except (OSError, ValueError):
        return os.path.basename(path)


def build_arguments(path: str) -> list[str]:
    args = [path, "--cell", CELL, "--port", PORT]
    if not SAVE_DATA:
        args.append("--no-save")
    if LIVE_PLOT:
        args.append("--live-plot")
    return args


def wait_between_tests(rest_minutes: float, next_name: str) -> None:
    seconds = max(float(rest_minutes) * 60.0, 0.0)
    if seconds == 0:
        return
    finish = time.time() + seconds
    print(f"\nResting at zero current for {rest_minutes:g} min before "
          f"{next_name}.")
    while True:
        remaining = finish - time.time()
        if remaining <= 0:
            break
        print(f"  next test in {remaining / 60.0:.1f} min", end="\r",
              flush=True)
        time.sleep(min(remaining, 30.0))
    print(" " * 48, end="\r")
    print("Rest complete.")


def main() -> None:
    if not CELL or not str(CELL).strip():
        raise ValueError("CELL must contain a config/cells cell ID")
    if REST_BETWEEN_TESTS_MIN < 0:
        raise ValueError("REST_BETWEEN_TESTS_MIN cannot be negative")

    steps = discover_tests()
    paths = [step for step in steps if isinstance(step, str)]
    print("Batch test plan")
    print(f"  Folder: {os.path.dirname(paths[0])}")
    print(f"  Cell: {CELL}")
    print(f"  Port: {PORT}")
    print(f"  Save data: {'yes' if SAVE_DATA else 'NO (terminal only)'}")
    print(f"  Inter-test rest: {REST_BETWEEN_TESTS_MIN:g} min")
    test_number = 0
    for step in steps:
        if isinstance(step, RestStep):
            print(f"      REST {step.minutes:g} min")
        else:
            test_number += 1
            print(f"  {test_number:02d}. {os.path.basename(step)} - "
                  f"{describe_test(step)}")

    if REQUIRE_CONFIRMATION:
        answer = input("\nType RUN to start this complete sequence: ").strip()
        if answer != "RUN":
            raise SystemExit("Batch cancelled.")

    test_number = 0
    for index, step in enumerate(steps):
        if isinstance(step, RestStep):
            next_test = next(
                (os.path.basename(candidate)
                 for candidate in steps[index + 1:]
                 if isinstance(candidate, str)),
                "end of sequence")
            wait_between_tests(step.minutes, next_test)
            continue

        path = step
        test_number += 1
        print("\n" + "=" * 72)
        print(f"Starting {test_number}/{len(paths)}: {os.path.basename(path)}")
        print("=" * 72)
        result = run_one_test(build_arguments(path))
        if result is None:
            raise SystemExit("Sequence stopped: test was interrupted.")
        if (STOP_ON_SAFETY_EVENT and
                str(result.reason).startswith("safety stop:")):
            raise SystemExit(
                f"Sequence stopped after safety event: {result.reason}")
        # Apply the default rest only when the immediately following action is
        # another test. An explicit RestStep handles its own interval.
        if index + 1 < len(steps) and isinstance(steps[index + 1], str):
            wait_between_tests(
                REST_BETWEEN_TESTS_MIN,
                os.path.basename(steps[index + 1]))

    print("\nAll selected tests completed.")


if __name__ == "__main__":
    main()

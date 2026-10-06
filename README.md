# EBC-20 Tester

Python control and analysis toolkit for the **ZKETECH EBC-A20** battery
tester. It lets you script charge, discharge, HPPC and dynamic-profile tests
on a real cell. You can then fit an equivalent-circuit model (ECM) to the data
and run a real-time SOC estimator against it.

ZKETECH does not publish the EBC-A20 serial protocol. The protocol was
reverse-engineered by sniffing traffic from the official Windows app, so read
[Known limitations](#known-limitations) before trusting numbers from a new
unit.

## What's in the repo

| Folder | What it does |
| --- | --- |
| [`Python/ebc_framework/`](Python/ebc_framework/README.md) | Drives the tester. Includes the serial protocol, safety layers, JSON-defined tests, a built-in simulator, resumable runs and auto-reconnect. **Start here.** |
| [`Python/ebc_framework/ecm_characterization/`](Python/ebc_framework/ecm_characterization/README.md) | Offline analysis. Fits an N-RC Thevenin model to HPPC data and builds pseudo-OCV curves and validation reports. |
| [`bms/`](bms/README.md) | Real-time SOC estimator (EKF/UKF) that only reads the test's CSV, plus offline replay and Q/R tuning tools. |
| `bms/sensor_bias_study_v2/` | Separate simulation study of how sensor bias affects SOC observers (uses PyBaMM). |

Documentation inside the framework:

- [`HOW_TO_LAUNCH.md`](Python/ebc_framework/HOW_TO_LAUNCH.md) is a
  step-by-step walkthrough for people who don't use a terminal much. It also
  covers the full paper test sequence.
- [`README.md`](Python/ebc_framework/README.md) is the full reference: test
  types, the JSON schema, the safety model, resume and reconnect.

## Quick start

### 1. Install

Python 3.10 or newer. From the repository root:

```bash
pip install -r requirements.txt
```

### 2. Check everything offline (no tester needed)

```bash
cd Python/ebc_framework
python selftest.py
python launchTest.py config/tests/connection_test.json --sim --time-accel 20
```

The self-test should end with `ALL CHECKS PASSED` and
`CONFIG-DRIVEN CHECKS PASSED`. The second command runs a full test on the
simulator and writes a CSV under `results/`.

### 3. Connect the real tester

1. Plug the EBC-A20 in over USB. It uses a CH340 adapter and appears as
   `COMx` on Windows (see Device Manager → Ports) or `/dev/ttyUSB0` on Linux.
2. Check the decoder against the tester's own display. With a cell
   connected, run the command below and compare the printed V/I with the LCD:

   ```bash
   python examples/verify_protocol.py COM5
   ```

3. Run the 2-minute connection test: 0.5 A discharge for 60 s, then 0.5 A
   charge for 60 s. Use `--cell` to pick the cell you actually have
   connected:

   ```bash
   python launchTest.py config/tests/connection_test.json --cell LP604765 --port COM5
   ```

### 4. Run a real test

From a terminal:

```bash
python launchTest.py --list-cells
python launchTest.py hppc --cell LP604765 --port COM5 --live-plot
```

From VS Code, open `Python/ebc_framework/runTestVSCode.py` and edit `TEST`,
`CELL`, `PORT` and `SIMULATOR` at the top. Then click **Run Python File**.
`runFolderVSCode.py` runs a whole ordered sequence of tests, such as the
`config/tests/paper/` protocols.

Every run writes these files to `results/<test name>/`:

- `<name>_<timestamp>.csv`: one row per sample, with time, wall clock,
  voltage, current, charge and mode.
- `<name>_<timestamp>_meta.json`: the exact test and cell configuration used.
- `<name>_<timestamp>_checkpoint.json`: lets you continue an interrupted run.

To pause a run, press **Ctrl-C**. This sends STOP to the tester. To continue
later:

```bash
python launchTest.py --resume results/<name>/<run>.csv --port COM5
```

## Defining cells and tests

- **Cells** are defined in `config/cells/*.json`. Each file holds the
  capacity, the normal voltage window, the hard emergency voltage limits and
  the current ratings. Add one file per cell you test.
- **Tests** are defined in `config/tests/*.json`. Currents are given as a
  C-rate by default, so the same test works on any cell. The test types are
  `dst`, `charge`, `discharge`, `hppc`, `rest`, `cycle` and `protocol`, a
  composite of discharge → rest → CC-CV charge → rest. The `comment` field in
  each file describes what it does.

Always dry-run a new or edited test with `--sim` first.

## Safety

The software is designed so that a stalled PC cannot leave a cell in an
unsafe state:

1. **Hardware cutoffs.** Every start command carries the tester's own cutoff
   (a low-voltage limit for discharge; a CV target and taper current for
   charge). These work even if Python crashes.
2. **Software limits.** Every sample is checked against the cell's hard
   voltage and current limits. The software sends STOP if a limit is crossed.
3. **Current capping.** Requested currents are silently capped to the cell's
   rating and the tester's own limits (5 A charge, 20 A discharge). Check
   the logged current, not just the JSON, to see what was actually applied.
4. **Startup check.** Before each test, the software confirms that readings
   arrive, that the voltage is plausible, and that a short C/10 pulse is
   really applied.

Never leave a charging cell unattended. Stop runs with Ctrl-C rather than
force-killing Python, so the final STOP command reaches the tester.

## Typical workflow

1. **Characterize.** Run the `config/tests/paper/` sequence: pseudo-OCV, CC
   discharges, pulse train, then the validation profile. See
   `HOW_TO_LAUNCH.md` for the order and preconditions.
2. **Fit the ECM.** Use `runPseudoOcvVSCode.py` and
   `runCharacterizationVSCode.py`, then check the fit on held-out data with
   `runValidationVSCode.py`.
3. **Estimate SOC.** Tune the filter offline with `bms/runKFTuningVSCode.py`,
   then run it live next to a test with `runTestWithBMSVSCode.py`.

## Known limitations

- **Field scaling was inferred.** The current channel needs a ×10 correction
  compared with the published reverse-engineering. That correction has only
  been confirmed below 10 A, and the Ah counter has not been checked against
  the LCD. Re-check both on high-current runs and on any new firmware
  revision.
- **About 1 Hz sampling.** The tester sends one status frame per second, and
  the PC cannot change that. Sub-second voltage steps (e.g. separating
  contact resistance from charge-transfer resistance) cannot be resolved.
- **No changing current while running.** The tester ignores a new start
  command while it is active. The framework always sends STOP and waits
  briefly before each setpoint change. If you write your own scripts against
  `EBCA20`, use `set_current()` or stop first.

## Credits

Protocol framing and decoding are based on public reverse-engineering work by
Maciej Grela / enkiusz
([pop.fsck.pl](https://pop.fsck.pl/hardware/zketech-ebc-a20.html)),
[Kazhuu/ebc-battery-tester](https://github.com/Kazhuu/ebc-battery-tester) and
[dev-strom/esp-ebc-mqtt](https://github.com/dev-strom/esp-ebc-mqtt).

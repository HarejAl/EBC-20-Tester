# EBC-20 Tester

Run battery experiments on a **ZKETECH EBC-A20** tester from Python. You
describe a test in a small JSON file (which currents to apply, for how long,
and when to stop). The script drives the tester and records the cell's
voltage, current and charge to a CSV file.

> ZKETECH does not publish the EBC-A20 serial protocol. It was
> reverse-engineered, so check the readings against the tester's display the
> first time you use a new unit (step 3 below).

---

## 1. Download and install

You need **Python 3.10 or newer** and **git**.

```bash
git clone https://github.com/HarejAl/EBC-20-Tester.git
cd EBC-20-Tester
pip install -r requirements.txt
```

Every command below is run from the `Python/ebc_framework` folder:

```bash
cd Python/ebc_framework
```

## 2. Connect the tester

1. Connect the cell to the EBC-A20 and switch the tester on.
2. Plug the tester into the PC with its USB cable.
3. Find its serial port:
   - **Windows:** Device Manager → *Ports (COM & LPT)* → "USB-SERIAL CH340
     (COM**5**)". The number varies by PC.
   - **Linux:** usually `/dev/ttyUSB0`.

## 3. First-time checks

Compare the readings with the tester's LCD. They should agree within a few
percent:

```bash
python examples/verify_protocol.py COM5
```

Then run the 2-minute connection test (0.5 A discharge for 60 s, then 0.5 A
charge for 60 s). It confirms the PC can both read from and control the
tester:

```bash
python launchTest.py connection_test --cell LP604765 --port COM5
```

The LCD should show `-PC-`, then a discharge, then a charge. Replace `COM5`
with your port and `LP604765` with your cell (see section 5).

## 4. Run an experiment

### From a terminal

```bash
python launchTest.py <test> --cell <cell> --port <port> [options]
```

```bash
python launchTest.py hppc --cell LP604765 --port COM5
```

| Option | What it does |
| --- | --- |
| `--cell ID` | Cell from `config/cells/` (overrides the one in the test file). |
| `--port COM5` | Serial port of the tester. |
| `--no-save` | Only print the measurements in the terminal and write nothing to disk. Use it for quick checks you don't need to keep. |
| `--live-plot` | Open a graph that updates during the run (needs saving enabled). |
| `--out file.csv` | Save to a specific CSV path instead of the default. |
| `--resume file.csv` | Continue an interrupted run (see below). |
| `--list-cells` | List the cells available in `config/cells/`. |

`<test>` can be a file name from `config/tests/` with or without `.json`
(`hppc`, `examples/charge_to_3v8`), or the full path to any JSON file.

### From VS Code (no typing)

Open `Python/ebc_framework/runTestVSCode.py` and edit the settings at the
top:

```python
TEST = "connection_test"   # which test file to run
CELL = "LP604765"          # which cell is connected
PORT = "COM5"              # tester serial port
SAVE_DATA = True           # False = print only, save nothing
LIVE_PLOT = False          # True = live graph (needs SAVE_DATA = True)
```

Then click **Run Python File** (▷, top right).

To run several tests in a row, with rests in between, use
`runFolderVSCode.py`. List the tests in `TEST_ORDER`. It shows the full plan
and asks you to type `RUN` before starting.

### While it runs

- Each measurement (about one per second) is printed:
  `t_exp = 12.0s  V = 3.912 V  I = -1.900 A  Q = 0.006 Ah  [discharge]`.
  Negative current means discharge and positive means charge.
- **Every test starts with a 10-second rest.** During it, the software checks
  that readings arrive and that a short C/10 test pulse actually draws
  current. If either check fails, the test stops before the real experiment
  begins.
- **To stop:** press **Ctrl-C** in the terminal. This sends STOP to the
  tester. Don't use VS Code's red stop button, because Python may be killed
  before it can send STOP.
- **USB drop:** if the USB link drops, the script reconnects automatically
  and continues the run.

### Output files

When saving is on (the default), each run creates `results/<test name>/`
with:

| File | Contents |
| --- | --- |
| `<name>_<date>_<time>.csv` | One row per measurement: `t_s, wall_clock, voltage_v, current_a, signed_current_a, charge_ah, cumulative_ah, mode`. |
| `..._meta.json` | The exact test and cell settings that produced the CSV. |
| `..._checkpoint.json` | Progress information used to resume the run. |

To plot a CSV after the run, or live with `--watch`:

```bash
python examples/plot_log.py results/hppc/hppc_20261007_101500.csv
```

To continue a run that was stopped with Ctrl-C:

```bash
python launchTest.py --resume results/hppc/hppc_20261007_101500.csv --port COM5
```

The resumed run appends to the same CSV and picks up exactly where it
stopped, after a fresh 10-second safety rest. With `--no-save`, nothing is
written, so a run can't be resumed.

## 5. Cells

Each cell you test needs a file in `config/cells/`. Copy an existing one and
edit it:

```json
{
  "id": "MyCell",
  "description": "18650 NMC 3 Ah",
  "chemistry": "NMC",
  "nominal_capacity_ah": 3.0,
  "nominal_voltage_v": 3.6,
  "voltage": { "max_v": 4.20, "min_v": 2.50, "storage_v": 3.70 },
  "current": { "max_charge_a": 3.0, "max_discharge_a": 6.0,
               "standard_charge_a": 1.5, "standard_discharge_a": 3.0 },
  "safety":  { "hard_max_v": 4.25, "hard_min_v": 2.45, "charge_taper_a": 0.06 }
}
```

| Field | Meaning |
| --- | --- |
| `nominal_capacity_ah` | Used to convert C-rates into amps (1C = this many A). |
| `max_v` / `min_v` | Default charge target and discharge cutoff. |
| `max_charge_a` / `max_discharge_a` | Requested currents above these are reduced automatically. |
| `hard_max_v` / `hard_min_v` | Emergency limits. A test may target any voltage inside them, but never outside. |
| `charge_taper_a` | Default current at which a CC-CV charge ends. |

Use the `"id"` with `--cell`. Run `python launchTest.py --list-cells` to
check that the file loads.

## 6. Build your own protocol

A test is one JSON file in `config/tests/`. Ready-to-copy examples are in
[`config/tests/examples/`](Python/ebc_framework/config/tests/examples).
Copy one, rename it, change the numbers, and run it with its file name.

**Conventions**

- Currents are **C-rates** by default (`"current_unit": "C"`, where 1.0 is the
  cell's capacity in amps). Set `"current_unit": "A"` for amps.
- **Sign:** negative means discharge, positive means charge, 0 means rest.
- Voltages are in V and times in seconds.
- Leave out `"cell"` (or set it to `""`) so the same file works with any cell
  through `--cell`.
- `max_duration_s` is a safety time limit. The test stops if it is reached.
- `sample_period_s` sets how often a row is logged. The tester itself
  measures about once per second.

### Example: charge until 3.8 V

[`examples/charge_to_3v8.json`](Python/ebc_framework/config/tests/examples/charge_to_3v8.json):

```json
{
  "name": "charge_to_3v8",
  "type": "charge",
  "sample_period_s": 1.0,
  "max_duration_s": 10800,
  "charge": {
    "protocol": "cc_cv",
    "c_rate": 0.5,
    "v_max": 3.8,
    "cutoff_c": 0.05,
    "rest_after_s": 600
  }
}
```

```bash
python launchTest.py examples/charge_to_3v8 --cell LP604765 --port COM5
```

The tester charges at 0.5C until the cell reaches **3.80 V**. It then holds
3.80 V while the current falls, and ends when the current drops below 0.05C.
Finally it logs a 10-minute rest. The EBC-A20 always finishes a charge this
way (CC-CV, constant current then constant voltage). To end sooner after
reaching 3.8 V, raise `cutoff_c` (e.g. `0.2`).

### Example: discharge until 3.0 V

[`examples/discharge_to_3v0.json`](Python/ebc_framework/config/tests/examples/discharge_to_3v0.json):

```json
{
  "name": "discharge_to_3v0",
  "type": "discharge",
  "sample_period_s": 1.0,
  "current_unit": "C",
  "current": 1.0,
  "v_cutoff": 3.0,
  "max_duration_s": 7200,
  "voltage_cutoff_rest_s": 300
}
```

This discharges at 1C until 3.00 V, then logs 5 minutes of rest
(`voltage_cutoff_rest_s`, 600 s if omitted). It prints the measured
capacity in Ah. To remove a fixed amount of charge instead, leave out
`v_cutoff` and limit the time: 0.2C for `"max_duration_s": 3600` removes
about 20% of the capacity.

### Example: your own current profile

[`examples/custom_profile.json`](Python/ebc_framework/config/tests/examples/custom_profile.json)
plays a list of steps, `repeat` times:

```json
{
  "name": "custom_profile",
  "type": "dst",
  "sample_period_s": 1.0,
  "current_unit": "A",
  "repeat": 3,
  "max_duration_s": 3600,
  "profile": [
    { "current": -1.0, "duration_s": 30 },
    { "current":  0.0, "duration_s": 60 },
    { "current":  0.5, "duration_s": 30 },
    { "current":  0.0, "duration_s": 60 }
  ]
}
```

A `dst` test stops only at the end of its repeats, at `max_duration_s`, or
at the cell's safety limits. To loop a profile **until a voltage** instead,
use a `protocol` test with `"discharge": {"protocol": "dst", "profile": [...],
"c_rate": ..., "v_min": ...}` (next example).

Keep steps **a few seconds or longer**. Every current change needs a STOP
and a short pause, and the tester reports only about once per second.

### Example: multi-phase cycle

[`examples/discharge_rest_charge.json`](Python/ebc_framework/config/tests/examples/discharge_rest_charge.json):
discharge, rest, charge, rest, repeated.

```json
{
  "name": "discharge_rest_charge",
  "type": "protocol",
  "sample_period_s": 1.0,
  "max_duration_s": 21600,
  "repeat": 2,
  "order": ["discharge", "charge"],
  "discharge": { "protocol": "crate", "c_rate": 1.0, "v_min": 3.0, "rest_after_s": 900 },
  "charge":    { "protocol": "cc_cv", "c_rate": 0.5, "v_max": 4.2,
                 "cutoff_c": 0.05, "rest_after_s": 900 }
}
```

- `"order": ["charge", "discharge"]` charges first.
- Set `"discharge": null` or `"charge": null` to skip a phase.
- The discharge `protocol` can be:
  - `"crate"`: constant current.
  - `"hppc"`: pulses, with `pulse_s` and `pulse_rest_s`.
  - `"dst"`: a staircase. Add a `"profile"` list as above; its largest step
    is scaled to `c_rate`.
- In `protocol` tests, `max_duration_s` applies to each phase separately.

### All test types

| `type` | Does | Main fields |
| --- | --- | --- |
| `charge` | CC-CV charge | `charge: {c_rate, v_max, cutoff_c, rest_after_s}` |
| `discharge` | Constant-current discharge to a voltage; reports capacity | `current`, `v_cutoff` |
| `dst` | Plays a list of current steps | `profile`, `repeat` |
| `hppc` | Discharge pulse and rest, repeated until `v_min` | `pulse_current`, `pulse_s`, `rest_s`, `v_min` |
| `rest` | Zero current; logs the voltage only | `max_duration_s` |
| `cycle` | Charge, rest, discharge, rest, repeated | `charge_current`, `discharge_current`, `repeat` |
| `protocol` | Any combination of discharge and charge phases with rests | see the example above |

Optional in every test: `"initial_rest_s"` (default 10). Setting it to `0`
also skips the startup checks.

More examples are in `config/tests/` and `config/tests/paper/` (the
characterization sequence; see
[`HOW_TO_LAUNCH.md`](Python/ebc_framework/HOW_TO_LAUNCH.md)).

## 7. Safety

- **Limits.** Voltage and current limits come from the cell file and are
  enforced twice:
  - By the tester itself: each command carries a cutoff voltage, which works
    even if the PC freezes.
  - By the software, which checks every reading and sends STOP if a limit is
    crossed.
- **Current capping.** Requests above the cell's rating or the tester's
  maximum (5 A charge, 20 A discharge) are reduced automatically. The CSV
  shows the current actually applied.
- **Reaching the low-voltage limit** is treated as the normal end of a
  discharge. The tester stops and a recovery rest is logged.
- **Faults abort the test.** These are over-voltage, over-current, or the
  tester not following a command.
- **Supervision.** Never leave a charging cell unattended.

## 8. Troubleshooting

| Problem | Fix |
| --- | --- |
| `could not open serial port COM5` | The tester is unplugged or off, or the port number is wrong (check Device Manager). Close any other program using the port, such as the official EBC app. |
| `no response from device` | The port opened but no data arrives. Power-cycle the tester, re-plug the USB cable, and check that it is the right port. |
| `implausible voltage reading at startup` | The cell isn't connected, or the wrong `--cell` is selected. |
| Write-path check failed | The PC reads the tester but commands have no effect. Re-plug and retry, and check the cell's leads. |
| Readings don't match the LCD | Run `examples/verify_protocol.py` and report the numbers; scaling may differ on your firmware. |
| Runs stop every few hours | In Device Manager, turn off "Allow the computer to turn off this device" for the USB adapter and hub, and disable sleep. |

## Repository layout

```
Python/ebc_framework/
  launchTest.py        command-line entry point
  runTestVSCode.py     VS Code launcher (one test)
  runFolderVSCode.py   VS Code launcher (sequence of tests)
  config/cells/        cell definitions
  config/tests/        test definitions (examples/, paper/, ...)
  ebc/                 library: protocol, serial transport, controller, runner
  examples/            verify_protocol.py, verify_stop.py, plot_log.py
  tests/               offline regression tests (python tests/run_all.py)
  results/             your measurements (not tracked by git)
```

Technical details (frame format, controller behaviour, all JSON options) are
in [`Python/ebc_framework/README.md`](Python/ebc_framework/README.md).

## Known limitations

- **Unconfirmed scaling.** The current reading needs a ×10 correction
  compared with the published protocol notes. This was confirmed only below
  10 A, and the Ah counter has not been checked against the LCD yet.
- **About 1 Hz sampling.** The tester reports about once per second, so
  sub-second voltage changes can't be captured.
- **No changing current while running.** The tester ignores a new command
  while it's active. The software handles this by sending STOP before every
  change, so the shortest practical step is a few seconds.

## Credits

The protocol work builds on public reverse-engineering by Maciej Grela /
enkiusz
([pop.fsck.pl](https://pop.fsck.pl/hardware/zketech-ebc-a20.html)),
[Kazhuu/ebc-battery-tester](https://github.com/Kazhuu/ebc-battery-tester) and
[dev-strom/esp-ebc-mqtt](https://github.com/dev-strom/esp-ebc-mqtt).

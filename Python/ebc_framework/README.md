# EBC-A20 control & experiment framework

Drive a **ZKETECH EBC-A20** battery tester from a PC for **SOC/SOH algorithm
validation** on real cells. Define tests and cells in JSON; one entry point
(`launchTest.py`) runs DST profiles, HPPC pulse tests, charge, discharge
(capacity/SOH), full cycles, or a composite multi-phase protocol — on the real
device or a built-in simulator.

## What "reverse engineered" means here

ZKETECH never published the EBC-A20 serial protocol. The byte format was
recovered by sniffing traffic between the official Windows app and the device
and inferring each field's meaning. The frame format is solid, but field
*scaling* is inferred and can vary by firmware revision — run
`examples/verify_protocol.py` once against your physical unit (compare decoded
V/I to the LCD) before trusting the numbers on a new setup.

## Measurement sampling rate

The device emits a status frame ~**once per second**; this cadence is set by
the EBC firmware and **cannot** be increased from the PC. Polling faster just
re-reads the same frame. `sample_period_s` in a test controls how often the
control loop re-evaluates/re-commands, not the hardware measurement rate.

## Getting started (new machine / new colleague)

Not comfortable with a terminal? See **`HOW_TO_LAUNCH.md`** for an explicit,
copy-paste walkthrough (opening PowerShell, finding the folder, exact
commands) — including how to run the paper protocols in `config/tests/paper/`.

1. Install Python 3.10+ and `pip install -r requirements.txt` from the
   repository root (pyserial, numpy, matplotlib; scipy is optional).
2. Run `python selftest.py` — it validates the protocol decoder, base-240
   encoding, and runs every test type on the built-in simulator. Should print
   `ALL CHECKS PASSED` / `CONFIG-DRIVEN CHECKS PASSED` with no hardware
   attached.
3. Connect the EBC-A20 over USB (CH340 chip → shows up as `/dev/ttyUSB0` on
   Linux, `COMx` on Windows — check Device Manager if unsure which COM number).
4. Run `python examples/verify_protocol.py COM5` (swap in your port) with a
   cell/load connected, and compare the printed V/I against the EBC's own LCD.
   If they don't match within a few percent, the decoder needs adjusting for
   your firmware before trusting anything else here.
5. Run the **connection test** (below) to confirm both discharge and charge
   commands actually drive the cell, not just the read path.
6. You're set — run any test in `config/tests/` against your own cell.

## Connection test

`config/tests/connection_test.json` is a 2-minute sanity check: 60 s discharge
at 0.5 A, then 60 s charge at 0.5 A. Run it first on any new setup:

```bash
python launchTest.py config/tests/connection_test.json --port COM5
```

Watch the LCD: it should show `-PC-`, then an active discharge for 60 s, then
an active charge for 60 s. 0.5 A is gentle enough to be safe on any cell in the
library — just edit the `"cell"` field to match whichever cell you actually
have connected.

## Layout

```
ebc/
  protocol.py    frame build/parse, base-240 + decode_ranged (verified)
  transport.py   SerialTransport (real) + SimulatorTransport (1-RC cell model)
  device.py      EBCA20 controller, SafetyLimits, generic run() loop
  experiment.py  DataLogger, ProfileController, dst_profile, capacity_test
  celllib.py     Cell + CellLibrary: load cell JSON, derive safety bounds
  runner.py      TestDef + run_test: dispatch JSON tests (all types below)
config/
  cells/*.json   cell library (capacity, voltage window, current ratings, bounds)
  tests/*.json   test definitions (dst / charge / discharge / hppc / cycle / protocol)
examples/
  verify_protocol.py   one-time real-hardware decoder check (see above)
  verify_stop.py       confirms STOP actually halts an active discharge
  plot_log.py          plot a logged CSV, standalone or live (see below)
ecm_characterization/  offline HPPC -> N-RC ECM fit, pseudo-OCV, validation
                       (see its own README.md)
launchTest.py    single CLI entry point -- run any JSON test, hardware or sim
run*VSCode.py    edit-settings-and-click launchers (single test, whole folder,
                 test + live BMS, ECM characterization, pseudo-OCV, validation)
resampleResults.py  resample raw CSVs onto a uniform time grid
results/<protocol>/  raw CSVs + metadata/checkpoints, grouped by protocol
selftest.py      offline validation of the whole stack -- run this first
selftest_*.py    focused offline regressions (link check, reconnect, recovery
                 rest, capacity glitch, transport framing)
```

## Running a test

### From VS Code (no command to type)

Open `runTestVSCode.py` and edit the settings at the top: `TEST`, `CELL`,
`PORT`, `SIMULATOR`, and `LIVE_PLOT`. Then click **Run Python File** in the
top-right corner, or select **EBC-20: run configured test** in Run and Debug
and press F5.

`TEST` may be just the file stem (for example `"discharge"` or `"paper/02a_cc_discharge_0p5C"`). `CELL`
is an ID from `config/cells`; it overrides the cell in the test. Set `CELL =
None` when you want to use the test JSON's own cell.

### From a terminal

```bash
# List the cell library:
python launchTest.py --list-cells

# Dry-run any test on the simulator (no hardware), accelerated:
python launchTest.py config/tests/dst_validation.json --sim --time-accel 60

# Run for real (Linux /dev/ttyUSB0, Windows COM3):
python launchTest.py config/tests/dst_validation.json --port COM3

# Select/override the cell at launch and show a live plot:
python launchTest.py dst_validation --cell cellAlex --port COM3 --live-plot
```

While running, `launchTest.py` prints one line per sample (`t=... V=... I=...
Ah=... [mode]`) and writes to
`results/<name>/<name>_<timestamp>.csv`. Sibling
`<name>_<timestamp>_meta.json` is written **as soon as the run starts** (not
after it finishes), so even an early-terminated or crashed run leaves you
knowing exactly what test/cell config produced the partial CSV.

Every test starts with a 10-second zero-current rest, which is included in the
CSV. This applies automatically to all test types and existing test files. If
needed for a special case, a test JSON can override the duration with
`"initial_rest_s": 120` (or use `0` to disable it).

### Stop now and continue later

Stop a running test with **Ctrl-C in VS Code's integrated terminal**. The
device receives STOP and the run keeps a sibling `<name>_checkpoint.json`.
Do not use VS Code's force-terminate/trash button for a normal pause, because
it may kill Python before it can send STOP. To continue from
VS Code, set `SELECT_RESUME_FILE = True` in `runTestVSCode.py`, run the file,
and select either the interrupted CSV or its checkpoint. Alternatively, set
`RESUME_FROM` directly to that path.

From a terminal, the equivalent is:

```bash
python launchTest.py --resume results/<name>/<interrupted-run>.csv --port COM5
```

Continuation appends to the original CSV and restores the cycle/phase plus
DST step, HPPC pulse/rest position, or cycle sub-phase. A fresh 10-second
zero-current safety rest always occurs before current is reapplied. Time spent
offline counts toward an interrupted rest period, but not toward an active
charge/discharge duration. Completed runs cannot be resumed accidentally.

### Automatic reconnect after a dropped connection

Real USB-serial connections occasionally drop mid-run (a CH340 driver hiccup,
Windows suspending the port, a cable issue) -- long enough to matter on an
unattended overnight test. On hardware runs, `launchTest.py` now catches
this automatically: it waits (backing off from 5s up to 120s between
attempts), reopens the port, and resumes from the same checkpoint `--resume`
would use -- no manual restart needed. This is on by default; disable it
with `--no-auto-reconnect`, or cap the attempts with
`--reconnect-max-attempts N` (default: retry indefinitely). It never
triggers on `--sim`. If a connection never comes back, it eventually gives
up and leaves the checkpoint for a manual `--resume`, same as before this
existed.

If a test is *still* stopping every few hours even with this in place, that
points at the port itself: in Device Manager, check Power Management on the
USB-serial adapter and its parent USB hub(s) (uncheck "allow the computer to
turn off this device to save power"), and disable system sleep for the
duration of a run.

### Watching a run live

Plotting is a separate process from the one talking to the hardware, so it
can't interfere with serial timing. Open a second terminal:

```bash
python examples/plot_log.py results/<name>/<name>_<timestamp>.csv --watch
```

refreshes every 2 s as the CSV grows. Drop `--watch` for a one-shot plot after
a run finishes. Run `plot_log.py` with no arguments (or click Run in VS Code)
to get a file-picker dialog defaulting to `results/` instead.

## Cells

A cell JSON captures datasheet facts and emergency bounds:

```json
{
  "id": "cellAlex",
  "description": "Pouch cell 7Ah",
  "chemistry": "NMC",
  "nominal_capacity_ah": 7.0,
  "nominal_voltage_v": 3.7,
  "voltage": { "max_v": 4.20, "min_v": 2.80, "storage_v": 3.80 },
  "current": { "max_charge_a": 5.0, "max_discharge_a": 20.0,
               "standard_charge_a": 5.0, "standard_discharge_a": 10.0 },
  "safety": { "hard_max_v": 4.25, "hard_min_v": 2.75, "charge_taper_a": 0.05 }
}
```

`max_v`/`min_v` are the normal operating defaults. Explicit test targets may
go outside that nominal window, but are capped at `hard_max_v`/`hard_min_v`.
Those hard limits are applied both to the tester-side cutoff command and the
software emergency STOP. Add a file
per cell to `config/cells/`. A test may reference a cell by its `id`, leave
`"cell": ""`, or omit `cell` entirely. In the latter two cases, select it at
launch with `--cell` or the VS Code launcher's `CELL` setting. A launch-time
cell always overrides the test value. Any current a test requests is
**silently clamped** (never raises) to the stricter of the
cell's own `max_charge_a`/`max_discharge_a` and the EBC-A20's own absolute
hardware ceiling (5 A charge / 20 A discharge) — check the logged current in
the CSV, not just the test JSON, to see what was actually applied if you
over-scale a profile.

## Tests

Test currents are **C-rate** (`"current_unit": "C"`) and scaled by the cell's
nominal capacity, so the same test runs across cells. Set `"current_unit":
"A"` for absolute amps. Types:

- `dst`       — staircase `profile` of `{current, duration_s}` steps you define
                yourself in the JSON (e.g. a published FUDS/DST table),
                repeated `repeat` times.
- `charge`    — CC charge to the cell's `max_v` (device handles CV taper).
- `discharge` — CC discharge to `min_v` (or an optional `v_cutoff` override,
                never lower than `hard_min_v`); reports usable Ah.
- `hppc`      — discharge pulse (`pulse_current`, `pulse_s`) / rest (`rest_s`),
                repeated until `v_min` -- resistance/relaxation characterization.
- `rest`      — passive observation at zero applied current; logs the cell for
                `max_duration_s` after explicitly stopping the tester.
- `cycle`     — charge / rest / discharge / rest, `repeat` times; logs
                per-cycle discharge capacity.
- `protocol`  — composite test combining an optional discharge phase, rest,
                optional CC-CV charge phase, and rest, repeated `repeat`
                times. This is the most flexible type -- see below.

### `protocol` schema

```json
{
  "type": "protocol", "cell": "cellAlex", "repeat": 3,
  "discharge": {
    "protocol": "dst",       // "dst" | "hppc" | "crate" | null (skip discharge)
    "c_rate": 2.0,           // scales the built-in DST staircase, or the
                             // pulse/constant current for hppc/crate
    "v_min": 3.0,            // stop once voltage drops to/below this
    "rest_after_s": 300,     // rest after the discharge phase ends
    "pulse_s": 60,           // hppc only
    "pulse_rest_s": 120      // hppc only (rest *between* pulses)
  },
  "charge": {
    "protocol": "cc_cv",     // only supported value, or null (skip charge)
    "c_rate": 1.0,
    "v_max": 4.2,            // default max_v; explicit values cap at hard_max_v
    "cutoff_c": 0.02,        // taper/termination current as a C-rate
    "rest_after_s": 300
  }
}
```

Either `"discharge"` or `"charge"` can be `null`/omitted for a charge-only or
discharge-only repeated protocol. Note: `protocol`'s `"dst"` discharge mode
plays the fixed built-in staircase shape (`ebc/experiment.py:dst_profile`)
scaled by a single C-rate number -- it can't reproduce an arbitrary custom
profile. If you need a specific published DST/FUDS table, use the standalone
`"dst"` test type instead, which reads whatever `profile` array you put in
the JSON directly.

## Safety

Three layers, all derived from the cell file:
1. **Device-side cutoffs** embedded in each command frame (low-V for
   discharge, CV + taper for charge) — protect the cell even if the PC/loop
   stalls.
2. **Software `SafetyLimits`** checked every loop; issues STOP on violation.
3. **Current clamping** on every `set_charge`/`set_discharge` call — caps to
   the stricter of the cell's rating and the EBC's own hardware ceiling,
   without ever raising an error (see Cells, above).

Set conservative bounds for your chemistry. Never leave a charging cell
unattended.

### Voltage-cutoff recovery rest

Reaching the low-voltage boundary during discharge is treated as a normal,
recoverable end-of-discharge condition rather than a completed useful pulse.
The runner immediately sends STOP, repeatedly confirms zero measured current,
and continues logging a 600-second zero-current recovery rest before ending
the test. Set `"voltage_cutoff_rest_s"` in a test JSON to override the default.

If execution is interrupted during this recovery, resume continues only its
remaining duration; it does not restart the discharge or HPPC pulse. The
checkpoint records `voltage cutoff reached`, allowing ECM characterization to
reject the final interrupted HPPC pulse automatically.

This does not weaken genuine fault handling. Measured overcurrent, hard
overvoltage, and repeated command-state confirmation failure still STOP and
abort immediately instead of continuing into an automatic rest.

## A real-hardware quirk worth knowing

Confirmed on real hardware: **the EBC only accepts a new start-charge/
start-discharge command when it's currently idle** — a command sent while
already actively driving current is silently ignored. `EBCA20.set_current()`
already handles this for you (it forces an explicit STOP with a brief settle
delay before every setpoint change), so you don't need to think about it when
writing a test — but if you're calling `set_charge`/`set_discharge` directly
in your own script rather than through `set_current()`/the test runner,
remember to stop first.

## Simulator

`--sim` swaps in a 1-RC Thevenin cell (OCV-SOC curve, coulomb integration) so
you can develop and validate control/estimation logic with no hardware.
`--time-accel N` compresses both the cell physics and profile-step durations
by N for fast dry runs. The real backend ignores acceleration. Always dry-run
a new test on the simulator before running it for real.

## Protocol provenance

Frame format, 9600 **8O1** serial, base-240 encoding and `decode_ranged` from
the documented reverse-engineering by Maciej Grela / enkiusz
(`pop.fsck.pl/hardware/zketech-ebc-a20.html`), cross-checked with
`Kazhuu/ebc-battery-tester` (FRAMES.md) and `dev-strom/esp-ebc-mqtt`. The
self-test confirms the decoder reproduces the documented frame CRC. Field
scaling for the *current* channel was found to differ from the documented
value on our unit (confirmed against the LCD) and is corrected in
`ebc/protocol.py` -- re-verify on any new firmware revision.

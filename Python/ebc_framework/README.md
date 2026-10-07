# EBC-A20 framework: technical reference

This is the reference for how the framework works and every JSON option. For
installing, connecting the tester and running a first experiment, read the
[repository README](../../README.md). For a copy-paste PowerShell
walkthrough and the paper characterization sequence, read
[`HOW_TO_LAUNCH.md`](HOW_TO_LAUNCH.md).

## Layout

```
ebc/
  protocol.py    frame build/parse, base-240 encoding, decode_ranged
  transport.py   SerialTransport (9600 8O1) + FrameReassembler
  device.py      EBCA20 controller, SafetyLimits, generic run() loop
  experiment.py  DataLogger, ProfileController, capacity_test
  celllib.py     Cell + CellLibrary: load cell JSON, derive safety limits
  runner.py      TestDef + run_test: dispatch JSON tests (all types below)
config/
  cells/*.json   cell library
  tests/         test definitions; examples/ = templates, paper/ = sequence
examples/
  verify_protocol.py   one-time decoder check against the LCD
  verify_stop.py       confirms STOP really halts an active discharge
  plot_log.py          plot a logged CSV, once or live (--watch)
tests/           offline regression tests on a fake tester (run_all.py)
launchTest.py    command-line entry point
runTestVSCode.py / runFolderVSCode.py   edit-and-click VS Code launchers
results/<test>/  CSV + _meta.json + _checkpoint.json per run (git-ignored)
```

## What "reverse engineered" means here

ZKETECH never published the EBC-A20 serial protocol. The byte format was
recovered by sniffing traffic between the official Windows app and the device
and inferring what each field means. The frame format is solid, but field
*scaling* is inferred and can vary between firmware revisions. Run
`examples/verify_protocol.py <port>` once on each unit and compare the
decoded V/I with the LCD.

- **Current scaling.** The current field decodes 10× too small with the
  documented table, so `parse_status` multiplies it by 10. This was
  confirmed on hardware only below 10 A.
- **Ah counter.** The charge (Ah) field has not yet been compared with the
  LCD.

## Measurement sampling rate

The device sends a status frame about **once per second**. The firmware sets
this rate and the PC cannot increase it. `sample_period_s` in a test controls
how often a row is logged and the controller is re-evaluated, not how often
the hardware measures. The link watchdog and the safety checks run at least
once a second, whatever `sample_period_s` is.

## Command line

```
python launchTest.py TEST --cell ID --port PORT [--no-save] [--live-plot]
                          [--out CSV] [--no-auto-reconnect]
                          [--reconnect-backoff-s S] [--reconnect-max-attempts N]
python launchTest.py --resume RUN.csv|RUN_checkpoint.json --port PORT
python launchTest.py --list-cells
```

`TEST` can be:

- a path to a JSON file;
- a file name inside `config/tests/`, with or without `.json`;
- a subfolder path such as `examples/charge_to_3v8`.

`--no-save` prints the samples but writes no CSV, metadata or checkpoint. It
can't be combined with `--resume`, `--out` or `--live-plot`. Automatic
reconnect still works within the same process, using in-memory progress.

### Output files

`<name>_<timestamp>.csv` has these columns:

| Column | Meaning |
| --- | --- |
| `t_s` | Elapsed test time in s. It is continuous across resumes, so analysis should use this column. |
| `wall_clock` | Local time of the sample. A jump here with no jump in `t_s` marks where a run was interrupted. |
| `voltage_v` | Cell voltage in V. |
| `current_a` | Magnitude of the current in A. |
| `signed_current_a` | Current with sign: + charge, − discharge, 0 when idle. |
| `charge_ah` | The device's Ah counter. It resets with every new command. |
| `cumulative_ah` | Software running total of the charge (signed). Gaps longer than about 3 sample periods are not integrated. |
| `mode` | `charge`, `discharge`, `idle` or `unknown`. |

`_meta.json` is written as soon as the run starts. Even a crashed run
therefore records which test and cell produced the partial CSV.

### Stop, resume, reconnect

- **Ctrl-C** sends STOP and keeps the checkpoint. Don't force-kill Python,
  because STOP might not be sent.
- **`--resume`** appends to the original CSV after a fresh 10-second
  zero-current rest. It restores the protocol cycle and phase, the DST step,
  the HPPC pulse/rest position, or the cycle sub-phase.
  - Time spent offline counts toward an interrupted rest, but not toward an
    active charge or discharge.
  - Completed runs refuse to resume.
- **Automatic reconnect.** If the USB-serial link drops, the script reopens
  the port and resumes from the latest in-memory state.
  - Back-off starts at 5 s and doubles up to 120 s. It resets after 3 healthy
    minutes.
  - A link that stays open but sends nothing for 15 s also counts as a drop.
  - Disable it with `--no-auto-reconnect`, or cap it with
    `--reconnect-max-attempts N` (consecutive failures).
- **If runs still stop every few hours**, the cause is usually Windows power
  management. In Device Manager, uncheck "allow the computer to turn off this
  device" for the USB-serial adapter and its hubs, and disable sleep.

## Cells

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

- **Default targets.** `max_v` and `min_v` are the default charge target and
  discharge cutoff.
- **Explicit targets.** A test may ask for a different voltage, such as
  `v_max`, `v_min` or `v_cutoff`. The request is capped to
  `hard_max_v` / `hard_min_v`, with a printed warning.
- **Optional fields.** If `safety` is missing, the hard limits default to
  `max_v + 0.05` and `min_v − 0.05`, and `charge_taper_a` defaults to 0.02C.
- **Choosing the cell.** A test may name a cell with `"cell": "<id>"`, or
  leave it empty and rely on `--cell`. `--cell` always takes priority.

## Test types

Currents are C-rates (`"current_unit": "C"`, the default), scaled by
`nominal_capacity_ah`. Use `"current_unit": "A"` for absolute amps. The sign
is + charge and − discharge.

Common fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Name of the results folder and file prefix. |
| `type` | required | One of the types below. |
| `cell` | none | Cell ID. Can be supplied at launch with `--cell`. |
| `sample_period_s` | 1.0 | Logging and control period. |
| `max_duration_s` | none | Safety time limit for the test. In `protocol` tests it applies to each phase. |
| `initial_rest_s` | 10 | Zero-current rest and link checks at the start. `0` skips both. |
| `voltage_cutoff_rest_s` | 600 | Recovery rest logged after a low-voltage cutoff. |

### `charge`

A CC-CV charge.

```json
"charge": { "protocol": "cc_cv", "c_rate": 0.5, "v_max": 3.8,
            "cutoff_c": 0.05, "rest_after_s": 600, "max_duration_s": 10800 }
```

- **How it ends.** The tester applies constant current until it reaches
  `v_max`, then holds `v_max` until the current falls below `cutoff_c`, and
  then stops on its own. The run ends when the tester reports end of charge
  or goes idle.
- **Defaults.** `v_max` defaults to the cell's `max_v`, and `cutoff_c`
  defaults to the cell's `charge_taper_a`.
- **Older syntax.** A top-level `"current": <C-rate>` (with no `charge`
  block) also works. It charges to `max_v` with the default taper.

### `discharge`

A constant-current discharge to `v_cutoff` (default `min_v`). It reports the
measured capacity.

```json
"current": 1.0, "v_cutoff": 3.0
```

- **End condition.** The discharge ends at `v_cutoff`, or when the device
  has really left discharge mode for 3 consecutive samples. A single garbled
  frame is ignored.
- **Default time limit.** The default `max_duration_s` is 6 h.

### `dst`

Plays a list of current steps.

```json
"repeat": 3,
"profile": [ { "current": -1.0, "duration_s": 30 },
             { "current":  0.0, "duration_s": 60 } ]
```

- **End condition.** The test ends after `repeat` passes, at `max_duration_s`,
  or at a safety limit.
- **Shape-only profiles.** Add `"profile_peak_c_rate": 2.0` to treat
  `current` as a shape: the profile is scaled so its largest step equals 2C.

### `hppc`

```json
"pulse_current": 1.0, "pulse_s": 60, "rest_s": 600, "v_min": 2.75
```

Discharge pulse, then rest, repeated until the voltage reaches `v_min`
during a pulse. During a rest, STOP is re-sent while any current is still
measured.

### `rest`

Zero current for `max_duration_s` (required). The tester is stopped
explicitly first.

### `cycle`

```json
"charge_current": 1.0, "discharge_current": 1.0, "repeat": 5,
"rest_after_charge_s": 300, "rest_after_discharge_s": 300
```

Charge to `max_v`, rest, discharge to `min_v`, rest, repeated. The
discharge capacity of each cycle is printed at the end.

### `protocol`

A combination of an optional discharge phase and an optional CC-CV charge
phase, each followed by a rest, repeated `repeat` times.

```json
{
  "type": "protocol", "repeat": 3,
  "order": ["discharge", "charge"],
  "discharge": {
    "protocol": "crate",     // "crate" | "hppc" | "dst"; null skips the phase
    "c_rate": 1.0,           // constant current, pulse current, or DST peak
    "v_min": 3.0,            // end of the discharge phase
    "rest_after_s": 300,
    "pulse_s": 60,           // hppc only
    "pulse_rest_s": 120,     // hppc only
    "profile": [ ... ],      // dst only, optional; largest step -> c_rate
    "max_duration_s": 3600   // optional per-phase time limit
  },
  "charge": {
    "protocol": "cc_cv",     // null skips the phase
    "c_rate": 0.5, "v_max": 4.2, "cutoff_c": 0.02,
    "rest_after_s": 300, "max_duration_s": 14400
  }
}
```

- **Order.** `"order": ["charge", "discharge"]` charges first.
- **`dst` discharge phase.** It loops its staircase until `v_min`. Give it
  a `"profile"` to use your own shape, scaled so the largest step equals
  `c_rate`. Without one, it uses the illustrative staircase built into
  `ebc/experiment.py:dst_profile`.

## Safety model

All limits come from the cell file.

1. **Tester-side cutoffs.** Every start command carries the cutoff voltage
   for discharge, or the CV target and taper current for charge. These
   protect the cell even if the PC stalls.
2. **Software checks.** Each received frame is checked against `hard_max_v`,
   against `hard_min_v` (while discharging), and against the current limit
   for the active direction. Any violation sends STOP and aborts the test.
3. **Current capping.** Every request is capped to the stricter of the
   cell's rating and the EBC's own limit (5 A charge, 20 A discharge). The cap
   is rounded down to the device's 10 mA steps, so it can never round above
   the cell's limit.
4. **Command confirmation.** If 3 consecutive frames contradict the
   requested state, the command is re-sent. After 3 failed retries the test
   stops with `command confirmation failed`.
5. **Startup link check.** A test first checks that a plausible voltage is
   being read. Then, during the initial rest, it checks that a C/10 pulse
   really draws current. If either fails, the test stops before the real
   experiment begins.

### Low-voltage cutoff

Reaching the low-voltage limit while discharging (`discharge`, `hppc`, a
`dst` that ends at its limit, or a software `v_min` stop) is treated as a
normal end of discharge:

- STOP is sent at once.
- Zero current is confirmed.
- A recovery rest of `voltage_cutoff_rest_s` (600 s by default) is logged.
- If the run is interrupted during the recovery rest, a resume finishes only
  the remaining rest.

Over-current, hard over-voltage and command-confirmation failure still abort
immediately.

## Hardware quirk: STOP before every setpoint change

The EBC accepts a new start-charge or start-discharge command only when it is
idle. A command sent while it is already driving current is silently ignored
(confirmed on hardware). `EBCA20.set_current()` therefore sends STOP and
waits 0.3 s before every change. If your own scripts call `set_charge` /
`set_discharge` directly, stop first.

## Offline tests

`python tests/run_all.py` runs every regression test against
`tests/fake_tester.py`. This is a cell and tester emulator used only by the
tests; experiments always use the real device. The tests cover:

- frame decoding;
- every test type and every example protocol;
- the link checks;
- reconnect and resume;
- the low-voltage recovery rest;
- `--no-save`.

## Protocol provenance

The frame format, 9600 baud **8O1** serial settings, base-240 encoding and
`decode_ranged` come from the documented reverse-engineering by Maciej Grela
/ enkiusz (`pop.fsck.pl/hardware/zketech-ebc-a20.html`). They were
cross-checked with `Kazhuu/ebc-battery-tester` (FRAMES.md) and
`dev-strom/esp-ebc-mqtt`. `tests/selftest.py` confirms that the decoder
reproduces the documented frame CRC.

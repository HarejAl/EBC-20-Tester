# Read-only real-time SOC monitor

The new runtime is under `realtime_bms/`; your original `ECM.py`, dual-filter
notebooks, and `sensor_bias_study_v2/` are preserved unchanged.

The estimator uses an N-RC-branch Thevenin model (`TheveninEcmModel`), N
configurable via the model JSON's `num_rc_branches` field:

```text
x = [SOC, V1, ..., VN]
V = OCV(SOC) - I R0(SOC) - sum_i Vi
SOC+ = SOC - I dt / (3600 Q)
Vi+ = exp(-dt/tau_i) Vi + Ri (1-exp(-dt/tau_i)) I
```

Current is positive for discharge and negative for charge. The monitor
automatically reverses the framework logger's `signed_current_a`, which is
positive for charge and negative for discharge.

## One-click combined test and BMS (recommended)

Open `Python/ebc_framework/runTestWithBMSVSCode.py`. Its first settings block
selects the protocol, cell, serial/simulator backend, resume, and test plot.
The second block selects the model, EKF/UKF, initial SOC, and filter tuning
(`Q_RC_PER_S`, one entry per RC branch, fastest first). Click **Run Python
File** once; it starts the CSV follower, runs the test, and stops the BMS
after draining the final samples.

The combined launcher defaults to `SIMULATOR = True` for safety. Change it to
`False` only when the intended tester and cell are connected. Even in combined
mode, only the EBC test owns the serial port; the BMS background thread remains
a read-only CSV consumer. If BMS initialization fails, the test is not started.

## Running beside a cell test as two scripts

1. Start the EBC test normally with `runTestVSCode.py` or
   `runFolderVSCode.py`.
2. Open `bms/runBMSVSCode.py` in a second VS Code terminal/process.
3. Set `INPUT_CSV` to the active measurement CSV, or leave it `None` to select
   the newest raw CSV under `Python/ebc_framework/results`.
4. Select `FILTER_TYPE = "ekf"` or `"ukf"`, set the initial SOC and model,
   then click **Run Python File**.
5. Stop the monitor with Ctrl+C after the cell test ends.

The BMS only tails the CSV. It does not import `ebc.device`, open COM5, or send
commands, so it cannot interfere with tester control. Each result row is
flushed immediately to `bms/results/<source>_bms.csv`. It includes SOC and its
2-sigma interval, every RC branch voltage (`v1_v`...`vN_v`), predicted/measured
voltage residual, NIS, outlier acceptance, interval, and status. A JSON state
checkpoint is written periodically for inspection and future resume support;
it now also records `num_rc_branches` and is rejected with a clear error
(rather than a cryptic shape mismatch) if resumed against a model with a
different branch count. A checkpoint from before this field existed is
treated as legacy 2-branch, with a printed warning.

## ECM parameters

`realtime_bms/example_2rc_model.json` demonstrates the required SOC-dependent
JSON schema, including the explicit `"num_rc_branches"` field the loader
requires. **Its numbers are examples, not calibrated parameters for your
cell.** Replace them before interpreting real SOC accuracy.

The loader also accepts your existing MATLAB format directly:

- `SOC`: SOC grid
- `Em`: OCV
- `R0`: series resistance
- `Rx`: RC resistances (the first `num_branches` columns are used)
- `taux`: RC time constants (the first `num_branches` columns are used)

In `runBMSVSCode.py`, set `MODEL_JSON = None`, `MODEL_MAT` to the `.mat` file,
and `MAT_CAPACITY_AH` to usable cell capacity. Loading `.mat` requires SciPy;
the JSON path needs only NumPy.

`ecm_characterization/characterize_hppc.py`'s `num_rc_branches` setting (1-4,
default 2) now fits multiple RC branches directly from real HPPC data --
see that module's README for what it takes for 2+ branches to actually be
identifiable (a short rest generally cannot support 2; a long, clean one can).
Its `ecm_parameters.json` output is not yet auto-converted into this folder's
model JSON schema; copy the relevant `rc_branches` values across by hand.

## EKF and UKF behavior

Both observers use identical dynamics and state constraints. The EKF uses
central finite-difference Jacobians, matching your notebook convention, and a
Joseph covariance update. The UKF implements scaled sigma points locally and
does not require FilterPy. Process noise scales with the actual sample
interval. Exact dynamics and coulomb counting use the full interval across
sampling gaps, which are also reported. Duplicate timestamps are logged but
not assimilated twice; CSV clock resets from resumed tests are stitched;
complete rows are consumed only after the logger has flushed them.

Voltage updates use a normalized-innovation-squared gate. Large voltage
outliers are logged but do not update SOC. This is estimator robustness only:
tester safety remains exclusively in the EBC controller.

`INITIALIZE_FROM_REST_VOLTAGE` can initialize SOC by inverting OCV when the
first sample is near zero current. Enable it only after a sufficiently long
rest; otherwise polarization voltage will bias the initialization.

## Offline validation

### Replay one configuration

Open `runOfflineBMSTestVSCode.py` and set the completed CSV, calibrated model,
initial SOC, reference source, filter, and Q/R values (`Q_RC_PER_S`, one
entry per branch). It writes:

- `offline_replay.csv` with reference/estimated SOC, uncertainty, RC states,
  voltage prediction, innovation, NIS, and update status;
- `offline_metrics.json` with SOC RMSE/MAE/max/final error, 2-sigma coverage,
  voltage-innovation statistics, NIS, and rejection fraction;
- `soc_replay.svg` and `voltage_replay.svg`.

### Tune Q/R weights

Open `runKFTuningVSCode.py`. Candidate tuples at the top define the grid for
filter type, `q_soc`, `q_rc_fast` (process noise for the fastest RC branch),
`q_rc_decay_ratio` (how much less noise each progressively slower branch
gets -- `q_i = q_fast * ratio**i`), and voltage measurement standard
deviation. This keeps the grid's size fixed regardless of the model's branch
count, while still preserving the fast/slow process-noise asymmetry a real
cell needs. The initial covariance, NIS gate, reference, and warm-up fraction
are also editable. It produces:

- `tuning_results.csv`, ranked by the composite score;
- `best_kf_config.json`, whose `observer_settings` names match the online
  launcher;
- `best_replay/`, containing a full replay and diagnostics for the winner.

SOC RMSE is the dominant score. NIS consistency, rejected-voltage fraction,
and 2-sigma coverage penalize candidates that look accurate only because they
are overconfident or ignore too many measurements. Start with the compact
grid, then narrow/expand the candidates around the best region.

For simulator data, use `REFERENCE_SOURCE = "column"` and `soc_true`. For a
real HPPC beginning at known SOC, `"coulomb_count"` provides a practical
provisional reference. It is not independent ground truth: tuning against it
mainly checks that voltage corrections remain compatible with charge balance.
Final weights should be replayed on a different protocol (for example DST or
a full cycle) and, when possible, an independently anchored SOC reference.
Set `TUNED_KF_CONFIG_JSON` in `runOfflineBMSTestVSCode.py` or
`runTestWithBMSVSCode.py` to load this export directly. The launcher keeps its
own initial SOC because that value belongs to the particular run.

The recommended sequence is:

1. Identify the ECM from the completed HPPC.
2. Run a baseline offline replay.
3. Tune a compact Q/R grid.
4. Replay the winner on a separate validation test.
5. Copy `observer_settings` into `runTestWithBMSVSCode.py` and run online.

### Automated regressions

Run:

```powershell
python -B realtime_bms/selftest_realtime_bms.py
python -B realtime_bms/selftest_offline.py
```

The first test creates EBC-format synthetic data with irregular intervals, a
duplicate timestamp, a resumed-clock reset, changing charge/discharge current,
and a large voltage outlier, for 1-, 2-, and 3-branch models. It verifies
convergence and outlier rejection for both EKF and UKF. The second test
exercises offline replay and Q/R tuning on the 2-branch case. Neither touches
the tester.

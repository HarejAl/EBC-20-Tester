# HPPC N-RC-branch Thevenin characterization

This folder converts a completed EBC HPPC logger CSV into parameters for:

`OCV(SOC) - R0(SOC) - [R1(SOC) || C1(SOC)] - ... - [RN(SOC) || CN(SOC)]`

where N (`num_rc_branches`, 1-4, default 2) is configurable. It is an offline
analysis tool. It never imports the serial/device layer and never modifies
the source CSV.

## Model and sign convention

The terminal-voltage model is `V = OCV - I*R0 - sum_i Vi`, with
`dVi/dt = (I*Ri - Vi)/tau_i` and `Ci = tau_i/Ri` for each branch i. Branches
are ordered fastest (smallest tau) first: R1/tau1 is the fastest relaxation,
RN/tauN the slowest. Exported current is positive for discharge and negative
for charge. This deliberately reverses the EBC logger's `signed_current_a`
convention, where charge is positive and discharge is negative.

SOC is coulomb-counted from the configured initial SOC and capacity using the
actual interval between every pair of samples. Units are: SOC as a 0-to-1
fraction, voltage in V, resistance in ohm, time constant in s, capacitance in
F, current in A, and capacity in Ah.

R0 comes from the pre-pulse relaxed voltage and the first loaded-voltage
samples. The post-pulse relaxation is fitted to
`V(t) = OCV_relaxed + sum_i A_i*exp(-t/tau_i)` by coordinate descent: one
branch's tau is grid-searched at a time (holding the others fixed), solving
the linear OCV+amplitudes sub-problem by least squares at every candidate,
cycling until the taus stop moving. Several differently-biased starting
spreads are tried and the best (lowest-error, non-degenerate) result is kept.
A configured `min_adjacent_tau_ratio` (default 2.5, auto-tightened to 3.0 for
3+ branches) keeps branches from collapsing onto the same time constant.

**More branches need real, well-separated relaxation data to identify --
this is not free by raising `num_rc_branches`.** A short rest (order 100s)
usually can't support 2 branches at all: the fit degenerates into an
oversized, nonphysical R/tau pair that gets rejected
(`nonphysical_or_unavailable_parameters`). A clean, long rest (order 600s+)
is what actually lets 2 branches resolve into a genuine fast + slow pair.
Above 2 branches is fragile even with good data; check
`pulse_characterization.csv`'s `quality_status`/`quality_reasons` (including
`rc_branch_tau_ratio_below_minimum`) before trusting the lookup table, not
just the pulse count.

OCV can drift during a long test; each lookup point is therefore local to its
pulse SOC. Charge and discharge are always kept as separate lookup-table
directions.

## VS Code usage

1. Open `runCharacterizationVSCode.py` at the repository root.
2. Edit the variables at the top: input CSV, cell JSON or capacity, initial
   SOC, expected duration, current threshold, relaxation window, SOC bin
   width, branch count, and output folder.
3. Click **Run Python File** in VS Code.

The input must follow the actual framework logger schema: `t_s`, `voltage_v`,
`current_a`, `signed_current_a`, `charge_ah`, and `mode`. Only `t_s`,
`voltage_v`, plus a signed-current source are required by the analyzer.

The equivalent command line is:

```powershell
python -m ecm_characterization.characterize_hppc results/hppc/run.csv `
  --cell-json config/cells/LP604765.json --initial-soc 1.0 `
  --expected-pulse-s 60 --current-threshold-a 0.1 `
  --relaxation-start-s 1 --relaxation-end-s 550 `
  --soc-bin-width 0.05 --num-rc-branches 2 `
  --output-dir results/hppc/ecm_characterization
```

## Outputs

- `pulse_characterization.csv`: every detected pulse, parameters (dynamic
  `r{i}_ohm`/`tau{i}_s`/`c{i}_f` columns per branch), fit errors, sample/gap
  metrics, status, and exclusion reasons.
- `ecm_lookup_table.csv`: SOC-binned medians of **accepted** pulses only,
  separated into charge and discharge rows, same flat per-branch columns.
- `ecm_parameters.json`: model/sign/units, `num_rc_branches`, configuration,
  input-cleaning counts, quality counts, and lookup values -- each lookup row
  nests its branches as `"rc_branches": [{"r_ohm", "tau_s", "c_f"}, ...]`,
  fastest first.
- `ocv_vs_soc.svg` and `ecm_parameters_vs_soc.svg`: the requested parameter
  curves, separated by direction.
- `detected_pulses.svg`: full-record pulse detection and quality overview.
- `relaxation_fit_pulse_NNN.svg`: measured and fitted relaxation (sum of all
  branches) for each pulse that has a usable post-pulse window.

## Validating the fit against a different recorded profile

`validate_profile.py` plays a fitted `ecm_parameters.json` open-loop (current
drives SOC and every RC branch, no voltage feedback) against a *different*
recorded logger CSV and compares predicted vs measured voltage -- a sanity
check that the fit isn't just reproducing the HPPC run it came from:

```powershell
python -m ecm_characterization.validate_profile results/some_other_run.csv `
  --ecm-json results/hppc/ecm_characterization/ecm_parameters.json `
  --initial-soc 1.0 --direction discharge `
  --output-dir results/some_other_run/ecm_validation
```

`--direction` picks which direction's lookup drives the whole profile
(`auto` follows the profile's dominant current sign, falling back with a
printed note if that direction has no lookup rows -- most HPPC runs only
characterize discharge). Outputs: `voltage_comparison.csv/svg`,
`voltage_error.svg`, and `validation_metrics.json` (RMSE/MAE/bias/max error).
`runValidationVSCode.py` at the repository root wraps the same call for
click-to-run use.

## Bad pulses and imperfect logs

Each pulse is `accepted`, `flagged`, or `rejected`. Rejected/flagged pulses
remain visible in the pulse CSV and diagnostics, but only fully accepted
pulses enter the lookup table. Rejection includes a duration more than 50%
away from the configured value (so a 660 s pulse expected to be 60 s is
automatically excluded), missing pre-rest/fit data, too few samples, and
nonphysical parameters. Warnings include a duration error over 20%, unstable
current, large sample gaps, and high relaxation RMSE. Thresholds are fields on
`CharacterizationConfig` for advanced use.

When the matching launcher checkpoint says the test ended at a voltage cutoff,
the final detected pulse is always rejected with
`terminal_pulse_interrupted_by_voltage_cutoff`, even if its measured duration
was close to nominal. Its 600-second recovery data remains available for
diagnostics but the pulse cannot enter the ECM lookup table.

Duplicate timestamps keep the later status row. If resumed data resets the
elapsed clock, segments are stitched in source-row order. Variable intervals
are used directly for SOC integration and relaxation fitting. Large gaps are
counted per pulse and surfaced as quality warnings rather than silently
interpolated. Invalid numeric rows are skipped and reported in JSON.

Run the offline regression with:

```powershell
python ecm_characterization/selftest_characterization.py
```

It generates temporary synthetic HPPC data containing charge/discharge
pulses, interval jitter, a duplicate, a missing sample, a resume time reset,
and a 660-second corrupt pulse (single-branch fit), plus a second synthetic
dataset with two known, well-separated time constants that the two-branch
fit must recover within tolerance. No hardware is accessed.

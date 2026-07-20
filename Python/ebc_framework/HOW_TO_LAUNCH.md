# How to launch a test

## Easiest: launch from VS Code

1. Open this project folder in VS Code.
2. Open `runTestVSCode.py`.
3. Change the settings at the top of the file (`TEST`, `CELL`, `PORT`,
   `SIMULATOR`, and `LIVE_PLOT`).
4. Click **Run Python File** at the top right. Alternatively, open **Run and
   Debug**, choose **EBC-20: run configured test**, and press F5.

The `CELL` setting overrides the cell stored in the protocol. Therefore a
protocol can omit `cell` or contain `"cell": ""`; selecting `CELL` in the
launcher is then required. Set `CELL = None` to use the protocol's own cell.

The remaining sections describe the equivalent terminal workflow.

## 1. Open a terminal

Press the Windows key, type `PowerShell`, press Enter. A terminal window opens.

## 2. Go to the project folder

Copy-paste this line and press Enter:

```powershell
cd "C:\Users\alexa\OneDrive - Politecnico di Milano\Desktop\Batteries\Tester\Python\ebc_framework"
```

**If you don't know the folder's path** (e.g. you're on a different PC, or the
project was moved): open the folder in File Explorer, click once in the
address bar at the top (it turns into a text path), copy that text, and paste
it between the quotes above in place of the path shown.

## 3. A note on the `python` command

Plain `python` may not work in your terminal — it depends on how Python was
installed. The commands below use the full path to a known-working Python:

```
C:\Users\alexa\anaconda3\python.exe
```

**If that exact path doesn't exist on your machine** (different user account,
different install), find your own Python first:
```powershell
where python
```
and swap whatever it prints in for `C:\Users\alexa\anaconda3\python.exe` in
every command below.

## 4. Confirm the setup works (do this once, on a new machine)

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'selftest.py'
```
Should print `ALL CHECKS PASSED`. No hardware needed for this step.

Then, with the EBC-A20 connected over USB, run the 2-minute connection test
(60s discharge at 0.5A, then 60s charge at 0.5A) to confirm the cable/port and
that both discharge and charge commands actually drive the cell:

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\connection_test.json' --port COM5
```

Swap `COM5` for your actual port — check Device Manager → Ports (COM & LPT)
if you're not sure which number it is.

## 5. Run any test

The general pattern is always:

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\<file>.json' --port COM5
```

To select a cell at launch (or override the one in the JSON) and open live
plotting automatically:

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' '<file>' --cell cellAlex --port COM5 --live-plot
```

It prints a live line per sample, saves the data to
`results\<name>_<timestamp>.csv`, and a `..._meta.json` file next to it
recording exactly what config produced that run.

## 6. Watch a test live (optional)

Open a **second** PowerShell window (repeat steps 1-2 in it), then once the
first terminal has printed the output path:

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'examples\plot_log.py' 'results\<name>_<timestamp>.csv' --watch
```

---

# Paper protocols (`config\tests\paper\`)

Run these in order. Each protocol's files are numbered so the sequence is
unambiguous. Every command follows the same pattern as step 5 above — just
the file name changes.

### Protocol 1 — Pseudo-OCV (Stage 0: electrode balancing)

Precondition: fully charge the cell and let it rest ≥1h first.

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\01_pseudo_ocv_discharge.json' --port COM5
```
~20 hours. **Only if your cell is LFP**, also run the reverse direction and
average the two curves to cancel hysteresis (edit `"cell"` in the file first
if you're using a different LFP cell than `cellAlex`):
```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\01b_pseudo_ocv_charge.json' --port COM5
```
For NMC/NCA cells (`cellAlex` as currently set up), skip `01b`.

### Protocol 2 — CC discharges at 0.5C / 1C / 2C

Precondition: full charge before starting `02a`. Run all three in order —
each one recharges the cell at the end, ready for the next:

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\02a_cc_discharge_0p5C.json' --port COM5
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\02b_cc_discharge_1C.json' --port COM5
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\02c_cc_discharge_2C.json' --port COM5
```
~1 day total. Run them one at a time, waiting for each to finish before
starting the next. The 2C step is 14 A on `cellAlex` — confirmed within both
the cell's own rating (20 A) and the EBC-A20's hardware ceiling (20 A), so it
runs at the full 2C, not reduced.

### Protocol 3 — Reduced HPPC-like pulse train

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\03a_pulse_train_precharge.json' --port COM5
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\03b_pulse_train.json' --port COM5
```
~5 hours total (run `03a` first, wait for it to finish, then `03b`).
**Note:** the EBC-A20 reports at a fixed ~1 Hz no matter what — it can't
capture the faster (100ms–1s) instantaneous voltage step some samplers use to
separate contact resistance from charge-transfer resistance. Expect that
separation to be less clean than a purpose-built potentiostat would give.

### Protocol 4 — Validation profile

```powershell
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\04a_validation_prep_80pct.json' --port COM5
& 'C:\Users\alexa\anaconda3\python.exe' 'launchTest.py' 'config\tests\paper\04b_validation_profile.json' --port COM5
```
~2.5 hours total, run back-to-back (don't leave days between `04a` and `04b`
or self-discharge drifts the SOC).

**Before running `04b`**, open `config\tests\paper\04b_validation_profile.json`
in a text editor and replace the placeholder `"profile"` array with your
actual WLTP/UDDS-derived current trace or the synthetic multisine — the file
as shipped only has a few illustrative dummy steps, not real data. This file
is the held-out set the fit is judged on; never use it for fitting.

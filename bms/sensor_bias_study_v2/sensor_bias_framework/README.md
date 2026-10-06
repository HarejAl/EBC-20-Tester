# Sensor-bias observer framework (PyBaMM plant + augmented-state ECM EKF)

Workflow:
1. `python run_experiments.py`  — runs the 22-case campaign (chemistry, profile,
   temperature, C-rate, DoD, drift rate, sensor age, voltage bias). Stores raw
   time series in `results/*.npz`, metrics in `results/index.csv`, and caches
   plant calibrations (pseudo-OCV + LS-fitted 2-RC ECM) in `results/calibration/`.
2. `python postprocess.py`      — generates all figures into `paper/figures/`
   (PDF+PNG, journal style) and the auto-generated LaTeX summary table into
   `paper/tables/tab_summary.tex`.
3. Compile `paper/main.tex` (pdflatex + bibtex). Introduction and methodology
   are drafted; the results section is scaffolded with figures but left empty.

Modules: `plant.py` (SPMe plant, OCV characterization, ECM calibration, CC /
CC-CV / DST profiles), `sensors.py` (bias & noise models), `observers.py`
(baseline and bias-augmented 2-RC EKF).

Requires: pybamm >= 25, numpy, scipy, matplotlib.
Delete `results/calibration/*.npz` to force re-calibration (e.g. after
changing the ECM structure).

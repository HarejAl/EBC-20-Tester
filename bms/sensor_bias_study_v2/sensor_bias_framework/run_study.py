"""
run_study.py — main entry point.

For each parameter set (Mohtat2020, Ai2020, OKane2022):
  1. characterize pseudo-OCV(SOC) and identify a crude ECM from PyBaMM (SPMe plant)
  2. simulate a 1C CC discharge and a full CC-CV cycle
  3. corrupt the measured current with a growing bias (ramp + step) and noise
  4. run baseline EKF vs bias-augmented EKF
  5. plot SOC error and bias tracking; print an RMSE summary table

Outputs land in ./output (PNG + PDF per figure, plus summary.csv).
"""

import os
import numpy as np
import matplotlib.pyplot as plt

# Anchor all paths to this script's location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, 'output')
STYLE_FILE = os.path.join(SCRIPT_DIR, 'journal.mplstyle')

plt.style.use(STYLE_FILE)

from plant import (PARAMETER_SETS, characterize_ocv, identify_ecm,
                   run_cc_discharge, run_cccv_cycle)
from sensors import bias_profile, corrupt_measurements
from observers import OCV, run_ekf

mm = 1 / 25.4

# ---------------- scenario configuration ----------------
SIGMA_I = 0.02          # current noise std [A]
SIGMA_V = 0.002         # voltage noise std [V]
BIAS_KIND = "ramp+step"
BIAS_RATE_A_PER_H = 0.15   # slow drift: 150 mA per hour of operation
BIAS_STEP_A = 0.10         # additional calibration-like step fault at mid-record
SOC0_GUESS = 0.90          # deliberately wrong initial SOC (truth starts at 1.0)
Q_B = 1e-8                 # bias random-walk process noise (tuning knob)
R_V_EFF = 2.5e-5           # effective voltage variance (inflated vs sensor noise
                           # to absorb residual ECM model error)
# ---------------------------------------------------------


def true_soc(profile, q_ah):
    return 1.0 - profile["dch_Ah"] / q_ah


def run_case(pset, profile_name, profile, ocv_obj, ecm):
    t, i_true, v_true = profile["t"], profile["I"], profile["V"]
    soc_true = true_soc(profile, ecm["Q_Ah"])

    b_true = bias_profile(t, kind=BIAS_KIND,
                          rate=BIAS_RATE_A_PER_H / 3600.0,
                          b_step=BIAS_STEP_A)
    i_meas, v_meas = corrupt_measurements(t, i_true, v_true, b_true,
                                          sigma_i=SIGMA_I, sigma_v=SIGMA_V)

    common = dict(ecm=ecm, ocv=ocv_obj, x0=[SOC0_GUESS, 0.0],
                  q_b=Q_B, r_v=R_V_EFF)
    res_base = run_ekf(t, i_meas, v_meas, augmented=False, **common)
    res_aug = run_ekf(t, i_meas, v_meas, augmented=True, **common)

    def rmse(x, y, skip=60):  # skip initial transient (60 s)
        return float(np.sqrt(np.mean((x[skip:] - y[skip:]) ** 2)))

    stats = {
        "param_set": pset, "profile": profile_name,
        "soc_rmse_baseline_pct": 100 * rmse(res_base["soc"], soc_true),
        "soc_rmse_augmented_pct": 100 * rmse(res_aug["soc"], soc_true),
        "bias_rmse_A": rmse(res_aug["b"], b_true),
        "final_bias_true_A": float(b_true[-1]),
        "final_bias_est_A": float(res_aug["b"][-1]),
    }

    # ---------------- figure ----------------
    W, H = 170, 110
    fig, axes = plt.subplots(2, 2, figsize=(W * mm, H * mm), layout='constrained')
    th = t / 3600.0

    ax = axes[0, 0]
    ax.plot(th, v_true, label='plant (SPMe)')
    ax.plot(th, v_meas, alpha=0.4, lw=0.5, label='measured')
    ax.set_xlabel('Time [h]'); ax.set_ylabel('Voltage [V]')
    ax.legend(frameon=False, fontsize=6)

    ax = axes[0, 1]
    ax.plot(th, 100 * soc_true, 'k', label='true')
    ax.plot(th, 100 * res_base["soc"], label='EKF baseline')
    ax.plot(th, 100 * res_aug["soc"], label='EKF + bias state')
    ax.set_xlabel('Time [h]'); ax.set_ylabel('SOC [%]')
    ax.legend(frameon=False, fontsize=6)

    ax = axes[1, 0]
    ax.plot(th, 100 * (res_base["soc"] - soc_true), label='baseline')
    ax.plot(th, 100 * (res_aug["soc"] - soc_true), label='+ bias state')
    ax.axhline(0, color='k', lw=0.5)
    ax.set_xlabel('Time [h]'); ax.set_ylabel('SOC error [%]')
    ax.legend(frameon=False, fontsize=6)

    ax = axes[1, 1]
    ax.plot(th, b_true, 'k', label='true bias')
    ax.plot(th, res_aug["b"], label='estimated')
    sig = np.sqrt(np.maximum(res_aug["p_b"], 0))
    ax.fill_between(th, res_aug["b"] - 2 * sig, res_aug["b"] + 2 * sig,
                    alpha=0.2, lw=0)
    ax.set_xlabel('Time [h]'); ax.set_ylabel('Current bias [A]')
    ax.legend(frameon=False, fontsize=6)

    for ax in axes.ravel():
        ax.spines[['top', 'right']].set_visible(False)

    fig.suptitle(f'{pset} — {profile_name}', fontsize=8)
    base = os.path.join(OUTPUT_DIR, f'{pset}_{profile_name}')
    fig.savefig(base + '.png')
    fig.savefig(base + '.pdf')
    plt.close(fig)
    return stats


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows = []
    for pset in PARAMETER_SETS:
        print(f'=== {pset} ===')
        soc_grid, ocv_vals, q_usable = characterize_ocv(pset)
        ocv_obj = OCV(soc_grid, ocv_vals)
        profiles = {
            'CC_discharge_1C': run_cc_discharge(pset),
            'CCCV_cycle': run_cccv_cycle(pset),
        }
        # calibrate ECM on bias-free commissioning data (the CC discharge)
        ecm = identify_ecm(pset, q_usable, ocv_fun=ocv_obj,
                           commissioning=profiles['CC_discharge_1C'])
        print(f'  ECM: R0={ecm["R0"]*1e3:.2f} mOhm, R1={ecm["R1"]*1e3:.2f} mOhm, '
              f'tau1={ecm["tau1"]:.0f} s, R2={ecm["R2"]*1e3:.2f} mOhm, '
              f'tau2={ecm["tau2"]:.0f} s, Q={ecm["Q_Ah"]:.2f} Ah, '
              f'fit RMS={ecm["fit_rms_mV"]:.1f} mV')
        for name, prof in profiles.items():
            stats = run_case(pset, name, prof, ocv_obj, ecm)
            rows.append(stats)
            print(f'  {name}: SOC RMSE baseline={stats["soc_rmse_baseline_pct"]:.2f}% '
                  f'| augmented={stats["soc_rmse_augmented_pct"]:.2f}% '
                  f'| bias RMSE={stats["bias_rmse_A"]*1e3:.1f} mA')

    # summary CSV
    import csv
    csv_path = os.path.join(OUTPUT_DIR, 'summary.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f'\nSummary written to {csv_path}')


if __name__ == '__main__':
    main()

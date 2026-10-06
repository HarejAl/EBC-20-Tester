"""
run_experiments.py — run the full experimental campaign and store raw results.

Design: a realistic BASELINE scenario plus one-factor-at-a-time sweeps:
  chemistry x profile     3 parameter sets x {CC, CC-CV, DST}
  temperature             10 / 25 / 40 degC          (OKane2022, DST)
  DST peak C-rate         1C / 2C / 3C               (OKane2022)
  depth of discharge      50 / 80 / 95 %             (OKane2022, DST)
  current-bias drift      0.005 / 0.02 / 0.05 A/h    (realistic, slow)
  abrupt step fault       +0.10 A mid-test           (calibration fault)
  sensor age b0           0 / 0.10 / 0.25 A
  voltage-sensor bias     5 mV with observer variants ('v', 'i', 'iv')
  battery aging (SOH)     100 / 95 / 90 / 85 %       (LAM + resistance
                          growth; observer keeps the FRESH calibration)

Realistic sensor-degradation magnitudes (Hall/shunt literature): accumulated
offset b0 of order 0.5-1 % of a 15-25 A full scale (~0.1 A here) and a slow
within-test drift of tens of mA/h at most (thermally driven).

Each case produces results/<case>.npz (full time series) and one row in
results/index.csv. Plant calibration (OCV + ECM) is cached per (parameter
set, temperature) and is ALWAYS performed on the fresh cell.

External signals: set cfg['profile_file'] to an .npz path (see
plant.load_profile_npz for the expected format) to bypass the PyBaMM plant.

Run:  python run_experiments.py
"""

import os
import json
import csv
import numpy as np

# Anchor all paths to this script's location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'results')
CALIB_DIR = os.path.join(RESULTS_DIR, 'calibration')

from plant import (characterize_ocv, identify_ecm, run_cc_discharge,
                   run_cccv_cycle, run_dst, load_profile_npz)
from sensors import bias_profile, voltage_bias_profile, corrupt_measurements
from observers import OCV, run_ekf

# ---------------- global settings ----------------
SIGMA_I = 0.02        # current sensor noise std [A]
SIGMA_V = 0.002       # voltage sensor noise std [V]
Q_B = 1e-8            # EKF current-bias random-walk process noise
Q_BV = 1e-12          # EKF voltage-bias random-walk process noise
R_V_EFF = 2.5e-5      # effective voltage variance (inflated for model error)
SOC0_GUESS = 0.90     # deliberately wrong observer initialization
SKIP_S = 120          # metric transient skip [s]

BASE = dict(profile='dst', temp_C=25.0, peak_c=2.0, dod=0.80,
            bias_kind='ramp', b0=0.10, rate_A_per_h=0.02, b_step_A=0.0,
            vbias_mV=0.0, observer='i', soh=1.0, delta_r_ohm=None,
            profile_file=None, seed=1)
# --------------------------------------------------


def build_cases():
    cases = {}

    def add(name, **over):
        cases[name] = {**BASE, **over}

    # chemistry x profile at baseline conditions
    for pset in ["Mohtat2020", "Ai2020", "OKane2022"]:
        add(f'{pset}_cc',   pset=pset, profile='cc')
        add(f'{pset}_cccv', pset=pset, profile='cccv')
        add(f'{pset}_dst',  pset=pset)

    # operating-condition sweeps on OKane2022 / DST
    for T in [10.0, 40.0]:
        add(f'OKane2022_dst_T{int(T)}', pset='OKane2022', temp_C=T)
    for pc in [1.0, 3.0]:
        add(f'OKane2022_dst_peak{int(pc)}C', pset='OKane2022', peak_c=pc)
    for dod in [0.50, 0.95]:
        add(f'OKane2022_dst_dod{int(100 * dod)}', pset='OKane2022', dod=dod)

    # sensor-degradation sweeps (realistic magnitudes)
    for r in [0.005, 0.05]:
        add(f'OKane2022_dst_drift{str(r).replace(".", "p")}',
            pset='OKane2022', rate_A_per_h=r)
    add('OKane2022_dst_stepfault', pset='OKane2022', bias_kind='ramp+step',
        b_step_A=0.10)
    for b0 in [0.0, 0.25]:
        add(f'OKane2022_dst_age{str(b0).replace(".", "p")}',
            pset='OKane2022', b0=b0)

    # voltage-sensor bias with matching / mismatched / both observer states
    add('OKane2022_dst_vbias5mV_obsV', pset='OKane2022', vbias_mV=5.0,
        b0=0.0, rate_A_per_h=0.0, observer='v')
    add('OKane2022_dst_vbias5mV_obsI', pset='OKane2022', vbias_mV=5.0,
        b0=0.0, rate_A_per_h=0.0, observer='i')
    add('OKane2022_dst_bothbias_obsIV', pset='OKane2022', vbias_mV=5.0,
        observer='iv')

    # battery-aging sweep: aged plant, FRESH observer calibration
    # (soh100 duplicates the baseline scenario under its stage name so the
    # aging figures have a fresh-cell anchor point)
    for soh in [1.00, 0.95, 0.90, 0.85, 0.80]:
        add(f'OKane2022_dst_soh{int(round(100 * soh))}', pset='OKane2022',
            soh=soh)

    return cases


# ---------------- calibration cache ----------------
_calib_cache = {}


def get_calibration(pset, temp_C):
    key = (pset, round(temp_C, 1))
    if key in _calib_cache:
        return _calib_cache[key]
    fname = os.path.join(CALIB_DIR, f'{pset}_T{key[1]}.npz')
    if os.path.exists(fname):
        d = np.load(fname, allow_pickle=True)
        calib = (OCV(d['soc_grid'], d['ocv']), json.loads(str(d['ecm'])))
    else:
        print(f'  [calibration] {pset} @ {temp_C} degC ...')
        soc_grid, ocv_vals, q = characterize_ocv(pset, temperature_C=temp_C)
        ocv_obj = OCV(soc_grid, ocv_vals)
        ecm = identify_ecm(pset, q, ocv_fun=ocv_obj, temperature_C=temp_C)
        np.savez(fname, soc_grid=soc_grid, ocv=ocv_vals, ecm=json.dumps(ecm))
        calib = (ocv_obj, ecm)
        print(f'    ECM: R0={ecm["R0"]*1e3:.2f} mOhm, '
              f'fit RMS={ecm["fit_rms_mV"]:.1f} mV')
    _calib_cache[key] = calib
    return calib


def get_profile(cfg, ecm_fresh):
    if cfg['profile_file']:
        return load_profile_npz(cfg['profile_file'])
    aging = None
    if cfg['soh'] < 1.0:
        dr = (cfg['delta_r_ohm'] if cfg['delta_r_ohm'] is not None
              else 2.0 * ecm_fresh['R0'] * (1.0 - cfg['soh']))
        aging = dict(soh=cfg['soh'], delta_r_ohm=dr)
    if cfg['profile'] == 'cc':
        return run_cc_discharge(cfg['pset'], temperature_C=cfg['temp_C'],
                                aging=aging)
    if cfg['profile'] == 'cccv':
        return run_cccv_cycle(cfg['pset'], temperature_C=cfg['temp_C'],
                              aging=aging)
    return run_dst(cfg['pset'], peak_c_rate=cfg['peak_c'], dod=cfg['dod'],
                   temperature_C=cfg['temp_C'],
                   q_usable_ah=ecm_fresh['Q_Ah'], aging=aging)


def run_case(name, cfg):
    ocv_obj, ecm = get_calibration(cfg['pset'], cfg['temp_C'])
    prof = get_profile(cfg, ecm)
    t, i_true, v_true = prof['t'], prof['I'], prof['V']
    # true SOC via coulomb counting of the true current, anchored at the
    # profile's true initial SOC and referenced to the AGED capacity
    # (capacity scales ~ with SOH under the LAM emulation); the observer
    # keeps the fresh calibration throughout.
    soc0 = prof.get('soc0', 1.0)
    q_true = ecm['Q_Ah'] * prof.get('soh', 1.0)
    soc_true = np.clip(soc0 - prof['dch_Ah'] / q_true, 0.0, 1.1)

    b_true = bias_profile(t, kind=cfg['bias_kind'], b0=cfg['b0'],
                          rate=cfg['rate_A_per_h'] / 3600.0,
                          b_step=cfg['b_step_A'])
    bv = voltage_bias_profile(t, bv0=cfg['vbias_mV'] * 1e-3)
    i_meas, v_meas = corrupt_measurements(t, i_true, v_true, b_true, bv,
                                          sigma_i=SIGMA_I, sigma_v=SIGMA_V,
                                          seed=cfg['seed'])

    common = dict(ecm=ecm, ocv=ocv_obj, x0=[SOC0_GUESS],
                  q_bi=Q_B, q_bv=Q_BV, r_v=R_V_EFF)
    res_base = run_ekf(t, i_meas, v_meas, augment=None, **common)
    res_aug = run_ekf(t, i_meas, v_meas, augment=cfg['observer'], **common)
    res_iv = run_ekf(t, i_meas, v_meas, augment='iv', **common)

    skip = np.searchsorted(t, t[0] + SKIP_S)

    def rmse(x, y):
        return float(np.sqrt(np.mean((x[skip:] - y[skip:]) ** 2)))

    row = {
        'case': name, **{k: cfg[k] for k in
                         ('pset', 'profile', 'temp_C', 'peak_c', 'dod',
                          'bias_kind', 'b0', 'rate_A_per_h', 'b_step_A',
                          'vbias_mV', 'observer', 'soh')},
        'soc_rmse_baseline_pct': 100 * rmse(res_base['soc'], soc_true),
        'soc_rmse_augmented_pct': 100 * rmse(res_aug['soc'], soc_true),
        'soc_rmse_aug_iv_pct': 100 * rmse(res_iv['soc'], soc_true),
        'soc_maxerr_baseline_pct': 100 * float(np.max(np.abs(
            res_base['soc'][skip:] - soc_true[skip:]))),
        'soc_maxerr_augmented_pct': 100 * float(np.max(np.abs(
            res_aug['soc'][skip:] - soc_true[skip:]))),
        'bias_rmse_mA': 1e3 * rmse(res_aug['b'], b_true),
        'final_bias_true_A': float(b_true[-1]),
        'final_bias_est_A': float(res_aug['b'][-1]),
        'final_vbias_est_mV': 1e3 * float(res_aug['bv'][-1]),
        'duration_h': float((t[-1] - t[0]) / 3600.0),
    }

    np.savez(os.path.join(RESULTS_DIR, f'{name}.npz'),
             t=t, I_true=i_true, V_true=v_true, I_meas=i_meas, V_meas=v_meas,
             b_true=b_true, bv=bv, soc_true=soc_true,
             soc_base=res_base['soc'], soc_aug=res_aug['soc'],
             b_hat=res_aug['b'], p_b=res_aug['p_b'],
             bv_hat=res_aug['bv'], p_bv=res_aug['p_bv'],
             soc_iv=res_iv['soc'], b_iv=res_iv['b'], bv_iv=res_iv['bv'],
             p_soc=res_aug['p_soc'],
             meta=json.dumps({**cfg, 'ecm': ecm}))
    return row


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(CALIB_DIR, exist_ok=True)
    cases = build_cases()
    rows = []
    for k, (name, cfg) in enumerate(cases.items(), 1):
        print(f'[{k}/{len(cases)}] {name}')
        try:
            row = run_case(name, cfg)
            rows.append(row)
            print(f'  SOC RMSE: baseline={row["soc_rmse_baseline_pct"]:.2f}% '
                  f'| augmented={row["soc_rmse_augmented_pct"]:.2f}% '
                  f'| bias RMSE={row["bias_rmse_mA"]:.0f} mA')
        except Exception as e:
            print(f'  FAILED: {e}')

    with open(os.path.join(RESULTS_DIR, 'index.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f'\n{len(rows)} cases written to {RESULTS_DIR}')


if __name__ == '__main__':
    main()

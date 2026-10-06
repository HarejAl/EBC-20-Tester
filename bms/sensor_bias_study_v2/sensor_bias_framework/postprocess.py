"""
postprocess.py — generate all paper figures and tables from ./results.

Figures (PDF + PNG) are written to ./paper/figures and the auto-generated
LaTeX summary table to ./paper/tables. Run after run_experiments.py:

    python postprocess.py
"""

import os
import json
import csv
import numpy as np
import matplotlib.pyplot as plt

# Anchor all paths to this script's location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, 'results')
FIG_DIR = os.path.join(SCRIPT_DIR, 'paper', 'figures')
TAB_DIR = os.path.join(SCRIPT_DIR, 'paper', 'tables')
STYLE_FILE = os.path.join(SCRIPT_DIR, 'journal.mplstyle')

plt.style.use(STYLE_FILE)
mm = 1 / 25.4

from plant import DST_BASE, DST_PEAK, DST_PERIOD_S, dst_current_array

C_BASE = 'tab:red'
C_AUG = 'tab:blue'


def load_index():
    with open(os.path.join(RESULTS_DIR, 'index.csv')) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            try:
                r[k] = float(v)
            except ValueError:
                pass
    return {r['case']: r for r in rows}


def load_case(name):
    d = np.load(os.path.join(RESULTS_DIR, f'{name}.npz'), allow_pickle=True)
    return d


def savefig(fig, name):
    fig.savefig(os.path.join(FIG_DIR, name + '.pdf'))
    fig.savefig(os.path.join(FIG_DIR, name + '.png'))
    plt.close(fig)
    print(f'  wrote {name}.pdf/.png')


# --------------------------------------------------------------------------

def fig_dst_profile():
    """One DST period: normalized and scaled to a 2C peak for a 5 Ah cell."""
    t, i = dst_current_array(q_nom_ah=5.0, peak_c_rate=2.0, n_rep=2)
    W, H = 85, 60
    fig, ax = plt.subplots(figsize=(W * mm, H * mm), layout='constrained')
    ax.step(t, i / 5.0, where='post')          # in C-rate units
    ax.axhline(0, color='k', lw=0.5)
    ax.set_xlabel('Time [s]')
    ax.set_ylabel('Current [C-rate]')
    ax.spines[['top', 'right']].set_visible(False)
    savefig(fig, 'fig_dst_profile')


def fig_ocv():
    W, H = 85, 65
    fig, ax = plt.subplots(figsize=(W * mm, H * mm), layout='constrained')
    for pset in ['Mohtat2020', 'Ai2020', 'OKane2022']:
        d = np.load(os.path.join(RESULTS_DIR, 'calibration',
                                 f'{pset}_T25.0.npz'), allow_pickle=True)
        ax.plot(100 * d['soc_grid'], d['ocv'], label=pset)
    ax.set_xlabel('SOC [%]')
    ax.set_ylabel('Pseudo-OCV [V]')
    ax.legend(frameon=False, fontsize=6)
    ax.spines[['top', 'right']].set_visible(False)
    savefig(fig, 'fig_ocv')


def fig_timeseries(case='OKane2022_dst', tag='okane_dst'):
    d = load_case(case)
    t = d['t'] / 3600.0
    W, H = 170, 110
    fig, axes = plt.subplots(2, 2, figsize=(W * mm, H * mm),
                             layout='constrained')
    ax = axes[0, 0]
    ax.plot(t, d['I_true'], lw=0.4, label='true')
    ax.plot(t, d['I_meas'], lw=0.4, alpha=0.5, label='measured')
    ax.set_xlabel('Time [h]'); ax.set_ylabel('Current [A]')
    ax.legend(frameon=False, fontsize=6)

    ax = axes[0, 1]
    ax.plot(t, 100 * d['soc_true'], 'k', label='true')
    ax.plot(t, 100 * d['soc_base'], color=C_BASE, label='EKF baseline')
    ax.plot(t, 100 * d['soc_aug'], color=C_AUG, label='EKF + bias state')
    ax.set_xlabel('Time [h]'); ax.set_ylabel('SOC [%]')
    ax.legend(frameon=False, fontsize=6)

    ax = axes[1, 0]
    ax.plot(t, 100 * (d['soc_base'] - d['soc_true']), color=C_BASE,
            label='baseline')
    ax.plot(t, 100 * (d['soc_aug'] - d['soc_true']), color=C_AUG,
            label='+ bias state')
    ax.axhline(0, color='k', lw=0.5)
    ax.set_xlabel('Time [h]'); ax.set_ylabel('SOC error [%]')
    ax.legend(frameon=False, fontsize=6)

    ax = axes[1, 1]
    ax.plot(t, d['b_true'], 'k', label='true bias')
    ax.plot(t, d['b_hat'], color=C_AUG, label='estimated')
    sig = np.sqrt(np.maximum(d['p_b'], 0))
    ax.fill_between(t, d['b_hat'] - 2 * sig, d['b_hat'] + 2 * sig,
                    color=C_AUG, alpha=0.2, lw=0)
    ax.set_xlabel('Time [h]'); ax.set_ylabel('Current bias [A]')
    ax.legend(frameon=False, fontsize=6)

    for ax in axes.ravel():
        ax.spines[['top', 'right']].set_visible(False)
    savefig(fig, f'fig_timeseries_{tag}')


def _bars(ax, labels, base, aug, xlabel):
    x = np.arange(len(labels))
    w = 0.38
    ax.bar(x - w / 2, base, w, color=C_BASE, label='baseline')
    ax.bar(x + w / 2, aug, w, color=C_AUG, label='+ bias state')
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=6)
    ax.set_xlabel(xlabel)
    ax.set_ylabel('SOC RMSE [%]')
    ax.spines[['top', 'right']].set_visible(False)


def fig_chemistry(idx):
    """3 chemistries x 3 profiles, baseline vs augmented."""
    W, H = 170, 60
    fig, axes = plt.subplots(1, 3, figsize=(W * mm, H * mm),
                             layout='constrained', sharey=True)
    profs = [('cc', 'CC 1C'), ('cccv', 'CC-CV cycle'), ('dst', 'DST 2C')]
    psets = ['Mohtat2020', 'Ai2020', 'OKane2022']
    for ax, (p, title) in zip(axes, profs):
        base = [idx[f'{ps}_{p}']['soc_rmse_baseline_pct'] for ps in psets]
        aug = [idx[f'{ps}_{p}']['soc_rmse_augmented_pct'] for ps in psets]
        _bars(ax, [ps.replace('20', "'") for ps in psets], base, aug, '')
        ax.set_title(title, fontsize=7)
    axes[0].legend(frameon=False, fontsize=6)
    for ax in axes[1:]:
        ax.set_ylabel('')
    savefig(fig, 'fig_chemistry')


def fig_sweeps(idx):
    """One-factor-at-a-time sweeps (OKane2022, DST)."""
    W, H = 170, 110
    fig, axes = plt.subplots(2, 2, figsize=(W * mm, H * mm),
                             layout='constrained')

    def get(names):
        return ([idx[n]['soc_rmse_baseline_pct'] for n in names],
                [idx[n]['soc_rmse_augmented_pct'] for n in names])

    b, a = get(['OKane2022_dst_T10', 'OKane2022_dst', 'OKane2022_dst_T40'])
    _bars(axes[0, 0], ['10', '25', '40'], b, a, 'Temperature [$^\\circ$C]')
    axes[0, 0].legend(frameon=False, fontsize=6)

    b, a = get(['OKane2022_dst_peak1C', 'OKane2022_dst',
                'OKane2022_dst_peak3C'])
    _bars(axes[0, 1], ['1C', '2C', '3C'], b, a, 'DST peak C-rate')

    b, a = get(['OKane2022_dst_drift0p005', 'OKane2022_dst',
                'OKane2022_dst_drift0p05'])
    _bars(axes[1, 0], ['0.005', '0.02', '0.05'], b, a,
          'Bias drift rate [A/h]')

    b, a = get(['OKane2022_dst_age0p0', 'OKane2022_dst',
                'OKane2022_dst_age0p25'])
    _bars(axes[1, 1], ['0.0', '0.10', '0.25'], b, a,
          'Initial bias $b_0$ [A] (sensor age)')
    savefig(fig, 'fig_sweeps')


def fig_bias_tracking():
    """Bias estimate vs true for slow drift and step-fault scenarios."""
    W, H = 85, 70
    fig, ax = plt.subplots(figsize=(W * mm, H * mm), layout='constrained')
    for name, lab in [('OKane2022_dst_drift0p005', '0.005 A/h'),
                      ('OKane2022_dst', '0.02 A/h'),
                      ('OKane2022_dst_stepfault', '0.02 A/h + step')]:
        d = load_case(name)
        t = d['t'] / 3600.0
        ln, = ax.plot(t, d['b_hat'], label=lab, lw=0.8)
        ax.plot(t, d['b_true'], '--', color=ln.get_color(), lw=0.6)
    ax.set_xlabel('Time [h]')
    ax.set_ylabel('Current bias [A]')
    ax.legend(frameon=False, fontsize=6)
    ax.spines[['top', 'right']].set_visible(False)
    savefig(fig, 'fig_bias_tracking')


def fig_vbias(idx):
    """Voltage-bias scenarios: observer-variant comparison."""
    W, H = 85, 70
    fig, ax = plt.subplots(figsize=(W * mm, H * mm), layout='constrained')
    for name, lab in [('OKane2022_dst_vbias5mV_obsV',
                       '$b_v$ inj., $b_v$ state'),
                      ('OKane2022_dst_vbias5mV_obsI',
                       '$b_v$ inj., $b_i$ state'),
                      ('OKane2022_dst_bothbias_obsIV',
                       'both inj., both states')]:
        d = load_case(name)
        ax.plot(d['t'] / 3600.0, 100 * (d['soc_aug'] - d['soc_true']),
                label=lab, lw=0.7)
    ax.axhline(0, color='k', lw=0.5)
    ax.set_xlabel('Time [h]')
    ax.set_ylabel('SOC error, augmented EKF [%]')
    ax.legend(frameon=False, fontsize=6)
    ax.spines[['top', 'right']].set_visible(False)
    savefig(fig, 'fig_vbias')


def fig_aging(idx):
    """Joint battery-aging x sensor-aging campaign: SOC RMSE vs SOH."""
    stages = [100, 95, 90, 85, 80]
    names = [f'OKane2022_dst_soh{s}' for s in stages]
    base = [idx[n]['soc_rmse_baseline_pct'] for n in names]
    aug_i = [idx[n]['soc_rmse_augmented_pct'] for n in names]
    aug_iv = [idx[n]['soc_rmse_aug_iv_pct'] for n in names]
    W, H = 85, 70
    fig, ax = plt.subplots(figsize=(W * mm, H * mm), layout='constrained')
    ax.plot(stages, base, 'o-', color=C_BASE, label='baseline EKF')
    ax.plot(stages, aug_i, 's-', color=C_AUG, label='EKF + $b_i$ state')
    ax.plot(stages, aug_iv, '^-', color='tab:green',
            label='EKF + $b_i$, $b_v$ states')
    ax.invert_xaxis()
    ax.set_xlabel('Battery state of health [%]')
    ax.set_ylabel('SOC RMSE [%]')
    ax.legend(frameon=False, fontsize=6)
    ax.spines[['top', 'right']].set_visible(False)
    savefig(fig, 'fig_aging')


def fig_aging_bias():
    """Lumped-bias estimate across life stages: the estimated b_i absorbs
    aging-induced disturbances beyond the injected sensor offset."""
    stages = [100, 95, 90, 85, 80]
    W, H = 85, 70
    fig, ax = plt.subplots(figsize=(W * mm, H * mm), layout='constrained')
    b_inj, b_est = [], []
    for s_ in stages:
        d = load_case(f'OKane2022_dst_soh{s_}')
        n = len(d['t'])
        b_inj.append(float(np.mean(d['b_true'][n // 2:])))
        b_est.append(float(np.mean(d['b_hat'][n // 2:])))
    ax.plot(stages, b_inj, 'o-', color='k', label='injected sensor offset')
    ax.plot(stages, b_est, 's-', color=C_AUG,
            label='estimated lumped bias')
    ax.invert_xaxis()
    ax.set_xlabel('Battery state of health [%]')
    ax.set_ylabel('Mean current bias, 2nd half of test [A]')
    ax.legend(frameon=False, fontsize=6)
    ax.spines[['top', 'right']].set_visible(False)
    savefig(fig, 'fig_aging_bias')


def table_summary(idx):
    """Auto-generate the LaTeX summary table of all cases."""
    lines = [
        '% Auto-generated by postprocess.py -- do not edit by hand',
        '\\begin{table*}[t]',
        '\\centering',
        '\\caption{Summary of all simulation cases. RMSE values computed '
        'after a 120\\,s transient. Baseline: EKF without bias state; '
        'augmented: EKF with current-bias state.}',
        '\\label{tab:summary}',
        '\\small',
        '\\begin{tabular}{llrrrrrrr}',
        '\\hline',
        'Case & Profile & $T$ [$^\\circ$C] & drift [A/h] & $b_0$ [A] & '
        '$b_v$ [mV] & RMSE$_\\mathrm{base}$ [\\%] & '
        'RMSE$_\\mathrm{aug}$ [\\%] & bias RMSE [mA] \\\\',
        '\\hline',
    ]
    for name, r in idx.items():
        lines.append(
            f"{name.replace('_', '\\_')} & {r['profile']} & "
            f"{r['temp_C']:.0f} & {r['rate_A_per_h']:.2f} & {r['b0']:.1f} & "
            f"{r['vbias_mV']:.0f} & {r['soc_rmse_baseline_pct']:.2f} & "
            f"{r['soc_rmse_augmented_pct']:.2f} & "
            f"{r['bias_rmse_mA']:.0f} \\\\")
    lines += ['\\hline', '\\end{tabular}', '\\end{table*}', '']
    path = os.path.join(TAB_DIR, 'tab_summary.tex')
    with open(path, 'w') as f:
        f.write('\n'.join(lines))
    print(f'  wrote tables/tab_summary.tex')


def main():
    os.makedirs(FIG_DIR, exist_ok=True)
    os.makedirs(TAB_DIR, exist_ok=True)
    idx = load_index()
    print('Generating figures...')
    fig_dst_profile()
    fig_ocv()
    fig_timeseries('OKane2022_dst', 'okane_dst')
    fig_timeseries('Ai2020_cccv', 'ai_cccv')
    fig_chemistry(idx)
    fig_sweeps(idx)
    fig_bias_tracking()
    fig_vbias(idx)
    fig_aging(idx)
    fig_aging_bias()
    table_summary(idx)
    print('Done.')


if __name__ == '__main__':
    main()

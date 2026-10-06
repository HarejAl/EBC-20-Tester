"""
plant.py — PyBaMM "plant" (virtual battery) for the sensor-bias observer study.

The plant is an SPMe model (physics-based), deliberately different in
structure from the ECM used by the observer. Supports three parameter sets,
arbitrary isothermal temperature, and emulated battery aging.

Aging emulation (aging = dict(soh=..., delta_r_ohm=...)):
  - loss of active material (LAM): both electrode active-material volume
    fractions scaled by `soh`  ->  capacity fade approximately proportional
  - ohmic resistance growth (SEI / contact / interconnect): `delta_r_ohm`
    applied as an exact series resistance on the terminal voltage

Provides:
  characterize_ocv()  pseudo-OCV(SOC), mean of C/25 charge/discharge branches
  identify_ecm()      LS calibration of a 2-RC ECM on bias-free data
  run_cc_discharge()  CC discharge
  run_cccv_cycle()    CC discharge + rest + CC-CV charge + rest
  run_dst()           CC-CV charge followed by scaled DST until target DoD
  load_profile_npz()  hook for externally generated signals
"""

import os
import numpy as np
import pybamm

# Anchor all paths to this script's location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PARAMETER_SETS = ["Mohtat2020", "Ai2020", "OKane2022"]

# DST base profile (USABC-style). User convention: current < 0 = discharge.
DST_BASE = [
    (0.00, 8), (-0.10, 8), (-0.25, 12), (0.15, 8), (-0.40, 10),
    (-0.20, 12), (0.10, 6), (-0.50, 8), (0.00, 8), (-0.15, 12),
]
DST_PERIOD_S = sum(d for _, d in DST_BASE)          # 92 s
DST_PEAK = max(abs(c) for c, _ in DST_BASE)          # 0.5


def _vmin_string(parameter_set):
    return "3.0 V" if parameter_set == "Ai2020" else "2.8 V"


def _params(parameter_set, temperature_C=25.0, aging=None):
    p = pybamm.ParameterValues(parameter_set)
    T = 273.15 + temperature_C
    p["Ambient temperature [K]"] = T
    p["Initial temperature [K]"] = T
    if aging is not None and aging.get("soh", 1.0) < 1.0:
        p["Negative electrode active material volume fraction"] *= aging["soh"]
        p["Positive electrode active material volume fraction"] *= aging["soh"]
    return p


def _solve(parameter_set, experiment, initial_soc=None, temperature_C=25.0,
           aging=None):
    model = pybamm.lithium_ion.SPMe()
    params = _params(parameter_set, temperature_C, aging)
    sim = pybamm.Simulation(model, parameter_values=params,
                            experiment=experiment)
    return sim.solve(initial_soc=initial_soc)


def _package(sol, soc0=1.0, aging=None):
    t = sol["Time [s]"].entries
    i = sol["Current [A]"].entries               # > 0 = discharge (PyBaMM)
    v = sol["Terminal voltage [V]"].entries
    dch = sol["Discharge capacity [A.h]"].entries
    out = {"t": t, "I": i, "V": v.copy(), "dch_Ah": dch - dch[0],
           "soc0": soc0, "soh": 1.0}
    if aging is not None:
        out["V"] = v - aging.get("delta_r_ohm", 0.0) * i
        out["soh"] = aging.get("soh", 1.0)
    return out


# --------------------------------------------------------------------------
# Characterization / calibration (always on the FRESH cell)
# --------------------------------------------------------------------------

def characterize_ocv(parameter_set, temperature_C=25.0, n_points=201):
    """Pseudo-OCV(SOC): mean of C/25 discharge and charge branches."""
    vmin = _vmin_string(parameter_set)
    exp = pybamm.Experiment([
        f"Discharge at C/25 until {vmin}",
        "Rest for 30 minutes",
        "Charge at C/25 until 4.2 V",
    ], period="60 seconds")
    sol = _solve(parameter_set, exp, initial_soc=1.0,
                 temperature_C=temperature_C)
    grid = np.linspace(0.0, 1.0, n_points)

    sd = sol.cycles[0].steps[0]
    dch = sd["Discharge capacity [A.h]"].entries
    vd = sd["Terminal voltage [V]"].entries
    q_used = dch[-1] - dch[0]
    soc_d = 1.0 - (dch - dch[0]) / q_used
    ocv_d = np.interp(grid, soc_d[::-1], vd[::-1])

    sc = sol.cycles[2].steps[0]
    dch_c = sc["Discharge capacity [A.h]"].entries
    vc = sc["Terminal voltage [V]"].entries
    soc_c = 1.0 - (dch_c - dch[0]) / q_used
    ocv_c = np.interp(grid, soc_c, vc)

    return grid, 0.5 * (ocv_d + ocv_c), q_used


def identify_ecm(parameter_set, q_usable_ah, ocv_fun, commissioning=None,
                 temperature_C=25.0):
    """
    Calibrate a 2-RC ECM (R0, R1/tau1, R2/tau2) by nonlinear least squares
    against bias-free commissioning data (default: fresh-cell 1C discharge
    at the same temperature). Mimics the realistic workflow: the model is
    calibrated when the pack is new and the instrumentation trusted, then
    deployed while sensors and cell degrade.
    """
    from scipy.optimize import least_squares

    if commissioning is None:
        commissioning = run_cc_discharge(parameter_set,
                                         temperature_C=temperature_C)
    t = commissioning["t"]; i = commissioning["I"]; v = commissioning["V"]
    soc = 1.0 - commissioning["dch_Ah"] / q_usable_ah

    def sim_v(theta):
        r0, r1, tau1, r2, tau2 = theta
        v1 = v2 = 0.0
        out = np.empty_like(v)
        for k in range(len(t)):
            dt = t[k] - t[k - 1] if k > 0 else 1.0
            a1 = np.exp(-dt / tau1); a2 = np.exp(-dt / tau2)
            v1 = a1 * v1 + r1 * (1 - a1) * i[k]
            v2 = a2 * v2 + r2 * (1 - a2) * i[k]
            out[k] = ocv_fun(soc[k]) - r0 * i[k] - v1 - v2
        return out

    mask = soc > 0.05                     # exclude the very steep tail
    res = least_squares(lambda th: (sim_v(th) - v)[mask],
                        x0=[10e-3, 5e-3, 30.0, 10e-3, 600.0],
                        bounds=([1e-4, 1e-5, 1.0, 1e-5, 60.0],
                                [0.5, 0.5, 300.0, 0.5, 6000.0]))
    r0, r1, tau1, r2, tau2 = res.x
    rms_mv = 1e3 * float(np.sqrt(np.mean(res.fun ** 2)))
    return {"R0": float(r0), "R1": float(r1), "tau1": float(tau1),
            "R2": float(r2), "tau2": float(tau2),
            "Q_Ah": float(q_usable_ah), "fit_rms_mV": rms_mv}


# --------------------------------------------------------------------------
# Test profiles
# --------------------------------------------------------------------------

def run_cc_discharge(parameter_set, c_rate=1.0, dt=1.0, temperature_C=25.0,
                     aging=None):
    vmin = _vmin_string(parameter_set)
    exp = pybamm.Experiment([f"Discharge at {c_rate}C until {vmin}"],
                            period=f"{dt} seconds")
    sol = _solve(parameter_set, exp, initial_soc=1.0,
                 temperature_C=temperature_C, aging=aging)
    return _package(sol, soc0=1.0, aging=aging)


def run_cccv_cycle(parameter_set, dt=1.0, temperature_C=25.0, aging=None):
    vmin = _vmin_string(parameter_set)
    exp = pybamm.Experiment([
        f"Discharge at 1C until {vmin}",
        "Rest for 10 minutes",
        "Charge at 0.5C until 4.2 V",
        "Hold at 4.2 V until C/50",
        "Rest for 10 minutes",
    ], period=f"{dt} seconds")
    sol = _solve(parameter_set, exp, initial_soc=1.0,
                 temperature_C=temperature_C, aging=aging)
    return _package(sol, soc0=1.0, aging=aging)


def load_profile_npz(path):
    """
    Hook for externally generated signals (user's own generator or
    experimental data). Expects an .npz with arrays t [s], I [A]
    (> 0 = discharge), V [V], dch_Ah [Ah], plus scalars soc0 and
    (optionally) soh.
    """
    d = np.load(path)
    return {"t": d["t"], "I": d["I"], "V": d["V"], "dch_Ah": d["dch_Ah"],
            "soc0": float(d["soc0"]),
            "soh": float(d["soh"]) if "soh" in d else 1.0}


def dst_current_array(q_nom_ah, peak_c_rate, n_rep, dt=1.0):
    """
    DST drive cycle: (1) normalize the base profile so the peak discharge
    magnitude is 1, (2) scale by peak_c_rate * q_nom_ah, (3) flip sign to
    the PyBaMM convention (> 0 = discharge), (4) sample-and-hold at dt and
    tile n_rep times. Returns (t [s], I [A]).
    """
    i_peak = peak_c_rate * q_nom_ah
    t_one = np.arange(0, DST_PERIOD_S, dt)
    i_one = np.zeros_like(t_one, dtype=float)
    t0 = 0.0
    for c, d in DST_BASE:
        sel = (t_one >= t0) & (t_one < t0 + d)
        i_one[sel] = -(c / DST_PEAK) * i_peak      # sign flip: user -> PyBaMM
        t0 += d
    t = np.concatenate([t_one + k * DST_PERIOD_S for k in range(n_rep)])
    i = np.tile(i_one, n_rep)
    return t, i


def dst_mean_discharge_a(q_nom_ah, peak_c_rate):
    """Mean discharge current of one scaled DST period [A]."""
    i_peak = peak_c_rate * q_nom_ah
    net = sum(-(c / DST_PEAK) * i_peak * d for c, d in DST_BASE)
    return net / DST_PERIOD_S


def run_dst(parameter_set, peak_c_rate=2.0, dod=0.8, dt=1.0,
            temperature_C=25.0, initial_soc=0.8, q_usable_ah=None,
            aging=None):
    """
    CC-CV charge (0.5C / 4.2 V / C-50 cut) followed by DST discharge until
    the target DoD (referenced to the aged usable capacity) or the voltage
    cut-off. The DST amplitude is scaled to the aged capacity so the peak
    C-rate stays meaningful as the cell fades.
    """
    p = _params(parameter_set, temperature_C, aging)
    soh = aging.get("soh", 1.0) if aging is not None else 1.0
    q_nom = p["Nominal cell capacity [A.h]"] * soh
    q_ref = (q_usable_ah if q_usable_ah is not None
             else p["Nominal cell capacity [A.h]"]) * soh

    i_mean = dst_mean_discharge_a(q_nom, peak_c_rate)
    n_rep = int(np.ceil(dod * q_ref * 3600.0 / (i_mean * DST_PERIOD_S)))
    t_dst, i_dst = dst_current_array(q_nom, peak_c_rate, n_rep, dt)
    vmin = _vmin_string(parameter_set).split()[0]
    dst_step = pybamm.step.current(np.column_stack([t_dst, i_dst]),
                                   termination=f"< {vmin} V")
    exp = pybamm.Experiment([
        ("Charge at 0.5C until 4.2 V",
         "Hold at 4.2 V until C/50",
         "Rest for 10 minutes"),
        (dst_step,),
    ], period="10 seconds")
    sol = _solve(parameter_set, exp, initial_soc=initial_soc,
                 temperature_C=temperature_C, aging=aging)
    return _package(sol, soc0=initial_soc, aging=aging)

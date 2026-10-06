"""
sensors.py — sensor degradation models.

Measured signals:
    I_meas(t) = I_true(t) + b_i(t) + w_i(t)
    V_meas(t) = V_true(t) + b_v(t) + w_v(t)

Current-sensor bias b_i(t) models slow degradation in time (sensor ageing,
un-modeled cabling/busbar resistance, thermal drift of Hall sensors):
    b_i(t) = b0 + rate * t  [+ step at t_step]  [+ random walk]
b0 represents the accumulated drift at the start of the test ("sensor age").
b_v is an optional voltage-sensor bias (constant or drifting), used to study
the current/voltage bias ambiguity.
"""

import os
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def bias_profile(t, kind="ramp", b0=0.0, rate=0.0, b_step=0.0, t_step=None,
                 rw_sigma=0.0, seed=0):
    """Current-sensor bias b_i(t) [A]."""
    t = np.asarray(t, dtype=float)
    T = t[-1] - t[0]
    b = np.full_like(t, b0)
    if kind in ("ramp", "ramp+step"):
        b += rate * (t - t[0])
    if kind in ("step", "ramp+step"):
        ts = t_step if t_step is not None else t[0] + 0.5 * T
        b += b_step * (t >= ts)
    if kind == "random_walk":
        rng = np.random.default_rng(seed)
        dt = np.diff(t, prepend=t[0])
        b += np.cumsum(rng.normal(0.0, rw_sigma, size=t.shape)
                       * np.sqrt(np.maximum(dt, 0)))
    return b


def voltage_bias_profile(t, bv0=0.0, rate_v=0.0):
    """Voltage-sensor bias b_v(t) [V] (constant + optional drift)."""
    t = np.asarray(t, dtype=float)
    return bv0 + rate_v * (t - t[0])


def corrupt_measurements(t, i_true, v_true, bias_i, bias_v=0.0,
                         sigma_i=0.02, sigma_v=0.002, seed=1):
    """Apply bias + white noise to the true plant signals."""
    rng = np.random.default_rng(seed)
    i_meas = i_true + bias_i + rng.normal(0.0, sigma_i, size=i_true.shape)
    v_meas = v_true + bias_v + rng.normal(0.0, sigma_v, size=v_true.shape)
    return i_meas, v_meas

"""
observers.py — EKF observers on a 2-state ECM (SOC + one RC branch),
with and without an augmented current-sensor-bias state.

Baseline EKF          x = [SOC, V1]
Bias-augmented EKF    x = [SOC, V1, b]      (b = current sensor bias, random-walk model)

Sign convention: I > 0 = discharge (PyBaMM). The observer only ever sees
I_meas = I_true + b + noise; the augmented filter internally uses
I_hat = I_meas - b_hat.

Measurement model:  V = OCV(SOC) - R0 * I - V1
RC dynamics:        V1' = -V1/tau1 + (R1/tau1) * I
SOC dynamics:       SOC' = -I / (3600 * Q_Ah)
"""

import os
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class OCV:
    """OCV(SOC) lookup with analytical-free derivative via finite differences."""

    def __init__(self, soc_grid, ocv_values):
        self.soc = np.asarray(soc_grid)
        self.ocv = np.asarray(ocv_values)

    def __call__(self, soc):
        return np.interp(soc, self.soc, self.ocv)

    def d_dsoc(self, soc, eps=1e-3):
        lo = np.clip(soc - eps, self.soc[0], self.soc[-1])
        hi = np.clip(soc + eps, self.soc[0], self.soc[-1])
        return (self(hi) - self(lo)) / max(hi - lo, 1e-9)


def run_ekf(t, i_meas, v_meas, ecm, ocv, augment='i',
            x0=None, P0=None, q_soc=1e-10, q_v1=1e-8, q_v2=1e-8,
            q_bi=1e-8, q_bv=1e-12, r_v=2.5e-5, augmented=None):
    """
    EKF on x = [SOC, V1, V2 (, b_i) (, b_v)].

    augment: None (baseline), 'i' (current-sensor bias state), 'v'
    (voltage-sensor bias state), or 'iv' (both; note the observability
    caveat of Zhao et al. 2017). Both bias states are random walks with
    process noises q_bi, q_bv, which set their tracking bandwidths.
    r_v is the effective voltage variance (inflated above the true sensor
    noise to absorb residual model error). `augmented` retained for
    backward compatibility: True -> 'i', False -> None.
    """
    if augmented is not None:
        augment = 'i' if augmented else None
    augment = augment or ''
    has_i = 'i' in augment
    has_v = 'v' in augment
    n = len(t)
    nx = 3 + has_i + has_v
    ix_bi = 3 if has_i else None
    ix_bv = 3 + has_i if has_v else None

    x = np.zeros(nx)
    x[0] = x0[0] if x0 is not None else 0.9
    pdiag = [1e-2, 1e-4, 1e-4] + ([1e-3] if has_i else [])                                + ([1e-6] if has_v else [])
    P = np.diag(pdiag) if P0 is None else P0.copy()
    qdiag = [q_soc, q_v1, q_v2] + ([q_bi] if has_i else [])                                 + ([q_bv] if has_v else [])
    Q = np.diag(qdiag)

    soc_hat = np.zeros(n); bi_hat = np.zeros(n); bv_hat = np.zeros(n)
    p_soc = np.zeros(n); p_bi = np.zeros(n); p_bv = np.zeros(n)
    q_s = 3600.0 * ecm["Q_Ah"]
    r0, r1, r2 = ecm["R0"], ecm["R1"], ecm["R2"]

    for k in range(n):
        dt = t[k] - t[k - 1] if k > 0 else (t[1] - t[0] if n > 1 else 1.0)
        bi = x[ix_bi] if has_i else 0.0
        i_hat = i_meas[k] - bi

        # --- predict ---
        a1 = np.exp(-dt / ecm["tau1"]); a2 = np.exp(-dt / ecm["tau2"])
        soc_p = x[0] - dt * i_hat / q_s
        v1_p = a1 * x[1] + r1 * (1 - a1) * i_hat
        v2_p = a2 * x[2] + r2 * (1 - a2) * i_hat

        F = np.eye(nx)
        F[0, 0] = 1.0; F[1, 1] = a1; F[2, 2] = a2
        if has_i:
            F[0, ix_bi] = dt / q_s
            F[1, ix_bi] = -r1 * (1 - a1)
            F[2, ix_bi] = -r2 * (1 - a2)
        x_new = x.copy()
        x_new[0], x_new[1], x_new[2] = soc_p, v1_p, v2_p
        x = x_new
        x[0] = np.clip(x[0], 0.0, 1.05)
        P = F @ P @ F.T + Q

        # --- update ---
        docv = ocv.d_dsoc(x[0])
        bi = x[ix_bi] if has_i else 0.0
        bv = x[ix_bv] if has_v else 0.0
        i_hat = i_meas[k] - bi
        v_pred = ocv(x[0]) - r0 * i_hat - x[1] - x[2] + bv
        H = np.zeros((1, nx))
        H[0, 0] = docv; H[0, 1] = -1.0; H[0, 2] = -1.0
        if has_i:
            H[0, ix_bi] = r0        # dV/db_i = +R0 (since i_hat = i_m - b_i)
        if has_v:
            H[0, ix_bv] = 1.0
        S = float((H @ P @ H.T).item()) + r_v
        K = (P @ H.T / S).ravel()
        x = x + K * (v_meas[k] - v_pred)
        x[0] = np.clip(x[0], 0.0, 1.05)
        P = (np.eye(nx) - np.outer(K, H)) @ P

        soc_hat[k] = x[0]
        if has_i:
            bi_hat[k] = x[ix_bi]; p_bi[k] = P[ix_bi, ix_bi]
        if has_v:
            bv_hat[k] = x[ix_bv]; p_bv[k] = P[ix_bv, ix_bv]
        p_soc[k] = P[0, 0]

    return {"soc": soc_hat, "b": bi_hat, "bv": bv_hat,
            "p_soc": p_soc, "p_b": p_bi, "p_bv": p_bv}

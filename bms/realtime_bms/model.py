"""SOC-dependent N-RC-branch Thevenin equivalent-circuit model."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class ParameterPoint:
    ocv_v: float
    r0_ohm: float
    r_ohm: tuple[float, ...]
    tau_s: tuple[float, ...]


class LookupTable:
    """Clamped piecewise-linear SOC lookup with finite-difference slopes."""

    def __init__(self, soc, values, name: str):
        x = np.asarray(soc, dtype=float).reshape(-1)
        y = np.asarray(values, dtype=float).reshape(-1)
        if len(x) < 2 or len(x) != len(y):
            raise ValueError(f"{name}: SOC and value arrays need equal length >= 2")
        order = np.argsort(x)
        x, y = x[order], y[order]
        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
            raise ValueError(f"{name}: values must be finite")
        if np.any(np.diff(x) <= 0):
            raise ValueError(f"{name}: SOC points must be unique")
        self.soc, self.values, self.name = x, y, name

    def __call__(self, soc: float) -> float:
        return float(np.interp(float(soc), self.soc, self.values))

    def derivative(self, soc: float, step: float = 1e-4) -> float:
        lo = max(float(soc) - step, float(self.soc[0]))
        hi = min(float(soc) + step, float(self.soc[-1]))
        return (self(hi) - self(lo)) / max(hi - lo, 1e-12)


class TheveninEcmModel:
    """State x=[SOC, V_1, ..., V_N], with positive current defined as discharge.

    V = OCV(SOC) - R0(SOC)*I - sum_i V_i
    SOC(k+1) = SOC(k) - eta(I)*I*dt/(capacity*3600)
    Vi(k+1) = exp(-dt/tau_i)*Vi + Ri*(1-exp(-dt/tau_i))*I

    Branches are ordered fastest (smallest tau) first, matching the HPPC
    characterization tool's lookup convention.
    """

    def __init__(self, soc, ocv_v, r0_ohm, rc_branches,
                 capacity_ah: float, charge_efficiency: float = 1.0):
        """rc_branches: sequence of (r_ohm, tau_s) SOC-aligned value array
        pairs, one pair per RC branch, ordered fastest (smallest tau) first."""
        if not math.isfinite(capacity_ah) or capacity_ah <= 0:
            raise ValueError("capacity_ah must be positive")
        if not 0 < charge_efficiency <= 1:
            raise ValueError("charge_efficiency must be in (0, 1]")
        if len(rc_branches) < 1:
            raise ValueError("at least one RC branch is required")
        self.capacity_ah = float(capacity_ah)
        self.charge_efficiency = float(charge_efficiency)
        self.ocv = LookupTable(soc, ocv_v, "ocv_v")
        self.r0 = LookupTable(soc, r0_ohm, "r0_ohm")
        self.r = [LookupTable(soc, r_ohm, f"r{i + 1}_ohm")
                 for i, (r_ohm, _) in enumerate(rc_branches)]
        self.tau = [LookupTable(soc, tau_s, f"tau{i + 1}_s")
                   for i, (_, tau_s) in enumerate(rc_branches)]
        self.num_branches = len(rc_branches)
        self.state_size = 1 + self.num_branches
        self.soc_min = max(0.0, float(self.ocv.soc[0]))
        self.soc_max = min(1.0, float(self.ocv.soc[-1]))
        for table in (self.r0, *self.r, *self.tau):
            if np.any(table.values <= 0):
                raise ValueError(f"{table.name} values must be positive")

    @classmethod
    def from_json(cls, path: str | Path) -> "TheveninEcmModel":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        table = raw.get("lookup", raw)
        num_branches = raw.get("num_rc_branches", table.get("num_rc_branches"))
        if num_branches is None:
            raise ValueError(
                "model JSON is missing the explicit \"num_rc_branches\" field")
        num_branches = int(num_branches)
        rc_branches = [(table[f"r{i}_ohm"], table[f"tau{i}_s"])
                      for i in range(1, num_branches + 1)]
        return cls(
            soc=table["soc"], ocv_v=table["ocv_v"], r0_ohm=table["r0_ohm"],
            rc_branches=rc_branches,
            capacity_ah=float(raw.get("capacity_ah", table.get("capacity_ah"))),
            charge_efficiency=float(raw.get("charge_efficiency", 1.0)))

    @classmethod
    def from_mat(cls, path: str | Path, capacity_ah: float,
                num_branches: int = 2) -> "TheveninEcmModel":
        """Load the user's MATLAB schema (SOC, Em, R0, Rx, taux).

        Rx and taux may contain more branches than requested; only the first
        `num_branches` (fastest-first, by column order) are used.
        """
        try:
            from scipy.io import loadmat
        except ImportError as exc:
            raise RuntimeError("SciPy is required only for MATLAB ECM files") from exc
        raw = loadmat(path)
        soc = np.asarray(raw["SOC"], dtype=float).reshape(-1)
        ocv = np.asarray(raw["Em"], dtype=float).reshape(-1)
        r0 = np.asarray(raw["R0"], dtype=float).reshape(-1)
        rx = np.asarray(raw["Rx"], dtype=float)
        tau = np.asarray(raw["taux"], dtype=float)
        if rx.shape[0] != len(soc) and rx.shape[1] == len(soc): rx = rx.T
        if tau.shape[0] != len(soc) and tau.shape[1] == len(soc): tau = tau.T
        if rx.shape[1] < num_branches or tau.shape[1] < num_branches:
            raise ValueError(
                f"MAT ECM needs at least {num_branches} Rx and taux branches")
        rc_branches = [(rx[:, i], tau[:, i]) for i in range(num_branches)]
        return cls(soc, ocv, r0, rc_branches, capacity_ah)

    def parameters(self, soc: float) -> ParameterPoint:
        z = float(np.clip(soc, self.soc_min, self.soc_max))
        return ParameterPoint(
            self.ocv(z), self.r0(z),
            tuple(r(z) for r in self.r), tuple(tau(z) for tau in self.tau))

    def transition(self, state, dt_s: float, current_a: float) -> np.ndarray:
        x = np.asarray(state, dtype=float).reshape(self.state_size)
        dt = max(float(dt_s), 0.0)
        p = self.parameters(x[0])
        efficiency = self.charge_efficiency if current_a < 0 else 1.0
        soc = x[0] - efficiency * current_a * dt / (3600 * self.capacity_ah)
        new_state = np.empty(self.state_size)
        new_state[0] = np.clip(soc, 0.0, 1.0)
        for i in range(self.num_branches):
            a = math.exp(-dt / p.tau_s[i])
            new_state[1 + i] = a * x[1 + i] + p.r_ohm[i] * (1 - a) * current_a
        return new_state

    def voltage(self, state, current_a: float) -> float:
        x = np.asarray(state, dtype=float).reshape(self.state_size)
        p = self.parameters(x[0])
        return p.ocv_v - p.r0_ohm * current_a - float(np.sum(x[1:]))

    def transition_jacobian(self, state, dt_s: float, current_a: float) -> np.ndarray:
        return self._numerical_jacobian(
            lambda x: self.transition(x, dt_s, current_a), state, self.state_size)

    def voltage_jacobian(self, state, current_a: float) -> np.ndarray:
        return self._numerical_jacobian(
            lambda x: np.array([self.voltage(x, current_a)]), state, 1)

    @staticmethod
    def _numerical_jacobian(function, state, output_size: int) -> np.ndarray:
        x = np.asarray(state, dtype=float).copy()
        jac = np.empty((output_size, len(x)))
        for column in range(len(x)):
            step = max(abs(x[column])*1e-5, 1e-7)
            hi, lo = x.copy(), x.copy()
            hi[column] += step; lo[column] -= step
            jac[:, column] = (function(hi)-function(lo))/(2*step)
        return jac

    def soc_from_ocv(self, voltage_v: float) -> float:
        order = np.argsort(self.ocv.values)
        return float(np.interp(voltage_v, self.ocv.values[order],
                               self.ocv.soc[order]))

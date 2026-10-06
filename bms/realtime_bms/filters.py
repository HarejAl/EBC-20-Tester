"""Small EKF and UKF implementations specialized for scalar voltage updates."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .model import TheveninEcmModel


@dataclass
class FilterUpdate:
    predicted_voltage_v: float
    innovation_v: float
    innovation_variance_v2: float
    normalized_innovation_squared: float
    accepted: bool


class BaseKalmanFilter:
    def __init__(self, model: TheveninEcmModel, state, covariance,
                 process_noise, voltage_variance_v2: float,
                 nis_gate: float = 9.0):
        self.model = model
        self.n = model.state_size
        self.x = np.asarray(state, dtype=float).reshape(self.n)
        self.P = np.asarray(covariance, dtype=float).reshape(self.n, self.n)
        self.Q_per_s = np.asarray(process_noise, dtype=float)
        if self.Q_per_s.ndim == 1: self.Q_per_s = np.diag(self.Q_per_s)
        self.Q_per_s = self.Q_per_s.reshape(self.n, self.n)
        self.R = float(voltage_variance_v2)
        self.nis_gate = float(nis_gate)
        if self.R <= 0 or self.nis_gate <= 0:
            raise ValueError("measurement variance and NIS gate must be positive")
        self._stabilize()

    @property
    def soc_std(self) -> float:
        return math.sqrt(max(float(self.P[0, 0]), 0.0))

    def _stabilize(self) -> None:
        self.x[0] = np.clip(self.x[0], 0.0, 1.0)
        self.P = .5*(self.P+self.P.T)
        eigenvalues, eigenvectors = np.linalg.eigh(self.P)
        self.P = eigenvectors @ np.diag(np.maximum(eigenvalues, 1e-14)) @ eigenvectors.T

    def export_state(self) -> dict:
        return {"state": self.x.tolist(), "covariance": self.P.tolist()}


class ExtendedKalmanFilter(BaseKalmanFilter):
    """EKF with central finite-difference model Jacobians and Joseph update."""

    def predict(self, dt_s: float, current_a: float) -> None:
        F = self.model.transition_jacobian(self.x, dt_s, current_a)
        self.x = self.model.transition(self.x, dt_s, current_a)
        self.P = F @ self.P @ F.T + self.Q_per_s*max(dt_s, 1e-6)
        self._stabilize()

    def update(self, voltage_v: float, current_a: float) -> FilterUpdate:
        predicted = self.model.voltage(self.x, current_a)
        H = self.model.voltage_jacobian(self.x, current_a)
        innovation = float(voltage_v-predicted)
        S = float((H @ self.P @ H.T).item()+self.R)
        nis = innovation*innovation/S
        accepted = math.isfinite(nis) and nis <= self.nis_gate
        if accepted:
            K = self.P @ H.T / S
            self.x = self.x + K[:, 0]*innovation
            identity = np.eye(self.n)
            # Joseph form preserves positive semidefiniteness numerically.
            residual = identity-K@H
            self.P = residual@self.P@residual.T + K*self.R@K.T
            self._stabilize()
        return FilterUpdate(predicted, innovation, S, nis, accepted)


class UnscentedKalmanFilter(BaseKalmanFilter):
    """Scaled unscented Kalman filter, implemented without FilterPy."""

    def __init__(self, *args, alpha: float = .2, beta: float = 2.0,
                 kappa: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.alpha, self.beta, self.kappa = alpha, beta, kappa
        n = self.n
        self.lam = alpha*alpha*(n+kappa)-n
        scale = n+self.lam
        if scale <= 0: raise ValueError("invalid UKF sigma-point scaling")
        self.wm = np.full(2*n+1, .5/scale)
        self.wc = self.wm.copy()
        self.wm[0] = self.lam/scale
        self.wc[0] = self.wm[0]+(1-alpha*alpha+beta)
        self._predicted_sigma = None

    def _sigma_points(self, mean, covariance):
        n = self.n
        scaled = (n+self.lam)*covariance
        jitter = 1e-14
        for _ in range(8):
            try:
                root = np.linalg.cholesky(scaled+jitter*np.eye(n)); break
            except np.linalg.LinAlgError:
                jitter *= 10
        else:
            raise RuntimeError("UKF covariance is not positive definite")
        points = [mean]
        points += [mean+root[:, i] for i in range(n)]
        points += [mean-root[:, i] for i in range(n)]
        return np.asarray(points)

    def predict(self, dt_s: float, current_a: float) -> None:
        sigma = self._sigma_points(self.x, self.P)
        propagated = np.asarray([self.model.transition(p, dt_s, current_a)
                                 for p in sigma])
        mean = np.sum(self.wm[:, None]*propagated, axis=0)
        delta = propagated-mean
        self.x = mean
        self.P = np.einsum("i,ij,ik->jk", self.wc, delta, delta)
        self.P += self.Q_per_s*max(dt_s, 1e-6)
        self._predicted_sigma = propagated
        self._stabilize()

    def update(self, voltage_v: float, current_a: float) -> FilterUpdate:
        sigma = self._sigma_points(self.x, self.P)
        z_sigma = np.asarray([self.model.voltage(p, current_a) for p in sigma])
        predicted = float(np.sum(self.wm*z_sigma))
        dz = z_sigma-predicted
        dx = sigma-self.x
        S = float(np.sum(self.wc*dz*dz)+self.R)
        cross = np.sum(self.wc[:, None]*dx*dz[:, None], axis=0)
        innovation = float(voltage_v-predicted)
        nis = innovation*innovation/S
        accepted = math.isfinite(nis) and nis <= self.nis_gate
        if accepted:
            gain = cross/S
            self.x = self.x+gain*innovation
            self.P = self.P-np.outer(gain, gain)*S
            self._stabilize()
        return FilterUpdate(predicted, innovation, S, nis, accepted)

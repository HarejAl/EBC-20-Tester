import scipy
import numpy as np
from scipy.interpolate import interp1d, RegularGridInterpolator


class ecm:
    """Equivalent Circuit Model with 1-D (SOC-only) lookup tables.

    Parameters
    ----------
    path : str
        Path to the .mat file containing ECM parameters
        (keys: SOC, Em, R0, Rx, taux).
    C : float
        Nominal cell capacity [Ah].
    """

    def __init__(self, path, C=5.0):
        p = scipy.io.loadmat(path)

        soc_aux      = p['SOC'].T[:, 0]
        # soc_aux     += np.linspace(0.1, 0, len(soc_aux)) / 100  # edge guard
        self.soc_aux = soc_aux
        self.Q_batt  = C * 3600   # Ah → As

        self._iR0   = interp1d(soc_aux, p['R0'].T[:, 0],    kind='linear', fill_value='extrapolate', bounds_error=False)
        self._iEm   = interp1d(soc_aux, p['Em'].T[:, 0],    kind='linear', fill_value='extrapolate', bounds_error=False)
        self._iTau1 = interp1d(soc_aux, p['taux'].T[:, 0],  kind='linear', fill_value='extrapolate', bounds_error=False)
        self._iTau2 = interp1d(soc_aux, p['taux'].T[:, 1],  kind='linear', fill_value='extrapolate', bounds_error=False)
        self._iTau3 = interp1d(soc_aux, p['taux'].T[:, 2],  kind='linear', fill_value='extrapolate', bounds_error=False)
        self._iR1   = interp1d(soc_aux, p['Rx'].T[:, 0],    kind='linear', fill_value='extrapolate', bounds_error=False)
        self._iR2   = interp1d(soc_aux, p['Rx'].T[:, 1],    kind='linear', fill_value='extrapolate', bounds_error=False)
        self._iR3   = interp1d(soc_aux, p['Rx'].T[:, 2],    kind='linear', fill_value='extrapolate', bounds_error=False)

    def generateA(self, soc, dT, T=None):
        tau1 = float(self._iTau1(soc))
        tau2 = float(self._iTau2(soc))
        tau3 = float(self._iTau3(soc))
        A = np.zeros((4, 4))
        A[0, 0] = 1
        A[1, 1] = np.exp(-dT / tau1)
        A[2, 2] = np.exp(-dT / tau2)
        A[3, 3] = np.exp(-dT / tau3)
        return A

    def generateB(self, soc, dT, T=None):
        tau1 = float(self._iTau1(soc))
        tau2 = float(self._iTau2(soc))
        tau3 = float(self._iTau3(soc))
        R1   = float(self._iR1(soc))
        R2   = float(self._iR2(soc))
        R3   = float(self._iR3(soc))
        B = np.zeros((4, 1))
        B[0, 0] = -dT / self.Q_batt
        B[1, 0] = (1 - np.exp(-dT / tau1)) * R1
        B[2, 0] = (1 - np.exp(-dT / tau2)) * R2
        B[3, 0] = (1 - np.exp(-dT / tau3)) * R3
        return B

    def F(self, x, dT, I, T=None):
        """State transition.  T is accepted but ignored (1-D model)."""
        soc = x[0]
        x   = x[:, None]
        soh = None
        if len(x) > 4:
            soh = np.clip(x[-1:, :], 0.6, 1)
            x   = x[:4, :]
        A = self.generateA(soc, dT)
        B = self.generateB(soc, dT)
        if soh is not None:
            B[0, 0] /= float(soh)
        x_out = A @ x + B * I
        if soh is not None:
            x_out = np.concatenate((x_out, soh), axis=0)
        return x_out[:, 0]

    def H(self, x, I, T=None):
        """Measurement function.  T is accepted but ignored (1-D model)."""
        soc = x[0]
        R0  = float(self._iR0(soc))
        Em  = float(self._iEm(soc))
        Vt  = Em - R0 * I - x[1] - x[2] - x[3]
        return np.array([Vt])


class ecm_T:
    """Equivalent Circuit Model with 2-D (Temperature × SOC) lookup tables.

    Parameters are bilinearly interpolated from a regular grid:
      axis-0 → Temperature [K]   (e.g. 5 points: 283.15 … 313.15 K)
      axis-1 → SOC       [0–1]   (e.g. 101 points: 0.00 … 1.00)

    All state/measurement functions expose the same interface as ``ecm``
    with an additional required ``T`` argument [K].

    Parameters
    ----------
    path : str
        Path to ``lookuptables_ECM_Mohtat2020.mat`` (MATLAB struct
        ``ecm_data`` with fields: Cbat, soc, temp, OCV, R0, R1, R2, R3,
        tau1, tau2, tau3).
    C : float
        Nominal cell capacity [Ah].  Used only for the coulomb-counting
        B-matrix entry; the stored Cbat is informational.
    """

    def __init__(self, path, C=5.0):
        raw   = scipy.io.loadmat(path)
        inner = raw['ecm_data'][0, 0]

        self.soc_axis  = inner['soc'].flatten()    # (n_soc,)  0..1
        self.temp_axis = inner['temp'].flatten()   # (n_temp,) K
        self.Q_batt    = C * 3600                  # Ah → As

        print(f'ECM initialized with C={C:.3f} Ah')

        def _rgi(mat):
            """RegularGridInterpolator over (temp, soc) with clamped extrapolation."""
            return RegularGridInterpolator(
                (self.temp_axis, self.soc_axis),
                mat,              # shape (n_temp, n_soc)
                method='linear',
                bounds_error=False,
                fill_value=None,  # extrapolate beyond grid edges
            )

        self._iOCV  = _rgi(inner['OCV'])
        self._iR0   = _rgi(inner['R0'])
        self._iR1   = _rgi(inner['R1'])
        self._iR2   = _rgi(inner['R2'])
        self._iR3   = _rgi(inner['R3'])
        self._iTau1 = _rgi(inner['tau1'])
        self._iTau2 = _rgi(inner['tau2'])
        self._iTau3 = _rgi(inner['tau3'])

    def _lookup(self, interp, T, soc):
        """Bilinear lookup at (T [K], soc), clamped to grid bounds."""
        T_c   = np.clip(float(T),   self.temp_axis[0], self.temp_axis[-1])
        soc_c = np.clip(float(soc), self.soc_axis[0],  self.soc_axis[-1])
        return float(interp([[T_c, soc_c]]))

    def generateA(self, soc, dT, T):
        tau1 = self._lookup(self._iTau1, T, soc)
        tau2 = self._lookup(self._iTau2, T, soc)
        tau3 = self._lookup(self._iTau3, T, soc)
        A = np.zeros((4, 4))
        A[0, 0] = 1
        A[1, 1] = np.exp(-dT / tau1)
        A[2, 2] = np.exp(-dT / tau2)
        A[3, 3] = np.exp(-dT / tau3)
        return A

    def generateB(self, soc, dT, T):
        tau1 = self._lookup(self._iTau1, T, soc)
        tau2 = self._lookup(self._iTau2, T, soc)
        tau3 = self._lookup(self._iTau3, T, soc)
        R1   = self._lookup(self._iR1, T, soc)
        R2   = self._lookup(self._iR2, T, soc)
        R3   = self._lookup(self._iR3, T, soc)
        B = np.zeros((4, 1))
        B[0, 0] = -dT / self.Q_batt
        B[1, 0] = (1 - np.exp(-dT / tau1)) * R1
        B[2, 0] = (1 - np.exp(-dT / tau2)) * R2
        B[3, 0] = (1 - np.exp(-dT / tau3)) * R3
        return B

    def F(self, x, dT, I, T):
        """State transition: [SOC, V1, V2, V3] → next state.

        Optionally accepts a 5th element (SOH fraction) for capacity scaling.
        T : float  temperature [K]
        """
        soc = x[0]
        x   = x[:, None]
        soh = None
        if len(x) > 4:
            soh = np.clip(x[-1:, :], 0.6, 1)
            x   = x[:4, :]
        A = self.generateA(soc, dT, T)
        B = self.generateB(soc, dT, T)
        if soh is not None:
            B[0, 0] /= float(soh)
        x_out = A @ x + B * I
        if soh is not None:
            x_out = np.concatenate((x_out, soh), axis=0)
        return x_out[:, 0]

    def H(self, x, I, T):
        """Measurement: terminal voltage.

        T : float  temperature [K]
        """
        soc = x[0]
        R0  = self._lookup(self._iR0,  T, soc)
        OCV = self._lookup(self._iOCV, T, soc)
        Vt  = OCV - R0 * I - x[1] - x[2] - x[3]
        return np.array([Vt])

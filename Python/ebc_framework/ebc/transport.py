"""
Transport backends for the EBC-A20.

Two interchangeable backends implement the same Transport interface:
  - SerialTransport: talks to the real device over the CH340 serial port.
  - SimulatorTransport: emulates a cell + device so experiments can be
    developed and the control logic validated with no hardware attached.

Both expose:
    write(frame: bytes) -> None
    read_frames() -> Iterator[bytes]   # yields complete SOF..EOF frames
"""

from __future__ import annotations
import time
import math
from collections import deque
from typing import Iterator

from . import protocol as P


class FrameReassembler:
    """
    Accumulates raw bytes and yields complete frames delimited by SOF/EOF.

    Robust against reads that split a frame across chunks or concatenate
    several frames — the failure mode of the naive 'read(64) and slice'
    approach.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._buf.extend(data)
        while True:
            # Drop anything before the first SOF.
            start = self._buf.find(P.SOF)
            if start == -1:
                self._buf.clear()
                return
            if start > 0:
                del self._buf[:start]
            # Need at least SOF + 1 byte to look for EOF.
            end = self._buf.find(P.EOF, 1)
            if end == -1:
                return  # incomplete; wait for more bytes
            frame = bytes(self._buf[:end + 1])
            del self._buf[:end + 1]
            yield frame


class SerialTransport:
    """Real-device backend over pyserial. 9600 8O1 per the protocol."""

    def __init__(self, port: str, timeout: float = 0.2) -> None:
        import serial  # lazy import so the simulator path needs no pyserial
        self._ser = serial.Serial(
            port=port, baudrate=9600,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_ODD,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
        )
        self._reasm = FrameReassembler()

    def write(self, frame: bytes) -> None:
        self._ser.write(frame)
        self._ser.flush()

    def read_frames(self) -> Iterator[bytes]:
        data = self._ser.read(256)
        if data:
            yield from self._reasm.feed(data)

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


class SimulatorTransport:
    """
    A lightweight cell + tester emulator.

    Cell model: a 1-RC Thevenin equivalent with an OCV-SOC curve and coulombic
    integration. Good enough to exercise SOC/SOH estimators and to validate the
    control loop end-to-end before touching hardware. NOT a high-fidelity
    electrochemical model.
    """

    def __init__(self, capacity_ah: float = 3.0, soc0: float = 0.8,
                 r0: float = 0.05, r1: float = 0.03, c1: float = 2000.0,
                 emit_period_s: float = 1.0, time_accel: float = 1.0) -> None:
        self.capacity_ah = capacity_ah
        self.soc = soc0
        self.r0, self.r1, self.c1 = r0, r1, c1
        self.v_rc = 0.0                 # RC branch voltage
        self.emit_period_s = emit_period_s
        # time_accel > 1 makes simulated cell dynamics run faster than
        # wall-clock — for fast offline testing only. Real backends ignore this.
        self.time_accel = time_accel

        self._mode = "idle"             # 'charge'|'discharge'|'idle'
        self._set_current = 0.0         # commanded magnitude (A)
        self._cutoff_v = 0.0
        self._charge_v = 0.0
        self._run_charge_ah = 0.0       # accumulated this run
        self._last_t = time.time()
        self._last_emit = 0.0
        self._outbox: deque[bytes] = deque()
        self._connected = False

    # --- OCV-SOC curve (smooth, monotonic, Li-ion-like) ---
    @staticmethod
    def _ocv(soc: float) -> float:
        soc = min(max(soc, 0.0), 1.0)
        # 3.0 V near-empty knee down to ~4.2 V full, with a steep drop below
        # ~10% SOC so a real cutoff (2.5 V) is reachable under load.
        base = 3.0 + 1.05 * soc + 0.05 * math.tanh(8 * (soc - 0.5))
        low_knee = -0.8 * math.exp(-40 * soc)   # pulls voltage down near empty
        return base + low_knee

    def write(self, frame: bytes) -> None:
        if len(frame) < 3 or frame[0] != P.SOF or frame[-1] != P.EOF:
            return
        pt = frame[1]
        if pt == P.PT_CONNECT:
            self._connected = True
        elif pt == P.PT_DISCONNECT:
            self._connected = False
            self._mode = "idle"
        elif pt == P.PT_STOP:
            self._mode = "idle"
            self._set_current = 0.0
        elif pt == P.PT_START_DISCHARGE:
            self._mode = "discharge"
            self._set_current = P.decode_base240(frame[2], frame[3]) / 100.0
            self._cutoff_v = P.decode_base240(frame[4], frame[5]) / 100.0
            self._run_charge_ah = 0.0
        elif pt == P.PT_START_CHARGE:
            self._mode = "charge"
            self._set_current = P.decode_base240(frame[2], frame[3]) / 100.0
            self._charge_v = P.decode_base240(frame[4], frame[5]) / 100.0
            self._run_charge_ah = 0.0

    def _advance(self, dt: float) -> None:
        # integrate in small slices for numerical stability under time_accel
        if dt <= 0:
            return
        max_slice = 0.5  # seconds of simulated time per Euler step
        remaining = dt
        while remaining > 1e-9:
            step = min(max_slice, remaining)
            self._advance_step(step)
            remaining -= step
            if self._mode == "idle":
                break

    def _advance_step(self, dt: float) -> None:
        if self._mode == "discharge":
            i = +self._set_current        # drawn out of cell
        elif self._mode == "charge":
            i = -self._set_current        # pushed into cell
        else:
            i = 0.0

        # Coulomb integration (sign: discharge lowers SOC)
        self.soc -= (i * dt / 3600.0) / self.capacity_ah
        self.soc = min(max(self.soc, 0.0), 1.0)
        self._run_charge_ah += abs(i) * dt / 3600.0

        # 1-RC dynamics
        dv = (i - self.v_rc / self.r1) / self.c1
        self.v_rc += dv * dt

        # terminal voltage
        self._v_terminal = self._ocv(self.soc) - i * self.r0 - self.v_rc

        # device-side cutoffs
        if self._mode == "discharge" and self._v_terminal <= self._cutoff_v:
            self._mode = "idle"; self._set_current = 0.0
        if self._mode == "charge" and self._v_terminal >= self._charge_v:
            self._mode = "idle"; self._set_current = 0.0

    def _emit(self) -> bytes:
        pt = {"discharge": P.PT_STATUS_DISCHARGE_RUN,
              "charge": P.PT_STATUS_CHARGE_RUN,
              "idle": P.PT_STATUS_IDLE}[self._mode]
        v_clamped = max(0.0, min(self._v_terminal, 57.0))   # <10A-range cap
        # Current is 0.01 A/count on real hardware (parse_status applies a
        # x10 correction on top of decode_ranged's 0.001 A/count table) --
        # match that scale here so simulated frames round-trip correctly.
        i_units = int(round(abs(self._set_current) * 100))
        v_units = int(round(v_clamped * 1000))
        c_units = int(round(self._run_charge_ah * 1000))

        def b240(x):
            x = max(0, min(x, 240 * 239 + 239))  # fits two base-240 bytes
            return divmod(x, 240)
        i1, i2 = b240(i_units)
        v1, v2 = b240(v_units)
        c1, c2 = b240(c_units)
        payload = bytes([pt, i1, i2, v1, v2, c1, c2, 0, 0,
                         0, 0, 0, 0, 0, 0, P.DEVICE_TYPE_A20])
        return bytes([P.SOF]) + payload + bytes([P.crc(payload), P.EOF])

    def read_frames(self) -> Iterator[bytes]:
        now = time.time()
        dt = (now - self._last_t) * self.time_accel
        self._last_t = now
        if not hasattr(self, "_v_terminal"):
            self._v_terminal = self._ocv(self.soc)
        self._advance(dt)
        if now - self._last_emit >= self.emit_period_s:
            self._last_emit = now
            self._outbox.append(self._emit())
        while self._outbox:
            yield self._outbox.popleft()

    def close(self) -> None:
        pass

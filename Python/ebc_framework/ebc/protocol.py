"""
EBC-A20 wire protocol: frame construction and parsing.

Protocol reference (verified, not guessed):
  Maciej Grela / enkiusz — https://pop.fsck.pl/hardware/zketech-ebc-a20.html
  Frame dumps cross-checked against Kazhuu/ebc-battery-tester (FRAMES.md)
  and dev-strom/esp-ebc-mqtt (decode_ranged).

Physical layer: 9600 baud, 8 data bits, ODD parity, 1 stop bit.

Frame layout:
  SOF(0xFA) | payload (16 bytes for status, fewer for commands) | CRC | EOF(0xF8)
  CRC = XOR of all payload bytes (between SOF and CRC, exclusive of both).

Numeric fields use base-240 encoding: value = 240*high + low. This avoids the
0xFA/0xF8 control bytes ever appearing inside a field. Physical quantities
(I, V, C) additionally carry range/scale info decoded by decode_ranged().
"""

from __future__ import annotations
from dataclasses import dataclass

SOF = 0xFA
EOF = 0xF8

# --- Packet type identifiers (PT, first payload byte) ---
# Commands (PC -> device)
PT_START_DISCHARGE = 0x01
PT_ADJUST_DISCHARGE = 0x07
PT_STOP_DISCHARGE = 0x08
PT_START_CHARGE = 0x21
PT_STOP = 0x02          # stop, used for both charge and discharge
PT_CONNECT = 0x05       # '-PC-' appears on LCD
PT_DISCONNECT = 0x06    # '-PC-' disappears from LCD

# Status frames (device -> PC), PT values seen in the wild
PT_STATUS_DISCHARGE_IDLE = 0x00
PT_STATUS_DISCHARGE_RUN = 0x0A
PT_STATUS_DISCHARGE_END = 0x14
PT_STATUS_CHARGE_RUN = 0x0C
PT_STATUS_CHARGE_END = 0x16
PT_STATUS_IDLE = 0x02
# FW report frames (0x64/0x66/0x70) are ignored for measurement purposes.

DEVICE_TYPE_A20 = 0x09

# Absolute hardware ceilings for the EBC-A20 itself (independent of any cell).
EBC_MAX_CHARGE_A = 5.0
EBC_MAX_DISCHARGE_A = 20.0


def crc(payload: bytes) -> int:
    """XOR checksum over payload bytes."""
    c = 0
    for b in payload:
        c ^= b
    return c & 0xFF


def encode_base240(value: int) -> tuple[int, int]:
    """Encode a non-negative integer as (high, low) base-240 bytes."""
    if value < 0:
        raise ValueError("base240 cannot encode negatives")
    high, low = divmod(value, 240)
    if high > 239:
        raise ValueError(f"value {value} too large for base240 field")
    return high, low


def decode_base240(high: int, low: int) -> int:
    """Inverse of encode_base240 for plain integer fields (time, etc.)."""
    return 240 * high + low


def decode_ranged(h: int, l: int) -> float:
    """
    Decode a base-240 physical quantity (current/voltage/charge) to a float,
    accounting for the range bits packed into the high byte.

    Verbatim algorithm from the documented protocol (esp-ebc-mqtt origin).
    """
    if h & 0x80 == 0x80:
        if h & 0xE0 == 0xE0:          # range > 200.0
            v = 240 * (h & 0x3F) + l
            m = 0.1
            offset = 0x1C00
        else:                          # 10.00 < range < 200.0
            v = 240 * (h & 0x7F) + l
            m = 0.01
            offset = 0x800
    else:                              # range < 10.00
        v = 240 * h + l
        m = 0.001
        offset = 0
    return (v - offset) * m


def _encode_current_10mA(amps: float) -> tuple[int, int]:
    """Command fields encode current in units of 10 mA, base-240."""
    return encode_base240(int(round(amps * 100)))   # 1.50 A -> 150 units


def _encode_voltage_10mV(volts: float) -> tuple[int, int]:
    """Command fields encode voltage in units of 10 mV, base-240."""
    return encode_base240(int(round(volts * 100)))  # 4.20 V -> 420 units


def _encode_time_min(minutes: int) -> tuple[int, int]:
    return encode_base240(int(minutes))


def _frame(payload: bytes) -> bytes:
    return bytes([SOF]) + payload + bytes([crc(payload), EOF])


# ----------------------- Command builders -----------------------

def build_connect() -> bytes:
    return _frame(bytes([PT_CONNECT, 0, 0, 0, 0, 0, 0]))


def build_disconnect() -> bytes:
    return _frame(bytes([PT_DISCONNECT, 0, 0, 0, 0, 0, 0]))


def build_stop() -> bytes:
    return _frame(bytes([PT_STOP, 0, 0, 0, 0, 0, 0]))


def build_start_discharge(current_a: float, cutoff_v: float,
                          time_min: int = 0) -> bytes:
    """
    Constant-current discharge.
      current_a : discharge current (A)
      cutoff_v  : device-side low-voltage cutoff (V) — HARDWARE backstop
      time_min  : optional time limit in minutes, 0 = none
    """
    cc1, cc2 = _encode_current_10mA(current_a)
    cv1, cv2 = _encode_voltage_10mV(cutoff_v)
    t1, t2 = _encode_time_min(time_min)
    payload = bytes([PT_START_DISCHARGE, cc1, cc2, cv1, cv2, t1, t2])
    return _frame(payload)


def build_start_charge(current_a: float, charge_v: float,
                       cutoff_a: float) -> bytes:
    """
    Constant-current / constant-voltage charge.
      current_a : charge current (A), 0.1-5 A on the A20
      charge_v  : CV target voltage (V)
      cutoff_a  : taper/termination current (A) — HARDWARE backstop
    """
    cc1, cc2 = _encode_current_10mA(current_a)
    cv1, cv2 = _encode_voltage_10mV(charge_v)
    cut1, cut2 = _encode_current_10mA(cutoff_a)
    payload = bytes([PT_START_CHARGE, cc1, cc2, cv1, cv2, cut1, cut2])
    return _frame(payload)


# ----------------------- Status parsing -----------------------

@dataclass
class Measurement:
    """A decoded status frame from the device."""
    pt: int                 # packet type
    voltage: float          # V
    current: float          # A (magnitude; sign by mode, see signed_current)
    charge: float           # accumulated charge for this run (Ah)
    device_type: int | None
    mode: str               # 'charge' | 'discharge' | 'idle' | 'unknown'
    raw: bytes

    @property
    def signed_current(self) -> float:
        """Discharge negative, charge positive (battery-centric sign)."""
        if self.mode == "discharge":
            return -self.current
        if self.mode == "charge":
            return +self.current
        return 0.0


_DISCHARGE_PTS = {PT_STATUS_DISCHARGE_IDLE, PT_STATUS_DISCHARGE_RUN,
                  PT_STATUS_DISCHARGE_END}
_CHARGE_PTS = {PT_STATUS_CHARGE_RUN, PT_STATUS_CHARGE_END}


def _mode_for_pt(pt: int) -> str:
    # PT_STATUS_DISCHARGE_IDLE means the device is in the discharge slot but
    # drawing no current (e.g. right after STOP) -- report it as idle, not
    # discharge, even though the docs group it with the discharge PTs.
    # Confirmed on real hardware 2026-07-01: current reads 0.000 A here.
    if pt == PT_STATUS_DISCHARGE_IDLE:
        return "idle"
    if pt in _DISCHARGE_PTS:
        return "discharge"
    if pt in _CHARGE_PTS:
        return "charge"
    if pt == PT_STATUS_IDLE:
        return "idle"
    return "unknown"


def parse_status(frame: bytes) -> Measurement | None:
    """
    Parse a full 19-byte status frame (SOF..EOF). Returns None if the frame
    is malformed, fails CRC, or is a FW-report frame we don't decode.

    Status payload layout (offsets within the 19-byte frame):
      [0]=SOF [1]=PT [2:4]=I [4:6]=V [6:8]=C(charge) [8:10]=E
      [10:12]=CC [12:14]=CV/CUTV [14:16]=CUT/T [16]=DT [17]=CRC [18]=EOF
    """
    if len(frame) != 19:
        return None
    if frame[0] != SOF or frame[18] != EOF:
        return None
    payload = frame[1:17]
    if crc(payload) != frame[17]:
        return None

    pt = frame[1]
    # FW-report frames carry firmware bytes where measurements would be; skip.
    if pt in (0x64, 0x66, 0x70):
        return None

    # decode_ranged's low-range multiplier (0.001/count) matches this unit's
    # voltage field exactly, but its current field reports 10x coarser
    # (0.01 A/count) -- confirmed on real hardware 2026-07-01: commanded
    # 0.5 A / 0.2 A discharges decoded as 0.050 / 0.020 before this factor,
    # matching the LCD only after multiplying by 10. Not yet confirmed above
    # the low range or for the charge (Ah) field -- re-check if higher
    # currents look off.
    current = decode_ranged(frame[2], frame[3]) * 10.0
    voltage = decode_ranged(frame[4], frame[5])
    charge = decode_ranged(frame[6], frame[7])
    dt = frame[16]
    return Measurement(
        pt=pt, voltage=voltage, current=current, charge=charge,
        device_type=dt, mode=_mode_for_pt(pt), raw=frame,
    )

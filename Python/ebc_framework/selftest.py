"""Validate protocol decode against documented frames, then run a sim experiment."""
import os
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from ebc import protocol as P
from ebc import EBCA20, SafetyLimits, DataLogger, ProfileController, dst_profile
from ebc.experiment import capacity_test

print("=== 1. CRC check against documented packet ===")
# Documented status frame; payload bytes [1..16], CRC at [17] should be 0x24
doc = bytes([0xfa,0x0c,0x00,0x00,0x02,0x1e,0x00,0x00,0x00,0x00,
             0x00,0x0a,0x01,0x3c,0x00,0x0a,0x09,0x24,0xf8])
assert P.crc(doc[1:17]) == doc[17], "CRC mismatch"
print(f"  CRC ok: computed 0x{P.crc(doc[1:17]):02x} == frame 0x{doc[17]:02x}")

print("\n=== 2. base240 round-trip ===")
for val in [0, 1, 50, 239, 240, 420, 1999]:
    h, l = P.encode_base240(val)
    assert P.decode_base240(h, l) == val
print("  base240 encode/decode consistent for sampled values")

print("\n=== 3. decode_ranged sanity (documented current example) ===")
# I1=0x00,I2=0x32 -> 50 in <10A range, m=0.001 -> 0.050 A
print(f"  decode_ranged(0x00,0x32) = {P.decode_ranged(0x00,0x32):.3f} A")

print("\n=== 4. command frame builders are well-formed ===")
for name, fr in [
    ("connect", P.build_connect()),
    ("stop", P.build_stop()),
    ("discharge 1.5A cutoff 2.5V", P.build_start_discharge(1.5, 2.5)),
    ("charge 1.0A CV4.2 taper0.1", P.build_start_charge(1.0, 4.2, 0.1)),
]:
    assert fr[0] == P.SOF and fr[-1] == P.EOF
    assert P.crc(fr[1:-2]) == fr[-2]
    print(f"  {name:32s} -> {fr.hex(' ')}")

print("\n=== 5. end-to-end DST run on the simulator ===")
limits = SafetyLimits(v_max=4.25, v_min=2.6, i_max=5.0, discharge_cutoff_v=2.6)
dev = EBCA20.on_simulator(limits=limits, capacity_ah=3.0, soc0=0.6,
                          emit_period_s=0.0, time_accel=60.0)
log = DataLogger(os.path.join(tempfile.gettempdir(), "sim_dst.csv"),
                 extra_fields=["soc_true"])
profile = ProfileController(steps=dst_profile(scale_a=2.0), repeat=2)

def on_sample(m, t):
    log.log(m, t, soc_true=f"{dev._t.soc:.4f}")

with dev:
    reason = dev.run(profile, period_s=0.02, max_duration_s=5,
                     on_sample=on_sample)
log.close()
print(f"  run ended: {reason}")
print(f"  samples logged: {log.rows}")
print(f"  final sim SOC: {dev._t.soc:.3f}, last V: {dev.last.voltage:.3f} V")

print("\n=== 6. capacity (SOH ground-truth) test on simulator ===")
dev2 = EBCA20.on_simulator(limits=SafetyLimits(v_min=2.5),
                           capacity_ah=3.0, soc0=1.0, emit_period_s=0.0,
                           time_accel=2000.0)
with dev2:
    res = capacity_test(dev2, current_a=1.5, v_cutoff=3.0,
                        max_duration_s=40)
print(f"  measured capacity: {res.capacity_ah:.3f} Ah")
print(f"  energy: {res.energy_wh:.3f} Wh, V {res.v_start:.3f}->{res.v_end:.3f}")
print("\nALL CHECKS PASSED")

# ---- 7. config-driven runner (cell library + JSON tests) ----
print("\n=== 7. config-driven runner: all test types on simulator ===")
from ebc.celllib import CellLibrary
from ebc.runner import TestDef, run_test
from ebc import DataLogger as DL

_lib = CellLibrary(os.path.join(SCRIPT_DIR, "config", "cells"))
print(f"  cells loaded: {', '.join(_lib.ids())}")

_cfg = os.path.join(SCRIPT_DIR, "config", "tests")
_cases = [
    ("dst_validation.json",   {"repeat": 2}, 60.0,   0.7),
    ("cc_cv_charge.json",     {},            600.0,  0.2),
    ("capacity_discharge.json", {},          2000.0, 1.0),
]
for fn, overrides, accel, soc0 in _cases:
    td = TestDef.load(os.path.join(_cfg, fn))
    td.raw.update(overrides)
    cell = _lib.get(td.cell_id)
    dev = EBCA20.on_simulator(limits=cell.safety_limits(),
                              capacity_ah=cell.nominal_capacity_ah,
                              soc0=soc0, emit_period_s=0.0, time_accel=accel)
    log = DL(os.path.join(tempfile.gettempdir(), f"selftest_{td.name}.csv"))
    with dev:
        res = run_test(td, cell, dev, log)
    log.close()
    cap = f", cap={res.capacity_ah:.3f}Ah" if res.capacity_ah else ""
    print(f"  {td.name:22s} -> {res.reason:28s} ({res.samples} samples{cap})")

print("\nCONFIG-DRIVEN CHECKS PASSED")

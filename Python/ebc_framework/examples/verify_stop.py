"""
Diagnose whether a generic STOP (PT 0x02) actually halts an in-progress
discharge on YOUR EBC-A20 firmware.

Both documented protocol references (pop.fsck.pl, Kazhuu/ebc-battery-tester)
say PT 0x02 alone should be sufficient to stop charge or discharge. If your
unit keeps discharging after this script sends STOP, that's a real firmware
mismatch worth capturing precisely (frame contents, timing) before trusting
any real-cell test.

Run this attended, with the EBC's own LCD visible, and be ready to stop the
device manually (front panel / unplug) if it keeps discharging.

Usage:
    python verify_stop.py COM5
"""
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from ebc import EBCA20, SafetyLimits
from ebc import protocol as P

DISCHARGE_A = 0.2       # gentle, easy to eyeball on the LCD
RUN_S = 5                # how long to discharge before sending STOP
WATCH_S = 20              # how long to keep watching after STOP


def main(port: str):
    dev = EBCA20.on_serial(port, limits=SafetyLimits())
    dev.connect()
    print("Connected. LCD should show '-PC-'.")
    print(f"Starting {DISCHARGE_A} A discharge for {RUN_S} s...\n")
    dev.set_discharge(DISCHARGE_A)

    def watch(seconds, label):
        t0 = time.time()
        last = None
        while time.time() - t0 < seconds:
            for frame in dev._t.read_frames():
                m = P.parse_status(frame)
                if m:
                    last = m
                    print(f"  [{label}] V={m.voltage:6.3f} V  I={m.current:6.3f} A  "
                          f"mode={m.mode:9s} PT=0x{m.pt:02x}  frame={frame.hex(' ')}")
            time.sleep(0.3)
        return last

    watch(RUN_S, "running")

    stop_frame = P.build_stop()
    print(f"\n>>> Sending STOP: {stop_frame.hex(' ')}\n")
    dev.stop()

    last = watch(WATCH_S, "post-stop")

    print()
    if last is None:
        print("No status frames observed after STOP -- inconclusive, check wiring/port.")
    elif last.mode == "discharge" and last.current > 0.01:
        print(f"STOP NOT HONORED: device still reports mode={last.mode}, "
              f"I={last.current:.3f} A after {WATCH_S}s.")
        print("Check the LCD now -- if it's still actively discharging, "
              "stop it manually (front panel / unplug).")
    else:
        print(f"STOP appears honored: last status mode={last.mode}, "
              f"I={last.current:.3f} A.")

    dev.disconnect()
    print("\nDisconnected.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])

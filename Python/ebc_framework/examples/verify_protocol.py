"""
One-time protocol verification for YOUR physical EBC-A20.

The frame format is well documented, but field *scaling* and a few packet
types were reverse-engineered and may differ across firmware revisions. Run
this with a known load/cell connected and sanity-check the printed values
against the EBC's own LCD and a multimeter.

If the printed V/I track the LCD within a few percent, the decoder is correct
for your unit and you can trust the framework. If they don't, capture a few
raw frames (printed in hex below) and compare against FRAMES.md in
Kazhuu/ebc-battery-tester.

Usage:
    python verify_protocol.py /dev/ttyUSB0
"""
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

from ebc import EBCA20, SafetyLimits
from ebc import protocol as P


def main(port: str):
    dev = EBCA20.on_serial(port, limits=SafetyLimits())
    dev.connect()
    print("Connected. The EBC LCD should now show '-PC-'.")
    print("Starting a gentle 0.5 A discharge for 15 s. Watch the LCD.\n")
    dev.set_discharge(0.5)

    t0 = time.time()
    try:
        while time.time() - t0 < 15:
            for frame in dev._t.read_frames():
                m = P.parse_status(frame)
                if m:
                    print(f"  V={m.voltage:6.3f} V   I={m.current:6.3f} A   "
                          f"Ah={m.charge:6.3f}   mode={m.mode:9s} "
                          f"PT=0x{m.pt:02x}   raw_I=[{frame[2]:#04x} {frame[3]:#04x}]"
                          f"   raw_V=[{frame[4]:#04x} {frame[5]:#04x}]"
                          f"   frame={frame.hex(' ')}")
                else:
                    print(f"  [unparsed/FW frame] {frame.hex(' ')}")
            time.sleep(0.5)
    finally:
        dev.disconnect()
    print("\nCompare the V and I above against the LCD reading.")
    print("Match within a few percent  ->  decoder OK for your firmware.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])

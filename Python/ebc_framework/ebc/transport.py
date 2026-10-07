"""
Serial transport for the EBC-A20.

SerialTransport talks to the real device over the CH340 USB-serial adapter
and exposes:
    write(frame: bytes) -> None
    read_frames() -> Iterator[bytes]   # yields complete SOF..EOF frames
"""

from __future__ import annotations
from typing import Iterator

from . import protocol as P


class FrameReassembler:
    """
    Accumulates raw bytes and yields complete device frames.

    Measurement/status responses are 19 bytes, but the real serial stream can
    also contain shorter framed traffic during connection and firmware
    reporting. Prefer a 19-byte candidate when present while retaining the
    delimiter-based recovery that is known to work with the physical tester.
    The checksum at byte 17 may legally equal EOF (0xF8), so that particular
    byte must not be mistaken for the end of a split 19-byte status response.
    """

    FRAME_LENGTH = 19

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

            # Known measurement frames always win, even when their CRC byte
            # at offset 17 happens to equal EOF.
            if (len(self._buf) >= self.FRAME_LENGTH
                    and self._buf[self.FRAME_LENGTH - 1] == P.EOF):
                frame_length = self.FRAME_LENGTH
            else:
                end = self._buf.find(P.EOF, 1)
                if end == -1:
                    return
                next_start = self._buf.find(P.SOF, 1, end)
                if next_start != -1:
                    # A newer SOF before the candidate's EOF is a stronger
                    # synchronization point; abandon the damaged prefix.
                    del self._buf[:next_start]
                    continue
                # At 18 buffered bytes an EOF at offset 17 may be the CRC of a
                # split status response. Wait for its real EOF byte.
                if end == self.FRAME_LENGTH - 2 and len(self._buf) < self.FRAME_LENGTH:
                    return
                # Preserve the original stream recovery for shorter firmware/
                # connection frames and malformed traffic. parse_status()
                # will ignore anything that is not a valid 19-byte response.
                frame_length = end + 1

            frame = bytes(self._buf[:frame_length])
            del self._buf[:frame_length]
            yield frame


class SerialTransport:
    """Real-device backend over pyserial. 9600 8O1 per the protocol."""

    def __init__(self, port: str, timeout: float = 0.2) -> None:
        import serial  # lazy import: only needed once a port is opened
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

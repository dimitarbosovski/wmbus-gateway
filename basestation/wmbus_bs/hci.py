"""SLIP + CRC16 framing of the IMST WM-Bus Gateway HCI.

Source: IMST "WM-Bus Gateway HCI Protocol Specification" V2.4 (wip, 2026-06-03):
UART 115200 8N1, SLIP (RFC 1055), frame = SAP | Msg | Payload | FCS (LSB first),
FCS = CRC-16/IBM-SDLC (check 0x906E). Ping request on the wire: C0 01 01 16 07 C0.
"""
from __future__ import annotations

from dataclasses import dataclass

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD
MAX_PAYLOAD = 500


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def encode(sap: int, msg: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    body = bytes([sap, msg]) + payload
    c = crc16(body)
    body += bytes([c & 0xFF, c >> 8])
    out = bytearray([END])
    for b in body:
        if b == END:
            out += bytes([ESC, ESC_END])
        elif b == ESC:
            out += bytes([ESC, ESC_ESC])
        else:
            out.append(b)
    out.append(END)
    return bytes(out)


@dataclass(frozen=True)
class Frame:
    sap: int
    msg: int
    payload: bytes
    wire_len: int  # bytes on the UART incl. escapes and the closing END


class SlipDecoder:
    """Byte-stream -> HCI frames. Bad CRC frames are counted and dropped."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.esc = False
        self.wire = 0
        self.crc_errors = 0
        self.overflows = 0

    def feed(self, data: bytes) -> list[Frame]:
        frames: list[Frame] = []
        for b in data:
            self.wire += 1
            if b == END:
                if len(self.buf) > 2 + MAX_PAYLOAD + 2:
                    self.overflows += 1
                elif len(self.buf) >= 4:
                    body, fcs = bytes(self.buf[:-2]), self.buf[-2] | (self.buf[-1] << 8)
                    if crc16(body) == fcs:
                        frames.append(Frame(body[0], body[1], body[2:], self.wire))
                    else:
                        self.crc_errors += 1
                self.buf.clear()
                self.esc = False
                self.wire = 0
                continue
            if b == ESC:
                self.esc = True
                continue
            if self.esc:
                self.esc = False
                b = END if b == ESC_END else ESC if b == ESC_ESC else b
            if len(self.buf) <= 2 + MAX_PAYLOAD + 2:
                self.buf.append(b)
        return frames

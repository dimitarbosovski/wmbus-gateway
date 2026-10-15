"""wM-Bus T1 / C1 physical layer for simulation (meter -> other, frame format A).

Sources:
  - T: 100 kchip/s, deviation +-50 kHz; C: 100 kbps NRZ, deviation +-45 kHz, both at
    868.95 MHz (IMST iM891A-XL datasheet, IMST Firmware Feature Overview Table 3-1).
  - Preamble n x (01), n >= 19 for T; sync 0000111101 (TI AN067 Table 9, SiLabs AN451).
  - 3-out-of-6 code table, access code 0x543D shared by T1/C1, and the C1 mode words
    0x54CD (format A) / 0x543D (format B) after it: rtl-wmbus t1_c1_packet_decoder.h (BSD).
  - CRC-16 polynomial 0x3D65, init 0, final complement, MSB first; format A blocks:
    L..A (10 B) + CRC, then 16 B + CRC (TI AN067 5.3-5.4, rtl-wmbus).
Model parameters: C preamble length, postamble; FSK is rectangular (no Gaussian shaping).
"""
from __future__ import annotations

import numpy as np

FS = 500_000                 # complex samples/s (STM32WL3x IQ: 2 MHz >> 2; TODO check RM0551)
CHIP_RATE = 100_000
SPS = FS // CHIP_RATE        # 5 samples per chip
DEV = {"T": 50_000.0, "C": 45_000.0}
N_PRE = {"T": 19, "C": 16}   # preamble "01" pairs; T: minimum per standard, C: 16

ENC3OF6 = [0x16, 0x0D, 0x0E, 0x0B, 0x1C, 0x19, 0x1A, 0x13,
           0x2C, 0x25, 0x26, 0x23, 0x34, 0x31, 0x32, 0x29]
DEC3OF6 = {c: i for i, c in enumerate(ENC3OF6)}
SYNC = [0, 0, 0, 0, 1, 1, 1, 1, 0, 1]
C_MODE_A, C_MODE_B = 0x54CD, 0x543D
POST = [0, 1, 0, 1]


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x3D65) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


def frame_a(header9: bytes, data: bytes) -> bytes:
    """Bytes on air: L + header (C M A, 9 B) + CRC, then data blocks of 16 B + CRC."""
    L = 9 + len(data)
    blk = bytes([L]) + header9
    out = bytearray(blk + crc16(blk).to_bytes(2, "big"))
    for i in range(0, len(data), 16):
        b = data[i:i + 16]
        out += b + crc16(b).to_bytes(2, "big")
    return bytes(out)


def air_len(L: int) -> int:
    """Bytes on air for format A incl. CRCs."""
    return L + 1 + 2 * (1 + -(-(L - 9) // 16))


def check_frame_a(fr: bytes) -> bool:
    if len(fr) < 12 or air_len(fr[0]) != len(fr):
        return False
    if crc16(fr[:10]) != int.from_bytes(fr[10:12], "big"):
        return False
    p = 12
    while p < len(fr):
        n = min(16, len(fr) - p - 2)
        if n <= 0 or crc16(fr[p:p + n]) != int.from_bytes(fr[p + n:p + n + 2], "big"):
            return False
        p += n + 2
    return True


def frame_data(fr: bytes) -> bytes:
    """Format A data bytes after block 1 (CI ...), CRCs removed."""
    out, p = bytearray(), 12
    while p < len(fr):
        n = min(16, len(fr) - p - 2)
        out += fr[p:p + n]
        p += n + 2
    return bytes(out)


def _bits(data: bytes) -> list[int]:
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


def bytes_to_chips_T(data: bytes) -> list[int]:
    out: list[int] = []
    for b in data:
        for nib in (b >> 4, b & 0x0F):
            c = ENC3OF6[nib]
            out += [(c >> (5 - i)) & 1 for i in range(6)]
    return out


def chips_for(mode: str, frame: bytes) -> np.ndarray:
    pre = [0, 1] * N_PRE[mode]
    if mode == "T":
        body = SYNC + bytes_to_chips_T(frame)
    else:
        body = SYNC + _bits(C_MODE_A.to_bytes(2, "big")) + _bits(frame)
    return np.array(pre + body + POST, dtype=np.int8)


def modulate(chips: np.ndarray, dev: float, rate_err: float = 0.0, dev_err: float = 0.0) -> np.ndarray:
    """Continuous-phase rectangular FSK, unit amplitude, complex baseband at FS."""
    cr = CHIP_RATE * (1.0 + rate_err)
    n = int(np.ceil(len(chips) * FS / cr))
    idx = np.minimum((np.arange(n) * cr / FS).astype(np.int64), len(chips) - 1)
    f = np.where(chips[idx] == 1, 1.0, -1.0) * dev * (1.0 + dev_err)
    return np.exp(1j * 2 * np.pi * np.cumsum(f) / FS)


def decode_T(hard: np.ndarray) -> bytes | None:
    """hard: 0/1 chips, multiple of 12. None if any codeword is invalid."""
    out = bytearray()
    for i in range(0, len(hard) - 11, 12):
        hi = DEC3OF6.get(int("".join(map(str, hard[i:i + 6])), 2))
        lo = DEC3OF6.get(int("".join(map(str, hard[i + 6:i + 12])), 2))
        if hi is None or lo is None:
            return None
        out.append((hi << 4) | lo)
    return bytes(out)


def decode_C(hard: np.ndarray) -> bytes:
    n = len(hard) // 8
    return bytes(int("".join(map(str, hard[i * 8:i * 8 + 8])), 2) for i in range(n))

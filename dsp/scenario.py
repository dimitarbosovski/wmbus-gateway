"""Meter population and received-signal synthesis."""
from __future__ import annotations

import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import phy
from receiver import Meter

MANUF = [bytes([0x2D, 0x2C]), bytes([0xA5, 0x11]), bytes([0xB4, 0x09]), bytes([0x93, 0x44])]


def make_meters(n: int, rng: np.random.Generator) -> list[Meter]:
    meters = []
    for i in range(n):
        meters.append(Meter(
            m=MANUF[i % len(MANUF)],
            ident=bytes(rng.integers(0, 256, 4, dtype=np.uint8)),
            ver=int(rng.integers(1, 0x40)), typ=int(rng.choice([0x06, 0x07, 0x04, 0x08])),
            mode="T" if i % 2 == 0 else "C",
            L=int(rng.choice([0x1E, 0x2E])),        # 16 or 32 encrypted bytes after the TPL header
            key=bytes(rng.integers(0, 256, 16, dtype=np.uint8)),
        ))
    return meters


def telegram(meter: Meter, rng: np.random.Generator) -> bytes:
    """OMS security mode 5 (AES-128-CBC, IV = M + A + 8 x ACC), short TPL header."""
    n_enc = meter.L - 9 - 5
    acc = int(rng.integers(0, 256))
    plain = b"\x2f\x2f" + bytes(rng.integers(0, 256, n_enc - 2, dtype=np.uint8))
    iv = meter.header9[1:3] + meter.header9[3:9] + bytes([acc]) * 8
    enc = Cipher(algorithms.AES(meter.key), modes.CBC(iv)).encryptor()
    ct = enc.update(plain) + enc.finalize()
    data = bytes([0x7A, acc, 0x00, 0x10 * (n_enc // 16), 0x05]) + ct
    return phy.frame_a(meter.header9, data)


def impair(rng, cfo_khz=20.0, rate_ppm=100.0, dev_pct=3.0):
    return dict(cfo=float(rng.uniform(-cfo_khz, cfo_khz) * 1e3),
                phase=float(rng.uniform(0, 2 * np.pi)),
                rate_err=float(rng.uniform(-rate_ppm, rate_ppm) * 1e-6),
                dev_err=float(rng.uniform(-dev_pct, dev_pct) / 100.0))


def waveform(meter: Meter, frame: bytes, imp: dict) -> np.ndarray:
    chips = phy.chips_for(meter.mode, frame)
    s = phy.modulate(chips, phy.DEV[meter.mode], imp["rate_err"], imp["dev_err"])
    n = np.arange(len(s))
    return s * np.exp(1j * (2 * np.pi * imp["cfo"] * n / phy.FS + imp["phase"]))


def mix(parts: list[tuple[np.ndarray, int, float]], noise_power: float, rng, pad: int = 2000) -> np.ndarray:
    """parts: (unit-amplitude waveform, start sample, amplitude)."""
    total = max(st + len(w) for w, st, _ in parts) + pad
    x = (rng.normal(size=total) + 1j * rng.normal(size=total)) * np.sqrt(noise_power / 2)
    for w, st, a in parts:
        x[st:st + len(w)] += a * w
    return x

"""Receivers for the simulation.

Common front end (both receivers):
  FM discriminator -> chip matched filter (integrate over 1 chip) -> sync correlator
  (16 preamble chips + 10 sync chips; matched filter for the access code, shared by T1/C1).

Baseline ("one demodulator", a model of a classic radio):
  once a packet header is acquired the receiver is busy until the end of that packet and
  does not look for another sync; no address templates; no cancellation.

Proposed (address-template matched filter):
  1. no busy lock: every sync peak is a candidate, also inside another packet;
  2. address-template bank: a matrix H (meters x chips) holds the chip pattern of each
     known meter's header (C, M, ID, version, type); score = H @ soft_chips (one
     matrix-vector product per candidate) -> meter identity even with chip errors; the
     known header bytes then repair the header before the CRC check;
  3. successive interference cancellation (SIC): a CRC-valid packet is re-modulated,
     its CFO / gain / phase / timing / chip-rate are estimated against the received IQ
     and it is subtracted; detection runs again on the residual.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import phy

SPS = phy.SPS
TPL_CHIPS = np.array([0, 1] * 8 + phy.SYNC, dtype=np.int8)        # 26 chips
TPL = 2.0 * TPL_CHIPS - 1.0
N_TPL = len(TPL)
C_A = 2.0 * np.array(phy._bits(phy.C_MODE_A.to_bytes(2, "big"))) - 1.0
C_B = 2.0 * np.array(phy._bits(phy.C_MODE_B.to_bytes(2, "big"))) - 1.0
DEV_NORM = 2 * np.pi * 47_500 / phy.FS


@dataclass
class Meter:
    m: bytes
    ident: bytes
    ver: int
    typ: int
    mode: str
    L: int
    c_field: int = 0x44
    key: bytes = b""          # AES-128 key of the meter (the gateway has it for decryption)

    @property
    def header9(self) -> bytes:
        return bytes([self.c_field]) + self.m + self.ident + bytes([self.ver, self.typ])


@dataclass
class Decoded:
    frame: bytes
    mode: str
    peak: int                 # sample index of the end of template chip 0
    meter: int | None = None
    repaired: bool = False
    verify: str = "crc"       # "crc": received CRC; "soft_crc+2f2f": see try_decode


@dataclass
class Templates:
    meters: list[Meter]
    HT: np.ndarray = field(init=False)
    HC: np.ndarray = field(init=False)

    bank: object | None = None        # optional Walsh-domain bank (walsh.WalshBank)

    def __post_init__(self):
        self.HT = np.array([2.0 * np.array(phy.bytes_to_chips_T(m.header9)) - 1 for m in self.meters])
        self.HC = np.array([2.0 * np.array(phy._bits(m.header9)) - 1 for m in self.meters])

    def score(self, mode: str, hs: np.ndarray) -> np.ndarray:
        """Cosine similarity of the received header chips with every meter's template."""
        if self.bank is not None:
            return self.bank.score(mode, hs)
        H = self.HT if mode == "T" else self.HC
        return H @ hs / (len(hs) * max(np.sqrt(np.mean(hs * hs)), 1e-9))


# ------------------------------------------------------------------ front end
def soft_chips(x: np.ndarray) -> np.ndarray:
    d = np.zeros(len(x))
    d[1:] = np.angle(x[1:] * np.conj(x[:-1])) / DEV_NORM
    m = np.convolve(d, np.ones(SPS) / SPS)[: len(d)]       # m[n] = mean(d[n-SPS+1..n])
    return m


def sync_peaks(m: np.ndarray, thr: float = 0.7) -> list[tuple[int, float]]:
    up = np.zeros((N_TPL - 1) * SPS + 1)
    up[::SPS] = TPL
    up_abs = np.abs(up)
    c = np.correlate(m, up, "valid")
    e = np.correlate(m * m, up_abs, "valid")
    nc = c / np.sqrt(N_TPL * np.maximum(e, 1e-12))
    cand = np.flatnonzero(nc > thr)
    peaks = []
    w = 3 * SPS
    for n in cand:
        lo, hi = max(0, n - w), min(len(nc), n + w + 1)
        if nc[n] == nc[lo:hi].max():
            if not peaks or n - peaks[-1][0] > w:
                peaks.append((int(n), float(nc[n])))
    return peaks


def chips_at(m: np.ndarray, peak: int, first: int, count: int) -> np.ndarray | None:
    """Soft values of data chips [first, first+count) after the 26 template chips."""
    idx = peak + (N_TPL + first + np.arange(count)) * SPS
    if idx[-1] >= len(m):
        return None
    return m[idx]


# ------------------------------------------------------------------ independent checks
def oms5_check(frame: bytes, key: bytes) -> bool:
    """Decrypt a mode-5 short-TPL telegram with this meter's key; True if it starts with
    the OMS verification bytes 2F 2F. The IV uses M and A from the header, so a wrong
    header or a wrong meter fails (false pass ~ 2^-16)."""
    d = phy.frame_data(frame)
    if len(d) < 5 + 16 or d[0] != 0x7A:
        return False
    acc, cfg = d[1], d[3] | (d[4] << 8)
    nblk = (cfg >> 4) & 0x0F
    if (cfg >> 8) & 0x1F != 5 or nblk == 0 or 5 + 16 * nblk > len(d):
        return False
    iv = frame[2:4] + frame[4:10] + bytes([acc]) * 8
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(d[5:21])[:2] == b"\x2f\x2f"


def soft_match(expected_chips: list[int], soft: np.ndarray | None) -> float:
    if soft is None:
        return 0.0
    e = 2.0 * np.array(expected_chips) - 1.0
    return float(e @ soft / (len(soft) * max(np.sqrt(np.mean(soft * soft)), 1e-9)))


# ------------------------------------------------------------------ decode
def try_decode(m: np.ndarray, peak: int, tpl: Templates | None, t_thr: float = 0.55,
               t_margin: float = 0.12, soft_crc_thr: float = 0.5) -> tuple[Decoded | None, int]:
    """Returns (decoded packet or None, number of data chips the receiver would be busy)."""
    head = chips_at(m, peak, 0, 16)
    if head is None:
        return None, 0
    if head @ C_A / 16 > 0.75:
        mode, off, per_byte = "C", 16, 8
    elif head @ C_B / 16 > 0.75:
        return None, 0                                   # format B not simulated
    else:
        mode, off, per_byte = "T", 0, 12

    # L field
    l_soft = chips_at(m, peak, off, per_byte)
    l_hard = (l_soft > 0).astype(np.int8)
    L = (phy.decode_T(l_hard) if mode == "T" else phy.decode_C(l_hard))
    L = L[0] if L else None

    # address-template bank
    meter_idx = None
    if tpl is not None:
        hs = chips_at(m, peak, off + per_byte, 9 * per_byte)
        if hs is not None:
            s = tpl.score(mode, hs)
            order = np.argsort(s)[::-1]
            if s[order[0]] > t_thr and s[order[0]] - s[order[1]] > t_margin:
                meter_idx = int(order[0])
                if L is None or L < 10:
                    L = tpl.meters[meter_idx].L
    if L is None or L < 10:
        return None, 0

    n_air = phy.air_len(L)
    soft = chips_at(m, peak, off, n_air * per_byte)
    if soft is None:
        return None, 0
    busy = off + n_air * per_byte
    hard = (soft > 0).astype(np.int8)
    if mode == "T":
        # decode codeword by codeword; invalid codewords -> 0 nibble (CRC will catch it)
        fr = bytearray()
        for i in range(0, len(hard), 12):
            hi = phy.DEC3OF6.get(int("".join(map(str, hard[i:i + 6])), 2), 0)
            lo = phy.DEC3OF6.get(int("".join(map(str, hard[i + 6:i + 12])), 2), 0)
            fr.append((hi << 4) | lo)
    else:
        fr = bytearray(phy.decode_C(hard))
    fr[0] = L
    received = bytes(fr)                      # header and its CRC exactly as received
    if meter_idx is not None and received[1:10] != tpl.meters[meter_idx].header9:
        mt = tpl.meters[meter_idx]
        rep = bytearray(received)
        rep[1:10] = mt.header9
        exp_crc = phy.crc16(bytes(rep[:10])).to_bytes(2, "big")
        verify = None
        if exp_crc == received[10:12]:
            verify = "crc"                    # the CRC received on air confirms the repair
        else:
            # the CRC chips are damaged too: accept only if they still agree softly with the
            # expected CRC AND the meter's key decrypts the payload (independent of the CRC)
            exp_chips = phy.bytes_to_chips_T(exp_crc) if mode == "T" else phy._bits(exp_crc)
            sm = soft_match(exp_chips, chips_at(m, peak, off + 10 * per_byte, 2 * per_byte))
            rep[10:12] = exp_crc
            if sm > soft_crc_thr and mt.key and phy.check_frame_a(bytes(rep)) and oms5_check(bytes(rep), mt.key):
                verify = "soft_crc+2f2f"
        if verify and phy.check_frame_a(bytes(rep)):
            return Decoded(bytes(rep), mode, peak, meter_idx, True, verify), busy
        meter_idx = None                      # repair not confirmed: fall back to the raw header
    if not phy.check_frame_a(received):
        return None, busy
    return Decoded(received, mode, peak, meter_idx, False, "crc"), busy


# ------------------------------------------------------------------ SIC
def _cfo(z: np.ndarray) -> float:
    coarse = np.angle(np.sum(z[1:] * np.conj(z[:-1]))) * phy.FS / (2 * np.pi)
    n = np.arange(len(z))
    z2 = z * np.exp(-1j * 2 * np.pi * coarse * n / phy.FS)
    lag = 25
    fine = np.angle(np.sum(z2[lag:] * np.conj(z2[:-lag]))) * phy.FS / (2 * np.pi * lag)
    return coarse + fine


def subtract(x: np.ndarray, d: Decoded, seg: int = 250,
             rate_grid=(-3e-4, -1.5e-4, 0.0, 1.5e-4, 3e-4),
             dev_grid=(-0.03, -0.015, 0.0, 0.015, 0.03)) -> np.ndarray:
    """Re-modulate a decoded packet and subtract it. Grid search over chip-rate error,
    deviation error and +-2 samples timing; CFO by data-aided phase difference;
    complex gain per 250-sample segment (absorbs slow phase drift)."""
    chips = phy.chips_for(d.mode, d.frame)
    pre_start = 2 * phy.N_PRE[d.mode] - 16                  # tx chip index of template chip 0
    s0 = d.peak - (SPS - 1) - pre_start * SPS
    best = None
    for re, de in itertools.product(rate_grid, dev_grid):
        r = phy.modulate(chips, phy.DEV[d.mode], rate_err=re, dev_err=de)
        for dt in (-2, -1, 0, 1, 2):
            a = s0 + dt
            lo, hi = max(a, 0), min(a + len(r), len(x))
            if hi - lo < len(r) // 2:
                continue
            rr = r[lo - a:hi - a]
            z = x[lo:hi] * np.conj(rr)
            f = _cfo(z)
            n = np.arange(hi - lo)
            rot = np.exp(1j * 2 * np.pi * f * n / phy.FS)
            metric = np.abs(np.mean(z * np.conj(rot)))
            if best is None or metric > best[0]:
                best = (metric, lo, hi, rr * rot, z * np.conj(rot))
    if best is None:
        return x
    _, lo, hi, ref, zr = best
    y = x.copy()
    for k in range(0, hi - lo, seg):                        # piecewise complex gain
        e = min(k + seg, hi - lo)
        g = np.mean(zr[k:e])
        y[lo + k:lo + e] -= g * ref[k:e]
    return y


# ------------------------------------------------------------------ receivers
def baseline(x: np.ndarray) -> list[Decoded]:
    m = soft_chips(x)
    out, busy_until = [], -1
    for peak, _ in sync_peaks(m):
        if peak <= busy_until:
            continue
        d, busy = try_decode(m, peak, None)
        if busy:
            busy_until = peak + (N_TPL + busy) * SPS
        if d:
            out.append(d)
    return out


def proposed(x: np.ndarray, tpl: Templates | None, sic: bool = True, max_iter: int = 4) -> list[Decoded]:
    res = x.copy()
    found: list[Decoded] = []
    for _ in range(max_iter + 1):
        m = soft_chips(res)
        new = None
        for peak, _ in sorted(sync_peaks(m), key=lambda p: -p[1]):
            d, _busy = try_decode(m, peak, tpl)
            if d and all(d.frame != f.frame for f in found):
                new = d
                break
        if new is None:
            break
        found.append(new)
        if not sic:
            # without SIC: collect every other decodable candidate in this pass
            for peak, _ in sync_peaks(m):
                d, _ = try_decode(m, peak, tpl)
                if d and all(d.frame != f.frame for f in found):
                    found.append(d)
            break
        res = subtract(res, new)
    return found

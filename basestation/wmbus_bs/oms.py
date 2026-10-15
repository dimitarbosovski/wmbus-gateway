"""OMS / EN 13757 TPL decryption on the host: security modes 5 and 7.

Same logic as gateway/firmware/oms.c (tested there with ASan/UBSan).
References: OMS Spec Annex N (mode 5 IV N.4.1; mode 7 KDF N.2.3/N.3.1; truncated
CMAC N.3.2). wmbusmeters (GPL-3.0) only as a cross-check of AFL layout, mode 7 IV = 0
and MAC input; no code copied.
Not covered: ELL (CI 8C/8D, AES-CTR), mode 10 (AES-CCM), fragmented AFL.
"""
from __future__ import annotations

from enum import Enum

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

CI_AFL, CI_TPL_SHORT, CI_TPL_LONG = 0x90, 0x7A, 0x72
FC_KEYINFO, FC_MAC, FC_COUNTER, FC_LEN, FC_CONTROL, FC_MORE = 0x0200, 0x0400, 0x0800, 0x1000, 0x2000, 0x4000
_MAC_LEN = {3: 2, 4: 4, 5: 8, 6: 12, 7: 16, 8: 12}


class Result(str, Enum):
    PLAIN = "plain"
    OK = "ok"
    NO_KEY = "no_key"
    UNSUPPORTED = "unsupported"
    CHECK_FAIL = "check_fail"
    FORMAT_ERR = "format_err"
    MAC_FAIL = "mac_fail"


def _cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return d.update(data) + d.finalize()


def _cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def kdf_mode7(mk: bytes, dc: int, mcr: bytes, ident: bytes) -> bytes:
    """K = CMAC(Mk, DC || MCR || ID || 07 x 7); DC 0x00 = Kenc, 0x01 = Kmac."""
    return _cmac(mk, bytes([dc]) + mcr + ident + b"\x07" * 7)


def meter_address(pkt: bytes) -> tuple[bytes, bytes]:
    """(M-field 2 bytes, ID 4 bytes) from the DLL header, as on air."""
    return pkt[2:4], pkt[4:8]


def manufacturer_code(m: bytes) -> str:
    v = m[0] | (m[1] << 8)
    return "".join(chr(((v >> s) & 0x1F) + 64) for s in (10, 5, 0))


def manufacturer_bytes(code: str) -> bytes:
    code = code.strip()
    if len(code) == 3 and code.isalpha():
        c = code.upper()
        v = ((ord(c[0]) - 64) << 10) | ((ord(c[1]) - 64) << 5) | (ord(c[2]) - 64)
    else:
        v = int(code, 16)
    return bytes([v & 0xFF, v >> 8])


def id_string(ident: bytes) -> str:
    return ident[::-1].hex()   # printed ID, e.g. 78 56 34 12 -> "12345678"


def _tpl_pos(pkt: bytes) -> tuple[int, dict]:
    """Position of the TPL CI and the parsed AFL fields (if any)."""
    t, afl = 10, {}
    if pkt[t] == CI_AFL:
        if t + 4 > len(pkt):
            raise ValueError
        end = t + 2 + pkt[t + 1]
        fc = pkt[t + 2] | (pkt[t + 3] << 8)
        p = t + 4
        if end > len(pkt):
            raise ValueError
        afl = {"fc": fc, "mcl": 0}
        if fc & FC_CONTROL:
            afl["mcl"] = pkt[p]; p += 1
        if fc & FC_KEYINFO:
            p += 2
        if fc & FC_COUNTER:
            afl["mcr"] = pkt[p:p + 4]; p += 4
        if fc & FC_MAC:
            n = _MAC_LEN.get(afl["mcl"] & 0x0F, 0)
            afl["mac"] = pkt[p:p + n]; p += n
        if fc & FC_LEN:                       # TODO: field order vs EN 13757-7
            afl["ml"] = pkt[p:p + 2]; p += 2
        if p > end:
            raise ValueError
        t = end
    if t >= len(pkt):
        raise ValueError
    return t, afl


def access_number(pkt: bytes) -> int | None:
    try:
        t, _ = _tpl_pos(pkt)
    except (ValueError, IndexError):
        return None
    if pkt[t] == CI_TPL_SHORT and t + 1 < len(pkt):
        return pkt[t + 1]
    if pkt[t] == CI_TPL_LONG and t + 9 < len(pkt):
        return pkt[t + 9]
    return None


def decrypt(pkt: bytes, key: bytes | None) -> tuple[Result, bytes]:
    """Returns (result, packet). On OK the encrypted part is replaced by plaintext."""
    if len(pkt) < 11 or pkt[0] + 1 != len(pkt):
        return Result.FORMAT_ERR, pkt
    try:
        t, afl = _tpl_pos(pkt)
        if pkt[t] == CI_TPL_SHORT:
            m, a, acc, cfg_pos = pkt[2:4], pkt[4:10], pkt[t + 1], t + 3
        elif pkt[t] == CI_TPL_LONG:
            m, a, acc, cfg_pos = pkt[t + 5:t + 7], pkt[t + 1:t + 5] + pkt[t + 7:t + 9], pkt[t + 9], t + 11
        else:
            return Result.UNSUPPORTED, pkt
        cfg = pkt[cfg_pos] | (pkt[cfg_pos + 1] << 8)
    except (ValueError, IndexError):
        return Result.FORMAT_ERR, pkt

    mode, nblk, d = (cfg >> 8) & 0x1F, (cfg >> 4) & 0x0F, cfg_pos + 2
    if mode == 0:
        return Result.PLAIN, pkt
    if key is None:
        return Result.NO_KEY, pkt

    if mode == 5:
        iv = m + a + bytes([acc]) * 8
        kenc = key
    elif mode == 7:
        if "mcr" not in afl or "mac" not in afl or d >= len(pkt):
            return Result.FORMAT_ERR, pkt
        if ((pkt[d] >> 4) & 0x03) != 1:          # cfg-ext: KDF-A only
            return Result.UNSUPPORTED, pkt
        d += 1
        ident = a[:4]
        kenc = kdf_mode7(key, 0x00, afl["mcr"], ident)
        kmac = kdf_mode7(key, 0x01, afl["mcr"], ident)
        mac = _cmac(kmac, bytes([afl["mcl"]]) + afl["mcr"] + afl.get("ml", b"") + pkt[t:])
        if mac[:len(afl["mac"])] != afl["mac"]:
            return Result.MAC_FAIL, pkt
        iv = bytes(16)
    else:
        return Result.UNSUPPORTED, pkt

    n = nblk * 16 if nblk else ((len(pkt) - d) // 16) * 16
    if n == 0 or d + n > len(pkt):
        return Result.FORMAT_ERR, pkt
    plain = _cbc_decrypt(kenc, iv, pkt[d:d + n])
    if plain[:2] != b"\x2f\x2f":
        return Result.CHECK_FAIL, pkt
    return Result.OK, pkt[:d] + plain + pkt[d + n:]

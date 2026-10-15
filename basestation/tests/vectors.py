#!/usr/bin/env python3
"""Test telegram generator (independent encryptor, same as gateway/test/gen_vectors.py)."""
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

KEY = bytes(range(16))                   # OMS Annex N test key 00..0F
M = bytes([0x93, 0x44])                  # M-field as on air (Annex N.4.1)
ID = bytes([0x78, 0x56, 0x34, 0x12])     # meter 12345678, LSB first
VER, TYPE = 0x10, 0x07                   # version, device type (water)
APL = bytes([0x2F, 0x2F, 0x04, 0x13, 0x39, 0x30, 0x00, 0x00,   # 12.345 m3
             0x02, 0xFD, 0x17, 0x00, 0x00])                    # error flags


def pad16(b):
    return b + b"\x2F" * ((-len(b)) % 16)


def cbc(key, iv, data):
    e = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return e.update(data) + e.finalize()


def cmac(key, data):
    c = CMAC(algorithms.AES(key)); c.update(data); return c.finalize()


def frame(body):                         # body = C M A CI ...
    return bytes([len(body)]) + body


def dll(c=0x44):
    return bytes([c]) + M + ID + bytes([VER, TYPE])


def mode5_short(key=KEY, acc=0x05):
    plain = pad16(APL)
    nblk = len(plain) // 16
    cfg = (5 << 8) | (nblk << 4)
    iv = M + ID + bytes([VER, TYPE]) + bytes([acc]) * 8
    tpl = bytes([0x7A, acc, 0x00, cfg & 0xFF, cfg >> 8]) + cbc(key, iv, plain)
    return frame(dll() + tpl), plain


def mode5_long(key=KEY, acc=0x2A):
    # DLL address of a repeater/adapter, meter address in the long TPL header
    plain = pad16(APL)
    nblk = len(plain) // 16
    cfg = (5 << 8) | (nblk << 4)
    iv = M + ID + bytes([VER, TYPE]) + bytes([acc]) * 8
    hdr = bytes([0x72]) + ID + M + bytes([VER, TYPE, acc, 0x00, cfg & 0xFF, cfg >> 8])
    dll_rep = bytes([0x44, 0xAA, 0xBB, 0x01, 0x02, 0x03, 0x04, 0x01, 0x31])
    return frame(dll_rep + hdr + cbc(key, iv, plain)), plain


def mode7_short(mk=KEY, mcr=bytes([0xB3, 0x0A, 0x00, 0x00]), mcl=0x25, tamper=False):
    kenc = cmac(mk, b"\x00" + mcr + ID + b"\x07" * 7)
    kmac = cmac(mk, b"\x01" + mcr + ID + b"\x07" * 7)
    plain = pad16(APL)
    nblk = len(plain) // 16
    cfg = (7 << 8) | (nblk << 4)
    tpl = bytes([0x7A, 0x11, 0x00, cfg & 0xFF, cfg >> 8, 0x10]) + cbc(kenc, b"\x00" * 16, plain)
    mac = cmac(kmac, bytes([mcl]) + mcr + tpl)[:8]
    if tamper:
        mac = bytes([mac[0] ^ 1]) + mac[1:]
    fc = 0x2000 | 0x0800 | 0x0400
    afl_body = bytes([fc & 0xFF, fc >> 8, mcl]) + mcr + mac
    afl = bytes([0x90, len(afl_body)]) + afl_body
    return frame(dll() + afl + tpl), plain, kenc, kmac


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def slip(data):
    out = bytearray([0xC0])
    for b in data:
        if b == 0xC0: out += b"\xDB\xDC"
        elif b == 0xDB: out += b"\xDB\xDD"
        else: out.append(b)
    out.append(0xC0)
    return bytes(out)


def hci_rx_event(pkt):
    payload = (1760000000).to_bytes(4, "little") + bytes([2, 5, 5, (-80) & 0xFF]) + pkt
    body = bytes([0x09, 0x20]) + payload
    c = crc16(body)
    return slip(body + bytes([c & 0xFF, c >> 8]))


def carr(name, b):
    return "static const uint8_t %s[%d] = {%s};\n" % (name, len(b), ",".join("0x%02X" % x for x in b))



def mode5_meter(ident: bytes, key=KEY, acc=1, m=M):
    """Mode 5 short-TPL telegram for any meter ID (4 bytes, as on air)."""
    plain = pad16(APL)
    nblk = len(plain) // 16
    cfg = (5 << 8) | (nblk << 4)
    iv = m + ident + bytes([VER, TYPE]) + bytes([acc]) * 8
    body = bytes([0x44]) + m + ident + bytes([VER, TYPE]) + bytes([0x7A, acc, 0x00, cfg & 0xFF, cfg >> 8])
    return frame(body + cbc(key, iv, plain))


def rx_event_payload(pkt, rssi=-80, pkt_info=2, dec_status=2, enc_mode=5):
    return (1760000000).to_bytes(4, "little") + bytes([dec_status, enc_mode, pkt_info, rssi & 0xFF]) + pkt


if __name__ == "__main__":
    print(mode5_short()[0].hex())

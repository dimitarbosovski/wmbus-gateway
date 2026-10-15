"""IMST WM-Bus Gateway HCI messages used by the base station.

Page numbers refer to the HCI Protocol Specification V2.4 (marked "wip").
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

SAP_DEVMGMT = 0x01
SAP_WMBUS = 0x09

DM_PING_REQ, DM_PING_RSP = 0x01, 0x02                     # p.9-10
WM_GET_CFG_REQ, WM_GET_CFG_RSP = 0x01, 0x02               # p.23-24, rsp = Status | cfg
WM_SET_CFG_REQ, WM_SET_CFG_RSP = 0x03, 0x04               # RAM only
WM_STATUS_REQ, WM_STATUS_RSP = 0x41, 0x42                 # Gateway Status Report, p.30-31
WM_RX_IND = 0x20                                          # p.34-36

CFG_LEN = 11                                              # p.26
LINK_MODES = {"off": 0, "S": 1, "T": 2, "CT": 3, "C": 5}
OPT_ADDR_FILTER, OPT_RX_NOTIFY = 0x0001, 0x0002

# Packet Info in the Rx event (p.35)
PACKET_INFO = {1: "S", 2: "T", 4: "C-A-50k", 5: "C-A", 6: "T-enh", 7: "T-custom",
               8: "C-A-custom", 20: "C-B-50k", 21: "C-B", 24: "C-B-custom"}


@dataclass(frozen=True)
class RxEvent:
    module_utc: int
    dec_status: int
    enc_mode: int
    pkt_info: int
    rssi: int
    pkt: bytes          # L C M M A A A A V T CI ... (CRCs removed)


def parse_rx(payload: bytes) -> RxEvent | None:
    if len(payload) < 8 + 11:
        return None
    utc, dec, enc, info, rssi = struct.unpack_from("<IBBBb", payload, 0)
    pkt = payload[8:]
    if pkt[0] + 1 > len(pkt):
        return None
    return RxEvent(utc, dec, enc, info, rssi, bytes(pkt[: pkt[0] + 1]))


@dataclass(frozen=True)
class GatewayStatus:
    status: int
    utc: int
    link_mode: int
    flags: int
    resets: int
    rx_packets: int
    rx_rejects: int
    rx_errors: int     # "CRC or decoding errors, but not decryption errors" (p.31)


def parse_status(payload: bytes) -> GatewayStatus | None:
    # Status(1) UTC(4) LastSync(4) LinkMode(1) Flags(2) Resets(4) RxPkt(4) RxRej(4) RxErr(4)
    # TxPkt(4) TxErr(4) Reserved(4). Field sizes from the table on p.30-31.
    if len(payload) < 32:
        return None
    st, utc, _sync, lm, flags, resets, rxp, rxr, rxe = struct.unpack_from("<BIIBHIIII", payload, 0)
    return GatewayStatus(st, utc, lm, flags, resets, rxp, rxr, rxe)


def patch_cfg(cfg: bytes, link_mode: int) -> bytes:
    """Keep the module's own values, change link mode, Rx notify on, address filter off,
    UI/LED signalling off."""
    b = bytearray(cfg[:CFG_LEN])
    opt = (b[1] | (b[2] << 8) | OPT_RX_NOTIFY) & ~OPT_ADDR_FILTER
    b[0] = link_mode
    b[1], b[2] = opt & 0xFF, (opt >> 8) & 0xFF
    b[3] = b[4] = 0
    return bytes(b)

"""Time on air of a wM-Bus telegram, used to decide whether two telegrams overlapped.

Sources:
  - T meter->other: 100 kchip/s, 3-out-of-6 (12 chips per byte), preamble n x (01) with
    n >= 19, sync 10 chips, postamble 2..8 chips (TI AN067/SWRA234 Table 9, SiLabs AN451).
  - Format A: first block L C M A (10 B) + CRC, then blocks of up to 16 B + CRC each.
  - C mode: NRZ (ST AN4772), 100 kbps (iM891A-XL datasheet); 50 kbps variants per HCI p.35.
  - T frames are "typically 3 to 8 ms" (ST AN4772 3.1.2) - used as a sanity check.
Preamble + sync length for C and S mode are config parameters (c_overhead_bits,
s_overhead_chips).
The module strips the CRCs and reports L without them (HCI p.36).
"""
from __future__ import annotations

import math


def _bytes_format_a(L: int) -> int:
    blocks = 1 + max(0, math.ceil((L - 9) / 16))
    return (L + 1) + 2 * blocks


def _bytes_format_b(L: int) -> int:
    # Format B: one CRC in block 2 (up to 125 B), a second one for longer frames (AN4772 5.2.2)
    return (L + 1) + (2 if L + 2 <= 127 else 4)


def airtime_s(pkt_info: int, L: int, c_overhead_bits: int = 48, s_overhead_chips: int = 48) -> float:
    if pkt_info in (2, 6, 7):                      # T: 3-out-of-6, 100 kchip/s
        chips = 2 * 19 + 10 + 12 * _bytes_format_a(L) + 8
        return chips / 100_000
    if pkt_info in (5, 8):                         # C 100 kbps, format A, NRZ
        return (c_overhead_bits + 8 * _bytes_format_a(L)) / 100_000
    if pkt_info in (21, 24):                       # C 100 kbps, format B
        return (c_overhead_bits + 8 * _bytes_format_b(L)) / 100_000
    if pkt_info == 4:
        return (c_overhead_bits + 8 * _bytes_format_a(L)) / 50_000
    if pkt_info == 20:
        return (c_overhead_bits + 8 * _bytes_format_b(L)) / 50_000
    if pkt_info == 1:                              # S: Manchester, 32.768 kchip/s
        return (s_overhead_chips + 16 * _bytes_format_a(L)) / 32_768
    return 0.010

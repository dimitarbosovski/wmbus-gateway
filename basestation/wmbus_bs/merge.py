"""Cross-sector merge: one telegram received by several sectors -> one record.

A copy is the same transmission when the packet bytes are identical (the module drops
frames with CRC errors, so every delivered copy is bit-exact) and it arrives within
`window_s` of the first copy. Consecutive transmissions of a meter are seconds apart
(T frames "every few seconds", ST AN4772), so 0.2 s cannot merge two transmissions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .airtime import airtime_s
from .imst import RxEvent


@dataclass
class Copy:
    sector: int
    rssi: int
    t_end: float        # estimated end of the transmission on air (time.monotonic)


@dataclass
class Telegram:
    seq: int
    pkt: bytes
    pkt_info: int
    t_first: float      # host time of the first copy
    wall_end: float     # wall clock of the end on air (from the first copy)
    copies: list[Copy] = field(default_factory=list)
    airtime: float = 0.0
    evaluated: bool = False
    # filled by finalize() when the merge window closes
    meter: bytes = b""
    sectors: frozenset = frozenset()
    best: Copy | None = None
    t_end: float = 0.0
    t_start: float = 0.0

    def finalize(self) -> "Telegram":
        self.meter = self.pkt[2:8]                 # M(2) + ID(4)
        self.sectors = frozenset(c.sector for c in self.copies)
        self.best = max(self.copies, key=lambda c: c.rssi)
        self.t_end = min(c.t_end for c in self.copies)
        self.t_start = self.t_end - self.airtime
        return self


class Merger:
    def __init__(self, window_s: float = 0.2, c_overhead_bits: int = 48, s_overhead_chips: int = 48):
        self.window_s = window_s
        self._open: dict[bytes, Telegram] = {}
        self._seq = 0
        self._air = dict(c_overhead_bits=c_overhead_bits, s_overhead_chips=s_overhead_chips)
        self.copies_total = 0

    def add(self, sector: int, ev: RxEvent, t_host: float, t_end: float, wall_end: float) -> None:
        self.copies_total += 1
        tg = self._open.get(ev.pkt)
        if tg is None:
            self._seq += 1
            tg = Telegram(self._seq, ev.pkt, ev.pkt_info, t_host, wall_end,
                          airtime=airtime_s(ev.pkt_info, ev.pkt[0], **self._air))
            self._open[ev.pkt] = tg
        if any(c.sector == sector for c in tg.copies):
            return                                  # same sector twice: ignore
        tg.copies.append(Copy(sector, ev.rssi, t_end))

    def flush(self, now: float) -> list[Telegram]:
        done = [tg for tg in self._open.values() if now - tg.t_first >= self.window_s]
        for tg in done:
            del self._open[tg.pkt]
            tg.finalize()
        done.sort(key=lambda t: t.seq)
        return done

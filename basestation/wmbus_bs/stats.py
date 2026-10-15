"""Collision and loss statistics for a multi-sector receiver.

Only decoded packets leave the iM891A-XL, so a collision is never seen directly.
It is inferred from three signals:
 1. Module counters (Gateway Status, HCI p.30-31): Rx-Error = "CRC or decoding errors"
    per sector. Collisions that start a reception but corrupt it end up here.
 2. A meter's "usual" sectors (hit rate >= usual_ratio over its last telegrams) that
    missed this telegram while another meter's telegram overlapped in time on that
    sector -> collision on that sector; the telegram itself was still received by
    another sector -> rescued by sector separation.
 3. Gaps in a meter's transmission period -> transmissions lost on all sectors.
Overlap uses host timestamps corrected for UART time and an estimated airtime, so it
is a statistical estimate. "shared_sector" pairs (two overlapping telegrams both
decoded by the same sector) are physically implausible for one demodulator and
indicate a timing error: use them to calibrate module_latency_ms.
"""
from __future__ import annotations

import bisect
import statistics
from collections import deque
from dataclasses import dataclass, field

from .merge import Telegram
from .oms import id_string, manufacturer_code


@dataclass
class MeterHistory:
    hist: deque = field(default_factory=lambda: deque(maxlen=20))      # sectors per telegram
    intervals: deque = field(default_factory=lambda: deque(maxlen=20))
    last_t: float | None = None
    n: int = 0
    lost: int = 0
    rssi_sum: dict = field(default_factory=dict)
    rssi_n: dict = field(default_factory=dict)

    def usual_sectors(self, ratio: float, min_history: int) -> frozenset[int]:
        if len(self.hist) < min_history:
            return frozenset()
        counts: dict[int, int] = {}
        for ss in self.hist:
            for s in ss:
                counts[s] = counts.get(s, 0) + 1
        return frozenset(s for s, c in counts.items() if c / len(self.hist) >= ratio)

    def period(self, min_history: int) -> float | None:
        if len(self.intervals) < min_history:
            return None
        return statistics.median(self.intervals)

    def add(self, tg: Telegram, min_history: int) -> int:
        lost_now = 0
        t = tg.t_end
        if self.last_t is not None:
            gap = t - self.last_t
            p = self.period(min_history)
            k = round(gap / p) if p else 1
            if p and k >= 2:
                lost_now = k - 1                     # keep the gap out of the period estimate
            elif gap > 0:
                self.intervals.append(gap)
        self.last_t = t
        self.hist.append(tg.sectors)
        self.n += 1
        self.lost += lost_now
        for c in tg.copies:
            self.rssi_sum[c.sector] = self.rssi_sum.get(c.sector, 0) + c.rssi
            self.rssi_n[c.sector] = self.rssi_n.get(c.sector, 0) + 1
        return lost_now


def _overlap(a: Telegram, b: Telegram, guard: float) -> bool:
    return a.t_start - guard < b.t_end and b.t_start - guard < a.t_end


class CollisionAnalyzer:
    def __init__(self, sectors: list[int], eval_delay_s: float = 0.5, keep_s: float = 3.0,
                 usual_ratio: float = 0.8, min_history: int = 10, guard_s: float = 0.001):
        self.sectors = sectors
        self.eval_delay_s, self.keep_s, self.guard_s = eval_delay_s, keep_s, guard_s
        self.usual_ratio, self.min_history = usual_ratio, min_history
        self.meters: dict[bytes, MeterHistory] = {}
        self._pending: deque[Telegram] = deque()
        self._rt: list[float] = []          # recent telegrams sorted by t_end
        self._rl: list[Telegram] = []
        self._max_air = 0.0
        self._status_prev: dict[int, tuple[int, int]] = {}
        self.reset_interval()

    def reset_interval(self) -> None:
        z = lambda: {s: 0 for s in self.sectors}  # noqa: E731
        self.c = dict(telegrams=0, copies=0, multi_sector=0, pairs=0, disjoint=0, shared=0,
                      rescued=0, lost=0)
        self.rx_ok, self.only_here, self.coll_miss, self.other_miss = z(), z(), z(), z()
        self.mod_rx, self.mod_err = z(), z()

    # ---- inputs ----
    def add(self, tg: Telegram) -> None:
        self._pending.append(tg)
        i = bisect.bisect_right(self._rt, tg.t_end)
        self._rt.insert(i, tg.t_end)
        self._rl.insert(i, tg)
        self._max_air = max(self._max_air, tg.airtime)
        self.c["telegrams"] += 1
        self.c["copies"] += len(tg.copies)
        if len(tg.copies) > 1:
            self.c["multi_sector"] += 1
        for c in tg.copies:
            self.rx_ok[c.sector] += 1
        if len(tg.copies) == 1:
            self.only_here[tg.copies[0].sector] += 1

    def add_module_status(self, sector: int, rx_packets: int, rx_errors: int) -> None:
        prev = self._status_prev.get(sector)
        self._status_prev[sector] = (rx_packets, rx_errors)
        if prev is None:
            return
        d_rx = rx_packets - prev[0] if rx_packets >= prev[0] else rx_packets   # counter reset
        d_err = rx_errors - prev[1] if rx_errors >= prev[1] else rx_errors
        self.mod_rx[sector] += d_rx
        self.mod_err[sector] += d_err

    def poll(self, now: float) -> None:
        while self._pending and self._pending[0].t_end + self.eval_delay_s <= now:
            self._evaluate(self._pending.popleft())
        i = bisect.bisect_left(self._rt, now - self.keep_s)
        if i:
            del self._rt[:i]
            del self._rl[:i]

    # ---- core ----
    def _evaluate(self, tg: Telegram) -> None:
        mh = self.meters.setdefault(tg.meter, MeterHistory())
        usual = mh.usual_sectors(self.usual_ratio, self.min_history)
        missed = usual - tg.sectors
        lo = bisect.bisect_left(self._rt, tg.t_start - self.guard_s)
        hi = bisect.bisect_right(self._rt, tg.t_end + self._max_air + self.guard_s)
        overlapping = [h for h in self._rl[lo:hi]
                       if h is not tg and h.meter != tg.meter and _overlap(h, tg, self.guard_s)]
        for h in overlapping:
            if h.evaluated:                         # count each pair once
                self.c["pairs"] += 1
                if h.sectors & tg.sectors:
                    self.c["shared"] += 1
                else:
                    self.c["disjoint"] += 1
        for s in missed:
            if any(s in h.sectors for h in overlapping):
                self.coll_miss[s] += 1
                self.c["rescued"] += 1
            else:
                self.other_miss[s] += 1
        self.c["lost"] += mh.add(tg, self.min_history)
        tg.evaluated = True

    # ---- outputs ----
    def report(self) -> dict:
        sectors = []
        for s in self.sectors:
            rx, err = self.mod_rx[s], self.mod_err[s]
            sectors.append({
                "s": s, "telegrams": self.rx_ok[s], "only_here": self.only_here[s],
                "collision_miss": self.coll_miss[s], "other_miss": self.other_miss[s],
                "module_rx": rx, "module_rx_errors": err,
                "module_error_ratio": round(err / (rx + err), 4) if rx + err else None,
            })
        return {
            "telegrams": self.c["telegrams"], "copies": self.c["copies"],
            "multi_sector": self.c["multi_sector"],
            "overlap_pairs": {"both_received": self.c["pairs"], "disjoint_sectors": self.c["disjoint"],
                              "shared_sector": self.c["shared"]},
            "rescued_by_other_sector": self.c["rescued"],
            "lost_on_all_sectors_est": self.c["lost"],
            "sectors": sectors,
        }

    def meter_report(self) -> list[dict]:
        out = []
        for meter, mh in self.meters.items():
            mean = {s: round(mh.rssi_sum[s] / mh.rssi_n[s], 1) for s in mh.rssi_sum}
            home = max(mean, key=mean.get) if mean else None
            p = mh.period(self.min_history)
            out.append({
                "m": manufacturer_code(meter[:2]), "id": id_string(meter[2:6]), "n": mh.n,
                "home_sector": home, "rssi_by_sector": {str(k): v for k, v in sorted(mean.items())},
                "hit_rate_by_sector": {str(s): round(mh.rssi_n[s] / mh.n, 3) for s in sorted(mh.rssi_n)},
                "period_s": round(p, 1) if p else None, "lost_est": mh.lost,
            })
        return out

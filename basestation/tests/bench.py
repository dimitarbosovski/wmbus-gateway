"""Throughput of merge + decrypt + JSON + collision analysis (no serial, no MQTT).
python3 tests/bench.py   -> telegrams per second of CPU time on this machine.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wmbus_bs import oms                                  # noqa: E402
from wmbus_bs.imst import RxEvent                         # noqa: E402
from wmbus_bs.merge import Merger                         # noqa: E402
from wmbus_bs.output import telegram_record               # noqa: E402
from wmbus_bs.stats import CollisionAnalyzer              # noqa: E402
import vectors as V                                       # noqa: E402

N_METERS, SECONDS, RATE = 2000, 10, 1000                  # 1000 telegrams/s for 10 s
pkts = [V.mode5_meter(i.to_bytes(4, "little"), acc=i & 0xFF) for i in range(N_METERS)]

m, an = Merger(0.2), CollisionAnalyzer([1, 2, 3, 4, 5, 6])
out = 0
t0 = time.process_time()
for k in range(SECONDS * RATE):
    t = k / RATE
    p = pkts[k % N_METERS]
    for s in ((k % 6) + 1, ((k + 1) % 6) + 1)[: 1 + (k % 2)]:   # 1.5 copies on average
        m.add(s, RxEvent(0, 2, 5, 2, -80, p), t + 0.005, t, 0)
    for tg in m.flush(t):
        an.add(tg)
        r, pkt = oms.decrypt(tg.pkt, V.KEY)
        json.dumps(telegram_record("BS", tg, r, pkt, oms.access_number(tg.pkt)))
        out += 1
    an.poll(t)
cpu = time.process_time() - t0
print(f"{out} telegrams in {cpu:.2f} s CPU -> {out / cpu:.0f} telegrams/s")

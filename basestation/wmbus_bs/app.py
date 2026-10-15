"""Base station main: 6 sectors -> merge -> decrypt -> MQTT, plus collision statistics.

Run:  python3 -m wmbus_bs.app /etc/wmbus-bs/config.toml
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
import tomllib

from . import oms
from .channel import Channel
from .keys import KeyStore
from .merge import Merger, Telegram
from .output import MqttPublisher, iso, telegram_record
from .stats import CollisionAnalyzer

log = logging.getLogger("wmbus_bs")


class Station:
    def __init__(self, cfg: dict, publisher, keystore: KeyStore):
        self.cfg, self.pub, self.keys = cfg, publisher, keystore
        self.station = cfg["station_id"]
        prefix = cfg.get("mqtt", {}).get("topic_prefix", "findy/bs")
        self.t_telegram = f"{prefix}/{self.station}/telegram"
        self.t_stats = f"{prefix}/{self.station}/stats"
        self.t_meters = f"{prefix}/{self.station}/meters"
        radio = cfg.get("radio", {})
        self.merger = Merger(cfg.get("merge_window_ms", 200) / 1000,
                             radio.get("c_overhead_bits", 48), radio.get("s_overhead_chips", 48))
        sectors = [s["id"] for s in cfg["sector"]]
        self.analyzer = CollisionAnalyzer(sectors, **cfg.get("collision", {}))
        self.channels = [
            Channel(s["id"], s["port"], s.get("link_mode", "CT"), radio.get("baud", 115200),
                    radio.get("module_latency_ms", 0.0), radio.get("status_period_s", 60),
                    on_rx=self.merger_add, on_status=self.on_status)
            for s in cfg["sector"]
        ]
        self.published = 0
        self._tasks: set[asyncio.Task] = set()

    # ---- callbacks from channels ----
    def merger_add(self, sector, ev, t_host, t_end, wall_end) -> None:
        self.merger.add(sector, ev, t_host, t_end, wall_end)

    def on_status(self, sector, st) -> None:
        self.analyzer.add_module_status(sector, st.rx_packets, st.rx_errors)

    # ---- per telegram ----
    def _publish(self, tg: Telegram, key: bytes | None) -> None:
        result, pkt_out = oms.decrypt(tg.pkt, key)
        rec = telegram_record(self.station, tg, result, pkt_out, oms.access_number(tg.pkt))
        self.pub.publish(self.t_telegram, rec)
        self.published += 1

    async def _fetch_and_publish(self, tg: Telegram, addr) -> None:
        key = await asyncio.get_running_loop().run_in_executor(None, self.keys.fetch, addr)
        self._publish(tg, key)

    def handle(self, tg: Telegram) -> None:
        self.analyzer.add(tg)
        addr = oms.meter_address(tg.pkt)
        known, key = self.keys.get_cached(addr)
        if known:
            self._publish(tg, key)
        else:
            task = asyncio.create_task(self._fetch_and_publish(tg, addr))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ---- loops ----
    async def pump(self, period_s: float = 0.02) -> None:
        while True:
            now = time.monotonic()
            for tg in self.merger.flush(now):
                self.handle(tg)
            self.analyzer.poll(now)
            await asyncio.sleep(period_s)

    async def reporter(self) -> None:
        si = self.cfg.get("stats_interval_s", 60)
        mi = self.cfg.get("meters_interval_s", 3600)
        next_m = time.monotonic() + mi
        while True:
            await asyncio.sleep(si)
            rep = self.analyzer.report()
            rep.update({"v": 1, "station": self.station, "ts": iso(time.time()), "interval_s": si,
                        "ports": [{"s": c.sector, "port": c.port, "ready": c.ready,
                                   "hci_crc_errors": c.dec.crc_errors} for c in self.channels]})
            self.pub.publish(self.t_stats, rep)
            self.analyzer.reset_interval()
            if time.monotonic() >= next_m:
                next_m += mi
                self.pub.publish(self.t_meters, {"v": 1, "station": self.station, "ts": iso(time.time()),
                                                 "meters": self.analyzer.meter_report()})

    async def run(self) -> None:
        tasks = [asyncio.create_task(c.run()) for c in self.channels]
        tasks += [asyncio.create_task(self.pump()), asyncio.create_task(self.reporter())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()


def build_keystore(cfg: dict) -> KeyStore:
    k = cfg.get("keys", {})
    ks = KeyStore(k.get("service_url") or None, k.get("service_token") or None,
                  k.get("cache_ttl_s", 3600), k.get("miss_ttl_s", 300))
    if k.get("file"):
        n = ks.load_file(k["file"])
        log.info("loaded %d keys from %s", n, k["file"])
    return ks


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with open(sys.argv[1] if len(sys.argv) > 1 else "/etc/wmbus-bs/config.toml", "rb") as f:
        cfg = tomllib.load(f)
    m = cfg["mqtt"]
    prefix = m.get("topic_prefix", "findy/bs")
    pub = MqttPublisher(m["host"], m.get("port", 8883), m.get("client_id", cfg["station_id"]),
                        m.get("username", ""), m.get("password", ""), m.get("tls", True),
                        m.get("ca_file", ""), m.get("version", 4), m.get("qos", 1),
                        f"{prefix}/{cfg['station_id']}/status")
    station = Station(cfg, pub, build_keystore(cfg))

    async def runner() -> None:
        task = asyncio.create_task(station.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(runner())
    finally:
        pub.close()


if __name__ == "__main__":
    main()

"""End-to-end: 6 fake iM891A-XL modules on pseudo-terminals -> Station -> published JSON.

The fake module answers Ping (after ignoring the first two, like a module that is
still booting), Get/Set Active Config and Gateway Status, and can inject Rx events.
"""
import asyncio
import os
import struct
import sys
import tty
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wmbus_bs import hci, imst                              # noqa: E402
from wmbus_bs.app import Station                            # noqa: E402
from wmbus_bs.keys import KeyStore                          # noqa: E402
from wmbus_bs.output import ListPublisher                   # noqa: E402
import vectors as V                                         # noqa: E402


class FakeModule:
    def __init__(self):
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self.slave = slave
        self.dec = hci.SlipDecoder()
        self.pings = 0
        self.set_cfg = None
        self.rx_packets = 0

    def start(self, loop):
        os.set_blocking(self.master, False)
        loop.add_reader(self.master, self._on_read)

    def _send(self, sap, msg, payload=b""):
        os.write(self.master, hci.encode(sap, msg, payload))

    def _on_read(self):
        try:
            data = os.read(self.master, 4096)
        except BlockingIOError:
            return
        for f in self.dec.feed(data):
            if (f.sap, f.msg) == (imst.SAP_DEVMGMT, imst.DM_PING_REQ):
                self.pings += 1
                if self.pings > 2:
                    self._send(1, imst.DM_PING_RSP, b"\x00")
            elif (f.sap, f.msg) == (imst.SAP_WMBUS, imst.WM_GET_CFG_REQ):
                cfg = bytes([0, 0x01, 0, 0x1F, 0, 50, 0, 0xE8, 0x03, 0, 0])   # filter on, LEDs on
                self._send(9, imst.WM_GET_CFG_RSP, b"\x00" + cfg)
            elif (f.sap, f.msg) == (imst.SAP_WMBUS, imst.WM_SET_CFG_REQ):
                self.set_cfg = f.payload
                self._send(9, imst.WM_SET_CFG_RSP, b"\x00")
            elif (f.sap, f.msg) == (imst.SAP_WMBUS, imst.WM_STATUS_REQ):
                self.rx_packets += 100
                p = struct.pack("<BIIBHIIIIIII", 0, 0, 0, 3, 0, 1, self.rx_packets, 0, self.rx_packets // 50, 0, 0, 0)
                self._send(9, imst.WM_STATUS_RSP, p)

    def inject(self, pkt, rssi):
        self._send(9, imst.WM_RX_IND, V.rx_event_payload(pkt, rssi))


class TestStationOverPty(unittest.TestCase):
    def test_end_to_end(self):
        asyncio.run(self._run())

    async def _run(self):
        loop = asyncio.get_running_loop()
        mods = [FakeModule() for _ in range(6)]
        for m in mods:
            m.start(loop)
        cfg = {
            "station_id": "BS-TEST", "merge_window_ms": 200, "stats_interval_s": 1, "meters_interval_s": 1,
            "radio": {"status_period_s": 0.4},
            "sector": [{"id": i + 1, "port": m.path, "link_mode": "CT"} for i, m in enumerate(mods)],
            "mqtt": {"topic_prefix": "findy/bs"},
        }
        ks = KeyStore()
        ks.put("4493", "12345678", V.KEY.hex())   # M-field 0x4493 of V.mode5_short()
        pub = ListPublisher()
        st = Station(cfg, pub, ks)
        task = asyncio.create_task(st.run())
        for _ in range(100):
            if all(c.ready for c in st.channels):
                break
            await asyncio.sleep(0.05)
        self.assertTrue(all(c.ready for c in st.channels), "modules not initialised")
        for m in mods:
            self.assertEqual(m.set_cfg[0], 3)                      # C/T
            self.assertEqual(m.set_cfg[1] & 0x03, 0x02)            # Rx notify on, filter off

        known, _ = V.mode5_short()
        other = V.mode5_meter(bytes.fromhex("99887766"), acc=7)
        mods[1].inject(known, -81)                                 # sector 2
        mods[2].inject(known, -67)                                 # sector 3 (best)
        mods[4].inject(other, -90)                                 # sector 5, no key
        await asyncio.sleep(1.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        tel = [o for t, o in pub.messages if t.endswith("/telegram")]
        self.assertEqual(len(tel), 2)
        a = next(o for o in tel if o["id"] == "12345678")
        self.assertEqual((a["dec"], a["sector"], a["rssi"]), ("ok", 3, -67))
        self.assertEqual([r["s"] for r in a["rx"]], [3, 2])
        self.assertEqual(bytes.fromhex(a["telegram"])[15:17], b"\x2f\x2f")
        b = next(o for o in tel if o["id"] == "66778899")
        self.assertEqual((b["dec"], b["sector"]), ("no_key", 5))
        stats = [o for t, o in pub.messages if t.endswith("/stats")]
        self.assertTrue(stats)
        s1 = stats[0]
        self.assertEqual((s1["telegrams"], s1["copies"], s1["multi_sector"]), (2, 3, 1))
        self.assertTrue(any(s["module_rx"] >= 100 for s in s1["sectors"]))
        meters = [o for t, o in pub.messages if t.endswith("/meters")]
        self.assertTrue(meters and len(meters[0]["meters"]) == 2)
        for m in mods:
            os.close(m.master)
            os.close(m.slave)


if __name__ == "__main__":
    unittest.main()

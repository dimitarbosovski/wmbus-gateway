"""Unit tests. Run from the project root:  python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wmbus_bs import hci, imst, oms                      # noqa: E402
from wmbus_bs.airtime import airtime_s                     # noqa: E402
from wmbus_bs.imst import RxEvent                          # noqa: E402
from wmbus_bs.merge import Merger                          # noqa: E402
from wmbus_bs.stats import CollisionAnalyzer               # noqa: E402
import vectors as V                                        # noqa: E402


class TestHci(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(hci.crc16(b"123456789"), 0x906E)

    def test_ping_matches_spec_example(self):                       # HCI spec p.9
        self.assertEqual(hci.encode(0x01, 0x01), bytes.fromhex("C0 01 01 16 07 C0"))

    def test_decode_pong(self):
        d = hci.SlipDecoder()
        (f,) = d.feed(bytes.fromhex("C0 01 02 00 A0 AF C0"))
        self.assertEqual((f.sap, f.msg, f.payload), (1, 2, b"\x00"))

    def test_escape_roundtrip_and_crc_error(self):
        d = hci.SlipDecoder()
        wire = hci.encode(0x09, 0x20, b"\xc0\xdb\x00\xc0")
        (f,) = d.feed(wire)
        self.assertEqual(f.payload, b"\xc0\xdb\x00\xc0")
        self.assertEqual(f.wire_len, len(wire) - 1)        # leading END belongs to the previous frame
        bad = bytearray(wire); bad[3] ^= 1
        self.assertEqual(d.feed(bytes(bad)), [])
        self.assertEqual(d.crc_errors, 1)


class TestOms(unittest.TestCase):
    def test_kdf_oms_annex_n(self):                                 # OMS Annex N.2.3
        mcr, ident = bytes.fromhex("B30A0000"), bytes.fromhex("78563412")
        self.assertEqual(oms.kdf_mode7(V.KEY, 0, mcr, ident).hex().upper(), "ECCF39D475D730B8284FDFDC1995D52F")
        self.assertEqual(oms.kdf_mode7(V.KEY, 1, mcr, ident).hex().upper(), "C9CD19FF5A9AAD5A6BBDA13BD2C4C7AD")

    def test_mode5_short_long(self):
        pkt, plain = V.mode5_short()
        r, out = oms.decrypt(pkt, V.KEY)
        self.assertEqual(r, oms.Result.OK)
        self.assertEqual(out[15:15 + len(plain)], plain)
        r, _ = oms.decrypt(V.mode5_long()[0], V.KEY)
        self.assertEqual(r, oms.Result.OK)
        self.assertEqual(oms.decrypt(pkt, bytes(16))[0], oms.Result.CHECK_FAIL)
        self.assertEqual(oms.decrypt(pkt, None)[0], oms.Result.NO_KEY)
        self.assertEqual(oms.access_number(pkt), 0x05)

    def test_mode7(self):
        pkt, plain, _, _ = V.mode7_short()
        r, out = oms.decrypt(pkt, V.KEY)
        self.assertEqual(r, oms.Result.OK)
        self.assertEqual(out[33:33 + len(plain)], plain)
        self.assertEqual(oms.decrypt(V.mode7_short(tamper=True)[0], V.KEY)[0], oms.Result.MAC_FAIL)

    def test_manufacturer_code(self):
        self.assertEqual(oms.manufacturer_bytes("KAM"), bytes([0x2D, 0x2C]))
        self.assertEqual(oms.manufacturer_code(bytes([0x2D, 0x2C])), "KAM")
        self.assertEqual(oms.id_string(bytes.fromhex("78563412")), "12345678")


class TestImst(unittest.TestCase):
    def test_patch_cfg_keeps_other_fields(self):
        cfg = bytes([0, 0x01, 0x00, 0x1F, 0x00, 50, 0, 0xE8, 0x03, 0, 0])
        out = imst.patch_cfg(cfg, imst.LINK_MODES["CT"])
        self.assertEqual(out[0], 3)
        self.assertEqual(out[1] & 0x03, imst.OPT_RX_NOTIFY)        # filter off, notify on
        self.assertEqual(out[3:5], b"\x00\x00")                      # LED signalling off
        self.assertEqual(out[5:], cfg[5:])

    def test_parse_status(self):
        import struct
        p = struct.pack("<BIIBHIIIIIII", 0, 100, 90, 3, 0, 2, 1000, 0, 37, 0, 0, 0)
        st = imst.parse_status(p)
        self.assertEqual((st.rx_packets, st.rx_errors, st.link_mode), (1000, 37, 3))


class TestAirtime(unittest.TestCase):
    def test_t_mode_frame_in_published_range(self):
        # L = 0x2A: 43 B + 4 CRCs = 51 B -> 38+10+612+8 chips = 6.68 ms
        self.assertAlmostEqual(airtime_s(2, 0x2A), 0.00668, places=6)
        self.assertTrue(0.003 <= airtime_s(2, 0x2A) <= 0.008)     # ST AN4772: "typically 3 to 8 ms"


def ev(pkt, rssi):
    return RxEvent(0, 2, 5, 2, rssi, pkt)


class TestMerge(unittest.TestCase):
    def test_three_sectors_one_telegram(self):
        m = Merger(0.2)
        pkt = V.mode5_meter(bytes.fromhex("01000000"))
        m.add(2, ev(pkt, -80), 10.000, 9.990, 0)
        m.add(3, ev(pkt, -70), 10.004, 9.991, 0)
        m.add(4, ev(pkt, -95), 10.010, 9.992, 0)
        self.assertEqual(m.flush(10.1), [])
        (tg,) = m.flush(10.21)
        self.assertEqual(tg.sectors, frozenset({2, 3, 4}))
        self.assertEqual((tg.best.sector, tg.best.rssi), (3, -70))
        m.add(2, ev(pkt, -80), 20.0, 19.99, 0)                        # next transmission
        self.assertEqual(len(m.flush(20.3)), 1)


class TestCollisions(unittest.TestCase):
    """Synthetic, deterministic timelines (seconds, monotonic)."""

    A, B, C = (bytes.fromhex(x) for x in ("11111111", "22222222", "33333333"))

    def feed(self, an, m, t_end, sector_rssi, ident, acc):
        pkt = V.mode5_meter(ident, acc=acc)
        for s, r in sector_rssi.items():
            m.add(s, ev(pkt, r), t_end + 0.005, t_end, 0)
        for tg in m.flush(t_end + 1):
            an.add(tg)
        an.poll(t_end + 1.0)

    def history(self, an, m, n=12, period=10.0):
        for i in range(n):
            self.feed(an, m, 10.0 + i * period, {2: -70, 3: -78}, self.A, i)
        return 10.0 + n * period

    def test_collision_on_sector_rescued_by_other(self):
        an, m = CollisionAnalyzer([1, 2, 3, 4, 5, 6]), Merger(0.2)
        t = self.history(an, m)
        # A reaches only sector 2; B is decoded on sector 3 at the same moment
        pa, pb = V.mode5_meter(self.A, acc=99), V.mode5_meter(self.B, acc=1)
        m.add(2, ev(pa, -70), t + 0.005, t, 0)
        m.add(3, ev(pb, -60), t + 0.006, t + 0.002, 0)
        for tg in m.flush(t + 1):
            an.add(tg)
        an.poll(t + 2)
        rep = an.report()
        self.assertEqual(rep["rescued_by_other_sector"], 1)
        self.assertEqual(rep["sectors"][2]["collision_miss"], 1)       # sector 3
        self.assertEqual(rep["overlap_pairs"], {"both_received": 1, "disjoint_sectors": 1, "shared_sector": 0})

    def test_miss_without_overlap_and_lost_transmission(self):
        an, m = CollisionAnalyzer([1, 2, 3, 4, 5, 6]), Merger(0.2)
        t = self.history(an, m)
        self.feed(an, m, t, {2: -70}, self.A, 50)                      # sector 3 missed, nothing overlapped
        self.feed(an, m, t + 20.0, {2: -70, 3: -78}, self.A, 51)       # one period skipped
        rep = an.report()
        self.assertEqual(rep["sectors"][2]["other_miss"], 1)
        self.assertEqual(rep["rescued_by_other_sector"], 0)
        self.assertEqual(rep["lost_on_all_sectors_est"], 1)
        (meter,) = an.meter_report()
        self.assertEqual((meter["id"], meter["home_sector"], meter["period_s"]), ("11111111", 2, 10.0))

    def test_shared_sector_pair_flags_timing_error(self):
        an, m = CollisionAnalyzer([1, 2, 3]), Merger(0.2)
        pa, pc = V.mode5_meter(self.A, acc=1), V.mode5_meter(self.C, acc=1)
        m.add(1, ev(pa, -70), 5.005, 5.000, 0)
        m.add(1, ev(pc, -72), 5.006, 5.001, 0)
        for tg in m.flush(6):
            an.add(tg)
        an.poll(7)
        self.assertEqual(an.report()["overlap_pairs"]["shared_sector"], 1)

    def test_module_counters_delta_and_reset(self):
        an = CollisionAnalyzer([1])
        an.add_module_status(1, 1000, 10)
        an.add_module_status(1, 1500, 40)
        an.add_module_status(1, 20, 1)                                  # module restarted
        s = an.report()["sectors"][0]
        self.assertEqual((s["module_rx"], s["module_rx_errors"]), (520, 31))


class TestKeyService(unittest.TestCase):
    def test_fetch_and_cache(self):
        import json
        import os
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlparse
        from wmbus_bs.keys import KeyStore
        calls = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                q = parse_qs(urlparse(self.path).query)
                calls.append((q["manufacturer"][0], q["id"][0], self.headers.get("Authorization")))
                if q["id"][0] == "12345678":
                    body = json.dumps({"key": V.KEY.hex()}).encode()
                    self.send_response(200); self.end_headers(); self.wfile.write(body)
                else:
                    self.send_response(404); self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ks = KeyStore(f"http://127.0.0.1:{srv.server_port}/keys", "tok")
        addr = (bytes([0x2D, 0x2C]), bytes.fromhex("78563412"))
        self.assertEqual(ks.get_cached(addr), (False, None))
        self.assertEqual(ks.fetch(addr), V.KEY)
        self.assertEqual(ks.get_cached(addr), (True, V.KEY))
        miss = (bytes([0x2D, 0x2C]), bytes.fromhex("01000000"))
        self.assertIsNone(ks.fetch(miss))
        self.assertEqual(ks.get_cached(miss), (True, None))          # negative cache
        self.assertEqual(calls[0], ("KAM", "12345678", "Bearer tok"))
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()

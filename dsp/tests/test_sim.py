"""python3 -m unittest discover -s tests -v   (from the project root)"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import phy                      # noqa: E402
import receiver as R            # noqa: E402
import scenario as S            # noqa: E402
import walsh as W               # noqa: E402


class TestPhy(unittest.TestCase):
    def test_crc_en13757_check_value(self):
        self.assertEqual(phy.crc16(b"123456789"), 0xC2B7)          # CRC-16/EN-13757

    def test_3of6_roundtrip_and_balance(self):
        data = bytes(range(256))
        chips = np.array(phy.bytes_to_chips_T(data))
        self.assertEqual(phy.decode_T(chips), data)
        self.assertTrue(all(bin(c).count("1") == 3 for c in phy.ENC3OF6))

    def test_access_code_and_c_mode_word(self):
        ch = phy.chips_for("C", phy.frame_a(bytes(9), bytes(21)))
        pre = 2 * phy.N_PRE["C"]
        word = int("".join(map(str, ch[pre - 6:pre + 10])), 2)
        self.assertEqual(word, 0x543D)                                # rtl-wmbus ACCESS_CODE_T1_C1
        self.assertEqual(int("".join(map(str, ch[pre + 10:pre + 26])), 2), 0x54CD)

    def test_frame_check(self):
        fr = phy.frame_a(bytes(range(9)), bytes(37))
        self.assertTrue(phy.check_frame_a(fr))
        bad = bytearray(fr); bad[20] ^= 1
        self.assertFalse(phy.check_frame_a(bytes(bad)))


class TestReceivers(unittest.TestCase):
    def setUp(self):
        self.meters = S.make_meters(100, np.random.default_rng(12345))
        self.tpl = R.Templates(self.meters)

    def test_single_packet_both_modes(self):
        rng = np.random.default_rng(1)
        for i in (0, 1):                                  # meter 0 = T1, meter 1 = C1
            fr = S.telegram(self.meters[i], rng)
            x = S.mix([(S.waveform(self.meters[i], fr, S.impair(rng)), 1500, 1.0)], 0.01, rng)
            self.assertTrue(any(d.frame == fr for d in R.baseline(x)))
            self.assertTrue(any(d.frame == fr for d in R.proposed(x, self.tpl)))

    def test_template_identifies_meter(self):
        rng = np.random.default_rng(2)
        fr = S.telegram(self.meters[4], rng)
        x = S.mix([(S.waveform(self.meters[4], fr, S.impair(rng)), 1500, 1.0)], 0.01, rng)
        (d,) = R.proposed(x, self.tpl, sic=False)
        self.assertEqual(d.meter, 4)

    def test_sic_recovers_weaker_overlapping_packet(self):
        rng = np.random.default_rng(3)
        a, b = self.meters[10], self.meters[11]
        fa, fb = S.telegram(a, rng), S.telegram(b, rng)
        wa, wb = S.waveform(a, fa, S.impair(rng)), S.waveform(b, fb, S.impair(rng))
        x = S.mix([(wa, 1500, 10 ** (9 / 20)), (wb, 1500 + len(wa) // 3, 1.0)], 0.01, rng)
        base = {d.frame for d in R.baseline(x)}
        prop = {d.frame for d in R.proposed(x, self.tpl)}
        self.assertEqual(base, {fa})                       # one demodulator: stronger only
        self.assertEqual(prop, {fa, fb})                   # SIC: both


class TestHeaderRepairVerification(unittest.TestCase):
    """A repaired header must be confirmed by something the template did not produce."""

    def setUp(self):
        self.meters = S.make_meters(100, np.random.default_rng(12345))

    def _rx(self, meter, rng, burst=None):
        fr = S.telegram(meter, rng)
        w = S.waveform(meter, fr, S.impair(rng, cfo_khz=0, rate_ppm=0, dev_pct=0))
        x = S.mix([(w, 1500, 1.0)], 0.001, rng)
        if burst:
            a, b = burst
            n = np.arange(b - a)
            x[a:b] += 3.0 * np.exp(1j * 2 * np.pi * 120e3 * n / phy.FS)   # strong tone over a few bytes
        return fr, x

    def test_oms5_check_needs_the_right_key(self):
        rng = np.random.default_rng(5)
        fr = S.telegram(self.meters[0], rng)
        self.assertTrue(R.oms5_check(fr, self.meters[0].key))
        self.assertFalse(R.oms5_check(fr, self.meters[1].key))

    def test_wrong_meter_is_not_accepted(self):
        # meter X is not in the bank, a near twin Y (one ID byte different, other key) is
        x = self.meters[0]
        y = R.Meter(x.m, bytes([x.ident[0] ^ 0x01]) + x.ident[1:], x.ver, x.typ, x.mode, x.L,
                    key=bytes(16))
        tpl = R.Templates([y] + self.meters[1:])
        fr, sig = self._rx(x, np.random.default_rng(6))
        got = R.proposed(sig, tpl, sic=False)
        self.assertEqual([d.frame for d in got], [fr])            # decoded as X from its own header
        self.assertIsNone(got[0].meter)
        self.assertFalse(got[0].repaired)

    def test_repair_confirmed_by_received_crc(self):
        mt = self.meters[0]                                        # T1
        start = 1500 + (2 * phy.N_PRE["T"] + 10 + 12 * 4) * phy.SPS  # header byte 4 (first ID byte)
        fr, sig = self._rx(mt, np.random.default_rng(7), burst=(start, start + 12 * phy.SPS))
        tpl = R.Templates(self.meters)
        self.assertEqual(R.baseline(sig), [])                       # plain decoder fails
        (d,) = R.proposed(sig, tpl, sic=False)
        self.assertEqual((d.frame, d.repaired, d.verify), (fr, True, "crc"))


class TestWalshDomain(unittest.TestCase):
    def setUp(self):
        self.meters = S.make_meters(100, np.random.default_rng(12345))
        self.t = R.Templates(self.meters)

    def test_fwht_is_hadamard_product(self):
        x = np.random.default_rng(0).normal(size=W.N)
        self.assertTrue(np.allclose(W.fwht(x), W.hadamard() @ x))
        self.assertTrue(np.allclose(W.hadamard() @ W.hadamard().T, W.N * np.eye(W.N)))

    def test_all_coefficients_equal_time_domain(self):
        tw = R.Templates(self.meters, bank=W.WalshBank(self.t.HT, self.t.HC))
        rng = np.random.default_rng(1)
        for mode, k in (("T", 108), ("C", 72)):
            s = rng.normal(size=k)
            self.assertTrue(np.allclose(self.t.score(mode, s), tw.score(mode, s)))   # Parseval

    def test_lowest_sequency_loses_meters_for_3of6(self):
        # 3-of-6 code words are DC-free: some meters have no energy in the 16 lowest sequencies
        bank = W.WalshBank(self.t.HT, self.t.HC, 16, "lowseq")
        self.assertGreater(bank.zero_rows, 0)

    def test_maxvar_32_identifies_meter(self):
        tw = R.Templates(self.meters, bank=W.WalshBank(self.t.HT, self.t.HC, 32, "maxvar"))
        rng = np.random.default_rng(2)
        for i in (0, 1):
            fr = S.telegram(self.meters[i], rng)
            x = S.mix([(S.waveform(self.meters[i], fr, S.impair(rng)), 1500, 1.0)], 0.01, rng)
            (d,) = R.proposed(x, tw, sic=False)
            self.assertEqual((d.frame, d.meter), (fr, i))


if __name__ == "__main__":
    unittest.main()

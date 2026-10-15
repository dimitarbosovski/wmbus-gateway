"""Walsh-domain address templates vs time-domain templates -> results/results_walsh.json

E3 identification: one telegram in noise; header soft chips at the detected sync peak;
   decision = best score > 0.55 and margin to the 2nd > 0.12 (same as the receiver).
   Outcome per trial: correct meter / wrong meter / no decision.
E4 end to end: E2 (SNR sweep, single telegram) and E1 (two overlapping telegrams) with
   the time-domain bank and with Walsh banks.
Usage: python3 experiments_walsh.py [trials]
"""
from __future__ import annotations

import json
import sys
import time
from multiprocessing import Pool

import numpy as np

import receiver as R
import scenario as S
import walsh as W

N_METERS = 100
SNRS = [0, 2, 4, 6, 8, 10]
VARIANTS = [("time", None, None), ("walsh_all", "all", 128),
            ("lowseq_64", "lowseq", 64), ("lowseq_32", "lowseq", 32), ("lowseq_16", "lowseq", 16),
            ("maxvar_64", "maxvar", 64), ("maxvar_32", "maxvar", 32), ("maxvar_16", "maxvar", 16)]
E1_SIRS = [-9, -3, 3, 9, 15]


def banks(meters):
    base = R.Templates(meters)
    out = {}
    for name, sel, m in VARIANTS:
        out[name] = base if sel is None else R.Templates(meters, bank=W.WalshBank(base.HT, base.HC, m, sel))
    return out


def e3_trial(args):
    snr, seed = args
    rng = np.random.default_rng(seed)
    meters = S.make_meters(N_METERS, np.random.default_rng(12345))
    bk = banks(meters)
    i = int(rng.integers(N_METERS))
    fr = S.telegram(meters[i], rng)
    x = S.mix([(S.waveform(meters[i], fr, S.impair(rng)), 1500, 1.0)], 10 ** (-snr / 10), rng)
    m = R.soft_chips(x)
    peaks = R.sync_peaks(m)
    res = {}
    for name, _, _ in VARIANTS:
        out = "none"
        if peaks:
            p = max(peaks, key=lambda q: q[1])[0]
            mode, off, per = (meters[i].mode, 16, 8) if meters[i].mode == "C" else ("T", 0, 12)
            hs = R.chips_at(m, p, off + per, 9 * per)
            if hs is not None:
                s = bk[name].score(mode, hs)
                o = np.argsort(s)[::-1]
                if s[o[0]] > 0.55 and s[o[0]] - s[o[1]] > 0.12:
                    out = "correct" if o[0] == i else "wrong"
        e2 = R.proposed(x, bk[name], sic=False)
        res[name] = (out, any(d.frame == fr for d in e2), sum(1 for d in e2 if d.frame != fr))
    return snr, res


def e1_trial(args):
    sir, seed = args
    rng = np.random.default_rng(seed)
    meters = S.make_meters(N_METERS, np.random.default_rng(12345))
    bk = banks(meters)
    ia, ib = rng.choice(N_METERS, 2, replace=False)
    fa, fb = S.telegram(meters[ia], rng), S.telegram(meters[ib], rng)
    wa = S.waveform(meters[ia], fa, S.impair(rng))
    wb = S.waveform(meters[ib], fb, S.impair(rng))
    amp_a, amp_b = (10 ** (sir / 20), 1.0) if sir >= 0 else (1.0, 10 ** (-sir / 20))
    sb = 1500 + int(rng.uniform(0.1, 0.9) * len(wa))
    x = S.mix([(wa, 1500, amp_a), (wb, sb, amp_b)], 10 ** (-20 / 10), rng)
    res = {}
    for name in ("time", "walsh_all", "maxvar_32", "lowseq_32"):
        got = R.proposed(x, bk[name])
        res[name] = (any(d.frame == fa for d in got) and any(d.frame == fb for d in got),
                     sum(1 for d in got if d.frame not in (fa, fb)))
    return sir, res


def main():
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    t0 = time.time()
    with Pool() as pool:
        e3 = pool.map(e3_trial, [(s, 500000 + 1000 * k + i) for k, s in enumerate(SNRS) for i in range(trials)])
        e1 = pool.map(e1_trial, [(s, 700000 + 1000 * k + i) for k, s in enumerate(E1_SIRS) for i in range(trials // 2)])
    out = {"trials_e3": trials, "trials_e1": trials // 2, "e3": {}, "e1": {}, "ops": {}}
    for snr in SNRS:
        rows = [r for s, r in e3 if s == snr]
        out["e3"][snr] = {name: {"correct": sum(r[name][0] == "correct" for r in rows) / len(rows),
                                 "wrong": sum(r[name][0] == "wrong" for r in rows) / len(rows),
                                 "decoded": sum(r[name][1] for r in rows) / len(rows),
                                 "misattributed": sum(r[name][2] for r in rows)}
                          for name, _, _ in VARIANTS}
    for sir in E1_SIRS:
        rows = [r for s, r in e1 if s == sir]
        out["e1"][sir] = {name: {"both": sum(r[name][0] for r in rows) / len(rows),
                                 "misattributed": sum(r[name][1] for r in rows)} for name in rows[0]}
    for name, sel, m in VARIANTS:
        out["ops"][name] = 250 * 108 if sel is None else W.N * 7 + 250 * m
    out["seconds"] = round(time.time() - t0, 1)
    json.dump(out, open("results/results_walsh.json", "w"), indent=1)
    for snr in SNRS:
        print(snr, {k: (v["correct"], v["wrong"], v["decoded"], v["misattributed"]) for k, v in out["e3"][snr].items()})
    for sir in E1_SIRS:
        print("E1", sir, out["e1"][sir])
    print("ops", out["ops"], "seconds", out["seconds"])


if __name__ == "__main__":
    main()

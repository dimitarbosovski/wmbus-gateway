"""Monte-Carlo experiments -> results/results.json

E1: two overlapping telegrams (A starts first, B starts 10-90 % into A), SIR = P_A / P_B,
    weaker signal at SNR 20 dB (noise in the 500 kHz sample bandwidth), random meter pair
    from 100 known meters (half T1, half C1), CFO +-20 kHz, chip rate +-100 ppm,
    deviation +-3 %.
E2: one telegram in noise, SNR sweep (does the template bank change sensitivity?).
Usage: python3 experiments.py [trials]
"""
from __future__ import annotations

import json
import sys
import time
from multiprocessing import Pool

import numpy as np

import receiver as R
import scenario as S

N_METERS = 100
SIRS = list(range(-15, 22, 3))
SNRS = list(range(0, 15, 2))
RX = ("baseline", "proposed", "no_template", "no_sic")


def run_rx(name, x, tpl):
    if name == "baseline":
        return R.baseline(x)
    if name == "proposed":
        return R.proposed(x, tpl)
    if name == "no_template":
        return R.proposed(x, None)
    return R.proposed(x, tpl, sic=False)


def e1_trial(args):
    sir, seed = args
    rng = np.random.default_rng(seed)
    meters = S.make_meters(N_METERS, np.random.default_rng(12345))
    tpl = R.Templates(meters)
    ia, ib = rng.choice(N_METERS, 2, replace=False)
    fa, fb = S.telegram(meters[ia], rng), S.telegram(meters[ib], rng)
    wa = S.waveform(meters[ia], fa, S.impair(rng))
    wb = S.waveform(meters[ib], fb, S.impair(rng))
    amp_a, amp_b = (10 ** (sir / 20), 1.0) if sir >= 0 else (1.0, 10 ** (-sir / 20))
    sb = 1500 + int(rng.uniform(0.1, 0.9) * len(wa))
    x = S.mix([(wa, 1500, amp_a), (wb, sb, amp_b)], noise_power=10 ** (-20 / 10), rng=rng)
    out = {}
    for name in RX:
        got = run_rx(name, x, tpl)
        out[name] = (any(d.frame == fa for d in got), any(d.frame == fb for d in got),
                     sum(1 for d in got if d.frame not in (fa, fb)),
                     sum(1 for d in got if d.repaired and d.frame in (fa, fb)))
    return sir, out


def e2_trial(args):
    snr, seed = args
    rng = np.random.default_rng(seed)
    meters = S.make_meters(N_METERS, np.random.default_rng(12345))
    tpl = R.Templates(meters)
    i = int(rng.integers(N_METERS))
    fr = S.telegram(meters[i], rng)
    w = S.waveform(meters[i], fr, S.impair(rng))
    x = S.mix([(w, 1500, 1.0)], noise_power=10 ** (-snr / 10), rng=rng)
    prop = R.proposed(x, tpl, sic=False)
    return snr, {"baseline": any(d.frame == fr for d in R.baseline(x)),
                 "proposed": any(d.frame == fr for d in prop),
                 "repaired": any(d.frame == fr and d.repaired for d in prop),
                 "repaired_crc": any(d.frame == fr and d.repaired and d.verify == "crc" for d in prop),
                 "repaired_2f2f": any(d.frame == fr and d.repaired and d.verify == "soft_crc+2f2f" for d in prop),
                 "wrong": sum(1 for d in prop if d.frame != fr)}


def main():
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    t0 = time.time()
    with Pool() as pool:
        e1 = pool.map(e1_trial, [(s, 1000 * k + i) for k, s in enumerate(SIRS) for i in range(trials)])
        e2 = pool.map(e2_trial, [(s, 900000 + 1000 * k + i) for k, s in enumerate(SNRS) for i in range(trials)])
    res = {"trials": trials, "e1": {}, "e2": {}}
    for sir in SIRS:
        rows = [o for s, o in e1 if s == sir]
        res["e1"][sir] = {name: {"a": sum(r[name][0] for r in rows) / len(rows),
                                 "b": sum(r[name][1] for r in rows) / len(rows),
                                 "both": sum(r[name][0] and r[name][1] for r in rows) / len(rows),
                                 "false": sum(r[name][2] for r in rows),
                                 "repaired": sum(r[name][3] for r in rows)} for name in RX}
    for snr in SNRS:
        rows = [o for s, o in e2 if s == snr]
        res["e2"][snr] = {k: sum(r[k] for r in rows) / len(rows)
                          for k in ("baseline", "proposed", "repaired", "repaired_crc", "repaired_2f2f")}
        res["e2"][snr]["wrong"] = sum(r["wrong"] for r in rows)
    res["seconds"] = round(time.time() - t0, 1)
    json.dump(res, open("results/results.json", "w"), indent=1)
    print(json.dumps(res["e1"], indent=0)[:3000])
    print(json.dumps(res["e2"]))
    print("seconds", res["seconds"])


if __name__ == "__main__":
    main()

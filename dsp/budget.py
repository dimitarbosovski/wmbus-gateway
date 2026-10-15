"""Processing load, memory, IQ link rate and collision probability for the gateway.
All numbers are computed from the parameters below -> results/budget.json.
"""
import json
import math

import phy
import receiver as R

FS = phy.FS
N_METERS = 250                 # upper end of 50-250
INTERVAL_S = 60                # meters transmit every <= 1 min
WINDOW_S = 180
TELEGRAMS_PER_S = N_METERS / INTERVAL_S


def airtime_s(mode, L):
    n_air = phy.air_len(L)
    chips = 2 * phy.N_PRE[mode] + 10 + (16 + 8 * n_air if mode == "C" else 12 * n_air) + 4
    return chips / phy.CHIP_RATE


# ---- telegram lengths used in the simulation (L = 0x1E / 0x2E, T1 and C1)
lengths = {f"{m}{L:#x}": airtime_s(m, L) for m in ("T", "C") for L in (0x1E, 0x2E)}
t_mean = sum(lengths.values()) / len(lengths)
t_max = airtime_s("T", 255)

# ---- pure ALOHA: a telegram is hit if another starts within +-T (vulnerable period 2T)
p_overlap = 1 - math.exp(-TELEGRAMS_PER_S * 2 * t_mean)

# ---- operations per second (1 op = 1 multiply-accumulate or add/compare)
OPS_DISC = 10        # quadricorrelator: conj multiply (4 mul + 2 add) + |x|^2 (2 mul + 1 add) + 1 div
OPS_MF = 2           # running sum over one chip
OPS_CORR = 2 * R.N_TPL   # 26 MAC correlation + 26 MAC energy
front_end = FS * (OPS_DISC + OPS_MF + OPS_CORR)
tpl_bank_per_candidate = N_METERS * 9 * 12            # T1 header 108 chips
tpl_bank = TELEGRAMS_PER_S * tpl_bank_per_candidate
GRID = 5 * 5 * 5                                       # rate x deviation x timing
OPS_SIC_SAMPLE = 25
sic_per_packet = GRID * OPS_SIC_SAMPLE * t_mean * FS
sic_all = sic_per_packet * TELEGRAMS_PER_S             # if every telegram were cancelled
sic_overlapped = sic_all * p_overlap                   # only when a second sync peak falls inside

# ---- memory: IQ ring buffer for two overlapping max-length telegrams, int16 I + int16 Q
iq_buf = 2 * t_max * FS * 4
soft_buf = 2 * t_max * FS * 2
tpl_mem = N_METERS * 108                               # int8 +-1
ram = iq_buf + soft_buf + tpl_mem

# ---- IQ link STM32WL3x -> main MCU
link_8bit = FS * 2 * 8
link_16bit = FS * 2 * 16

res = {
    "params": {"meters": N_METERS, "interval_s": INTERVAL_S, "window_s": WINDOW_S, "fs": FS},
    "airtime_ms": {k: round(v * 1e3, 2) for k, v in lengths.items()} | {"T_L255": round(t_max * 1e3, 1)},
    "telegrams_per_s": round(TELEGRAMS_PER_S, 2),
    "p_overlap": round(p_overlap, 4),
    "ops_per_s": {"front_end": front_end, "template_bank": tpl_bank, "sic_if_all": sic_all,
                  "sic_overlapped_only": sic_overlapped, "sic_per_packet": sic_per_packet},
    "ram_bytes": {"iq": iq_buf, "soft": soft_buf, "templates": tpl_mem, "total": ram},
    "link_bit_s": {"iq8": link_8bit, "iq16": link_16bit},
}
json.dump(res, open("results/budget.json", "w"), indent=1)
print(json.dumps(res, indent=1))

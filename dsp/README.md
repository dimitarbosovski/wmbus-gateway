# Receiver DSP

Receiver for the battery gateway that decodes wM-Bus telegrams overlapping in time.
It works on complex IQ samples at 500 kS/s (STM32WL3x in IQ mode) and covers T1 and
C1, frame format A. This directory holds the reference implementation in
Python/NumPy, the Monte-Carlo experiments and the compute budget for the MCU port.

![Receiver](../docs/img/dsp-receiver.svg)

## Signal chain

1. **Front end.** FM discriminator, chip matched filter (integrate over one chip,
   5 samples), sync correlator over 16 preamble chips + 10 sync chips. The access
   code `0x543D` is shared by T1 and C1; the C1 mode word `0x54CD` follows it.
2. **No busy lock.** Every correlator peak is a candidate, also inside a telegram
   that is already being decoded. A classic radio stays locked on the first packet
   until its end.
3. **Address templates.** Matrix H has one row per known meter: the chips of its
   header (C, M, ID, version, type), 3-out-of-6 for T1 (108 chips), NRZ for C1
   (72 chips). The score is H · s, one matrix-vector product per candidate. The
   rows are not orthogonal: with 100 meters the correlation between rows is 0.27 on
   average and up to 0.75, because C field and manufacturer repeat. A meter is
   accepted at score > 0.55 and a margin ≥ 0.12 to the second best.
4. **Header repair.** When the template wins but the received header differs, the
   known header is accepted only with an independent check:
   - the CRC received on air matches the repaired header, or
   - the CRC chips match softly *and* the meter key decrypts the first block to
     `2F 2F` (OMS mode 5).

   Otherwise the header stays as received.
5. **Successive interference cancellation.** A decoded telegram is re-modulated;
   CFO, chip rate, deviation, phase and a piecewise complex gain are estimated from
   the received IQ, and the telegram is subtracted. Detection runs again on the
   residual, up to 4 times. With the deviation estimate the cancellation is
   28–32 dB (20–22 dB without it).

```python
# repaired header is accepted only with an independent check
rep[1:10] = meter.header9
exp_crc = crc16(rep[:10])
if exp_crc == received_crc:                             # CRC received on air
    verify = "crc"
elif soft_match(exp_crc_chips, rx_crc_chips) > 0.5 and oms5_check(rep, meter.key):
    verify = "soft_crc+2f2f"                            # meter key decrypts to 2F2F
else:
    rep = received                                      # no repair
```

## Walsh-domain address matrix

`walsh.py` moves the template bank into the Walsh-Hadamard domain.

- The header chips of every known meter are zero-padded to N = 128 and transformed
  once with the fast Walsh-Hadamard transform (FWHT) when the key list is loaded.
- Per candidate: one FWHT of the received soft chips (128 × 7 additions), then the
  comparison in the Walsh domain.
- With all 128 coefficients the scores are identical to the time domain, because
  the Walsh matrix is orthogonal (Parseval). `tests/test_sim.py` checks this.
- Modified matrix: only M coefficients are kept per mode, either the ones that vary
  most across the known meters (`maxvar`) or the lowest sequencies (`lowseq`, a
  sequency low-pass).

Meter identification, one telegram in noise, 200 telegrams per point, 100 meters
(T1 and C1), SNR in 500 kHz. "Wrong meter" is the template decision before the
header check; "misattributed" counts telegrams after the check.

| bank | ops/candidate (250 meters) | correct @ 0 dB | correct @ 2 dB | correct @ 4 dB | correct @ 6 dB | wrong meter, max | misattributed |
|---|---:|---:|---:|---:|---:|---:|---:|
| time domain, H·s | 27,000 | 44 % | 89 % | 98.5 % | 100 % | 0 % | 0 |
| Walsh, all 128 | 32,896 | 44 % | 89 % | 98.5 % | 100 % | 0 % | 0 |
| Walsh, max-variance 64 | 16,896 | 37 % | 87 % | 98 % | 100 % | 0 % | 0 |
| Walsh, max-variance 32 | 8,896 | 23.5 % | 75 % | 94 % | 99 % | 0.5 % | 0 |
| Walsh, max-variance 16 | 4,896 | 11 % | 45.5 % | 70 % | 80 % | 3.5 % | 0 |
| Walsh, lowest sequency 64 | 16,896 | 27.5 % | 81.5 % | 95.5 % | 98 % | 0.5 % | 0 |
| Walsh, lowest sequency 32 | 8,896 | 8.5 % | 32.5 % | 53 % | 58.5 % | 0.5 % | 0 |
| Walsh, lowest sequency 16 | 4,896 | 0.5 % | 3.5 % | 5.5 % | 9 % | 1.5 % | 0 |

Two overlapping telegrams, both received (100 per point):

| SIR, dB | time domain, H·s | Walsh, all 128 | Walsh, max-variance 32 | Walsh, lowest sequency 32 |
|---:|---:|---:|---:|---:|
| -9 | 91 % | 91 % | 91 % | 90 % |
| -3 | 70 % | 70 % | 70 % | 70 % |
| 3 | 70 % | 70 % | 70 % | 68 % |
| 9 | 95 % | 95 % | 95 % | 95 % |
| 15 | 97 % | 97 % | 97 % | 94 % |

- 64 max-variance coefficients identify meters almost as well as the full bank
  with 37 % fewer operations; 32 lose at SNR ≤ 4 dB.
- Lowest sequency does not work here: 3-out-of-6 code words are DC-free and the
  address sits in the higher sequencies. With 16 coefficients some meters have no
  energy at all in the kept set.
- The template bank is below 1 % of the total load, so the Walsh variant saves
  operations there without changing the overall budget. It pays off with many more
  meters or when templates are searched at every position without a sync.

## Results

### Two overlapping telegrams

A starts first, B starts 10–90 % into A. SIR = P_A / P_B; negative means the later
telegram B is stronger. Weaker signal at SNR 20 dB (500 kHz), CFO ±20 kHz, chip rate
±100 ppm, deviation ±3 %, random pair out of 100 meters, payload encrypted with OMS
mode 5. 200 trials per point.

| SIR A/B, dB | baseline: stronger | baseline: both | no cancellation: stronger | SIC, no templates: both | templates + SIC: both |
|---:|---:|---:|---:|---:|---:|
| -15 | 2.5 % | 0 % | 100 % | 84 % | 86 % |
| -12 | 4 % | 0 % | 100 % | 91.5 % | 93.5 % |
| -9 | 2.5 % | 0 % | 100 % | 93.5 % | 95.5 % |
| -6 | 3.5 % | 0 % | 100 % | 85 % | 85.5 % |
| -3 | 2.5 % | 0 % | 97.5 % | 73.5 % | 75 % |
| 0 | 0 % | 0 % | 0 % | 1 % | 1.5 % |
| 3 | 98 % | 0 % | 98 % | 74 % | 79 % |
| 6 | 100 % | 0 % | 100 % | 84.5 % | 86.5 % |
| 9 | 100 % | 0 % | 100 % | 88.5 % | 89.5 % |
| 12 | 100 % | 0 % | 100 % | 95.5 % | 97.5 % |
| 15 | 100 % | 0 % | 100 % | 96 % | 98 % |
| 18 | 100 % | 0 % | 100 % | 89 % | 94.5 % |
| 21 | 100 % | 0 % | 100 % | 81.5 % | 88 % |

- Baseline = one demodulator that does not look for a new sync while it receives a
  packet. It never gets both, and it loses the stronger telegram in 96–98 % of the
  cases when that one arrives second.
- Searching for sync everywhere already saves the stronger telegram. Cancellation
  brings the weaker one; the templates add 0–7 percentage points on top.
- At equal power (0 dB) neither receiver gets anything.
- No false telegrams in any run.

### Single telegram in noise

| SNR, dB | baseline | proposed | header repaired (CRC) | header repaired (soft CRC + 2F2F) | wrong |
|---:|---:|---:|---:|---:|---:|
| 0 | 0 % | 0 % | 0 % | 0 % | 0 |
| 2 | 0 % | 0 % | 0 % | 0 % | 0 |
| 4 | 0 % | 2 % | 2 % | 0 % | 0 |
| 6 | 36 % | 45 % | 8.5 % | 0.5 % | 0 |
| 8 | 91.5 % | 92 % | 0.5 % | 0 % | 0 |
| 10 | 99.5 % | 99.5 % | 0 % | 0 % | 0 |
| 12 | 100 % | 100 % | 0 % | 0 % | 0 |
| 14 | 99.5 % | 100 % | 0 % | 0 % | 0 |

The template bank adds 9 points at 6 dB; 8.5 of them are confirmed by the CRC
received on air.

## What it means for the gateway

| Quantity | Value | How |
|---|---|---|
| telegrams per second | 4.17 | 250 meters / 60 s |
| mean airtime | 5.2 ms | T1 and C1, L = 0x1E and 0x2E: 3.6–7.1 ms |
| overlapping telegrams | 4.2 % | 1 − e^(−λ·2T), pure ALOHA |
| meter missed in a 180 s window | ≤ 0.0075 % | 0.042³: three transmissions, each overlapped one lost |
| meter missed in a 60 s window | ≤ 4.2 % | one transmission in the window |

With a 180 s window collisions hardly cost any meter even without the filter,
because every meter transmits three times. The filter matters when the window is
cut down to one transmission (less radio current), when the network gets denser, or
with meters that transmit less often.

## Compute budget

| Part | ops/s | |
|---|---:|---|
| discriminator + filter + correlator | 32 M | 500 kS/s × (10 + 2 + 52) ops per sample |
| template bank | 0.11 M | 250 meters × 108 chips × 4.2 candidates/s |
| cancellation, only on overlap | 1.4 M | 8.1 M per telegram (5 × 5 × 5 grid) × 4.2 % overlapped |
| RAM | 233 KB | IQ for 2 telegrams of 35 ms (int16) + soft chips + templates |
| WL3x -> MCU link | 8 / 16 Mbit/s | 8- or 16-bit I and Q at 500 kS/s |

About 34 M ops/s in total, ~21 % of an STM32U575 at 160 MHz at one op per cycle
(counted from the code). On the MCU the rate/deviation grid of the cancellation can
be replaced by a direct estimate at about a tenth of the cost. Next step up if
needed: STM32H743 (480 MHz, up to 1 MB RAM).

![Gateway with IQ radio](../docs/img/gateway-iq-hardware.svg)

- STM32WL3x: I/Q data access, wM-Bus, Cortex-M0+ at 64 MHz, 6.8 mA Rx at 868 MHz.
  It only delivers IQ; processing is on the main MCU.
- Splitter or two antennas: a splitter costs about 3 dB per branch, two antennas
  keep the sensitivity.
- STM32U575: Cortex-M33 160 MHz with DSP and FPU, 786 KB SRAM, 19.5 µA/MHz.
- Extra current for a 180 s window: about (6.8 + 3.1) mA × 0.05 h ≈ 0.5 mAh/day
  without peripherals.
- The simulated receiver also decodes single telegrams as well as the baseline, so
  with the IQ path in place the iM891A-XL can be dropped.

## Model

- rectangular FSK (no Gaussian shaping), AWGN, no multipath, two telegrams per
  trial, format A only
- CFO ±20 kHz, chip rate ±100 ppm, deviation ±3 %, C1 preamble 16 × `01`
- the baseline models a single demodulator that ignores new syncs while it is
  receiving a packet

## Files

| File | |
|---|---|
| `phy.py` | T1 (3-out-of-6) and C1 (NRZ), format A, CRC-16 EN 13757, FSK modulator |
| `receiver.py` | sync correlator, template bank, decoder, header check, cancellation, baseline |
| `walsh.py` | FWHT, Walsh-domain template bank (all / max-variance / lowest sequency) |
| `scenario.py` | meters, telegrams, CFO / rate / deviation, mixing with noise |
| `experiments.py` | SIR and SNR sweeps, four receivers -> `results/results.json` |
| `experiments_walsh.py` | meter identification and pairs per Walsh variant -> `results/results_walsh.json` |
| `budget.py` | ops, RAM, link rate, overlap probability -> `results/budget.json` |
| `tests/` | CRC = 0xC2B7, 3-out-of-6 table, access code, meter identification, SIC on two telegrams, rejection of unverified header repair, Walsh = time domain |

```sh
pip install numpy cryptography
python3 -m unittest discover -s tests
python3 experiments.py 200
python3 experiments_walsh.py 200
python3 budget.py
```

"""Address templates in the Walsh domain (modified Walsh address matrix).

The header chips of every known meter (T1: 108, C1: 72; +-1) are zero-padded to N = 128
and transformed with the fast Walsh-Hadamard transform (FWHT): Hw = H . W.
For a candidate the received soft chips s are transformed once: sw = W s (N log2 N
additions). Because W is orthogonal (W W^T = N I), Hw . sw / N = H . s, so with all N
coefficients the scores are identical to the time-domain bank (Parseval).
"Modified": only a subset of M coefficients is kept per mode,
  - "lowseq": the M lowest-sequency Walsh functions (sequency low-pass), or
  - "maxvar": the M coefficients that vary most across the known meters (they carry the
    address information; coefficients that are equal for all meters are dropped).
Score = cosine similarity over the kept coefficients (equals the time-domain score for M = N).
"""
from __future__ import annotations

import numpy as np

N = 128


def fwht(a: np.ndarray) -> np.ndarray:
    """Unnormalised fast Walsh-Hadamard transform over the last axis (natural order)."""
    a = np.array(a, dtype=float, copy=True)
    h = 1
    n = a.shape[-1]
    while h < n:
        a = a.reshape(a.shape[:-1] + (n // (2 * h), 2, h))
        x, y = a[..., 0, :].copy(), a[..., 1, :].copy()
        a[..., 0, :], a[..., 1, :] = x + y, x - y
        a = a.reshape(a.shape[:-3] + (n,))
        h *= 2
    return a


def hadamard(n: int = N) -> np.ndarray:
    return fwht(np.eye(n))


def sequency(n: int = N) -> np.ndarray:
    """Number of sign changes of each natural-order Hadamard row."""
    Hm = hadamard(n)
    return np.count_nonzero(np.diff(np.sign(Hm), axis=1), axis=1)


def pad(x: np.ndarray, n: int = N) -> np.ndarray:
    out = np.zeros(x.shape[:-1] + (n,))
    out[..., : x.shape[-1]] = x
    return out


class WalshBank:
    """Drop-in replacement for the time-domain score H @ s."""

    def __init__(self, HT: np.ndarray, HC: np.ndarray, m: int = N, select: str = "all"):
        self.m, self.select = m, select
        self.Hw, self.idx = {}, {}
        seq = sequency()
        for mode, H in (("T", HT), ("C", HC)):
            Hw = fwht(pad(H))
            if select == "all" or m >= N:
                idx = np.arange(N)
            elif select == "lowseq":
                idx = np.argsort(seq, kind="stable")[:m]
            elif select == "maxvar":
                idx = np.argsort(-Hw.var(axis=0), kind="stable")[:m]
            else:
                raise ValueError(select)
            self.idx[mode] = np.sort(idx)
            hw = Hw[:, self.idx[mode]]
            nrm = np.linalg.norm(hw, axis=1, keepdims=True)
            # a row can be all zero (e.g. 3-of-6 is DC-free, so its lowest-sequency
            # coefficients may vanish): such a meter can never be identified with this set
            self.Hw[mode] = np.divide(hw, nrm, out=np.zeros_like(hw), where=nrm > 0)
            self.zero_rows = getattr(self, "zero_rows", 0) + int((nrm == 0).sum())

    def score(self, mode: str, s: np.ndarray) -> np.ndarray:
        sw = fwht(pad(s))[self.idx[mode]]
        return self.Hw[mode] @ sw / max(np.linalg.norm(sw), 1e-12)

    def ops_per_candidate(self, n_meters: int) -> int:
        return N * int(np.log2(N)) + n_meters * self.m      # FWHT adds + kept MACs

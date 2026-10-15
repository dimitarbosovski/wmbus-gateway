"""Meter keys: static CSV/JSON file and/or an HTTP(S) key service.

CSV columns: manufacturer,id,key   e.g.  KAM,12345678,000102030405060708090A0B0C0D0E0F
  manufacturer: 3-letter flag or 4 hex digits (M-field value); id: printed meter ID.
JSON: [{"manufacturer": "KAM", "id": "12345678", "key": "0001..."}]
Key service (optional): GET <url>?manufacturer=KAM&id=12345678 -> {"key": "<32 hex>"} or 404.
"""
from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .oms import manufacturer_bytes, manufacturer_code, id_string

Addr = tuple[bytes, bytes]   # (M 2 B, ID 4 B) as on air


def _addr(manufacturer: str, ident: str) -> Addr:
    return manufacturer_bytes(manufacturer), bytes.fromhex(ident.strip().zfill(8))[::-1]


class KeyStore:
    def __init__(self, service_url: str | None = None, service_token: str | None = None,
                 cache_ttl_s: float = 3600.0, miss_ttl_s: float = 300.0, timeout_s: float = 2.0):
        self._static: dict[Addr, bytes] = {}
        self._cache: dict[Addr, tuple[float, bytes | None]] = {}
        self.service_url, self.service_token = service_url, service_token
        self.cache_ttl_s, self.miss_ttl_s, self.timeout_s = cache_ttl_s, miss_ttl_s, timeout_s

    def __len__(self) -> int:
        return len(self._static)

    def load_file(self, path: str | Path) -> int:
        p = Path(path)
        if p.suffix.lower() == ".json":
            rows = json.loads(p.read_text())
        else:
            with p.open(newline="") as f:
                rows = [r for r in csv.DictReader(f)]
        n = 0
        for r in rows:
            key = bytes.fromhex(r["key"].strip())
            if len(key) != 16:
                raise ValueError(f"key for {r['id']} is not 16 bytes")
            self._static[_addr(r["manufacturer"], r["id"])] = key
            n += 1
        return n

    def put(self, manufacturer: str, ident: str, key_hex: str) -> None:
        self._static[_addr(manufacturer, ident)] = bytes.fromhex(key_hex)

    def get_cached(self, addr: Addr) -> tuple[bool, bytes | None]:
        """(known, key). known=False means: ask the service (blocking) via fetch()."""
        if addr in self._static:
            return True, self._static[addr]
        if not self.service_url:
            return True, None
        hit = self._cache.get(addr)
        if hit and hit[0] > time.monotonic():
            return True, hit[1]
        return False, None

    def fetch(self, addr: Addr) -> bytes | None:
        """Blocking HTTP call; run it in an executor."""
        q = urllib.parse.urlencode({"manufacturer": manufacturer_code(addr[0]), "id": id_string(addr[1])})
        req = urllib.request.Request(f"{self.service_url}?{q}")
        if self.service_token:
            req.add_header("Authorization", f"Bearer {self.service_token}")
        key = None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                key = bytes.fromhex(json.loads(r.read())["key"])
                if len(key) != 16:
                    key = None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return None            # transient: do not cache
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            return None
        ttl = self.cache_ttl_s if key else self.miss_ttl_s
        self._cache[addr] = (time.monotonic() + ttl, key)
        return key

"""One iM891A-XL on one serial port = one sector."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import serial

from . import hci, imst

log = logging.getLogger("wmbus_bs.channel")


class Channel:
    def __init__(self, sector: int, port: str, link_mode: str = "CT", baud: int = 115200,
                 module_latency_ms: float = 0.0, status_period_s: float = 60.0,
                 on_rx: Callable | None = None, on_status: Callable | None = None):
        self.sector, self.port, self.baud = sector, port, baud
        self.link_mode = imst.LINK_MODES[link_mode]
        self.module_latency_s = module_latency_ms / 1000.0
        self.status_period_s = status_period_s
        self.on_rx, self.on_status = on_rx, on_status
        self.dec = hci.SlipDecoder()
        self.ser: serial.Serial | None = None
        self._waiters: dict[tuple[int, int], asyncio.Future] = {}
        self.ready = False
        self.rx_events = 0
        self._failed: asyncio.Event | None = None

    # ---------------------------------------------------------------- serial I/O
    def _open(self) -> None:
        self.ser = serial.Serial(self.port, self.baud, timeout=0, write_timeout=1)
        self.ser.reset_input_buffer()
        asyncio.get_running_loop().add_reader(self.ser.fileno(), self._on_readable)

    def _close(self) -> None:
        if self.ser:
            try:
                asyncio.get_running_loop().remove_reader(self.ser.fileno())
            except (ValueError, OSError):
                pass
            self.ser.close()
            self.ser = None
        self.ready = False

    def _on_readable(self) -> None:
        t = time.monotonic()
        wall = time.time()
        try:
            data = self.ser.read(4096)
        except (serial.SerialException, OSError) as e:
            log.error("sector %d: read error %s", self.sector, e)
            self._close()
            if self._failed:
                self._failed.set()
            return
        for f in self.dec.feed(data):
            self._dispatch(f, t, wall)

    def _dispatch(self, f: hci.Frame, t: float, wall: float) -> None:
        if f.sap == imst.SAP_WMBUS and f.msg == imst.WM_RX_IND:
            ev = imst.parse_rx(f.payload)
            if ev and self.on_rx:
                self.rx_events += 1
                # end on air = host time - UART transfer of this frame - module latency
                lag = f.wire_len * 10 / self.baud + self.module_latency_s
                self.on_rx(self.sector, ev, t, t - lag, wall - lag)
            return
        fut = self._waiters.pop((f.sap, f.msg), None)
        if fut and not fut.done():
            fut.set_result(f.payload)

    async def request(self, sap: int, msg: int, rsp: int, payload: bytes = b"",
                      timeout: float = 1.0) -> bytes:
        """HCI spec p.4: a response is expected in under 1 s."""
        fut = asyncio.get_running_loop().create_future()
        self._waiters[(sap, rsp)] = fut
        self.ser.write(hci.encode(sap, msg, payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop((sap, rsp), None)

    # ---------------------------------------------------------------- module setup
    async def _init_module(self) -> None:
        for _ in range(15):                         # boot time not documented: poll
            try:
                await self.request(imst.SAP_DEVMGMT, imst.DM_PING_REQ, imst.DM_PING_RSP, timeout=0.2)
                break
            except asyncio.TimeoutError:
                continue
        else:
            raise TimeoutError("no ping response")
        rsp = await self.request(imst.SAP_WMBUS, imst.WM_GET_CFG_REQ, imst.WM_GET_CFG_RSP)
        if len(rsp) < 1 + imst.CFG_LEN or rsp[0] != 0:
            raise RuntimeError(f"get config failed {rsp.hex()}")
        cfg = imst.patch_cfg(rsp[1:1 + imst.CFG_LEN], self.link_mode)
        rsp = await self.request(imst.SAP_WMBUS, imst.WM_SET_CFG_REQ, imst.WM_SET_CFG_RSP, cfg)
        if not rsp or rsp[0] != 0:
            raise RuntimeError(f"set config failed {rsp.hex()}")

    async def poll_status(self) -> imst.GatewayStatus | None:
        rsp = await self.request(imst.SAP_WMBUS, imst.WM_STATUS_REQ, imst.WM_STATUS_RSP)
        st = imst.parse_status(rsp)
        if st and self.on_status:
            self.on_status(self.sector, st)
        return st

    async def run(self) -> None:
        backoff = 1.0
        self._failed = asyncio.Event()
        while True:
            try:
                self._failed.clear()
                self._open()
                await self._init_module()
                self.ready = True
                backoff = 1.0
                log.info("sector %d on %s ready", self.sector, self.port)
                while True:
                    await self.poll_status()
                    try:
                        await asyncio.wait_for(self._failed.wait(), self.status_period_s)
                        raise ConnectionError("serial port lost")
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                self._close()
                raise
            except Exception as e:                  # noqa: BLE001 - keep the station running
                log.error("sector %d (%s): %s - retry in %.0f s", self.sector, self.port, e, backoff)
                self._close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

"""JSON records and MQTT publishing."""
from __future__ import annotations

import json
import logging
import ssl
from datetime import datetime, timezone

from .imst import PACKET_INFO
from .merge import Telegram
from .oms import Result, id_string, manufacturer_code

log = logging.getLogger("wmbus_bs.output")


def iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def telegram_record(station: str, tg: Telegram, result: Result, pkt_out: bytes, acc: int | None) -> dict:
    best = tg.best
    return {
        "v": 1,
        "station": station,
        "seq": tg.seq,
        "ts": iso(tg.wall_end),
        "m": manufacturer_code(tg.pkt[2:4]),
        "id": id_string(tg.pkt[4:8]),
        "ver": tg.pkt[8],
        "type": tg.pkt[9],
        "mode": PACKET_INFO.get(tg.pkt_info, str(tg.pkt_info)),
        "acc": acc,
        "dec": result.value,
        "rssi": best.rssi,
        "sector": best.sector,
        "rx": [{"s": c.sector, "rssi": c.rssi} for c in sorted(tg.copies, key=lambda c: -c.rssi)],
        "telegram": pkt_out.hex(),
    }


class ListPublisher:
    """For tests and dry runs."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    def publish(self, topic: str, obj: dict, retain: bool = False) -> None:
        self.messages.append((topic, obj))

    def close(self) -> None:
        pass


class MqttPublisher:
    def __init__(self, host: str, port: int = 8883, client_id: str = "", username: str = "",
                 password: str = "", tls: bool = True, ca_file: str = "", version: int = 4,
                 qos: int = 1, status_topic: str = ""):
        import paho.mqtt.client as mqtt

        proto = mqtt.MQTTv5 if version == 5 else mqtt.MQTTv311
        try:                                         # paho-mqtt >= 2.0
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=proto)
        except AttributeError:                       # paho-mqtt 1.x (Debian bookworm package)
            self.c = mqtt.Client(client_id=client_id, protocol=proto)
        if username:
            self.c.username_pw_set(username, password)
        if tls:
            self.c.tls_set(ca_certs=ca_file or None, cert_reqs=ssl.CERT_REQUIRED)
        self.qos, self.status_topic = qos, status_topic
        if status_topic:
            self.c.will_set(status_topic, json.dumps({"online": False}), qos=1, retain=True)
        self.c.on_connect = self._on_connect
        self.c.reconnect_delay_set(1, 60)
        self.c.connect_async(host, port, keepalive=60)
        self.c.loop_start()

    def _on_connect(self, client, userdata, flags, rc, *args) -> None:
        log.info("MQTT connected rc=%s", rc)
        if self.status_topic:
            client.publish(self.status_topic, json.dumps({"online": True}), qos=1, retain=True)

    def publish(self, topic: str, obj: dict, retain: bool = False) -> None:
        self.c.publish(topic, json.dumps(obj, separators=(",", ":")), qos=self.qos, retain=retain)

    def close(self) -> None:
        if self.status_topic:
            self.c.publish(self.status_topic, json.dumps({"online": False}), qos=1, retain=True)
        self.c.loop_stop()
        self.c.disconnect()

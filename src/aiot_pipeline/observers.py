"""Read-only views of the pipeline for the Kitchen UI.

MqttObserver subscribes to every application topic so the UI can show what actually travels
through Mosquitto. PipelineMetrics polls a handful of Prometheus recording rules once per
interval on the server, so any number of browsers costs the same two queries.
Neither sits on the data or control path: if they fail, the pipeline is unaffected.
"""

import json
import logging
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .topics import MQTT_SUFFIX_TO_KAFKA

LOG = logging.getLogger("kitchen-observers")
RATE_WINDOW_SECONDS = 10.0

# Who publishes and who consumes each MQTT topic suffix.
TOPIC_ROLES = {
    "readings": ("16 cảm biến (edge)", "Edge model + Bridge → Kafka"),
    "predictions": ("Edge model (cửa sổ 60 s)", "Bridge → Kafka"),
    "ground-truth": ("Nguồn phát lại (nhãn)", "Bridge → Kafka"),
    "control-requests": ("Kitchen UI", "Bridge → Kafka"),
    "commands": ("Safety Controller", "Thiết bị + Bridge"),
    "actuator-state": ("Thiết bị (ACK)", "Bridge → Kafka"),
    "demo-scenario": ("Kitchen UI", "Edge (chỉ demo)"),
    "demo-state": ("Edge (chỉ demo)", "Kitchen UI"),
}
TOPIC_ORDER = list(TOPIC_ROLES)
BROKER_TOPICS = {
    "$SYS/broker/clients/connected": "clients_connected",
    "$SYS/broker/subscriptions/count": "subscriptions",
    "$SYS/broker/retained messages/count": "retained_messages",
    "$SYS/broker/load/messages/received/1min": "load_received_1min",
    "$SYS/broker/uptime": "uptime_seconds",
}


def summarize_payload(payload: bytes, limit: int = 360) -> str:
    """Compact, human-readable JSON preview of an MQTT payload."""
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        text = payload[:limit].decode("utf-8", errors="replace")
        return text
    if isinstance(data, dict):
        compact: dict[str, Any] = {}
        for key, value in data.items():
            if key == "schema_version":
                continue
            if key in {"event_id", "command_id"}:
                value = f"{str(value)[:8]}…"
            elif key == "observed_at":
                value = str(value)[11:23]
            elif isinstance(value, float):
                value = round(value, 2)
            elif isinstance(value, list):
                value = [round(item) if isinstance(item, int | float) else item for item in value]
            compact[key] = value
        data = compact
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else f"{text[:limit]}…"


class MqttObserver:
    def __init__(self, config: MqttConfig | None = None) -> None:
        self.config = config or MqttConfig()
        self.lock = threading.Lock()
        self.topics: dict[str, dict[str, Any]] = {}
        self.broker: dict[str, float] = {}
        # Latest leak/room-air state published by the simulator (demo only).
        self.demo: dict[str, Any] | None = None
        self.connected = False
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="kitchen-ui-observer",
            protocol=mqtt.MQTTv5,
        )
        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)

    def start(self) -> None:
        self.client.connect_async(self.config.host, self.config.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = not reason_code.is_failure
        if self.connected:
            # QoS 0: the observer must never slow the broker down or hold messages for itself.
            client.subscribe("aiot/v1/nodes/+/+", qos=0)
            for topic in BROKER_TOPICS:
                client.subscribe(topic, qos=0)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self.connected = False

    def _on_message(self, client, userdata, message) -> None:
        self.observe(message.topic, message.payload, bool(message.retain), time.time())

    def observe(self, topic: str, payload: bytes, retained: bool, at: float) -> None:
        if topic.startswith("$SYS/"):
            if name := BROKER_TOPICS.get(topic):
                try:
                    with self.lock:
                        self.broker[name] = float(payload.split()[0])
                except (ValueError, IndexError):
                    pass
            return
        parts = topic.split("/")
        if len(parts) != 5:
            return
        suffix = parts[4]
        preview = summarize_payload(payload)
        if suffix == "demo-state":
            try:
                demo = json.loads(payload)
            except ValueError:
                demo = None
            if isinstance(demo, dict):
                with self.lock:
                    self.demo = {**demo, "received_at": at}
        with self.lock:
            stats = self.topics.setdefault(
                topic,
                {
                    "topic": topic,
                    "suffix": suffix,
                    "node_id": parts[3],
                    "count": 0,
                    "arrivals": deque(),
                },
            )
            stats["count"] += 1
            stats["bytes"] = len(payload)
            stats["last_at"] = at
            stats["retained"] = retained
            stats["preview"] = preview
            arrivals: deque[float] = stats["arrivals"]
            arrivals.append(at)
            while arrivals and at - arrivals[0] > RATE_WINDOW_SECONDS:
                arrivals.popleft()

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self.lock:
            topics = []
            for stats in self.topics.values():
                recent = sum(1 for stamp in stats["arrivals"] if now - stamp <= RATE_WINDOW_SECONDS)
                publisher, subscriber = TOPIC_ROLES.get(stats["suffix"], ("?", "?"))
                kafka = MQTT_SUFFIX_TO_KAFKA.get(stats["suffix"])
                topics.append(
                    {
                        "topic": stats["topic"],
                        "suffix": stats["suffix"],
                        "node_id": stats["node_id"],
                        "publisher": publisher,
                        "subscriber": subscriber,
                        "kafka_topic": str(kafka) if kafka else None,
                        "count": stats["count"],
                        "rate": round(recent / RATE_WINDOW_SECONDS, 2),
                        "bytes": stats["bytes"],
                        "age_s": round(now - stats["last_at"], 1),
                        "retained": stats["retained"],
                        "preview": stats["preview"],
                    }
                )
            broker = dict(self.broker)
            demo = dict(self.demo) if self.demo else None
        order = {suffix: index for index, suffix in enumerate(TOPIC_ORDER)}
        topics.sort(key=lambda item: (order.get(item["suffix"], 99), item["node_id"]))
        return {
            "connected": self.connected,
            "broker": f"{self.config.host}:{self.config.port}",
            "qos": self.config.qos,
            "rate": round(sum(item["rate"] for item in topics), 2),
            "topics": topics,
            "stats": broker,
            "demo": demo,
        }


# ------------------------------------------------------------------ Prometheus summary
RATE_SERIES = {
    "aiot:mqtt_ingress:rate1m": "mqtt_ingress",
    "aiot:kafka_produced:rate1m": "kafka_produced",
    "aiot:kafka_appended:rate1m": "kafka_appended",
    "aiot:sink_consumed:rate1m": "sink_consumed",
    "aiot:influx_written:rate1m": "influx_written",
}
LATENCY_SERIES = {
    "aiot:bridge_event_age:p95": "edge_to_bridge_p95",
    "aiot:influx_write:p95": "influx_write_p95",
    "aiot:control_loop_lag:p95": "control_loop_p95",
    "aiot:ack_latency:p95": "ack_p95",
}
SUMMARY_QUERY = (
    '{__name__=~"aiot:.+"} or up or aiot_mqtt_broker_stat{job="mqtt-kafka-bridge"}'
    " or aiot_actuator_pending_commands"
)


def _number(item: dict[str, Any]) -> float | None:
    try:
        value = float(item["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def summarize_prometheus(series: list[dict], alerts: list[dict]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "rates": {},
        "latency": {},
        "lag": {},
        "up": {},
        "broker": {},
        "sensor_age": {},
        "pending_commands": 0.0,
        "dead_letter_5m": None,
        "alerts": [],
    }
    for item in series:
        labels = item.get("metric", {})
        name = labels.get("__name__", "")
        value = _number(item)
        if name in RATE_SERIES:
            summary["rates"][RATE_SERIES[name]] = value
        elif name in LATENCY_SERIES:
            summary["latency"][LATENCY_SERIES[name]] = value
        elif name in {"aiot:pipeline_latency:p95", "aiot:pipeline_latency:p50"}:
            if labels.get("measurement") == "sensor_reading":
                summary["latency"][f"edge_to_influx_{name.rsplit(':', 1)[-1]}"] = value
        elif name == "aiot:consumer_lag:sum":
            summary["lag"][labels.get("consumergroup", "?")] = value
        elif name == "aiot:sensor_age:max":
            summary["sensor_age"][labels.get("node_id", "?")] = value
        elif name == "aiot:dead_letter:increase5m":
            summary["dead_letter_5m"] = value
        elif name == "up":
            summary["up"][labels.get("job", "?")] = value
        elif name == "aiot_mqtt_broker_stat":
            summary["broker"][labels.get("name", "?")] = value
        elif name == "aiot_actuator_pending_commands" and value:
            summary["pending_commands"] += value
    summary["alerts"] = sorted(
        (
            {
                "name": item["metric"].get("alertname", "?"),
                "severity": item["metric"].get("severity", "info"),
                "node_id": item["metric"].get("node_id"),
            }
            for item in alerts
        ),
        key=lambda alert: (alert["severity"] != "critical", alert["name"]),
    )
    return summary


class PipelineMetrics:
    def __init__(self, url: str | None = None, interval: float = 5.0) -> None:
        self.url = (url or os.getenv("PROMETHEUS_URL", "http://prometheus:9090")).rstrip("/")
        self.interval = interval
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.summary: dict[str, Any] | None = None
        self.fetched_at: float | None = None
        self.error: str | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="pipeline-metrics", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _query(self, expression: str) -> list[dict]:
        url = f"{self.url}/api/v1/query?query={urllib.parse.quote(expression)}"
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response)["data"]["result"]

    def fetch(self) -> None:
        try:
            summary = summarize_prometheus(
                self._query(SUMMARY_QUERY), self._query('ALERTS{alertstate="firing"}')
            )
        except Exception as error:  # Prometheus is optional for the kitchen screen.
            with self.lock:
                self.error = str(error)
            return
        with self.lock:
            self.summary = summary
            self.fetched_at = time.time()
            self.error = None

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.fetch()
            self.stop_event.wait(self.interval)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ok": self.summary is not None and self.error is None,
                "error": self.error,
                "age_s": None
                if self.fetched_at is None
                else round(time.time() - self.fetched_at, 1),
                **(self.summary or {}),
            }

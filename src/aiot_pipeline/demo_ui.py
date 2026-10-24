import asyncio
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import uvicorn
from confluent_kafka import OFFSET_END, Consumer, KafkaError, KafkaException, TopicPartition
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import KafkaConfig, MqttConfig, env_bool
from .contracts import (
    DEMO_SCENARIOS,
    ActuatorCommand,
    ActuatorState,
    ControlRequest,
    DemoScenario,
    GroundTruth,
    Prediction,
    SensorReading,
)
from .edge_model import FEATURES_PER_CHANNEL, WINDOW_S, RidgeModel
from .observers import MqttObserver, PipelineMetrics
from .sensors import sensors_as_dicts
from .topics import KafkaTopic, mqtt_topic

LOG = logging.getLogger("kitchen-demo")
STATIC_DIR = Path(__file__).with_name("static")
HISTORY_POINTS = 600
RATE_WINDOW_SECONDS = 10.0
STREAM_INTERVAL_SECONDS = 0.2
STREAM_HEARTBEAT_SECONDS = 15.0
REPLAY_THRESHOLD_SECONDS = 30.0
# How long the kitchen screen keeps following a node that stopped sending before it switches.
# Every node publishes at 1 Hz, so 5 s of silence means it stopped (e.g. simulator → ESP32).
ACTIVE_NODE_SILENCE_SECONDS = 5.0
# On start the screen reloads this much recent telemetry from Kafka, so a fresh page (or a
# restarted UI) shows its trends and logs at once instead of filling them second by second.
BACKFILL_SECONDS = HISTORY_POINTS
BACKFILL_TOPICS = {
    str(KafkaTopic.SENSOR),
    str(KafkaTopic.PREDICTION),
    str(KafkaTopic.GROUND_TRUTH),
}

# What the screen says about each edge model, keyed by the model_name its predictions carry.
MODEL_CARDS: dict[str, dict[str, Any]] = {
    "tabm-int8": {
        "title": "TabM INT8",
        "summary": "ensemble 32 member · 4 × 1024 · INT8 QDQ",
        "trained_on": "UCI 322 ethylene_CO (dữ liệu thật) · cùng blob INT8 với firmware ESP32",
        "window_s": 60,
        "features": ["last", "mean", "std", "min", "max", "slope", "d5", "d15", "d30"],
        "alarm": {
            "warning": "TB 5 s của P(CO) ≥ τ = 0.48",
            "danger": "TB 5 s của CO ≥ 200 ppm · khóa gas, sơ tán",
            "note": "C₂H₄ được ước lượng nhưng không kích hoạt alarm",
        },
    },
    "ridge-window60": {
        "title": "Ridge",
        "summary": "hồi quy tuyến tính có điều chuẩn",
        "window_s": WINDOW_S,
        "features": list(FEATURES_PER_CHANNEL),
        "alarm": {
            "warning": "TB 5 s: CO ≥ 50 ppm (tắt < 40) hoặc C₂H₄ ≥ 3 ppm",
            "danger": "TB 5 s của CO ≥ 200 ppm · khóa gas, sơ tán",
            "note": "Ngưỡng theo notebook huấn luyện",
        },
    },
}

MODEL_BY_TOPIC = {
    str(KafkaTopic.SENSOR): SensorReading,
    str(KafkaTopic.PREDICTION): Prediction,
    str(KafkaTopic.GROUND_TRUTH): GroundTruth,
    str(KafkaTopic.CONTROL_REQUEST): ControlRequest,
    str(KafkaTopic.ACTUATOR_COMMAND): ActuatorCommand,
    str(KafkaTopic.ACTUATOR_STATE): ActuatorState,
}
LATEST_STATE_TOPICS = {str(KafkaTopic.ACTUATOR_COMMAND), str(KafkaTopic.ACTUATOR_STATE)}
EVENT_TYPES = (
    "sensor",
    "prediction",
    "ground_truth",
    "control_request",
    "actuator_command",
    "actuator_state",
)
TYPE_BY_TOPIC = {
    str(KafkaTopic.SENSOR): "sensor",
    str(KafkaTopic.PREDICTION): "prediction",
    str(KafkaTopic.GROUND_TRUTH): "ground_truth",
    str(KafkaTopic.CONTROL_REQUEST): "control_request",
    str(KafkaTopic.ACTUATOR_COMMAND): "actuator_command",
    str(KafkaTopic.ACTUATOR_STATE): "actuator_state",
}


class ControlAction(BaseModel):
    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    mode: str = Field(pattern=r"^(auto|manual|mute|unmute)$")
    fan_on: bool = False
    exhaust_on: bool = False
    gas_valve_closed: bool = False
    alarm_on: bool = False
    suppression_on: bool = False


class DemoAction(BaseModel):
    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    scenario: str = Field(pattern="^(" + "|".join(DEMO_SCENARIOS) + ")$")
    duration_s: int = Field(default=90, ge=10, le=600)


class ControlPublisher:
    def __init__(self) -> None:
        self.config = MqttConfig()
        self.lock = threading.Lock()
        self.sequence = 0
        self.connected = False
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="kitchen-demo-control-v1",
            protocol=mqtt.MQTTv5,
        )
        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = not reason_code.is_failure

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self.connected = False

    def start(self) -> None:
        self.client.connect(self.config.host, self.config.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def publish(self, action: ControlAction) -> ControlRequest:
        with self.lock:
            sequence = self.sequence
            self.sequence += 1
        request = ControlRequest(
            node_id=action.node_id,
            observed_at=datetime.now(UTC),
            sequence=sequence,
            mode=action.mode,
            fan_on=action.fan_on,
            exhaust_on=action.exhaust_on,
            gas_valve_closed=action.gas_valve_closed,
            alarm_on=action.alarm_on,
            suppression_on=action.suppression_on,
        )
        self._send(mqtt_topic(action.node_id, "control-requests"), request.model_dump_json())
        return request

    def publish_demo(self, action: DemoAction) -> DemoScenario:
        with self.lock:
            sequence = self.sequence
            self.sequence += 1
        request = DemoScenario(
            node_id=action.node_id,
            observed_at=datetime.now(UTC),
            sequence=sequence,
            scenario=action.scenario,
            duration_s=action.duration_s,
        )
        # Demo-only: goes straight to the simulator, which injects the episode at the edge so
        # every downstream stage (Kafka, model, controller, ACK, InfluxDB) reacts for real.
        self._send(mqtt_topic(action.node_id, "demo-scenario"), request.model_dump_json())
        return request

    def _send(self, topic: str, payload: str) -> None:
        result = self.client.publish(topic, payload, qos=self.config.qos)
        result.wait_for_publish(timeout=5)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(mqtt.error_string(result.rc))


class KitchenState:
    def __init__(self, node_id: str | None = None) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        # Every node keeps its own view (latest records + short history), followed or not: the
        # screen shows one node at a time and switching is only a matter of pointing at another
        # view, so nothing that node sent meanwhile (its ACK, its first prediction) is lost.
        self.views: dict[str, dict[str, Any]] = {}
        empty = self._new_view()
        self.latest: dict[str, dict[str, Any] | None] = empty["latest"]
        # Short rolling history so the UI can draw trends right after a page load.
        self.history: dict[str, deque[list[Any]]] = empty["history"]
        self.counts = {key: 0 for key in EVENT_TYPES}
        # Node the screen is following (see _follows): several nodes may stream at the same time.
        # DEMO_NODE_ID pins it to one node.
        self.pinned_node = node_id or None
        self.active_node: str | None = None
        self.active_seen = 0.0
        self.events: deque[dict[str, Any]] = deque(maxlen=24)
        self.arrivals: dict[str, deque[float]] = {key: deque() for key in self.latest}
        self.latency_ms: dict[str, float | None] = {key: None for key in self.latest}
        self.version = 0
        self.started_at = time.time()
        self.kafka_error: str | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._consume, name="kitchen-kafka", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)

    def _consume(self) -> None:
        config = KafkaConfig()
        consumer = Consumer(
            {
                "bootstrap.servers": config.bootstrap_servers,
                "group.id": "kitchen-demo-ui-v1",
                # A restarted UI should get its partitions back within seconds, even if the
                # previous process could not leave the group cleanly (open SSE connections).
                "session.timeout.ms": 6000,
                "queued.max.messages.kbytes": 4096,
                "heartbeat.interval.ms": 1500,
                "enable.auto.commit": True,
                "auto.offset.reset": "latest",
            }
        )

        def start_at_live_edge(consumer: Consumer, partitions: list) -> None:
            # A live view starts at the live edge, with two exceptions so nothing on the screen
            # has to wait: the last BACKFILL_SECONDS of readings and predictions (trends and logs
            # are full at once) and the last actuator command/ACK (the current device state).
            # Offsets are still committed so kafka-exporter keeps reporting this group's lag.
            starts = backfill_offsets(consumer, partitions, time.time() - BACKFILL_SECONDS)
            for partition in partitions:
                partition.offset = starts.get((partition.topic, partition.partition), OFFSET_END)
                if partition.topic in LATEST_STATE_TOPICS:
                    low, high = consumer.get_watermark_offsets(partition, timeout=5)
                    if high > low:
                        partition.offset = high - 1
            consumer.assign(partitions)

        consumer.subscribe(list(MODEL_BY_TOPIC), on_assign=start_at_live_edge)
        try:
            while not self.stop_event.is_set():
                message = consumer.poll(0.5)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() != KafkaError._PARTITION_EOF:
                        with self.lock:
                            self.kafka_error = str(message.error())
                    continue
                try:
                    model = MODEL_BY_TOPIC[message.topic()]
                    event = model.model_validate_json(message.value())
                    self.record(
                        TYPE_BY_TOPIC[message.topic()],
                        event,
                        message.topic(),
                        message.partition(),
                        message.offset(),
                        time.time(),
                    )
                except Exception as error:
                    LOG.warning("Could not decode Kafka record: %s", error)
        finally:
            consumer.close()

    def record(
        self,
        event_type: str,
        event: Any,
        topic: str,
        partition: int,
        offset: int,
        received_at: float,
    ) -> None:
        data = event.model_dump(mode="json")
        observed_at = event.observed_at.timestamp()
        latency_ms = max(0.0, (received_at - observed_at) * 1000)
        # Retained MQTT ACKs and Kafka replays are old on arrival; they are not live latency.
        replayed = latency_ms > REPLAY_THRESHOLD_SECONDS * 1000
        with self.lock:
            self.counts[event_type] += 1
            self.kafka_error = None
            if not replayed:
                previous = self.latency_ms[event_type]
                # Exponential moving average keeps the number readable at 1 Hz.
                self.latency_ms[event_type] = (
                    latency_ms if previous is None else previous * 0.8 + latency_ms * 0.2
                )
            arrivals = self.arrivals[event_type]
            if not replayed:  # a backfill burst is not a message rate
                arrivals.append(received_at)
            while arrivals and received_at - arrivals[0] > RATE_WINDOW_SECONDS:
                arrivals.popleft()
            view = self.views.get(event.node_id)
            if view is None:
                view = self.views[event.node_id] = self._new_view()
            view["latest"][event_type] = data
            if event_type == "sensor":
                view["history"]["sensor"].append([observed_at, event.sequence, *event.sensors])
            elif event_type == "prediction":
                # [t, seq, co, ethylene, alarm, inference_ms, top channels, window_s]
                view["history"]["prediction"].append(
                    [
                        observed_at,
                        event.sequence,
                        event.co_ppm,
                        event.ethylene_ppm,
                        event.alarm_level,
                        event.inference_ms,
                        ",".join(event.top_channels),
                        event.window_s,
                    ]
                )
            elif event_type == "ground_truth":
                # [t, seq, co, ethylene]: pairs with the prediction of the same sequence.
                view["history"]["ground_truth"].append(
                    [observed_at, event.sequence, event.co_ppm, event.ethylene_ppm]
                )
            # A replayed record is placed at the time it was observed, a live one when it arrived
            # (robust to a device clock that is off, e.g. an ESP32 without NTP).
            following = self._follows(event.node_id, observed_at if replayed else received_at)
            if not following:
                # Another node is talking (the ESP32 board next to the simulated one): keep its
                # view up to date, but do not mix its readings into the kitchen screen.
                self.version += 1
                return
            self.events.appendleft(
                {
                    "type": event_type,
                    "topic": topic,
                    "partition": partition,
                    "offset": offset,
                    "sequence": event.sequence,
                    "node_id": event.node_id,
                    "observed_at": observed_at,
                    "received_at": received_at,
                    "latency_ms": round(latency_ms, 1),
                    "replayed": replayed,
                }
            )
            self.version += 1

    def _follows(self, node_id: str, now: float) -> bool:
        """The kitchen screen follows one node at a time.

        Several nodes can stream at once (the simulator as `virtual-gas-01`, the ESP32 board as its
        own id). Mixing them in one panel would make every value jump between two recordings, so the
        screen sticks to the node it is showing and only switches when that node goes quiet, or
        never when DEMO_NODE_ID pins it.
        """
        if self.pinned_node and node_id != self.pinned_node:
            return False
        if node_id == self.active_node:
            self.active_seen = max(self.active_seen, now)
            return True
        if self.active_node is None or now - self.active_seen > ACTIVE_NODE_SILENCE_SECONDS:
            self.active_node, self.active_seen = node_id, now
            self.latest = self.views[node_id]["latest"]
            self.history = self.views[node_id]["history"]
            return True
        return False

    @staticmethod
    def _new_view() -> dict[str, Any]:
        return {
            "latest": dict.fromkeys(EVENT_TYPES),
            "history": {
                "sensor": deque(maxlen=HISTORY_POINTS),
                "prediction": deque(maxlen=HISTORY_POINTS),
                "ground_truth": deque(maxlen=HISTORY_POINTS),
            },
        }

    def _stream_stats(self, now: float) -> dict[str, dict[str, float | None]]:
        stats = {}
        for event_type, arrivals in self.arrivals.items():
            recent = sum(1 for stamp in arrivals if now - stamp <= RATE_WINDOW_SECONDS)
            latency = self.latency_ms[event_type]
            stats[event_type] = {
                "rate": round(recent / RATE_WINDOW_SECONDS, 2),
                "latency_ms": None if latency is None else round(latency, 1),
                "last_seen": arrivals[-1] if arrivals else None,
            }
        return stats

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            return {
                "room": "Kitchen Room",
                "version": self.version,
                "node_id": self.active_node,
                "node_pinned": self.pinned_node is not None,
                "latest": dict(self.latest),
                "counts": dict(self.counts),
                "events": list(self.events),
                "stream": self._stream_stats(now),
                "kafka_error": self.kafka_error,
                "uptime_seconds": round(now - self.started_at),
                "server_time": now,
            }

    def history_snapshot(self) -> dict[str, list[list[Any]]]:
        with self.lock:
            return {key: list(values) for key, values in self.history.items()}


def backfill_offsets(
    consumer: Consumer, partitions: list, since: float
) -> dict[tuple[str, int], int]:
    """First offset at or after `since` (epoch s) of every readings/predictions partition."""
    wanted = [
        TopicPartition(partition.topic, partition.partition, int(since * 1000))
        for partition in partitions
        if partition.topic in BACKFILL_TOPICS
    ]
    if not wanted:
        return {}
    try:
        found = consumer.offsets_for_times(wanted, timeout=5)
    except KafkaException as error:
        LOG.warning("No backfill, starting at the live edge: %s", error)
        return {}
    # A partition with nothing that recent answers -1: it starts at the live edge.
    return {(tp.topic, tp.partition): tp.offset for tp in found if tp.offset >= 0}


STATE = KitchenState(os.getenv("DEMO_NODE_ID", "").strip() or None)
CONTROL = ControlPublisher()
OBSERVER = MqttObserver()
PIPELINE = PipelineMetrics()
DEMO_CONTROLS = env_bool("DEMO_CONTROLS_ENABLED", True)


def full_snapshot() -> dict[str, Any]:
    return {**STATE.snapshot(), "mqtt": OBSERVER.snapshot(), "pipeline": PIPELINE.snapshot()}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    STATE.start()
    CONTROL.start()
    OBSERVER.start()
    PIPELINE.start()
    yield
    PIPELINE.stop()
    OBSERVER.stop()
    CONTROL.stop()
    STATE.stop()


GRAFANA_PUBLIC_URL = os.getenv("GRAFANA_PUBLIC_URL", "http://localhost:3000").rstrip("/")


def grafana_origins(url: str) -> list[str]:
    """The siren switch on the Grafana gas dashboard posts to /api/control from the browser:
    allow Grafana's origin (and its 127.0.0.1 / localhost twin), nothing else."""
    twins = {
        url,
        url.replace("//localhost", "//127.0.0.1"),
        url.replace("//127.0.0.1", "//localhost"),
    }
    return sorted(twins)


app = FastAPI(title="Kitchen Room AIoT Demo", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=grafana_origins(GRAFANA_PUBLIC_URL),
    allow_methods=["POST"],
    allow_headers=["*"],
)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    return full_snapshot()


@app.get("/api/history")
def get_history() -> dict[str, list[list[Any]]]:
    return STATE.history_snapshot()


@app.get("/api/stream", include_in_schema=False)
async def stream_state(request: Request) -> StreamingResponse:
    """Server-Sent Events: push a snapshot only when a new Kafka record arrived."""

    async def events():
        last_version = -1
        last_sent = 0.0
        while not STATE.stop_event.is_set() and not await request.is_disconnected():
            now = time.monotonic()
            if STATE.version != last_version:
                snapshot = full_snapshot()
                last_version = snapshot["version"]
                last_sent = now
                yield f"data: {json.dumps(snapshot, separators=(',', ':'))}\n\n"
            elif now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                last_sent = now
                yield ": keep-alive\n\n"
            await asyncio.sleep(STREAM_INTERVAL_SECONDS)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/control")
def post_control(action: ControlAction) -> dict[str, Any]:
    try:
        request = CONTROL.publish(action)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"MQTT control unavailable: {error}") from error
    return {"status": "accepted", "request": request.model_dump(mode="json")}


@app.post("/api/demo")
def post_demo(action: DemoAction) -> dict[str, Any]:
    if not DEMO_CONTROLS:
        raise HTTPException(status_code=403, detail="Demo controls are disabled")
    try:
        request = CONTROL.publish_demo(action)
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"MQTT unavailable: {error}") from error
    return {"status": "accepted", "request": request.model_dump(mode="json")}


def model_cards() -> dict[str, dict[str, Any]]:
    cards = {name: {"name": name, **card} for name, card in MODEL_CARDS.items()}
    try:
        ridge = RidgeModel.load()
        cards[ridge.name] = {**cards.get(ridge.name, {}), "name": ridge.name}
        cards[ridge.name]["trained_on"] = ridge.trained_on
    except (OSError, ValueError, TypeError):
        pass
    return cards


def model_info() -> dict[str, Any] | None:
    """The model edge-inference serves (EDGE_MODEL); the screen switches to the card of the
    model_name its predictions actually carry, e.g. the ESP32's TabM."""
    backend = os.getenv("EDGE_MODEL", "tabm").strip().lower()
    name = "tabm-int8" if backend == "tabm" else "ridge-window60"
    return model_cards().get(name)


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "demo_controls": DEMO_CONTROLS,
        "scenarios": list(DEMO_SCENARIOS),
        "sensors": sensors_as_dicts(),
        "mqtt": {
            "broker": f"{OBSERVER.config.host}:{OBSERVER.config.port}",
            "qos": OBSERVER.config.qos,
        },
        "grafana_url": GRAFANA_PUBLIC_URL,
        "model": model_info(),
        "models": model_cards(),
        "node_id": STATE.pinned_node,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda *_args: STATE.stop_event.set())
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()

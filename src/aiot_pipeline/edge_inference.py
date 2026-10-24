"""Edge inference service: 16 sensors → model → prediction → log.

    MQTT .../readings (16 MOX values, 1 Hz)
      → per-node 60 s window → 144 features → TabM INT8 (or Ridge) → CO / C2H4 ppm → alarm level
      → MQTT .../predictions (same observed_at and sequence as the reading)
      → prediction log: one JSON line per prediction with its 16 inputs and outputs

It runs next to the sensors (on the edge gateway in a real deployment), so it reads the local
MQTT broker directly and never needs Kafka. Kafka and InfluxDB receive the prediction through
the normal bridge path like every other event. A node is predicted from its first reading on
(the TabM pads a window shorter than 60 s, see tabm_model.pad_window): no one-minute warm-up.

Nodes that run their own model (the Wokwi ESP32-S3 with TabM INT8) publish predictions
themselves: the gateway notices and steps aside, and EDGE_SKIP_NODES (comma-separated) pins
nodes it must never serve.
"""

import json
import logging
import logging.handlers
import os
import signal
import threading
import time
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.subscribeoptions import SubscribeOptions

from .config import MqttConfig
from .contracts import Prediction, SensorReading
from .edge_model import NodeInference, RidgeModel
from .metrics import INFERENCE_LATENCY, PREDICTED_PPM, PREDICTIONS, start_metrics_server
from .tabm_model import MAX_MEMBERS, NodeTabMInference, TabMEngine
from .topics import mqtt_topic

LOG = logging.getLogger("edge-inference")


def prediction_log(path: Path | None) -> logging.Logger:
    """JSON-lines log of every prediction, rotated at 5 MiB × 3 files."""
    logger = logging.getLogger("edge-inference.predictions")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if path is not None and not logger.handlers:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def node_list(value: str) -> frozenset[str]:
    return frozenset(node.strip() for node in value.split(",") if node.strip())


class EdgeInference:
    """Runs one backend, chosen by EDGE_MODEL.

    `tabm` (default) runs the INT8 TabM in this container on exactly the blob and integer kernels
    the ESP32 firmware embeds, so both deployment modes produce comparable numbers. `ridge` keeps
    the original pure-Python model.
    """

    def __init__(
        self,
        model: RidgeModel | None = None,
        log_path: Path | None = None,
        backend: str | None = None,
    ) -> None:
        self.backend = (backend or os.getenv("EDGE_MODEL", "tabm")).strip().lower()
        if self.backend not in {"tabm", "ridge"}:
            raise ValueError(f"EDGE_MODEL must be 'tabm' or 'ridge', got {self.backend!r}")
        self.log = prediction_log(log_path)
        if self.backend == "tabm":
            self.tabm = TabMEngine(
                model_dir=os.getenv("TABM_MODEL_DIR") or None,
                members=int(os.getenv("TABM_MEMBERS", str(MAX_MEMBERS))),
            )
            self.infer_every = int(os.getenv("TABM_INFER_EVERY_S", str(self.tabm.infer_every_s)))
            self.nodes: dict[str, NodeTabMInference] = {}
            self.name = self.tabm.name
            self.version = self.tabm.version
            self.trained_on = "UCI 322 ethylene_CO · INT8 QDQ export, the same blob as the ESP32"
        else:
            self.model = model or RidgeModel.load()
            self.nodes = {}
            self.name = self.model.name
            self.version = self.model.version
            self.trained_on = self.model.trained_on

    def prime(self, reading: SensorReading) -> None:
        """Feed the node's window without producing a prediction.

        Used while a device runs the model itself: the gateway keeps the 60 s window warm so it can
        take over immediately (with a full window) the moment the device stops.
        """
        if self.backend == "tabm":
            node = self.nodes.setdefault(
                reading.node_id, NodeTabMInference(self.tabm, self.infer_every)
            )
            node.prime(reading.sensors, reading.sequence)
        else:
            node = self.nodes.setdefault(reading.node_id, NodeInference(self.model))
            node.prime(reading.sensors)

    def infer(self, reading: SensorReading) -> tuple[Prediction | None, dict | None]:
        started = time.perf_counter()
        if self.backend == "tabm":
            node = self.nodes.setdefault(
                reading.node_id, NodeTabMInference(self.tabm, self.infer_every)
            )
            estimate, level, window = node.step(reading.sensors, reading.sequence)
            if estimate is None:  # the window is not full yet, or this is not an inference tick
                return None, None
            co_ppm = float(estimate.ppm[0])
            ethylene_ppm = float(estimate.ppm[1])
            top_channels: list[str] = []
            extra = {
                "prob": [round(float(value), 4) for value in estimate.prob],
                "uct_ppm": [round(float(value), 2) for value in estimate.uct_ppm],
            }
        else:
            node = self.nodes.setdefault(reading.node_id, NodeInference(self.model))
            estimate, level, window = node.step(reading.sensors)
            co_ppm = estimate.co_ppm
            ethylene_ppm = estimate.ethylene_ppm
            top_channels = estimate.top_channels
            extra = {}
        elapsed = time.perf_counter() - started
        prediction = Prediction(
            node_id=reading.node_id,
            observed_at=reading.observed_at,
            sequence=reading.sequence,
            co_ppm=co_ppm,
            ethylene_ppm=ethylene_ppm,
            model_name=self.name,
            model_version=self.version,
            alarm_level=level,
            inference_ms=round(elapsed * 1000, 3),
            window_s=window,
            top_channels=top_channels,
        )
        record = {
            "observed_at": reading.observed_at.isoformat(),
            "node_id": reading.node_id,
            "sequence": reading.sequence,
            "input": [round(value, 1) for value in reading.sensors],
            "window_s": window,
            "co_ppm": round(co_ppm, 2),
            "ethylene_ppm": round(ethylene_ppm, 3),
            "alarm_level": level,
            "top_channels": top_channels,
            "inference_ms": prediction.inference_ms,
            "model": f"{self.name}/{self.version}",
            **extra,
        }
        self.log.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        INFERENCE_LATENCY.observe(elapsed)
        PREDICTIONS.labels(node_id=reading.node_id, alarm_level=level).inc()
        PREDICTED_PPM.labels(node_id=reading.node_id, gas="co").set(co_ppm)
        PREDICTED_PPM.labels(node_id=reading.node_id, gas="ethylene").set(ethylene_ppm)
        return prediction, record


class EdgeInferenceService:
    """Serves every node the gateway owns, and steps aside for nodes that predict on-device.

    The mode is automatic: a node that publishes its own predictions (the ESP32-S3 firmware) is
    left alone while those predictions keep arriving, and the gateway resumes by itself once they
    stop. `EDGE_SKIP_NODES` remains as a manual pin for a node that should never be served here.
    """

    def __init__(self) -> None:
        self.config = MqttConfig()
        log_path = os.getenv("PREDICTION_LOG", "/logs/predictions.jsonl")
        self.engine = EdgeInference(log_path=Path(log_path) if log_path else None)
        self.skip_nodes = node_list(os.getenv("EDGE_SKIP_NODES", ""))
        self.on_device_timeout = float(os.getenv("EDGE_ON_DEVICE_TIMEOUT_S", "30"))
        # node_id -> monotonic timestamp of the last prediction that was not ours.
        self.on_device: dict[str, float] = {}
        # event_id of the predictions we published, so our own MQTT echo is not mistaken for a
        # device running its own model. Only a fallback: the predictions subscription uses No
        # Local, because with tens of nodes the echo can trail more than 64 newer predictions.
        self.published: deque[str] = deque(maxlen=64)
        self.stop_event = threading.Event()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="edge-inference-v1",
            protocol=mqtt.MQTTv5,
        )
        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.error("MQTT connection rejected: %s", reason_code)
            return
        client.subscribe("aiot/v1/nodes/+/readings", qos=self.config.qos)
        # Predictions from other clients tell us which nodes run their own model. No Local (MQTT 5):
        # the broker never sends our own predictions back, however far behind this loop runs.
        client.subscribe(
            "aiot/v1/nodes/+/predictions",
            options=SubscribeOptions(qos=self.config.qos, noLocal=True),
        )
        engine = self.engine
        LOG.info(
            "Serving %s/%s on the gateway (%s)", engine.name, engine.version, engine.trained_on
        )
        LOG.info(
            "A node that publishes its own predictions is left alone for %.0f s; pinned nodes: %s",
            self.on_device_timeout,
            sorted(self.skip_nodes) or "none",
        )

    def _device_predicts(self, node_id: str) -> bool:
        seen = self.on_device.get(node_id)
        return seen is not None and time.monotonic() - seen < self.on_device_timeout

    def _note_prediction_from_elsewhere(self, payload: bytes) -> None:
        try:
            event = Prediction.model_validate_json(payload)
        except Exception:
            LOG.warning("Ignored an unreadable prediction on the wire")
            return
        if str(event.event_id) in self.published:
            return  # the broker echoed our own message back
        resumed = not self._device_predicts(event.node_id)
        self.on_device[event.node_id] = time.monotonic()
        if resumed:
            LOG.info(
                "Node %s runs its own model (%s/%s): the gateway pauses",
                event.node_id,
                event.model_name,
                event.model_version,
            )

    def _on_message(self, client, userdata, message) -> None:
        try:
            if message.topic.endswith("/predictions"):
                self._note_prediction_from_elsewhere(message.payload)
                return
            reading = SensorReading.model_validate_json(message.payload)
            if reading.node_id in self.on_device and not self._device_predicts(reading.node_id):
                del self.on_device[reading.node_id]
                LOG.info("Node %s stopped predicting: the gateway takes over", reading.node_id)
            if reading.node_id in self.skip_nodes or self._device_predicts(reading.node_id):
                self.engine.prime(reading)
                return
            prediction, _ = self.engine.infer(reading)
            if prediction is None:  # window still filling, or not this node's inference tick
                return
            self.published.append(str(prediction.event_id))
            client.publish(
                mqtt_topic(reading.node_id, "predictions"),
                prediction.model_dump_json(),
                qos=self.config.qos,
            )
        except Exception:
            LOG.exception("Could not run inference for %s", message.topic)

    def run(self) -> None:
        self.client.connect_async(self.config.host, self.config.port, keepalive=30)
        self.client.loop_start()
        try:
            self.stop_event.wait()
        finally:
            self.client.disconnect()
            self.client.loop_stop()

    def stop(self, *_args) -> None:
        self.stop_event.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    start_metrics_server()
    service = EdgeInferenceService()
    signal.signal(signal.SIGTERM, service.stop)
    signal.signal(signal.SIGINT, service.stop)
    service.run()


if __name__ == "__main__":
    main()

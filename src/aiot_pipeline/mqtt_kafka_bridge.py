import json
import logging
import signal
import threading
import time
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from confluent_kafka import KafkaException, Producer
from pydantic import ValidationError

from .config import KafkaConfig, MqttConfig
from .contracts import (
    EVENT_MODELS,
    ActuatorCommand,
    ActuatorState,
    GroundTruth,
    Prediction,
    SensorReading,
)
from .metrics import (
    ACK_LATENCY,
    ACK_PENDING,
    ACK_TIMEOUTS,
    EVENT_AGE,
    GAS_PPM,
    KAFKA_DELIVERY_FAILURES,
    KAFKA_PRODUCED,
    LAST_EVENT_TIMESTAMP,
    MQTT_BROKER_STAT,
    MQTT_BYTES,
    MQTT_CONNECTED,
    MQTT_INVALID,
    MQTT_MESSAGES,
    SENSOR_READING,
    SEQUENCE_GAPS,
    start_metrics_server,
)
from .sensors import SENSORS
from .topics import MQTT_SUBSCRIPTIONS, MQTT_SUFFIX_TO_KAFKA, KafkaTopic

LOG = logging.getLogger("mqtt-kafka-bridge")
ACK_TIMEOUT_SECONDS = 5.0
BROKER_STATS = {
    "$SYS/broker/clients/connected": "clients_connected",
    "$SYS/broker/clients/disconnected": "clients_disconnected",
    "$SYS/broker/clients/total": "clients_total",
    "$SYS/broker/subscriptions/count": "subscriptions",
    "$SYS/broker/retained messages/count": "retained_messages",
    "$SYS/broker/messages/received": "messages_received",
    "$SYS/broker/messages/sent": "messages_sent",
    "$SYS/broker/bytes/received": "bytes_received",
    "$SYS/broker/bytes/sent": "bytes_sent",
    "$SYS/broker/uptime": "uptime_seconds",
    "$SYS/broker/load/messages/received/1min": "load_received_1min",
    "$SYS/broker/load/messages/sent/1min": "load_sent_1min",
}


def record_payload_metrics(event: object) -> None:
    """Expose what each device is publishing so dashboards can show it per channel."""
    if isinstance(event, SensorReading):
        for sensor, value in zip(SENSORS, event.sensors, strict=True):
            SENSOR_READING.labels(
                node_id=event.node_id, channel=sensor.channel, sensor_type=sensor.sensor_type
            ).set(value)
    elif isinstance(event, Prediction | GroundTruth):
        source = "prediction" if isinstance(event, Prediction) else "ground_truth"
        GAS_PPM.labels(node_id=event.node_id, gas="co", source=source).set(event.co_ppm)
        GAS_PPM.labels(node_id=event.node_id, gas="ethylene", source=source).set(event.ethylene_ppm)


class AckTracker:
    """Match actuator commands with device ACKs seen on MQTT and measure the round trip."""

    def __init__(self, timeout: float = ACK_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self.lock = threading.Lock()
        self.pending: dict[str, tuple[str, float]] = {}

    def _publish_pending(self, node_id: str) -> None:
        ACK_PENDING.labels(node_id=node_id).set(
            sum(1 for pending_node, _ in self.pending.values() if pending_node == node_id)
        )

    def command(self, command: ActuatorCommand, now: float) -> None:
        with self.lock:
            self.pending[str(command.event_id)] = (command.node_id, now)
            self._publish_pending(command.node_id)

    def ack(self, state: ActuatorState, now: float) -> float | None:
        with self.lock:
            entry = self.pending.pop(str(state.command_id), None)
            if entry is None:
                # Retained ACKs replayed after a reconnect have no pending command.
                return None
            node_id, sent_at = entry
            latency = max(0.0, now - sent_at)
            ACK_LATENCY.labels(node_id=node_id).observe(latency)
            self._publish_pending(node_id)
            return latency

    def expire(self, now: float) -> int:
        with self.lock:
            expired = [
                (command_id, node_id)
                for command_id, (node_id, sent_at) in self.pending.items()
                if now - sent_at > self.timeout
            ]
            for command_id, node_id in expired:
                del self.pending[command_id]
                ACK_TIMEOUTS.labels(node_id=node_id).inc()
                LOG.warning("Actuator command %s on %s was not acknowledged", command_id, node_id)
            for node_id in {node_id for _, node_id in expired}:
                self._publish_pending(node_id)
            return len(expired)


class Bridge:
    def __init__(self, mqtt_config: MqttConfig, kafka_config: KafkaConfig) -> None:
        self.mqtt_config = mqtt_config
        self.stop_event = threading.Event()
        self.last_sequences: dict[tuple[str, str], int] = {}
        self.acks = AckTracker()
        self.producer = Producer(
            {
                "bootstrap.servers": kafka_config.bootstrap_servers,
                "client.id": "mqtt-kafka-bridge-v1",
                "enable.idempotence": True,
                "acks": "all",
                "compression.type": "snappy",
                "linger.ms": 20,
                "batch.num.messages": 1000,
                # librdkafka buffers up to 1 GiB by default while Kafka is unreachable. Bound
                # it well below the 128 MiB container limit; overflow raises BufferError, which
                # is counted as an invalid message and logged instead of exhausting memory.
                "queue.buffering.max.kbytes": 32768,
                "queue.buffering.max.messages": 50000,
            }
        )
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="mqtt-kafka-bridge-v1",
            protocol=mqtt.MQTTv5,
        )
        if mqtt_config.username:
            self.client.username_pw_set(mqtt_config.username, mqtt_config.password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.error("MQTT connection rejected: %s", reason_code)
            return
        for topic in MQTT_SUBSCRIPTIONS:
            client.subscribe(topic, qos=self.mqtt_config.qos)
        client.subscribe("$SYS/broker/#", qos=0)
        MQTT_CONNECTED.set(1)
        LOG.info(
            "Subscribed to %d application topics with QoS %d",
            len(MQTT_SUBSCRIPTIONS),
            self.mqtt_config.qos,
        )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        MQTT_CONNECTED.set(0)
        if not self.stop_event.is_set():
            LOG.warning("MQTT disconnected: %s", reason_code)

    def _delivery_report(self, error, message) -> None:
        if error is not None:
            KAFKA_DELIVERY_FAILURES.labels(topic=message.topic()).inc()
            LOG.error("Kafka delivery failed for %s: %s", message.topic(), error)
        else:
            KAFKA_PRODUCED.labels(topic=message.topic()).inc()

    def _record_broker_stat(self, topic: str, payload: bytes) -> None:
        if name := BROKER_STATS.get(topic):
            try:
                # Most values are plain numbers; uptime arrives as "1234 seconds".
                MQTT_BROKER_STAT.labels(name=name).set(float(payload.split()[0]))
            except (ValueError, IndexError):
                LOG.debug("Ignored non-numeric Mosquitto statistic %s", topic)

    def _send_dead_letter(self, mqtt_topic_name: str, payload: bytes, error: Exception) -> None:
        envelope = {
            "failed_at": datetime.now(UTC).isoformat(),
            "source": "mqtt",
            "source_topic": mqtt_topic_name,
            "error": str(error),
            "payload": payload.decode("utf-8", errors="replace")[:8192],
        }
        self.producer.produce(
            str(KafkaTopic.DEAD_LETTER),
            value=json.dumps(envelope, separators=(",", ":")).encode(),
            callback=self._delivery_report,
        )

    def _on_message(self, client, userdata, message) -> None:
        if message.topic.startswith("$SYS/"):
            self._record_broker_stat(message.topic, message.payload)
            return
        try:
            suffix = message.topic.rsplit("/", 1)[-1]
            MQTT_MESSAGES.labels(event_type=suffix).inc()
            MQTT_BYTES.labels(event_type=suffix).inc(len(message.payload))
            kafka_topic = MQTT_SUFFIX_TO_KAFKA[suffix]
            model = EVENT_MODELS[suffix]
            event = model.model_validate_json(message.payload)

            topic_node_id = message.topic.split("/")[3]
            if event.node_id != topic_node_id:
                raise ValueError(
                    f"node_id mismatch: topic={topic_node_id!r}, payload={event.node_id!r}"
                )

            event_age = max(0.0, datetime.now(UTC).timestamp() - event.observed_at.timestamp())
            EVENT_AGE.labels(event_type=suffix).observe(event_age)
            LAST_EVENT_TIMESTAMP.labels(event_type=suffix, node_id=event.node_id).set(
                event.observed_at.timestamp()
            )
            sequence_key = (suffix, event.node_id)
            previous = self.last_sequences.get(sequence_key)
            if previous is not None and event.sequence > previous + 1:
                SEQUENCE_GAPS.labels(event_type=suffix, node_id=event.node_id).inc(
                    event.sequence - previous - 1
                )
            self.last_sequences[sequence_key] = max(event.sequence, previous or 0)
            record_payload_metrics(event)
            if isinstance(event, ActuatorCommand):
                self.acks.command(event, time.monotonic())
            elif isinstance(event, ActuatorState):
                self.acks.ack(event, time.monotonic())

            self.producer.produce(
                str(kafka_topic),
                key=event.node_id.encode(),
                value=event.model_dump_json().encode(),
                timestamp=int(event.observed_at.timestamp() * 1000),
                callback=self._delivery_report,
            )
            self.producer.poll(0)
        except (KeyError, ValueError, ValidationError, BufferError, KafkaException) as error:
            MQTT_INVALID.labels(reason=type(error).__name__).inc()
            LOG.warning("Rejected MQTT message on %s: %s", message.topic, error)
            try:
                self._send_dead_letter(message.topic, message.payload, error)
                self.producer.poll(0)
            except Exception:
                LOG.exception("Could not write dead-letter event")

    def run(self) -> None:
        self.client.connect(self.mqtt_config.host, self.mqtt_config.port, keepalive=30)
        self.client.loop_start()
        try:
            while not self.stop_event.wait(1):
                self.producer.poll(0)
                self.acks.expire(time.monotonic())
        finally:
            self.client.disconnect()
            self.client.loop_stop()
            remaining = self.producer.flush(10)
            if remaining:
                LOG.error("Shutdown left %d Kafka messages undelivered", remaining)

    def stop(self, *_args) -> None:
        self.stop_event.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    start_metrics_server()
    bridge = Bridge(MqttConfig(), KafkaConfig())
    signal.signal(signal.SIGTERM, bridge.stop)
    signal.signal(signal.SIGINT, bridge.stop)
    while True:
        try:
            bridge.run()
            return
        except OSError as error:
            if bridge.stop_event.is_set():
                return
            LOG.warning("MQTT unavailable (%s); retrying in 5s", error)
            time.sleep(5)


if __name__ == "__main__":
    main()

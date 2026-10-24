import logging
import signal
import threading
import time
from dataclasses import dataclass

from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException

from .config import InfluxConfig, KafkaConfig
from .contracts import (
    ActuatorCommand,
    ActuatorState,
    ControlRequest,
    GroundTruth,
    Prediction,
    SensorReading,
)
from .metrics import (
    INFLUX_BATCH_SIZE,
    INFLUX_LAST_WRITE_TIMESTAMP,
    INFLUX_POINTS_WRITTEN,
    INFLUX_WRITE_DURATION,
    INFLUX_WRITE_FAILURES,
    KAFKA_CONSUMED,
    PIPELINE_LATENCY,
    SINK_INVALID,
    start_metrics_server,
)
from .topics import KafkaTopic

LOG = logging.getLogger("influx-sink")
MEASUREMENT_BY_TOPIC = {
    str(KafkaTopic.SENSOR): "sensor_reading",
    str(KafkaTopic.PREDICTION): "gas_prediction",
    str(KafkaTopic.GROUND_TRUTH): "gas_ground_truth",
    str(KafkaTopic.CONTROL_REQUEST): "actuator_control_request",
    str(KafkaTopic.ACTUATOR_COMMAND): "actuator_command",
    str(KafkaTopic.ACTUATOR_STATE): "actuator_state",
}


# InfluxDB answers these when the batch itself is at fault: a point outside the retention period
# (a device that published before its clock was set) or malformed line protocol. The valid points
# of such a partial write are stored; retrying the same batch would fail forever and stall the
# partition, so the sink logs the rejection and commits past it.
REJECTED_BATCH_STATUS = frozenset({400, 422})

SENSOR_FIELDS = tuple(f"s{index}" for index in range(1, 17))


@dataclass(frozen=True)
class SinkRecord:
    point: Point
    measurement: str
    observed_at: float


def event_to_point(topic: str, payload: bytes) -> Point:
    return event_to_record(topic, payload).point


def event_to_record(topic: str, payload: bytes) -> SinkRecord:
    if topic == KafkaTopic.SENSOR:
        event = SensorReading.model_validate_json(payload)
        point = (
            Point("sensor_reading")
            .tag("node_id", event.node_id)
            .tag("schema_version", str(event.schema_version))
            .field("sequence", event.sequence)
        )
        for name, value in zip(SENSOR_FIELDS, event.sensors, strict=True):
            point.field(name, value)
    elif topic == KafkaTopic.PREDICTION:
        event = Prediction.model_validate_json(payload)
        point = (
            Point("gas_prediction")
            .tag("node_id", event.node_id)
            .tag("model_name", event.model_name)
            .tag("model_version", event.model_version)
            .tag("alarm_level", event.alarm_level)
            .field("sequence", event.sequence)
            .field("co_ppm", event.co_ppm)
            .field("ethylene_ppm", event.ethylene_ppm)
        )
        if event.inference_ms is not None:
            point.field("inference_ms", event.inference_ms)
        if event.window_s is not None:
            point.field("window_s", event.window_s)
        if event.top_channels:
            point.field("top_channels", ",".join(event.top_channels))
    elif topic == KafkaTopic.GROUND_TRUTH:
        event = GroundTruth.model_validate_json(payload)
        point = (
            Point("gas_ground_truth")
            .tag("node_id", event.node_id)
            .field("sequence", event.sequence)
            .field("co_ppm", event.co_ppm)
            .field("ethylene_ppm", event.ethylene_ppm)
        )
    elif topic == KafkaTopic.CONTROL_REQUEST:
        event = ControlRequest.model_validate_json(payload)
        point = (
            Point("actuator_control_request")
            .tag("node_id", event.node_id)
            .tag("mode", event.mode)
            .field("sequence", event.sequence)
            .field("fan_on", event.fan_on)
            .field("exhaust_on", event.exhaust_on)
            .field("gas_valve_closed", event.gas_valve_closed)
            .field("alarm_on", event.alarm_on)
            .field("suppression_on", event.suppression_on)
        )
    elif topic == KafkaTopic.ACTUATOR_COMMAND:
        event = ActuatorCommand.model_validate_json(payload)
        point = (
            Point("actuator_command")
            .tag("node_id", event.node_id)
            .tag("mode", event.mode)
            .tag("source", event.source)
            .field("sequence", event.sequence)
            .field("fan_on", event.fan_on)
            .field("exhaust_on", event.exhaust_on)
            .field("gas_valve_closed", event.gas_valve_closed)
            .field("alarm_on", event.alarm_on)
            .field("suppression_on", event.suppression_on)
            .field("alarm_muted", event.alarm_muted)
            .field("reason", event.reason)
        )
    elif topic == KafkaTopic.ACTUATOR_STATE:
        event = ActuatorState.model_validate_json(payload)
        point = (
            Point("actuator_state")
            .tag("node_id", event.node_id)
            .tag("mode", event.mode)
            .tag("source", event.source)
            .field("sequence", event.sequence)
            .field("fan_on", event.fan_on)
            .field("exhaust_on", event.exhaust_on)
            .field("gas_valve_closed", event.gas_valve_closed)
            .field("alarm_on", event.alarm_on)
            .field("suppression_on", event.suppression_on)
            .field("alarm_muted", event.alarm_muted)
            .field("command_id", str(event.command_id))
        )
    else:
        raise ValueError(f"Unsupported topic: {topic}")
    return SinkRecord(
        point=point.time(event.observed_at, WritePrecision.NS),
        measurement=MEASUREMENT_BY_TOPIC[topic],
        observed_at=event.observed_at.timestamp(),
    )


class InfluxSink:
    def __init__(self, kafka_config: KafkaConfig, influx_config: InfluxConfig) -> None:
        self.config = influx_config
        self.stop_event = threading.Event()
        self.consumer = Consumer(
            {
                "bootstrap.servers": kafka_config.bootstrap_servers,
                "group.id": kafka_config.consumer_group,
                "queued.max.messages.kbytes": 16384,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                "isolation.level": "read_committed",
            }
        )
        self.influx = InfluxDBClient(
            url=influx_config.url,
            token=influx_config.token,
            org=influx_config.org,
        )
        self.writer = self.influx.write_api(write_options=SYNCHRONOUS)
        # Batches are flushed twice a second; log a summary instead of every batch.
        self.log_every = 30.0
        self.logged_at = time.monotonic()
        self.window_points = 0
        self.window_batches = 0

    def _flush(self, records: list[SinkRecord], offsets: dict[tuple[str, int], int]) -> None:
        if not offsets:
            return
        rejected = False
        if records:
            INFLUX_BATCH_SIZE.observe(len(records))
            started_at = time.monotonic()
            try:
                self.writer.write(
                    bucket=self.config.bucket,
                    org=self.config.org,
                    record=[record.point for record in records],
                    write_precision=WritePrecision.NS,
                )
            except ApiException as error:
                INFLUX_WRITE_FAILURES.inc()
                if error.status not in REJECTED_BATCH_STATUS:
                    raise
                rejected = True
                LOG.error(
                    "InfluxDB rejected a batch of %d points (HTTP %s); committing past it: %s",
                    len(records),
                    error.status,
                    error.body,
                )
            except Exception:
                INFLUX_WRITE_FAILURES.inc()
                raise
            finally:
                INFLUX_WRITE_DURATION.observe(time.monotonic() - started_at)
        # Commit exactly the records in this batch, including a validated poison record that
        # was intentionally skipped. Do not commit the consumer's potentially newer position.
        commit_offsets = [
            TopicPartition(topic, partition, offset + 1)
            for (topic, partition), offset in offsets.items()
        ]
        self.consumer.commit(offsets=commit_offsets, asynchronous=False)
        durable_at = time.time()
        for record in [] if rejected else records:
            INFLUX_POINTS_WRITTEN.labels(measurement=record.measurement).inc()
            PIPELINE_LATENCY.labels(measurement=record.measurement).observe(
                max(0.0, durable_at - record.observed_at)
            )
        INFLUX_LAST_WRITE_TIMESTAMP.set(durable_at)
        self.window_points += len(records)
        self.window_batches += 1
        if time.monotonic() - self.logged_at >= self.log_every:
            LOG.info(
                "Wrote %d points in %d batches during the last %.0fs",
                self.window_points,
                self.window_batches,
                time.monotonic() - self.logged_at,
            )
            self.logged_at = time.monotonic()
            self.window_points = 0
            self.window_batches = 0
        records.clear()
        offsets.clear()

    def run(self) -> None:
        self.consumer.subscribe(
            [
                str(KafkaTopic.SENSOR),
                str(KafkaTopic.PREDICTION),
                str(KafkaTopic.GROUND_TRUTH),
                str(KafkaTopic.CONTROL_REQUEST),
                str(KafkaTopic.ACTUATOR_COMMAND),
                str(KafkaTopic.ACTUATOR_STATE),
            ]
        )
        records: list[SinkRecord] = []
        offsets: dict[tuple[str, int], int] = {}
        pending_count = 0
        last_flush = time.monotonic()
        try:
            while not self.stop_event.is_set():
                message = self.consumer.poll(0.25)
                if message is not None:
                    if message.error():
                        if message.error().code() != KafkaError._PARTITION_EOF:
                            raise KafkaException(message.error())
                    else:
                        KAFKA_CONSUMED.labels(topic=message.topic()).inc()
                        offsets[(message.topic(), message.partition())] = message.offset()
                        pending_count += 1
                        try:
                            records.append(event_to_record(message.topic(), message.value()))
                        except Exception:
                            SINK_INVALID.labels(topic=message.topic()).inc()
                            # Bridge validates contracts first. A poison record must not block
                            # the partition if malformed data is inserted directly into Kafka.
                            LOG.exception(
                                "Skipping invalid Kafka event topic=%s partition=%s offset=%s",
                                message.topic(),
                                message.partition(),
                                message.offset(),
                            )

                due = time.monotonic() - last_flush >= self.config.flush_seconds
                if offsets and (pending_count >= self.config.batch_size or due):
                    self._flush(records, offsets)
                    pending_count = 0
                    last_flush = time.monotonic()
        finally:
            try:
                self._flush(records, offsets)
            finally:
                self.consumer.close()
                self.writer.close()
                self.influx.close()

    def stop(self, *_args) -> None:
        self.stop_event.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    start_metrics_server()
    sink = InfluxSink(KafkaConfig(), InfluxConfig())
    signal.signal(signal.SIGTERM, sink.stop)
    signal.signal(signal.SIGINT, sink.stop)
    sink.run()


if __name__ == "__main__":
    main()

import logging
import os

from prometheus_client import Counter, Gauge, Histogram, start_http_server

LOG = logging.getLogger("metrics")

MQTT_CONNECTED = Gauge("aiot_mqtt_connected", "Bridge connection state to Mosquitto")
MQTT_MESSAGES = Counter(
    "aiot_mqtt_messages_received_total",
    "MQTT application messages received by the bridge",
    ["event_type"],
)
MQTT_INVALID = Counter(
    "aiot_mqtt_invalid_messages_total",
    "MQTT messages rejected by contract validation",
    ["reason"],
)
MQTT_BROKER_STAT = Gauge(
    "aiot_mqtt_broker_stat",
    "Mosquitto values published through the $SYS hierarchy",
    ["name"],
)
MQTT_BYTES = Counter(
    "aiot_mqtt_bytes_received_total",
    "MQTT payload bytes received by the bridge",
    ["event_type"],
)
SENSOR_READING = Gauge(
    "aiot_sensor_reading",
    "Latest raw reading of each MOX channel, as published on MQTT",
    ["node_id", "channel", "sensor_type"],
)
GAS_PPM = Gauge(
    "aiot_gas_ppm",
    "Latest gas concentration on MQTT per node and gas: source=prediction (edge model) or "
    "source=ground_truth (reference of the replayed recording)",
    ["node_id", "gas", "source"],
)
KAFKA_PRODUCED = Counter(
    "aiot_kafka_messages_produced_total", "Messages acknowledged by Kafka", ["topic"]
)
KAFKA_DELIVERY_FAILURES = Counter(
    "aiot_kafka_delivery_failures_total", "Kafka producer delivery failures", ["topic"]
)
EVENT_AGE = Histogram(
    "aiot_event_age_seconds",
    "Age of an event when it reaches the bridge",
    ["event_type"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 300, float("inf")),
)
LAST_EVENT_TIMESTAMP = Gauge(
    "aiot_last_event_timestamp_seconds",
    "Observed-at timestamp of the newest valid event",
    ["event_type", "node_id"],
)
SEQUENCE_GAPS = Counter(
    "aiot_sequence_gaps_total",
    "Missing sequence values detected by the bridge",
    ["event_type", "node_id"],
)
KAFKA_CONSUMED = Counter(
    "aiot_kafka_messages_consumed_total",
    "Kafka records consumed by the Influx sink",
    ["topic"],
)
SINK_INVALID = Counter(
    "aiot_sink_invalid_events_total", "Records rejected by the Influx sink", ["topic"]
)
INFLUX_POINTS_WRITTEN = Counter(
    "aiot_influx_points_written_total",
    "Points successfully written to InfluxDB",
    ["measurement"],
)
INFLUX_WRITE_FAILURES = Counter("aiot_influx_write_failures_total", "Failed InfluxDB write batches")
INFLUX_WRITE_DURATION = Histogram(
    "aiot_influx_write_duration_seconds",
    "Duration of synchronous InfluxDB writes",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, float("inf")),
)
INFLUX_BATCH_SIZE = Histogram(
    "aiot_influx_batch_size",
    "Number of points in each InfluxDB write batch",
    buckets=(1, 5, 10, 25, 50, 100, 200, 500, float("inf")),
)
INFLUX_LAST_WRITE_TIMESTAMP = Gauge(
    "aiot_influx_last_success_timestamp_seconds",
    "Timestamp of the most recent successful InfluxDB write",
)
# Edge inference: 16-channel window → model → prediction.
INFERENCE_LATENCY = Histogram(
    "aiot_inference_seconds",
    "Feature extraction + model time for one prediction",
    buckets=(0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, float("inf")),
)
PREDICTIONS = Counter(
    "aiot_predictions_total",
    "Predictions made by the edge model",
    ["node_id", "alarm_level"],
)
PREDICTED_PPM = Gauge(
    "aiot_predicted_ppm",
    "Latest gas concentration estimated by the edge model",
    ["node_id", "gas"],
)
PIPELINE_LATENCY = Histogram(
    "aiot_pipeline_latency_seconds",
    "End-to-end latency from observed_at on the device to a durable InfluxDB write",
    ["measurement"],
    buckets=(0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10, 30, float("inf")),
)

# Control plane: command -> device ACK, observed by the bridge on MQTT.
ACK_LATENCY = Histogram(
    "aiot_actuator_ack_latency_seconds",
    "Time from an actuator command on MQTT to the matching device ACK",
    ["node_id"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, float("inf")),
)
ACK_PENDING = Gauge(
    "aiot_actuator_pending_commands",
    "Actuator commands published without a matching device ACK yet",
    ["node_id"],
)
ACK_TIMEOUTS = Counter(
    "aiot_actuator_ack_timeouts_total",
    "Actuator commands that were not acknowledged within the ACK timeout",
    ["node_id"],
)

# Safety controller decisions.
CONTROL_LOOP_LAG = Histogram(
    "aiot_control_loop_lag_seconds",
    "Age of a prediction when the safety controller evaluates it",
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, float("inf")),
)
CONTROL_COMMANDS = Counter(
    "aiot_control_commands_total",
    "Actuator commands published by the safety controller",
    ["node_id", "mode", "trigger"],
)
CONTROL_DEDUPLICATED = Counter(
    "aiot_control_commands_deduplicated_total",
    "Decisions that did not publish because the desired state was unchanged",
    ["node_id"],
)
ALARM_LEVEL = Gauge(
    "aiot_alarm_level",
    "Latest prediction alarm level: 0 normal, 1 warning, 2 danger",
    ["node_id"],
)
SIREN_MUTED = Gauge(
    "aiot_siren_muted",
    "1 while an operator has silenced the siren of the node",
    ["node_id"],
)
OVERRIDE_ACTIVE = Gauge(
    "aiot_direct_override_active",
    "1 while an operator direct override is active for the node",
    ["node_id"],
)
DESIRED_OUTPUT = Gauge(
    "aiot_actuator_desired_output",
    "Last actuator output commanded by the safety controller (1 on/closed)",
    ["node_id", "output"],
)


def start_metrics_server() -> None:
    port = int(os.getenv("METRICS_PORT", "8000"))
    start_http_server(port)
    LOG.info("Prometheus metrics listening on port %d", port)

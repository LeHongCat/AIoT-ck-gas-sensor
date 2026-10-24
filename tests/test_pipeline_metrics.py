from datetime import UTC, datetime

from aiot_pipeline.contracts import ActuatorCommand, ActuatorState, SensorReading
from aiot_pipeline.influx_sink import event_to_record
from aiot_pipeline.mqtt_kafka_bridge import AckTracker
from aiot_pipeline.topics import KafkaTopic


def command(node_id: str = "node-1") -> ActuatorCommand:
    return ActuatorCommand(
        node_id=node_id,
        observed_at=datetime.now(UTC),
        sequence=1,
        mode="auto",
        source="safety-controller",
        fan_on=True,
        exhaust_on=True,
        gas_valve_closed=False,
        alarm_on=True,
        reason="prediction alarm level is warning",
    )


def ack_for(cmd: ActuatorCommand) -> ActuatorState:
    return ActuatorState(
        node_id=cmd.node_id,
        observed_at=datetime.now(UTC),
        sequence=cmd.sequence,
        command_id=cmd.event_id,
        mode=cmd.mode,
        source=cmd.source,
        fan_on=cmd.fan_on,
        exhaust_on=cmd.exhaust_on,
        gas_valve_closed=cmd.gas_valve_closed,
        alarm_on=cmd.alarm_on,
    )


def test_ack_tracker_measures_round_trip() -> None:
    tracker = AckTracker(timeout=5)
    cmd = command()
    tracker.command(cmd, now=10.0)
    latency = tracker.ack(ack_for(cmd), now=10.04)
    assert latency is not None and abs(latency - 0.04) < 1e-9
    assert tracker.pending == {}
    # A duplicate ACK (MQTT QoS 1) is not counted twice.
    assert tracker.ack(ack_for(cmd), now=10.05) is None


def test_ack_tracker_ignores_retained_ack_without_command() -> None:
    tracker = AckTracker(timeout=5)
    assert tracker.ack(ack_for(command()), now=1.0) is None


def test_ack_tracker_expires_unacknowledged_commands() -> None:
    tracker = AckTracker(timeout=5)
    tracker.command(command(), now=0.0)
    assert tracker.expire(now=4.9) == 0
    assert tracker.expire(now=5.1) == 1
    assert tracker.pending == {}


def test_sink_record_carries_measurement_and_observed_time() -> None:
    observed_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    event = SensorReading(node_id="node-1", observed_at=observed_at, sequence=1, sensors=[1.0] * 16)
    record = event_to_record(str(KafkaTopic.SENSOR), event.model_dump_json().encode())
    assert record.measurement == "sensor_reading"
    assert record.observed_at == observed_at.timestamp()
    assert record.point.to_line_protocol().startswith("sensor_reading,")

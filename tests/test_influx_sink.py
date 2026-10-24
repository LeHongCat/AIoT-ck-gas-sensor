from datetime import UTC, datetime
from uuid import uuid4

import pytest
from influxdb_client.rest import ApiException

from aiot_pipeline.config import InfluxConfig
from aiot_pipeline.contracts import ActuatorState, GroundTruth, SensorReading
from aiot_pipeline.influx_sink import InfluxSink, event_to_point, event_to_record
from aiot_pipeline.topics import KafkaTopic


def test_sensor_point_contains_all_channels() -> None:
    event = SensorReading(
        node_id="node-1",
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
        sequence=3,
        sensors=[float(value) for value in range(16)],
    )
    line = event_to_point(
        str(KafkaTopic.SENSOR), event.model_dump_json().encode()
    ).to_line_protocol()
    assert line.startswith("sensor_reading,node_id=node-1,schema_version=1 ")
    assert "s1=0" in line
    assert "s16=15" in line


def test_actuator_state_point_contains_acknowledged_outputs() -> None:
    event = ActuatorState(
        node_id="node-1",
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
        sequence=4,
        command_id=uuid4(),
        mode="auto",
        source="safety-controller",
        fan_on=True,
        exhaust_on=True,
        gas_valve_closed=False,
        alarm_on=True,
    )
    line = event_to_point(
        str(KafkaTopic.ACTUATOR_STATE), event.model_dump_json().encode()
    ).to_line_protocol()
    assert line.startswith("actuator_state,mode=auto,node_id=node-1,source=safety-controller ")
    assert "fan_on=true" in line
    assert "exhaust_on=true" in line


def test_ground_truth_point_pairs_with_the_prediction_timestamp() -> None:
    observed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    event = GroundTruth(
        node_id="node-1", observed_at=observed, sequence=7, co_ppm=240.0, ethylene_ppm=8.33
    )
    point = event_to_point(str(KafkaTopic.GROUND_TRUTH), event.model_dump_json().encode())
    line = point.to_line_protocol()
    assert line.startswith("gas_ground_truth,node_id=node-1 ")
    assert "co_ppm=240" in line and "ethylene_ppm=8.33" in line and "sequence=7i" in line
    # Same observed_at as the reading and its prediction: Flux joins them on _time.
    assert line.endswith(str(int(observed.timestamp() * 1e9)))


class _Writer:
    def __init__(self, status: int) -> None:
        self.status = status

    def write(self, **_kwargs) -> None:
        raise ApiException(status=self.status, reason="rejected")


class _Consumer:
    def __init__(self) -> None:
        self.committed = []

    def commit(self, offsets, asynchronous) -> None:
        self.committed.extend((tp.topic, tp.partition, tp.offset) for tp in offsets)


def _sink(status: int) -> InfluxSink:
    sink = object.__new__(InfluxSink)
    sink.config = InfluxConfig()
    sink.writer = _Writer(status)
    sink.consumer = _Consumer()
    sink.log_every = 30.0
    sink.logged_at = 0.0
    sink.window_points = 0
    sink.window_batches = 0
    return sink


def _batch() -> tuple[list, dict]:
    # A device that published before NTP set its clock: InfluxDB drops it (retention, HTTP 422).
    event = GroundTruth(
        node_id="node-1",
        observed_at=datetime(1970, 1, 1, 0, 0, 21, tzinfo=UTC),
        sequence=21,
        co_ppm=0.0,
        ethylene_ppm=0.0,
    )
    topic = str(KafkaTopic.GROUND_TRUTH)
    return [event_to_record(topic, event.model_dump_json().encode())], {(topic, 0): 41}


def test_rejected_batch_is_committed_instead_of_stalling_the_partition() -> None:
    sink = _sink(422)
    records, offsets = _batch()
    sink._flush(records, offsets)
    assert sink.consumer.committed == [(str(KafkaTopic.GROUND_TRUTH), 0, 42)]
    assert not records and not offsets


def test_unavailable_influx_is_retried_rather_than_committed() -> None:
    sink = _sink(503)
    records, offsets = _batch()
    with pytest.raises(ApiException):
        sink._flush(records, offsets)
    assert sink.consumer.committed == []

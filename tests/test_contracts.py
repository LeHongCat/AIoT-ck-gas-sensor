import inspect
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aiot_pipeline import contracts, edge_inference, safety_controller, topics
from aiot_pipeline.contracts import ControlRequest, Prediction, SensorReading
from aiot_pipeline.influx_sink import MEASUREMENT_BY_TOPIC
from aiot_pipeline.safety_controller import automatic_outputs, automation_should_take_over


def test_sensor_requires_exactly_16_values() -> None:
    with pytest.raises(ValidationError):
        SensorReading(
            node_id="node-1",
            observed_at=datetime.now(UTC),
            sequence=1,
            sensors=[1.0] * 15,
        )


def test_event_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        SensorReading(
            node_id="node-1",
            observed_at=datetime.now(),
            sequence=1,
            sensors=[1.0] * 16,
        )


def test_prediction_round_trip() -> None:
    event = Prediction(
        node_id="node-1",
        observed_at=datetime.now(UTC),
        sequence=7,
        co_ppm=51.2,
        ethylene_ppm=3.1,
        model_name="ESN_RLS",
        model_version="v1",
        alarm_level="warning",
    )
    assert Prediction.model_validate_json(event.model_dump_json()) == event


def test_control_request_round_trip() -> None:
    event = ControlRequest(
        node_id="node-1",
        observed_at=datetime.now(UTC),
        sequence=8,
        mode="manual",
        fan_on=True,
        exhaust_on=True,
    )
    assert ControlRequest.model_validate_json(event.model_dump_json()) == event


def test_automatic_safety_policy() -> None:
    assert automatic_outputs("normal") == {
        "fan_on": False,
        "exhaust_on": False,
        "gas_valve_closed": False,
        "alarm_on": False,
        "suppression_on": False,
    }
    assert automatic_outputs("warning")["fan_on"] is True
    assert automatic_outputs("warning")["gas_valve_closed"] is False
    assert automatic_outputs("danger")["gas_valve_closed"] is True


def test_auto_resumes_after_ttl_and_danger_preempts_override() -> None:
    assert automation_should_take_over("warning", override_deadline=130, now=100) is False
    assert automation_should_take_over("warning", override_deadline=100, now=100) is True
    assert automation_should_take_over("danger", override_deadline=130, now=100) is True


def test_ground_truth_travels_the_pipeline_but_never_feeds_the_model() -> None:
    """The recording's CO / C2H4 reach Kafka and InfluxDB as a reference for the dashboards;
    the edge model reads readings only and the controller acts on predictions only."""
    assert contracts.EVENT_MODELS["ground-truth"] is contracts.GroundTruth
    assert topics.MQTT_SUFFIX_TO_KAFKA["ground-truth"] == topics.KafkaTopic.GROUND_TRUTH
    assert "aiot/v1/nodes/+/ground-truth" in topics.MQTT_SUBSCRIPTIONS
    assert topics.KafkaTopic.GROUND_TRUTH in topics.TOPIC_SETTINGS
    assert MEASUREMENT_BY_TOPIC[str(topics.KafkaTopic.GROUND_TRUTH)] == "gas_ground_truth"

    edge = inspect.getsource(edge_inference.EdgeInferenceService._on_connect)
    assert "ground-truth" not in edge and "/readings" in edge
    controller = inspect.getsource(safety_controller.SafetyController.run)
    assert "GROUND_TRUTH" not in controller and "KafkaTopic.PREDICTION" in controller


def test_ground_truth_rejects_negative_concentrations() -> None:
    with pytest.raises(ValidationError):
        contracts.GroundTruth(
            node_id="n1", observed_at=datetime.now(UTC), sequence=0, co_ppm=-1, ethylene_ppm=0
        )

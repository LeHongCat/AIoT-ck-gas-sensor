"""Operator siren mute: the siren goes quiet, everything else keeps running and is logged.

Muting is a ControlRequest like any operator action (MQTT → Kafka → Safety Controller), so it
is audited. The controller stays in AUTO: fans, exhaust and the gas valve follow the alarm
level, commands and ACKs keep flowing to Kafka/InfluxDB, and only alarm_on drops. A new DANGER
sounds the siren again; a suppression discharge always sounds it.
"""

from datetime import UTC, datetime
from uuid import uuid4

import paho.mqtt.client as mqtt

from aiot_pipeline.contracts import ActuatorCommand, ActuatorState, ControlRequest, Prediction
from aiot_pipeline.influx_sink import event_to_point
from aiot_pipeline.safety_controller import (
    SafetyController,
    automatic_outputs,
    siren_rearms,
    takeover_outputs,
)
from aiot_pipeline.topics import KafkaTopic

NODE = "node-1"


class _Published:
    rc = mqtt.MQTT_ERR_SUCCESS

    def wait_for_publish(self, timeout: float | None = None) -> None:
        return None


class _FakeMqtt:
    def __init__(self) -> None:
        self.commands: list[ActuatorCommand] = []

    def publish(self, topic: str, payload: str, qos: int = 0) -> _Published:
        assert topic == f"aiot/v1/nodes/{NODE}/commands"
        self.commands.append(ActuatorCommand.model_validate_json(payload))
        return _Published()


def _controller() -> SafetyController:
    # Skip __init__: no Kafka consumer or broker connection, only the decision logic.
    controller = SafetyController.__new__(SafetyController)
    controller.qos = 1
    controller.predictions = {}
    controller.last_outputs = {}
    controller.sequences = {}
    controller.override_until = {}
    controller.suppression_until = {}
    controller.siren_muted = {}
    controller.override_seconds = 30.0
    controller.client = _FakeMqtt()
    return controller


def _predict(controller: SafetyController, level: str, sequence: int) -> None:
    controller._handle_prediction(
        Prediction(
            node_id=NODE,
            observed_at=datetime.now(UTC),
            sequence=sequence,
            co_ppm={"normal": 5.0, "warning": 80.0, "danger": 300.0}[level],
            ethylene_ppm=0.0,
            model_name="test",
            model_version="v1",
            alarm_level=level,
        )
    )


def _request(controller: SafetyController, mode: str) -> None:
    controller._handle_request(
        ControlRequest(node_id=NODE, observed_at=datetime.now(UTC), sequence=0, mode=mode)
    )


def test_muted_siren_only_drops_the_alarm_output() -> None:
    for level in ("warning", "danger"):
        armed, muted = automatic_outputs(level), automatic_outputs(level, siren_muted=True)
        assert armed["alarm_on"] is True and muted["alarm_on"] is False
        assert {k: v for k, v in muted.items() if k != "alarm_on"} == {
            k: v for k, v in armed.items() if k != "alarm_on"
        }
    # The suppression interlock wins: a discharge always sounds the siren.
    assert takeover_outputs("warning", suppression_active=True, siren_muted=True)["alarm_on"]


def test_only_a_new_danger_rearms_the_siren() -> None:
    assert siren_rearms("warning", "danger") is True
    assert siren_rearms(None, "danger") is True
    assert siren_rearms("danger", "danger") is False
    assert siren_rearms("normal", "warning") is False


def test_mute_keeps_auto_control_running_until_a_new_danger() -> None:
    controller = _controller()
    sent = controller.client.commands

    _predict(controller, "warning", 1)
    assert sent[-1].alarm_on is True and sent[-1].alarm_muted is False

    _request(controller, "mute")
    muted = sent[-1]
    assert muted.mode == "auto" and muted.source == "safety-controller"
    assert muted.alarm_on is False and muted.alarm_muted is True
    assert muted.fan_on and muted.exhaust_on and not muted.gas_valve_closed
    assert "siren muted by operator" in muted.reason

    _predict(controller, "warning", 2)  # same decision: deduplicated, still muted
    assert sent[-1] is muted

    _predict(controller, "danger", 3)
    rearmed = sent[-1]
    assert rearmed.alarm_on is True and rearmed.alarm_muted is False
    assert rearmed.gas_valve_closed and "re-armed" in rearmed.reason

    _request(controller, "mute")  # silencing during the danger is allowed...
    assert sent[-1].alarm_on is False and sent[-1].gas_valve_closed is True
    _predict(controller, "danger", 4)  # ...and holds while it is the same danger
    assert sent[-1].alarm_on is False

    _request(controller, "unmute")
    assert sent[-1].alarm_on is True and sent[-1].alarm_muted is False
    assert "siren unmuted by operator" in sent[-1].reason
    assert all(command.mode == "auto" for command in sent)


def test_mute_without_prediction_waits_for_the_first_one() -> None:
    controller = _controller()
    _request(controller, "mute")
    assert controller.client.commands == []
    _predict(controller, "warning", 1)
    assert controller.client.commands[-1].alarm_on is False
    assert controller.client.commands[-1].alarm_muted is True


def test_mute_flag_is_logged_with_commands_and_acks() -> None:
    command = ActuatorCommand(
        node_id=NODE,
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
        sequence=5,
        mode="auto",
        source="safety-controller",
        fan_on=True,
        exhaust_on=True,
        gas_valve_closed=False,
        alarm_on=False,
        alarm_muted=True,
        reason="prediction alarm level is warning; siren muted by operator",
    )
    line = event_to_point(
        str(KafkaTopic.ACTUATOR_COMMAND), command.model_dump_json().encode()
    ).to_line_protocol()
    assert "alarm_muted=true" in line and "alarm_on=false" in line and "mode=auto" in line

    ack = ActuatorState(
        node_id=NODE,
        observed_at=datetime(2026, 1, 2, tzinfo=UTC),
        sequence=5,
        command_id=uuid4(),
        mode="auto",
        source="safety-controller",
        fan_on=True,
        exhaust_on=True,
        gas_valve_closed=False,
        alarm_on=False,
        alarm_muted=True,
    )
    line = event_to_point(
        str(KafkaTopic.ACTUATOR_STATE), ack.model_dump_json().encode()
    ).to_line_protocol()
    assert "alarm_muted=true" in line

    request = ControlRequest(node_id=NODE, observed_at=datetime.now(UTC), sequence=0, mode="mute")
    line = event_to_point(
        str(KafkaTopic.CONTROL_REQUEST), request.model_dump_json().encode()
    ).to_line_protocol()
    assert line.startswith("actuator_control_request,mode=mute,")


def test_an_ack_without_the_flag_is_still_valid() -> None:
    """Firmware built before the flag existed keeps its ACKs accepted (alarm_muted=False)."""
    ack = ActuatorState.model_validate(
        {
            "node_id": NODE,
            "observed_at": "2026-01-02T00:00:00Z",
            "sequence": 1,
            "command_id": str(uuid4()),
            "mode": "auto",
            "source": "safety-controller",
            "fan_on": False,
            "exhaust_on": False,
            "gas_valve_closed": False,
            "alarm_on": False,
        }
    )
    assert ack.alarm_muted is False

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

SensorVector = Annotated[list[float], Field(min_length=16, max_length=16)]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    event_id: UUID = Field(default_factory=uuid4)
    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    observed_at: datetime
    sequence: int = Field(ge=0)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value.astimezone(UTC)


class SensorReading(Event):
    event_type: Literal["sensor.reading"] = "sensor.reading"
    sample_rate_hz: float = Field(default=1.0, gt=0)
    sensors: SensorVector


class Prediction(Event):
    event_type: Literal["gas.prediction"] = "gas.prediction"
    co_ppm: float = Field(ge=0)
    ethylene_ppm: float = Field(ge=0)
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=64)
    alarm_level: Literal["normal", "warning", "danger"]
    inference_ms: float | None = Field(default=None, ge=0)
    # Seconds of sensor history the model used, and the channels that drove the estimate.
    window_s: int | None = Field(default=None, ge=1, le=600)
    top_channels: list[str] = Field(default_factory=list, max_length=16)


class GroundTruth(Event):
    """Reference concentration of the replayed recording (the dataset's CO / C2H4 set-points).

    Published next to the reading with the same observed_at and sequence, so it pairs 1:1 with
    the prediction of that second. It is a reference for evaluation and display only: the edge
    model never reads it and the safety controller never acts on it.
    """

    event_type: Literal["gas.ground_truth"] = "gas.ground_truth"
    co_ppm: float = Field(ge=0)
    ethylene_ppm: float = Field(ge=0)


class ControlRequest(Event):
    event_type: Literal["actuator.control-request"] = "actuator.control-request"
    # auto: cancel a direct override · manual: direct override with the outputs below ·
    # mute / unmute: silence or re-arm the siren while detection, control and logging go on.
    mode: Literal["auto", "manual", "mute", "unmute"]
    fan_on: bool = False
    exhaust_on: bool = False
    gas_valve_closed: bool = False
    alarm_on: bool = False
    suppression_on: bool = False


class ActuatorCommand(Event):
    event_type: Literal["actuator.command"] = "actuator.command"
    mode: Literal["auto", "manual"]
    source: Literal["safety-controller", "operator"]
    fan_on: bool
    exhaust_on: bool
    gas_valve_closed: bool
    alarm_on: bool
    suppression_on: bool = False
    # An operator silenced the siren: alarm_on stays False until unmuted or a new DANGER.
    alarm_muted: bool = False
    reason: str = Field(min_length=1, max_length=256)


class ActuatorState(Event):
    event_type: Literal["actuator.state"] = "actuator.state"
    command_id: UUID
    mode: Literal["auto", "manual"]
    source: Literal["safety-controller", "operator"]
    fan_on: bool
    exhaust_on: bool
    gas_valve_closed: bool
    alarm_on: bool
    suppression_on: bool = False
    alarm_muted: bool = False


DEMO_SCENARIOS = ("co_warning", "co_spike", "ethylene_spike", "mixture", "clear")


class DemoScenario(Event):
    """Demo-only request telling the simulator to inject a gas episode at the edge."""

    event_type: Literal["demo.scenario"] = "demo.scenario"
    scenario: Literal["co_warning", "co_spike", "ethylene_spike", "mixture", "clear"]
    duration_s: int = Field(default=90, ge=10, le=600)


class DemoState(Event):
    """Demo-only telemetry from the simulator: the leak, the room air and how it is clearing."""

    event_type: Literal["demo.state"] = "demo.state"
    scenario: str | None = None
    auto: bool = False
    phase: Literal["clean", "leaking", "isolated", "clearing"]
    remaining_s: float | None = None
    source_co_ppm: float = Field(default=0.0, ge=0)
    source_ethylene_ppm: float = Field(default=0.0, ge=0)
    room_co_ppm: float = Field(ge=0)
    room_ethylene_ppm: float = Field(ge=0)
    ventilation: Literal["natural", "hood", "roof", "full"]
    eta_safe_s: float | None = None


EVENT_MODELS = {
    "readings": SensorReading,
    "predictions": Prediction,
    "ground-truth": GroundTruth,
    "control-requests": ControlRequest,
    "commands": ActuatorCommand,
    "actuator-state": ActuatorState,
}

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aiot_pipeline.contracts import ControlRequest, DemoScenario
from aiot_pipeline.edge_model import NodeInference, RidgeModel, classify_alarm
from aiot_pipeline.safety_controller import automatic_outputs, manual_outputs, takeover_outputs
from aiot_pipeline.sensors import SENSOR_TYPES, SENSORS
from aiot_pipeline.simulator import (
    CHANNELS,
    GasSchedule,
    KitchenAir,
    Leak,
    SensorModel,
    leak_phase,
)

MODEL = RidgeModel.load()


def test_clean_air_readings_match_dataset_baselines() -> None:
    model = SensorModel(seed=1)
    readings = [model.step(0.0, 0.0) for _ in range(30)][-1]
    assert len(readings) == 16
    for value, channel in zip(readings, CHANNELS, strict=True):
        assert 0.9 * channel.base < value < 1.1 * channel.base
    assert 250 < min(readings) and max(readings) < 2100


def test_full_scale_co_reaches_dataset_peaks_and_recovers_slowly() -> None:
    model = SensorModel(seed=1)
    for _ in range(300):
        peak = model.step(533.33, 0.0)
    assert 7000 < max(peak) < 9500
    after_rise = model.step(0.0, 0.0)
    for _ in range(29):
        after_rise = model.step(0.0, 0.0)
    # 30 s after the gas is removed the strongest channel is still far above its baseline.
    strongest = max(range(16), key=lambda index: CHANNELS[index].co_gain)
    assert after_rise[strongest] > 2 * CHANNELS[strongest].base


def test_alarm_policy_follows_notebook_thresholds() -> None:
    assert classify_alarm(55, 0, warning_latched=False) == "warning"
    assert classify_alarm(45, 0, warning_latched=False) == "normal"
    assert classify_alarm(45, 0, warning_latched=True) == "warning"
    assert classify_alarm(35, 0, warning_latched=True) == "normal"
    assert classify_alarm(210, 0, warning_latched=False) == "danger"
    assert classify_alarm(0, 3.2, warning_latched=False) == "warning"


def run_closed_loop(scenario: str, seconds: int = 150, purge_at: int | None = None):
    """Leak → kitchen air → 16 MOX sensors → edge model → Auto policy, one step per second."""
    schedule, air, sensors, node = (
        GasSchedule(),
        KitchenAir(),
        SensorModel(seed=1),
        NodeInference(MODEL),
    )
    for _ in range(80):  # warm the window on clean air
        node.step(sensors.step(0.0, 0.0))
    schedule.trigger(scenario, duration_s=90, now=0)
    outputs = {"fan_on": False, "exhaust_on": False, "gas_valve_closed": False}
    timeline = []
    for t in range(1, seconds):
        leak = schedule.advance(t, outputs["gas_valve_closed"], air.is_safe())
        co, ethylene = air.step(leak, outputs["fan_on"], outputs["exhaust_on"])
        level = node.step(sensors.step(co, ethylene))[1]
        outputs = {key: value for key, value in automatic_outputs(level).items() if key in outputs}
        if purge_at is not None and t >= purge_at:
            outputs = {"fan_on": True, "exhaust_on": True, "gas_valve_closed": True}
        timeline.append((t, level, co, ethylene, leak_phase(leak, air)))
    return timeline


def first(timeline, predicate) -> int:
    return next(t for t, *rest in timeline if predicate(*rest))


def test_co_spike_trips_danger_then_clears_once_the_valve_isolates_the_leak() -> None:
    timeline = run_closed_loop("co_spike")
    danger = first(timeline, lambda level, co, eth, phase: level == "danger")
    isolated = first(timeline, lambda level, co, eth, phase: phase == "isolated")
    normal = first(timeline, lambda level, co, eth, phase: level == "normal" and phase == "clean")
    assert danger < 30, "a 533 ppm source must reach DANGER quickly"
    assert isolated <= danger + 2, "closing the gas valve cuts the source"
    assert normal - danger < 45, "ventilation clears the kitchen in well under a minute"
    # The leak never comes back after the valve reopens.
    assert all(phase == "clean" for _, _, _, _, phase in timeline[normal:])


def test_ventilation_lowers_the_room_level_of_an_ongoing_leak() -> None:
    still, ventilated = KitchenAir(), KitchenAir()
    leak = Leak("co_warning", until=1e9)
    for _ in range(200):
        still.step(leak, fan_on=False, exhaust_on=False)
        ventilated.step(leak, fan_on=True, exhaust_on=True)
    assert ventilated.co < 0.55 * still.co
    assert ventilated.co > 40, "a warning-level leak stays in WARNING while it emits"


def test_purge_isolates_a_warning_leak_and_the_alarm_clears_in_real_time() -> None:
    timeline = run_closed_loop("ethylene_spike", purge_at=30)
    assert timeline[25][1] == "warning"
    cleared = next(t for t, level, *_ in timeline if t > 30 and level == "normal")
    assert cleared - 30 < 30, "after the purge the alarm clears within half a minute"
    assert timeline[cleared][3] < 3


def test_cutting_the_source_lets_the_air_decay_instead_of_resetting() -> None:
    schedule, air = GasSchedule(), KitchenAir()
    schedule.trigger("co_spike", duration_s=90, now=0)
    for t in range(60):
        air.step(schedule.advance(t, False, air.is_safe()), fan_on=True, exhaust_on=True)
    before = air.co
    schedule.trigger("clear", duration_s=90, now=60)
    leak = schedule.advance(61, False, air.is_safe())
    air.step(leak, fan_on=True, exhaust_on=True)
    assert leak is None and 0 < air.co < before
    assert leak_phase(leak, air) == "clearing"
    assert 0 < air.eta_safe(fan_on=True, exhaust_on=True) < 60


def test_schedule_auto_cycle_starts_one_leak_per_period() -> None:
    schedule, air = GasSchedule(auto_every=100, auto_seconds=20), KitchenAir()
    leaks = [schedule.advance(t, False, air.is_safe()) for t in range(200)]
    assert all(leak is None for leak in leaks[:80])
    assert all(leak is not None and leak.auto for leak in leaks[80:100])
    assert all(leak is None for leak in leaks[100:180])


def test_demo_scenario_contract_rejects_unknown_scenario() -> None:
    DemoScenario(node_id="n", observed_at=datetime.now(UTC), sequence=0, scenario="co_spike")
    with pytest.raises(ValidationError):
        DemoScenario(node_id="n", observed_at=datetime.now(UTC), sequence=0, scenario="fire")


def test_suppression_interlocks_gas_and_alarm_and_is_never_automatic() -> None:
    request = ControlRequest(
        node_id="n",
        observed_at=datetime.now(UTC),
        sequence=0,
        mode="manual",
        fan_on=True,
        suppression_on=True,
    )
    outputs = manual_outputs(request)
    assert outputs["suppression_on"] and outputs["gas_valve_closed"] and outputs["alarm_on"]
    assert all(
        automatic_outputs(level)["suppression_on"] is False
        for level in ("normal", "warning", "danger")
    )


def test_danger_takeover_keeps_running_suppression() -> None:
    kept = takeover_outputs("danger", suppression_active=True)
    assert kept["suppression_on"] and kept["gas_valve_closed"] and kept["fan_on"]
    assert takeover_outputs("normal", suppression_active=False)["suppression_on"] is False


def test_simulator_channels_follow_shared_sensor_layout() -> None:
    assert tuple(channel.sensor for channel in CHANNELS) == SENSOR_TYPES
    assert [sensor.channel for sensor in SENSORS] == [f"S{i}" for i in range(1, 17)]
    assert all(sensor.location and sensor.target for sensor in SENSORS)

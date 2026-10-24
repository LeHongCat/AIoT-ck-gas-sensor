import re
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from confluent_kafka import TopicPartition
from pydantic import ValidationError

from aiot_pipeline.contracts import ActuatorState, Prediction, SensorReading
from aiot_pipeline.demo_ui import (
    STATIC_DIR,
    ControlAction,
    KitchenState,
    backfill_offsets,
    get_config,
)
from aiot_pipeline.sensors import SENSOR_TYPES
from aiot_pipeline.topics import KafkaTopic


def test_kitchen_demo_assets_are_packaged() -> None:
    assert (STATIC_DIR / "index.html").is_file()
    assert (STATIC_DIR / "styles.css").is_file()
    assert (STATIC_DIR / "app.js").is_file()
    assert (STATIC_DIR / "kitchen.js").is_file()
    assert (STATIC_DIR / "flow.js").is_file()


def test_ui_shows_pipeline_map_mqtt_inspector_and_device_state() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for element_id in ("flowSvg", "topicList", "gasHero", "actuatorStateList", "controlAck"):
        assert element_id in html
    # The record already contains the gas, so the screen must not offer controls
    # that cannot change it.
    for removed in ("heroScenario", "purgeButton", "suppressButton", "modeHud", "cancelOverride"):
        assert removed not in html
    assert html.count('<use href="#sensorCan" />') == 16


def test_enose_board_holds_each_channel_once_with_its_type() -> None:
    # The 16 sensors are one array on one board (rows by type), not spread over the plan.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert html.count('class="sensor-node"') == 16
    assert 'class="sensor-layer"' not in html
    found = dict(re.findall(r'class="sensor-node" data-index="(\d+)" data-sensor="(\w+)"', html))
    assert {int(index): sensor for index, sensor in found.items()} == dict(enumerate(SENSOR_TYPES))


def test_screen_has_no_operator_or_demo_controls() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'class="actuator-control"' not in html
    assert "data-scenario" not in html
    assert 'id="cancelOverride"' not in html
    assert "Chưa nhận ACK từ thiết bị" in html


def test_kitchen_state_starts_empty() -> None:
    snapshot = KitchenState().snapshot()
    assert snapshot["room"] == "Kitchen Room"
    assert snapshot["latest"]["sensor"] is None
    assert snapshot["counts"]["prediction"] == 0


def test_control_action_rejects_invalid_node_id() -> None:
    with pytest.raises(ValidationError):
        ControlAction(node_id="bad node", mode="manual", fan_on=True)


def _prediction(sequence: int, observed_at: datetime, node_id: str = "node-1") -> Prediction:
    return Prediction(
        node_id=node_id,
        observed_at=observed_at,
        sequence=sequence,
        co_ppm=60.0,
        ethylene_ppm=1.0,
        model_name="m",
        model_version="v1",
        alarm_level="warning",
    )


def test_record_tracks_history_rate_and_latency() -> None:
    state = KitchenState()
    now = time.time()
    for sequence in range(3):
        observed = datetime.fromtimestamp(now - 0.05, UTC)
        state.record("prediction", _prediction(sequence, observed), "t", 0, sequence, now)
    snapshot = state.snapshot()
    assert snapshot["version"] == 3
    assert snapshot["counts"]["prediction"] == 3
    assert snapshot["stream"]["prediction"]["rate"] > 0
    assert 0 < snapshot["stream"]["prediction"]["latency_ms"] < 1000
    history = state.history_snapshot()["prediction"]
    assert [row[1] for row in history] == [0, 1, 2]


def test_replayed_events_do_not_skew_latency() -> None:
    state = KitchenState()
    now = time.time()
    old = datetime.fromtimestamp(now - 3600, UTC)
    state.record("prediction", _prediction(1, old), "t", 0, 1, now)
    assert state.snapshot()["stream"]["prediction"]["latency_ms"] is None
    assert state.snapshot()["events"][0]["replayed"] is True


def test_industrial_kitchen_scene_assets() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="kitchenSvg"' in html
    assert 'id="crewRoster"' in html
    assert 'id="evacBanner"' in html
    assert "/assets/kitchen.js" in html
    script = (STATIC_DIR / "kitchen.js").read_text(encoding="utf-8")
    assert "window.KitchenScene" in script
    assert "evacuate" in script


def _reading(node_id: str, sequence: int, observed_at: datetime) -> SensorReading:
    return SensorReading(
        node_id=node_id, observed_at=observed_at, sequence=sequence, sensors=[1500.0] * 16
    )


def test_pinned_node_ignores_other_nodes() -> None:
    state = KitchenState("esp32-edge-01")
    now = time.time()
    observed = datetime.fromtimestamp(now, UTC)
    state.record("sensor", _reading("virtual-gas-01", 1, observed), "t", 0, 1, now)
    assert state.snapshot()["latest"]["sensor"] is None
    state.record("sensor", _reading("esp32-edge-01", 1, observed), "t", 0, 2, now)
    snapshot = state.snapshot()
    assert snapshot["latest"]["sensor"]["node_id"] == "esp32-edge-01"
    assert snapshot["node_id"] == "esp32-edge-01" and snapshot["node_pinned"] is True


def test_backfilled_node_gives_way_to_a_live_one() -> None:
    """Old telemetry reloaded at start must not keep the screen on a node that went quiet."""
    state = KitchenState()
    now = time.time()
    old = datetime.fromtimestamp(now - 300, UTC)
    for sequence in range(3):
        state.record("sensor", _reading("virtual-gas-01", sequence, old), "t", 0, sequence, now)
    assert state.snapshot()["node_id"] == "virtual-gas-01"
    # A backfill burst is history, not a message rate.
    assert state.snapshot()["stream"]["sensor"]["rate"] == 0

    live = datetime.fromtimestamp(now, UTC)
    state.record("sensor", _reading("esp32-edge-01", 0, live), "t", 1, 0, now)
    snapshot = state.snapshot()
    assert snapshot["node_id"] == "esp32-edge-01"
    # The new node starts from a clean screen: no rows of the previous recording.
    assert [row[1] for row in state.history_snapshot()["sensor"]] == [0]


def test_backfill_starts_ten_minutes_back_on_telemetry_topics() -> None:
    asked = []

    class FakeConsumer:
        def offsets_for_times(self, partitions, timeout):
            asked.extend(partitions)
            return [
                TopicPartition(tp.topic, tp.partition, 40 if tp.partition == 0 else -1)
                for tp in partitions
            ]

    partitions = [
        TopicPartition(str(KafkaTopic.SENSOR), 0),
        TopicPartition(str(KafkaTopic.SENSOR), 1),
        TopicPartition(str(KafkaTopic.ACTUATOR_STATE), 0),
    ]
    starts = backfill_offsets(FakeConsumer(), partitions, since=1000.0)
    assert {tp.topic for tp in asked} == {str(KafkaTopic.SENSOR)}
    assert all(tp.offset == 1_000_000 for tp in asked)  # epoch ms
    assert starts == {(str(KafkaTopic.SENSOR), 0): 40}  # nothing that recent → live edge


def test_config_describes_the_model_that_predicts(monkeypatch) -> None:
    monkeypatch.setenv("EDGE_MODEL", "tabm")
    config = get_config()
    assert config["model"]["name"] == "tabm-int8"
    assert "P(CO)" in config["model"]["alarm"]["warning"]
    assert {"tabm-int8", "ridge-window60"} <= set(config["models"])
    monkeypatch.setenv("EDGE_MODEL", "ridge")
    assert get_config()["model"]["name"] == "ridge-window60"


def test_a_node_switched_to_shows_what_it_sent_meanwhile() -> None:
    """The controller sends one command per state change: an ACK that arrived while the screen
    still followed the previous node must not be lost when it switches."""
    state = KitchenState()
    now = time.time()
    live = datetime.fromtimestamp(now, UTC)
    state.record("sensor", _reading("virtual-gas-01", 1, live), "t", 0, 1, now)
    ack = ActuatorState(
        node_id="esp32-edge-01",
        observed_at=live,
        sequence=0,
        command_id=uuid4(),
        mode="auto",
        source="safety-controller",
        fan_on=True,
        exhaust_on=True,
        gas_valve_closed=False,
        alarm_on=True,
    )
    state.record("actuator_state", ack, "t", 0, 0, now)
    assert state.snapshot()["latest"]["actuator_state"] is None  # still on virtual-gas-01

    state.record("prediction", _prediction(0, live, "esp32-edge-01"), "t", 0, 0, now)

    later = now + 6  # virtual-gas-01 went quiet (simulator stopped)
    state.record("sensor", _reading("esp32-edge-01", 1, live), "t", 0, 2, later)
    latest = state.snapshot()["latest"]
    assert state.snapshot()["node_id"] == "esp32-edge-01"
    assert latest["actuator_state"]["node_id"] == "esp32-edge-01"
    assert latest["actuator_state"]["fan_on"] is True
    # Its first prediction, sent before the switch, is on screen too.
    assert latest["prediction"]["sequence"] == 0
    assert [row[1] for row in state.history_snapshot()["prediction"]] == [0]


def test_meter_labels_and_marks_sit_where_the_bar_draws_their_value() -> None:
    """Linear meters: the bar, the truth marker, the threshold marks and the scale labels all sit
    at value / max, so distance on the bar is proportional to ppm (3 → 20 is wider than 0 → 3)."""
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    const = {
        name: float(value) for name, value in re.findall(r"const (CO_MAX|ETH_MAX) = ([\d.]+);", app)
    }
    assert "const METER_MAX = { co: CO_MAX, ethylene: ETH_MAX };" in app
    tops = {"co": const["CO_MAX"], "eth": const["ETH_MAX"]}
    for gas, top in tops.items():
        block = html.split(f'class="gas-reading {gas}"', 1)[1]
        block = block.split("</div>\n          </div>", 1)[0]
        labels = re.findall(r'<span style="left:([\d.]+)%">([\d.]+)', block)
        assert len(labels) >= 3
        for left, value in labels:
            assert abs(float(left) - min(100.0, float(value) / top * 100)) < 0.1, (gas, value, left)
        marks = re.findall(
            r'class="mark (?:warning|danger)" style="left:([\d.]+)%" title="([\d.]+) ppm"', block
        )
        assert marks
        for left, value in marks:
            assert abs(float(left) - float(value) / top * 100) < 0.01, (gas, value, left)

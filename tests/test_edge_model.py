from datetime import UTC, datetime
from types import SimpleNamespace

from aiot_pipeline.contracts import Prediction, SensorReading
from aiot_pipeline.edge_inference import EdgeInference, EdgeInferenceService, node_list
from aiot_pipeline.edge_model import (
    FEATURES_PER_CHANNEL,
    AlarmFilter,
    NodeInference,
    RidgeModel,
    extract_features,
    feature_names,
)
from aiot_pipeline.simulator import SensorModel

MODEL = RidgeModel.load()


def test_window_gives_144_named_features() -> None:
    window = [[1000.0 + channel for channel in range(16)] for _ in range(60)]
    assert len(extract_features(window)) == 16 * len(FEATURES_PER_CHANNEL) == 144
    assert feature_names()[:2] == ["S1.last", "S1.mean"]
    assert len(MODEL.coef["co"]) == len(MODEL.mean) == 144


def test_model_reads_gas_from_the_16_channels() -> None:
    sensors, node = SensorModel(seed=4), NodeInference(MODEL)
    for _ in range(90):
        clean, _, _ = node.step(sensors.step(0.0, 0.0))
    assert clean.co_ppm < 15 and clean.ethylene_ppm < 1 and clean.top_channels == []
    for _ in range(60):
        co, level, _ = node.step(sensors.step(300.0, 0.0))
    assert 200 < co.co_ppm < 420 and level == "danger"
    # CO is explained by the CO-sensitive TGS2600/TGS2620 channels.
    assert {"S3", "S4", "S7", "S8", "S11", "S12", "S15", "S16"} & set(co.top_channels)
    for _ in range(60):
        eth, _, _ = node.step(sensors.step(0.0, 12.0))
    assert 7 < eth.ethylene_ppm < 17
    assert "S2" in eth.top_channels  # the most ethylene-sensitive TGS2602


def test_committed_model_meets_its_reported_accuracy() -> None:
    assert MODEL.metrics["rmse_co_ppm"] < 20
    assert MODEL.metrics["alarm_agreement"] > 0.9


def test_alarm_filter_uses_five_second_average_and_hysteresis() -> None:
    alarm = AlarmFilter()
    assert [alarm.update(60, 0) for _ in range(5)][-1] == "warning"
    assert [alarm.update(45, 0) for _ in range(5)][-1] == "warning"  # latched above 40
    assert [alarm.update(30, 0) for _ in range(5)][-1] == "normal"


def test_inference_produces_prediction_and_log_record(tmp_path) -> None:
    engine = EdgeInference(MODEL, log_path=tmp_path / "predictions.jsonl", backend="ridge")
    reading = SensorReading(
        node_id="n1", observed_at=datetime.now(UTC), sequence=42, sensors=[1500.0] * 16
    )
    prediction, record = engine.infer(reading)
    assert prediction.sequence == 42 and prediction.observed_at == reading.observed_at
    assert prediction.model_name == MODEL.name and prediction.window_s == 1
    assert record["input"] == [1500.0] * 16 and record["sequence"] == 42
    for handler in engine.log.handlers:
        handler.flush()
    logged = (tmp_path / "predictions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert '"sequence":42' in logged[-1]


def test_nodes_with_on_device_inference_are_skipped(monkeypatch) -> None:
    assert node_list(" virtual-gas-01, ,n2 ") == {"virtual-gas-01", "n2"}
    monkeypatch.setenv("PREDICTION_LOG", "")
    monkeypatch.setenv("EDGE_SKIP_NODES", "virtual-gas-01")
    # The skip rule is backend-independent: keep the fast pure-Python model here.
    monkeypatch.setenv("EDGE_MODEL", "ridge")
    service = EdgeInferenceService()
    published = []
    client = SimpleNamespace(publish=lambda topic, payload, qos: published.append(topic))
    for node in ("virtual-gas-01", "n1"):
        reading = SensorReading(
            node_id=node, observed_at=datetime.now(UTC), sequence=1, sensors=[1500.0] * 16
        )
        message = SimpleNamespace(
            topic=f"aiot/v1/nodes/{node}/readings", payload=reading.model_dump_json().encode()
        )
        service._on_message(client, None, message)
    assert published == ["aiot/v1/nodes/n1/predictions"]


def test_gateway_never_receives_its_own_predictions(monkeypatch) -> None:
    """With tens of nodes our echo can trail more than 64 newer predictions and the gateway would
    pause a node it serves (seen at 40 nodes in scripts/bench/load_test.py): MQTT 5 No Local."""
    monkeypatch.setenv("PREDICTION_LOG", "")
    monkeypatch.setenv("EDGE_MODEL", "ridge")
    service = EdgeInferenceService()
    subscriptions: dict[str, object] = {}
    client = SimpleNamespace(
        subscribe=lambda topic, qos=0, options=None: subscriptions.update({topic: options})
    )
    service._on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
    assert subscriptions["aiot/v1/nodes/+/predictions"].noLocal


def test_gateway_hands_the_node_over_while_the_device_predicts(monkeypatch) -> None:
    """Mode A and mode B switch by themselves: no environment change while a demo runs."""
    monkeypatch.setenv("PREDICTION_LOG", "")
    monkeypatch.delenv("EDGE_SKIP_NODES", raising=False)
    # One reading must produce one prediction here, so use the model that predicts every reading.
    monkeypatch.setenv("EDGE_MODEL", "ridge")
    service = EdgeInferenceService()
    published: list[tuple[str, bytes]] = []
    client = SimpleNamespace(
        publish=lambda topic, payload, qos: published.append((topic, payload.encode()))
    )
    reading = SensorReading(
        node_id="n1", observed_at=datetime.now(UTC), sequence=1, sensors=[1500.0] * 16
    )
    message = SimpleNamespace(
        topic="aiot/v1/nodes/n1/readings", payload=reading.model_dump_json().encode()
    )

    # Nothing else predicts for n1: the gateway serves it.
    service._on_message(client, None, message)
    assert [topic for topic, _ in published] == ["aiot/v1/nodes/n1/predictions"]

    # The broker echoes our own prediction back: that is not a device running its own model.
    own = published[0][1]
    service._on_message(
        client, None, SimpleNamespace(topic="aiot/v1/nodes/n1/predictions", payload=own)
    )
    assert service.on_device == {}

    # Now the board publishes for the same node: the gateway pauses (but keeps the window warm).
    device = Prediction(
        node_id="n1",
        observed_at=datetime.now(UTC),
        sequence=2,
        co_ppm=10.0,
        ethylene_ppm=1.0,
        model_name="tabm-int8",
        model_version="seed42-k4",
        alarm_level="normal",
    )
    service._on_message(
        client,
        None,
        SimpleNamespace(
            topic="aiot/v1/nodes/n1/predictions", payload=device.model_dump_json().encode()
        ),
    )
    assert service._device_predicts("n1")
    published.clear()
    service._on_message(client, None, message)
    assert published == []

    # The device goes quiet: after the timeout the gateway takes over again.
    service.on_device["n1"] -= service.on_device_timeout + 1
    service._on_message(client, None, message)
    assert [topic for topic, _ in published] == ["aiot/v1/nodes/n1/predictions"]

"""The gateway must be able to run the same INT8 TabM the ESP32 firmware embeds."""

import json
import struct
from pathlib import Path

import numpy as np

from aiot_pipeline.edge_inference import EdgeInference
from aiot_pipeline.tabm_model import MAX_MEMBERS, NodeTabMInference, TabMEngine, pad_window

ROOT = Path(__file__).resolve().parents[1]
DEMO_CSV = ROOT / "demo_data" / "demo_1hz.csv"
GOLDEN = ROOT / "wokwi" / "esp32-replay" / "esp32-replay" / "host_test" / "golden.bin"


def sensor_rows(seconds: int = 70) -> list[list[float]]:
    lines = DEMO_CSV.read_text(encoding="utf-8").strip().splitlines()[1 : seconds + 1]
    return [[float(value) for value in line.split(",")[3:19]] for line in lines]


def test_descriptor_matches_the_blob() -> None:
    engine = TabMEngine()
    assert engine.members == MAX_MEMBERS
    assert engine.blob.size == engine.descriptor["blob_size"]
    assert engine.name == "tabm-int8"


def test_gateway_predicts_with_the_int8_tabm() -> None:
    """Real readings give tabm-int8 predictions from the first reading, then one per 5 s."""
    engine = EdgeInference(backend="tabm")
    predictions = []
    for sequence, sensors in enumerate(sensor_rows()):
        prediction, _ = engine.infer(_reading(sequence, sensors))
        if prediction is not None:
            predictions.append(prediction)

    assert predictions, "the TabM never produced a prediction"
    first = predictions[0]
    # No one-minute warm-up: the first reading is predicted on a padded window.
    assert first.sequence == 0 and first.window_s == 1
    assert first.model_name == "tabm-int8"
    assert first.model_version == "seed42"
    assert first.co_ppm >= 0 and first.ethylene_ppm >= 0
    assert [p.sequence for p in predictions[1:]] == list(range(5, 70, 5))
    assert predictions[-1].window_s == 60
    payload = json.loads(first.model_dump_json())
    assert payload["model_name"] == "tabm-int8" and payload["schema_version"] == 1


def test_short_window_is_padded_with_its_oldest_reading() -> None:
    rows = [np.full(16, value, dtype=np.float32) for value in (1.0, 2.0, 3.0)]
    window = pad_window(rows, 60)
    assert window.shape == (60, 16)
    assert (window[:58] == 1.0).all() and window[58, 0] == 2.0 and window[59, 0] == 3.0
    full = [np.full(16, value, dtype=np.float32) for value in range(60)]
    assert (pad_window(full, 60) == np.stack(full)).all()


def test_cold_start_agrees_with_the_full_window_in_clean_air() -> None:
    """Padding only stands in for the missing history: clean air stays normal from second 1."""
    engine = TabMEngine()
    node = NodeTabMInference(engine, infer_every_s=1)
    levels = [node.step(sensors, sequence)[1] for sequence, sensors in enumerate(sensor_rows(60))]
    assert set(levels) == {"normal"}


def test_kernels_match_onnxruntime_on_the_golden_windows() -> None:
    """The float32-BLAS matmul must stay exact: same outputs as onnxruntime (golden.bin)."""
    data = GOLDEN.read_bytes()
    assert data[:8] == b"TABMGLD2"
    count, subset = struct.unpack("<II", data[8:16])
    record = (60 * 16 + 144 + 2 * 10) * 4
    full, reduced = TabMEngine(), TabMEngine(members=subset)
    for k in range(0, count, 8):
        values = np.frombuffer(data[16 + k * record : 16 + (k + 1) * record], dtype="<f4")
        window, z = values[:960].reshape(60, 16), values[960:1104]
        raw, raw_subset = values[1104:1114], values[1114:1124]
        np.testing.assert_allclose(full.features(window), z, atol=1e-5)
        np.testing.assert_allclose(full._forward(z), raw, atol=1e-2)
        # The reduced outputs come from the export's own integer kernels: bit for bit.
        np.testing.assert_array_equal(reduced._forward(z), raw_subset)


def _reading(sequence: int, sensors: list[float]):
    from datetime import UTC, datetime

    from aiot_pipeline.contracts import SensorReading

    return SensorReading(
        node_id="n1",
        observed_at=datetime.now(UTC),
        sequence=sequence,
        sensors=sensors,
    )


def test_a_restarted_node_starts_over_without_warm_up() -> None:
    """The replay restarts at sequence 0 (simulator or board reboot): fresh window and alarm."""
    node = NodeTabMInference(TabMEngine(members=4), infer_every_s=5)
    rows = sensor_rows(12)
    for sequence, sensors in enumerate(rows):
        node.step(sensors, 100 + sequence)
    assert len(node.window) == 12
    estimate, level, window = node.step(rows[0], 0)
    assert estimate is not None and window == 1 and level == "normal"

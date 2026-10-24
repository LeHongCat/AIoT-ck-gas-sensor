"""Train the edge Ridge model: 16-channel MOX window → CO / C2H4 ppm.

    uv run python tools/train_edge_model.py                      # synthetic, notebook-calibrated
    uv run python tools/train_edge_model.py --dataset data/ethylene_CO.txt   # UCI 322 recording

Features come from aiot_pipeline.edge_model.extract_features, the exact function the edge
service runs, so training and inference cannot drift apart. The result is written to
src/aiot_pipeline/models/edge_ridge_v1.json together with its test metrics.
"""

import argparse
import random
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiot_pipeline.edge_model import (  # noqa: E402
    MODEL_PATH,
    WINDOW_S,
    AlarmFilter,
    RidgeModel,
    classify_alarm,
    compress,
    expand,
    extract_features,
)
from aiot_pipeline.simulator import (  # noqa: E402
    KitchenAir,
    SensorModel,
    dataset_rows,
    ventilation_rate,
)

ALPHAS = (0.1, 1.0, 10.0, 100.0, 1000.0)


def synthetic_recording(seconds: int, seed: int) -> tuple[list[list[float]], np.ndarray]:
    """Leaks of random strength, ventilation and valve isolation, through the MOX model."""
    rng = random.Random(seed)
    sensors = SensorModel(seed=seed)
    co = ethylene = 0.0
    readings: list[list[float]] = []
    truth: list[tuple[float, float]] = []

    def run(duration: int, source_co: float, source_eth: float, fan: bool, exhaust: bool) -> None:
        nonlocal co, ethylene
        vent = ventilation_rate(fan, exhaust)
        for _ in range(duration):
            co = KitchenAir._approach(co, source_co, vent, 1.0)
            ethylene = KitchenAir._approach(ethylene, source_eth, vent, 1.0)
            readings.append(sensors.step(co, ethylene))
            truth.append((co, ethylene))

    while len(readings) < seconds:
        run(rng.randint(30, 240), 0.0, 0.0, rng.random() < 0.3, rng.random() < 0.3)
        kind = rng.random()
        source_co = rng.uniform(60, 600) if kind < 0.65 else 0.0
        source_eth = rng.uniform(4, 22) if kind > 0.4 else 0.0
        duration = rng.randint(40, 240)
        fan, exhaust = rng.random() < 0.6, rng.random() < 0.6
        if rng.random() < 0.4:  # the valve isolates the leak part-way through
            cut = rng.randint(10, duration)
            run(cut, source_co, source_eth, fan, exhaust)
            run(duration - cut, 0.0, 0.0, True, True)
        else:
            run(duration, source_co, source_eth, fan, exhaust)
    return readings[:seconds], np.array(truth[:seconds])


def dataset_recording(path: Path) -> tuple[list[list[float]], np.ndarray]:
    readings, truth = [], []
    for sensors, co, ethylene in dataset_rows(path):
        readings.append(sensors)
        truth.append((co, ethylene))
    return readings, np.array(truth)


def featurise(readings: list[list[float]]) -> np.ndarray:
    window: list[list[float]] = []
    rows = []
    for reading in readings:
        window.append(reading)
        if len(window) > WINDOW_S:
            window.pop(0)
        rows.append(extract_features(window))
    return np.array(rows)


def fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float, mean, std):
    z = (x - mean) / std
    gram = z.T @ z + alpha * np.eye(z.shape[1])
    coef = np.linalg.solve(gram, z.T @ (y - y.mean(axis=0)))
    return coef, y.mean(axis=0)


def predict_ppm(x, mean, std, coef, intercept) -> np.ndarray:
    compressed = ((x - mean) / std) @ coef + intercept
    return np.column_stack(
        [
            [expand(v, "co") for v in compressed[:, 0]],
            [expand(v, "ethylene") for v in compressed[:, 1]],
        ]
    )


def alarm_agreement(truth: np.ndarray, predicted: np.ndarray) -> float:
    true_filter, pred_filter = AlarmFilter(), AlarmFilter()
    hits = 0
    for (co, eth), (co_hat, eth_hat) in zip(truth, predicted, strict=True):
        hits += true_filter.update(co, eth) == pred_filter.update(co_hat, eth_hat)
    return hits / len(truth)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, help="UCI 322 ethylene_CO.txt (optional)")
    parser.add_argument("--hours", type=float, default=4.0, help="synthetic training length")
    parser.add_argument("--out", type=Path, default=MODEL_PATH)
    args = parser.parse_args()

    started = time.time()
    if args.dataset:
        readings, truth = dataset_recording(args.dataset)
        # Notebook split: train 0-60 %, test 60-80 %, last 20 % kept for the demo.
        n = len(readings)
        train_end, test_end = int(n * 0.6), int(n * 0.8)
        x_all = featurise(readings[:test_end])
        x_train, y_train = x_all[:train_end], truth[:train_end]
        x_test, y_test = x_all[train_end:test_end], truth[train_end:test_end]
        trained_on = f"UCI 322 {args.dataset.name} (train 0-60 %, test 60-80 %)"
    else:
        train_readings, y_train = synthetic_recording(int(args.hours * 3600), seed=7)
        test_readings, y_test = synthetic_recording(5400, seed=99)
        x_train, x_test = featurise(train_readings), featurise(test_readings)
        trained_on = (
            f"synthetic {args.hours:g} h · MOX model calibrated to UCI 322 "
            "(random leaks, ventilation, valve isolation); test 1.5 h unseen seed"
        )
    print(f"features: train {x_train.shape}, test {x_test.shape} ({time.time() - started:.0f}s)")

    mean, std = x_train.mean(axis=0), x_train.std(axis=0) + 1e-6
    target = np.column_stack(
        [
            [compress(v, "co") for v in y_train[:, 0]],
            [compress(v, "ethylene") for v in y_train[:, 1]],
        ]
    )
    # Pick alpha on the last 20 % of the training span (time-ordered validation).
    split = int(len(x_train) * 0.8)
    best = None
    for alpha in ALPHAS:
        coef, intercept = fit_ridge(x_train[:split], target[:split], alpha, mean, std)
        val = predict_ppm(x_train[split:], mean, std, coef, intercept)
        rmse = float(np.sqrt(np.mean((val[:, 0] - y_train[split:, 0]) ** 2)))
        print(f"alpha={alpha:<7g} validation RMSE CO {rmse:6.2f} ppm")
        if best is None or rmse < best[1]:
            best = (alpha, rmse)
    alpha = best[0]
    coef, intercept = fit_ridge(x_train, target, alpha, mean, std)

    clean = (y_train[:, 0] < 1.0) & (y_train[:, 1] < 0.1)
    predicted = predict_ppm(x_test, mean, std, coef, intercept)
    error = predicted - y_test
    metrics = {
        "rmse_co_ppm": round(float(np.sqrt(np.mean(error[:, 0] ** 2))), 2),
        "mae_co_ppm": round(float(np.mean(np.abs(error[:, 0]))), 2),
        "rmse_ethylene_ppm": round(float(np.sqrt(np.mean(error[:, 1] ** 2))), 3),
        "mae_ethylene_ppm": round(float(np.mean(np.abs(error[:, 1]))), 3),
        "alarm_agreement": round(alarm_agreement(y_test, predicted), 4),
        "test_samples": len(y_test),
        "train_samples": len(y_train),
    }
    model = RidgeModel(
        name="ridge-window60",
        version="v1",
        trained_on=trained_on,
        mean=[round(float(v), 6) for v in mean],
        std=[round(float(v), 6) for v in std],
        coef={
            "co": [round(float(v), 8) for v in coef[:, 0]],
            "ethylene": [round(float(v), 8) for v in coef[:, 1]],
        },
        intercept={"co": round(float(intercept[0]), 8), "ethylene": round(float(intercept[1]), 8)},
        alpha=alpha,
        metrics=metrics,
        reference=[round(float(v), 6) for v in x_train[clean].mean(axis=0)],
    )
    model.save(args.out)
    print(f"alpha={alpha} test metrics: {metrics}")
    print(f"wrote {args.out} in {time.time() - started:.0f}s")
    # Sanity: a classify_alarm on clean air must be normal.
    assert classify_alarm(0.0, 0.0, False) == "normal"


if __name__ == "__main__":
    main()

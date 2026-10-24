"""Reference pipeline for the TabM INT8 edge model (the C++ port on the ESP32 must match it).

    16 sensors (1 Hz) ─► (x - x_mean) / x_std ─► last 60 s ─► 144 features ─► standardise
    ─► TabM INT8 (ONNX) ─► 10 raw outputs ─► P(present), ppm, DER uncertainty ─► alarm level

Everything comes from the benchmark notebook (aiot_gas_tinyml_benchmark_v8_new_ipynb.ipynb):
`window_features()`, `postprocess()`, `preprocessing.json` and `thresholds.json`. Alarm policy
(the notebook's CO alarm): WARNING when the 5 s causal mean of P(CO) reaches tau; DANGER when the
5 s mean of the CO estimate reaches 200 ppm. C2H4 is estimated and reported, never alarmed.

Needs numpy + onnxruntime (not part of the pipeline image), e.g. the `aiot` conda env:

    python tools/tabm_reference.py --csv demo_data/demo_1hz.csv
"""

import argparse
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
# Copied from the benchmark output (tinyml_benchmark_v8_new): the ONNX model,
# preprocessing.json and thresholds.json.
MODEL_DIR = ROOT / "model"
MODEL_KEY = "TabM_seed42"
ONNX_NAME = f"{MODEL_KEY}_int8_static.onnx"

WINDOW_S = 60
DELTA_STEPS = (5, 15, 30)  # d5, d15, d30 = last - value k steps earlier
FEATURE_KINDS = ("last", "mean", "std", "min", "max", "slope", "d5", "d15", "d30")
N_GAS = 2  # CO, Ethylene
GAMMA_MAX = 1.5  # ppm head is clipped to 1.5 x the largest train set-point
ALARM_SMOOTH_S = 5
CO_DANGER_PPM = 200.0


@dataclass(frozen=True)
class Bundle:
    x_mean: np.ndarray  # (16,)
    x_std: np.ndarray
    feature_mean: np.ndarray  # (144,)
    feature_std: np.ndarray
    y_max: np.ndarray  # (2,)
    tau: np.ndarray  # (2,) presence thresholds
    uct_cut: np.ndarray  # (2,) "high uncertainty" cut in ppm
    onnx_path: Path


def load_bundle(model_dir: Path = MODEL_DIR) -> Bundle:
    prep = json.loads((model_dir / "preprocessing.json").read_text(encoding="utf-8"))
    thresholds = json.loads((model_dir / "thresholds.json").read_text(encoding="utf-8"))[MODEL_KEY]
    assert prep["window_s"] == WINDOW_S and prep["resample_hz"] == 1
    assert prep["signal_transform"] == "none" and prep["sensor_grouping"] == "none"
    expected = [f"{kind}_{c}" for kind in FEATURE_KINDS for c in prep["sensor_cols"]]
    assert prep["feature_names"] == expected, "feature layout differs from the notebook"
    f32 = lambda key: np.asarray(prep[key], np.float32)  # noqa: E731
    return Bundle(
        x_mean=f32("x_mean"),
        x_std=f32("x_std"),
        feature_mean=f32("feature_mean"),
        feature_std=f32("feature_std"),
        y_max=f32("y_max"),
        tau=np.asarray(thresholds["tau"], np.float64),
        uct_cut=np.asarray(thresholds["uct_cut_ppm"], np.float64),
        onnx_path=model_dir / ONNX_NAME,
    )


def window_features(windows: np.ndarray) -> np.ndarray:
    """(m, 60, 16) normalised windows -> (m, 144), kind-major like the notebook."""
    last = windows[:, -1]
    t = np.arange(windows.shape[1], dtype=np.float32)
    t -= t.mean()
    slope = np.einsum("mwc,w->mc", windows, t) / float((t**2).sum())
    feats = [last, windows.mean(1), windows.std(1), windows.min(1), windows.max(1), slope]
    feats += [last - windows[:, -1 - k] for k in DELTA_STEPS]
    return np.concatenate(feats, 1).astype(np.float32)


def model_inputs(sensors: np.ndarray, bundle: Bundle) -> tuple[np.ndarray, np.ndarray]:
    """Raw (N, 16) readings -> (end index of each full window, standardised (m, 144) features)."""
    xn = ((sensors - bundle.x_mean) / bundle.x_std).astype(np.float32)
    windows = np.lib.stride_tricks.sliding_window_view(xn, (WINDOW_S, xn.shape[1]))[:, 0]
    feats = window_features(np.ascontiguousarray(windows))
    z = ((feats - bundle.feature_mean) / bundle.feature_std).astype(np.float32)
    return np.arange(WINDOW_S - 1, len(sensors)), z


def softplus(v: np.ndarray) -> np.ndarray:
    return np.logaddexp(0.0, v)


def decode(raw: np.ndarray, bundle: Bundle) -> dict[str, np.ndarray]:
    """(m, 10) raw outputs, layout (5, n_gas): logit, gamma, nu, alpha, beta."""
    o = raw.astype(np.float64).reshape(len(raw), 5, N_GAS)
    logit, gamma = o[:, 0], o[:, 1]
    nu = softplus(o[:, 2]) + 1e-6
    alpha = softplus(o[:, 3]) + 1.0
    beta = softplus(o[:, 4]) + 1e-6
    prob = 1.0 / (1.0 + np.exp(-logit))
    ppm_cond = np.clip(gamma, 0.0, GAMMA_MAX) * bundle.y_max
    present = prob >= bundle.tau
    scale = np.sqrt(beta * (1 + nu) / (alpha * nu))
    return {
        "prob": prob,
        "present": present,
        "ppm": np.where(present, ppm_cond, 0.0),  # hurdle: 0 when predicted absent
        "uct_ppm": (scale + 1.0 / np.sqrt(nu)) * bundle.y_max,
    }


class AlarmFilter:
    """5-sample causal means of P(CO) and the CO estimate -> normal / warning / danger."""

    def __init__(self, tau: np.ndarray) -> None:
        self.tau = tau
        self.prob: deque[np.ndarray] = deque(maxlen=ALARM_SMOOTH_S)
        self.co_ppm: deque[float] = deque(maxlen=ALARM_SMOOTH_S)

    def update(self, prob: np.ndarray, co_ppm: float) -> str:
        self.prob.append(prob)
        self.co_ppm.append(co_ppm)
        if sum(self.co_ppm) / len(self.co_ppm) >= CO_DANGER_PPM:
            return "danger"
        if np.mean(self.prob, axis=0)[0] >= self.tau[0]:
            return "warning"
        return "normal"


def run_onnx(z: np.ndarray, onnx_path: Path) -> np.ndarray:
    import onnxruntime as ort

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    return np.concatenate(
        [session.run(None, {"x": z[i : i + 1024]})[0] for i in range(0, len(z), 1024)]
    )


def load_csv(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """demo_1hz.csv: t_s, CO, Ethylene, S1..S16 (one row per second)."""
    data = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.float64)
    return data[:, 0], data[:, 1:3].astype(np.float32), data[:, 3:19].astype(np.float32)


def f1(truth: np.ndarray, hat: np.ndarray) -> float:
    tp = float(np.sum(truth & hat))
    return 2 * tp / max(float(np.sum(truth) + np.sum(hat)), 1.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--csv", type=Path, default=ROOT / "demo_data" / "demo_1hz.csv")
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "tabm_reference.csv")
    args = parser.parse_args()

    bundle = load_bundle(args.model_dir)
    t_s, gas, sensors = load_csv(args.csv)
    end_idx, z = model_inputs(sensors, bundle)
    raw = run_onnx(z, bundle.onnx_path)
    out = decode(raw, bundle)
    alarm = AlarmFilter(bundle.tau)
    levels = [alarm.update(out["prob"][i], out["ppm"][i, 0]) for i in range(len(raw))]

    truth = gas[end_idx]
    present = truth >= np.array([50.0, 1.0])  # PRESENT_PPM of the notebook
    print(f"{len(end_idx)} windows from {args.csv.name} ({t_s[0]:.0f}–{t_s[-1]:.0f} s)")
    for j, name in enumerate(("CO", "C2H4")):
        rmse = float(np.sqrt(np.mean((out["ppm"][:, j] - truth[:, j]) ** 2)))
        print(f"  {name:5s} F1 {f1(present[:, j], out['present'][:, j]):.3f} | RMSE {rmse:.2f} ppm")
    counts = {level: levels.count(level) for level in ("normal", "warning", "danger")}
    print("  alarm levels:", counts)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    header = "t_s,co_true,eth_true,p_co,p_eth,co_ppm,eth_ppm,uct_co,uct_eth,alarm"
    rows = [
        f"{t_s[i]:.0f},{truth[k, 0]:.2f},{truth[k, 1]:.2f},{out['prob'][k, 0]:.4f},"
        f"{out['prob'][k, 1]:.4f},{out['ppm'][k, 0]:.2f},{out['ppm'][k, 1]:.3f},"
        f"{out['uct_ppm'][k, 0]:.1f},{out['uct_ppm'][k, 1]:.2f},{levels[k]}"
        for k, i in enumerate(end_idx)
    ]
    args.out.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    print("Wrote", args.out)


if __name__ == "__main__":
    main()

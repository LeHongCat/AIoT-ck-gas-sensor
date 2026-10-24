"""Model quality and consistency of the TabM INT8 edge model (Q01-Q13, D02-D04, D08 host).

Evaluation data: demo_data/demo_1hz.csv, the LAST 20 % of the UCI 322 recording (t = 33 669-42 087 s,
8 419 s at 1 Hz). It is not the notebook's 60-80 % test block; every figure is "on the last 20 %".

Configurations (same preprocessing, window, decode and thresholds):

* INT8-32 ONNX  - the exported QDQ model run by onnxruntime (reference);
* INT8-32 int   - the integer kernels of aiot_pipeline.tabm_model.TabMEngine (gateway and firmware);
* INT8-4 int    - the same kernels with members 0-3 only (the ESP32 configuration).

FP32 (M1 / M4) is not available: the repository only holds the INT8 model, so D01 is not measured.

    uv run --no-project --with numpy --with onnxruntime --with scipy python scripts/bench/model_quality.py
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # as in the container
os.environ.setdefault("OMP_NUM_THREADS", "1")

import argparse  # noqa: E402
import csv  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402
from scipy import stats  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import tabm_reference as ref  # noqa: E402

from aiot_pipeline.tabm_model import Estimate, TabMAlarmFilter, TabMEngine, pad_window  # noqa: E402

PRESENT_PPM = np.array([50.0, 1.0])  # notebook PRESENT_PPM: ground-truth presence for F1
GASES = ("CO", "C2H4")
STATES = ("không khí", "chỉ CO", "chỉ C2H4", "hỗn hợp")


# ---- decoding with the DER parameters --------------------------------------------------------


def decode_full(raw: np.ndarray, tau: np.ndarray, y_max: np.ndarray, gamma_max: float) -> dict:
    o = raw.astype(np.float64).reshape(len(raw), 5, 2)
    logit, gamma = o[:, 0], o[:, 1]
    nu = np.logaddexp(0.0, o[:, 2]) + 1e-6
    alpha = np.logaddexp(0.0, o[:, 3]) + 1.0
    beta = np.logaddexp(0.0, o[:, 4]) + 1e-6
    prob = 1.0 / (1.0 + np.exp(-logit))
    present = prob >= tau
    ppm = np.where(present, np.clip(gamma, 0.0, gamma_max) * y_max, 0.0)
    # Deep evidential regression: the predictive distribution of the (normalised) concentration
    # is Student-t with 2*alpha degrees of freedom, location gamma, scale^2 = beta(1+nu)/(nu*alpha).
    scale = np.sqrt(beta * (1 + nu) / (alpha * nu))
    q = stats.t.ppf(0.95, 2 * alpha)
    lo = np.maximum(0.0, (gamma - q * scale) * y_max)
    hi = np.maximum(0.0, (gamma + q * scale) * y_max)
    return {"prob": prob, "present": present, "ppm": ppm, "gamma": gamma, "lo90": lo, "hi90": hi}


# ---- metrics --------------------------------------------------------------------------------


def prf(truth: np.ndarray, hat: np.ndarray) -> dict:
    tp = int(np.sum(truth & hat))
    fp = int(np.sum(~truth & hat))
    fn = int(np.sum(truth & ~hat))
    tn = int(np.sum(~truth & ~hat))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "positives": tp + fn,
        "negatives": tn + fp,
    }


def four_state(present: np.ndarray) -> np.ndarray:
    return present[:, 0].astype(int) + 2 * present[:, 1].astype(int)  # 0 none, 1 CO, 2 C2H4, 3 mix


def quality(truth_ppm: np.ndarray, out: dict) -> dict:
    truth_present = truth_ppm >= PRESENT_PPM
    result = {"windows": len(truth_ppm), "per_gas": {}}
    for j, gas in enumerate(GASES):
        err = out["ppm"][:, j] - truth_ppm[:, j]
        pos = truth_present[:, j]
        inside = (truth_ppm[pos, j] >= out["lo90"][pos, j]) & (
            truth_ppm[pos, j] <= out["hi90"][pos, j]
        )
        result["per_gas"][gas] = {
            **prf(pos, out["present"][:, j]),
            "rmse_ppm": round(float(np.sqrt(np.mean(err**2))), 3),
            "mae_ppm": round(float(np.mean(np.abs(err))), 3),
            "picp90": round(float(np.mean(inside)), 4) if pos.any() else None,
            "interval90_mean_width_ppm": round(
                float(np.mean(out["hi90"][pos, j] - out["lo90"][pos, j])), 3
            )
            if pos.any()
            else None,
            "picp90_windows": int(pos.sum()),
        }
    true_state, hat_state = four_state(truth_present), four_state(out["present"])
    matrix = np.zeros((4, 4), dtype=int)
    for t, h in zip(true_state, hat_state, strict=True):
        matrix[t, h] += 1
    f1s = []
    per_state = {}
    for k, name in enumerate(STATES):
        stat = prf(true_state == k, hat_state == k)
        per_state[name] = {"f1": stat["f1"], "support": int(np.sum(true_state == k))}
        if stat["positives"]:
            f1s.append(stat["f1"])
    result["four_state"] = {
        "confusion_rows_true_cols_pred": matrix.tolist(),
        "states": STATES,
        "per_state": per_state,
        "f1_macro_present_states": round(float(np.mean(f1s)), 4),
        "states_absent_from_data": [STATES[k] for k in range(4) if not np.any(true_state == k)],
    }
    return result


# ---- alarms on the continuous timeline -------------------------------------------------------


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index ranges where mask is True."""
    edges = np.diff(np.concatenate([[0], mask.astype(int), [0]]))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True))


def alarm_timeline(out: dict, engine_cfg, every: int) -> dict:
    """Levels with the gateway/firmware filter at the given inference cadence (held in between)."""
    alarm = TabMAlarmFilter(engine_cfg)
    n = len(out["prob"])
    level = np.empty(n, dtype=object)
    signal = np.zeros(n, dtype=bool)  # Q10: 5 s mean of P(CO) >= tau
    current, current_signal = "normal", False
    window: list[tuple[int, float]] = []
    for s in range(n):
        if s % every == 0:
            p = out["prob"][s]
            estimate = Estimate(
                prob=p, present=p >= engine_cfg.tau, ppm=out["ppm"][s], uct_ppm=np.zeros(2)
            )
            current = alarm.update(estimate, s)
            window = [(q, v) for q, v in window if q + engine_cfg.alarm_smooth_s > s] + [
                (s, float(p[0]))
            ]
            current_signal = sum(v for _, v in window) / len(window) >= engine_cfg.tau[0]
        level[s] = current
        signal[s] = current_signal
    return {"level": level, "signal": signal}


def alarm_metrics(truth_ppm: np.ndarray, timeline: dict, hours: float) -> dict:
    co_present = truth_ppm[:, 0] >= PRESENT_PPM[0]
    episodes = runs(co_present)
    level = timeline["level"]
    on = level != "normal"
    events = runs(on)
    signal_events = runs(timeline["signal"])
    false_signal = [a for a, _ in signal_events if not co_present[a]]
    false_level = [a for a, _ in events if not co_present[a]]
    escalations = sum(
        1 for s in range(1, len(level)) if level[s - 1] == "warning" and level[s] == "danger"
    )
    per_episode = []
    for start, end in episodes:
        seg = level[start:end]
        warn = next((i for i, v in enumerate(seg) if v != "normal"), None)
        peak = float(truth_ppm[start:end, 0].max())
        danger = next((i for i, v in enumerate(seg) if v == "danger"), None)
        per_episode.append(
            {
                "start_index": int(start),
                "duration_s": int(end - start),
                "peak_ppm": round(peak, 2),
                "alarm_already_on_at_start": bool(level[start] != "normal"),
                "detected": warn is not None,
                "delay_warning_s": warn,
                "danger_expected": peak >= 200.0,
                "delay_danger_s": danger,
            }
        )
    delays_w = [e["delay_warning_s"] for e in per_episode if e["delay_warning_s"] is not None]
    delays_d = [
        e["delay_danger_s"]
        for e in per_episode
        if e["danger_expected"] and e["delay_danger_s"] is not None
    ]

    def describe(values):
        if not values:
            return None
        return {
            "n": len(values),
            "min": min(values),
            "median": statistics.median(values),
            "max": max(values),
        }

    return {
        "duration_h": round(hours, 3),
        "co_episodes": len(episodes),
        "episodes_detected": sum(e["detected"] for e in per_episode),
        "episodes_missed": sum(not e["detected"] for e in per_episode),
        "delay_warning_s": describe(delays_w),
        "danger_episodes": sum(e["danger_expected"] for e in per_episode),
        "danger_reached": len(delays_d),
        "delay_danger_s": describe(delays_d),
        "Q10_false_signal_onsets": len(false_signal),
        "Q10_fa_per_h": round(len(false_signal) / hours, 3),
        "Q13_false_level_onsets": len(false_level),
        "Q13_fa_per_h": round(len(false_level) / hours, 3),
        "warning_to_danger_escalations": escalations,
        "alarm_events": len(events),
        "seconds_by_level": {k: int(np.sum(level == k)) for k in ("normal", "warning", "danger")},
        "episodes": per_episode,
    }


# ---- main ------------------------------------------------------------------------------------


def integer_raw(
    engine: TabMEngine, sensors: np.ndarray, end_idx: np.ndarray
) -> tuple[np.ndarray, np.ndarray, list[float]]:
    feats, raws, times = [], [], []
    for i in end_idx:
        window = sensors[i - ref.WINDOW_S + 1 : i + 1]
        started = time.perf_counter()
        f = engine.features(window)
        raw = engine._forward(f)
        times.append(time.perf_counter() - started)
        feats.append(f)
        raws.append(raw)
    return np.stack(feats), np.stack(raws), times


def timing(values: list[float]) -> dict:
    ordered = sorted(values)
    q = statistics.quantiles(ordered, n=100, method="inclusive")
    return {
        "n": len(ordered),
        "p50_ms": round(q[49] * 1000, 3),
        "p95_ms": round(q[94] * 1000, 3),
        "windows_per_s": round(1 / statistics.fmean(ordered), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=ROOT / "demo_data" / "demo_1hz.csv")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs/bench/run/model")
    args = parser.parse_args()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle = ref.load_bundle()
    t_s, gas, sensors = ref.load_csv(args.csv)
    end_idx, z = ref.model_inputs(sensors, bundle)
    truth = gas[end_idx].astype(np.float64)
    hours = len(end_idx) / 3600
    engines = {"INT8-32 int": TabMEngine(members=32), "INT8-4 int": TabMEngine(members=4)}
    cfg = engines["INT8-32 int"]
    y_max, tau, gmax = bundle.y_max.astype(np.float64), bundle.tau, cfg.gamma_max

    raws, feats, times = {}, {}, {}
    started = time.perf_counter()
    raws["INT8-32 ONNX"] = ref.run_onnx(z, bundle.onnx_path)
    onnx_s = time.perf_counter() - started
    for name, engine in engines.items():
        feats[name], raws[name], times[name] = integer_raw(engine, sensors, end_idx)
        print(f"{name}: {len(end_idx)} windows", flush=True)

    outputs = {name: decode_full(raw, tau, y_max, gmax) for name, raw in raws.items()}
    engine_cfg = SimpleNamespace(
        tau=cfg.tau, co_danger_ppm=cfg.co_danger_ppm, alarm_smooth_s=cfg.alarm_smooth_s
    )
    results = {
        "data": {
            "file": "demo_data/demo_1hz.csv",
            "scope": "last 20 % of the UCI 322 recording, not the notebook's 60-80 % test block",
            "t_s_range": [float(t_s[0]), float(t_s[-1])],
            "rows": len(t_s),
            "full_windows": len(end_idx),
            "startup_windows_excluded": ref.WINDOW_S - 1,
        },
        "thresholds": {
            "presence_truth_ppm": PRESENT_PPM.tolist(),
            "presence_tau": tau.tolist(),
            "alarm": f"warning: 5 s mean P(CO) >= {tau[0]:.2f}; danger: 5 s mean CO >= {cfg.co_danger_ppm:g} ppm; C2H4 never alarms",
            "picp": "90 % Student-t interval of the DER output (2 alpha dof), on windows where the gas is present",
        },
        "four_member_subset": "members 0-3 of 32",
        "fp32": "not available (no FP32 checkpoint in the repository): D01 / M1 / M4 not measured",
        "configs": {},
    }
    for name, out in outputs.items():
        entry = {"quality": quality(truth, out), "alarms": {}}
        for every in (1, 5):
            entry["alarms"][f"every_{every}s"] = alarm_metrics(
                truth, alarm_timeline(out, engine_cfg, every), hours
            )
        results["configs"][name] = entry

    a, b = outputs["INT8-32 ONNX"], outputs["INT8-32 int"]
    ref_feats = z
    near = np.abs(a["prob"] - tau) < 0.02
    results["consistency_python"] = {
        "D03_features_ref_vs_int_max_abs": float(np.max(np.abs(feats["INT8-32 int"] - ref_feats))),
        "D03_features_ref_vs_int_mean_abs": float(
            np.mean(np.abs(feats["INT8-32 int"] - ref_feats))
        ),
        "D04_raw_onnx_vs_int_max_abs": float(
            np.max(np.abs(raws["INT8-32 int"] - raws["INT8-32 ONNX"]))
        ),
        "D04_raw_onnx_vs_int_mean_abs": float(
            np.mean(np.abs(raws["INT8-32 int"] - raws["INT8-32 ONNX"]))
        ),
        "D04_prob_max_abs": float(np.max(np.abs(a["prob"] - b["prob"]))),
        "D04_ppm_max_abs": [float(v) for v in np.max(np.abs(a["ppm"] - b["ppm"]), axis=0)],
        "D04_presence_decisions_differ": [
            int(v) for v in np.sum(a["present"] != b["present"], axis=0)
        ],
        "D04_windows_near_tau_pm0.02": [int(v) for v in near.sum(axis=0)],
        "D04_decisions_differ_near_tau": [
            int(v) for v in np.sum((a["present"] != b["present"]) & near, axis=0)
        ],
    }
    # Startup: the gateway predicts at once on a window padded with its oldest reading.
    startup = []
    for i in range(ref.WINDOW_S - 1):
        window = pad_window(list(sensors[: i + 1]), ref.WINDOW_S)
        estimate = cfg.predict(window)
        startup.append(estimate.present.tolist())
    st_truth = gas[: ref.WINDOW_S - 1] >= PRESENT_PPM
    st_hat = np.array(startup)
    results["startup_padded_windows"] = {
        "windows": len(startup),
        "presence_agreement_CO": round(float(np.mean(st_hat[:, 0] == st_truth[:, 0])), 4),
        "presence_agreement_C2H4": round(float(np.mean(st_hat[:, 1] == st_truth[:, 1])), 4),
        "note": "reported apart from the full windows (padded with the oldest reading, as the gateway does)",
    }
    results["D08_host"] = {
        "blas_threads": os.environ.get("OPENBLAS_NUM_THREADS"),
        "scope": "features + integer forward of one window, host CPU, one thread",
        **{name: timing(values) for name, values in times.items()},
        "INT8-32 ONNX batched_windows_per_s": round(len(end_idx) / onnx_s, 1),
    }
    (out_dir / "quality_metrics.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    with (out_dir / "quality_metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "config",
                "gas",
                "precision",
                "recall",
                "f1",
                "positives",
                "negatives",
                "rmse_ppm",
                "mae_ppm",
                "picp90",
                "width90_ppm",
            ]
        )
        for name, entry in results["configs"].items():
            for gas_name, g in entry["quality"]["per_gas"].items():
                writer.writerow(
                    [
                        name,
                        gas_name,
                        g["precision"],
                        g["recall"],
                        g["f1"],
                        g["positives"],
                        g["negatives"],
                        g["rmse_ppm"],
                        g["mae_ppm"],
                        g["picp90"],
                        g["interval90_mean_width_ppm"],
                    ]
                )
    with gzip.open(
        out_dir / "prediction_trace.csv.gz", "wt", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        header = ["t_s", "co_true", "eth_true"]
        for name in outputs:
            key = name.replace(" ", "_")
            header += [
                f"{key}_{f}"
                for f in (
                    "p_co",
                    "p_eth",
                    "co_ppm",
                    "eth_ppm",
                    "co_lo90",
                    "co_hi90",
                    "eth_lo90",
                    "eth_hi90",
                )
            ]
        writer.writerow(header)
        for k, i in enumerate(end_idx):
            row = [f"{t_s[i]:.0f}", f"{truth[k, 0]:.2f}", f"{truth[k, 1]:.2f}"]
            for out in outputs.values():
                row += [
                    f"{out['prob'][k, 0]:.4f}",
                    f"{out['prob'][k, 1]:.4f}",
                    f"{out['ppm'][k, 0]:.2f}",
                    f"{out['ppm'][k, 1]:.3f}",
                    f"{out['lo90'][k, 0]:.1f}",
                    f"{out['hi90'][k, 0]:.1f}",
                    f"{out['lo90'][k, 1]:.2f}",
                    f"{out['hi90'][k, 1]:.2f}",
                ]
            writer.writerow(row)
    for name, entry in results["configs"].items():
        q = entry["quality"]
        print(
            name,
            {g: (v["f1"], v["rmse_ppm"], v["picp90"]) for g, v in q["per_gas"].items()},
            "macroF1",
            q["four_state"]["f1_macro_present_states"],
        )
        for cad, al in entry["alarms"].items():
            print(
                "  ",
                cad,
                {
                    k: al[k]
                    for k in (
                        "co_episodes",
                        "episodes_detected",
                        "delay_warning_s",
                        "delay_danger_s",
                        "Q10_fa_per_h",
                        "Q13_fa_per_h",
                    )
                },
            )
    print(json.dumps(results["consistency_python"]), json.dumps(results["D08_host"]))


if __name__ == "__main__":
    main()

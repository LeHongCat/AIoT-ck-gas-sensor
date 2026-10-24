"""TabM INT8 engine for the gateway: the same kernels and the same post-processing as the ESP32.

`src/tabm_int8.cpp` on the board and this module on the gateway consume the very same
`model/tabm_int8.bin` with the layout in `model/tabm_int8.json` (both written by
`tools/export_tabm_esp32.py`), so the two deployment modes differ only in where the inference runs:

    window 60 s ─► 144 features ─► INT8 forward (4 blocks, 32 members) ─► decode ─► alarm filter

Everything is integer arithmetic in the QDQ domain; numpy is only used to run the same sums faster
than Python loops. The int8 x int8 dot products go through float32 BLAS: every product is at most
128 * 128 = 2^14 and every partial sum over 1024 inputs at most 2^24, so float32 holds them exactly
and the result is bit-identical to an int32 accumulator, about 10x faster (~8 ms for 32 members).
`tools/export_tabm_esp32.py` checks these kernels against onnxruntime before it writes the blob,
and `host_test/host_test.cpp` checks the C++ against the same golden windows.

The model reads a 60 s window. Until a node has sent 60 readings the window is padded with its
oldest reading (the sensors are assumed steady before the first sample), so a node gets a
prediction from its very first reading instead of after a one-minute warm-up.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

WINDOW_S = 60
SENSORS = 16
MAX_MEMBERS = 32
# The blob and its descriptor live together, next to the firmware that embeds the blob.
DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parents[2] / "wokwi" / "esp32-replay" / "esp32-replay" / "model"
)


@dataclass(frozen=True)
class QParam:
    scale: float
    zero_point: int


@dataclass(frozen=True)
class Block:
    din: int
    r: QParam
    mul: QParam
    w_scale: float
    matmul: QParam
    s: QParam
    mul1: QParam
    bias: QParam
    out: QParam


@dataclass(frozen=True)
class Estimate:
    prob: np.ndarray  # P(present) per gas
    present: np.ndarray
    ppm: np.ndarray
    uct_ppm: np.ndarray


def _qparam(payload: dict) -> QParam:
    return QParam(float(payload["scale"]), int(payload["zero_point"]))


def _quantize(values: np.ndarray, qparam: QParam) -> np.ndarray:
    """ONNX QuantizeLinear: round half to even, saturate to int8 (rintf in the C++)."""
    scaled = np.rint(values.astype(np.float32) / np.float32(qparam.scale)) + np.float32(
        qparam.zero_point
    )
    return np.clip(scaled, -128.0, 127.0).astype(np.int8)


def _dequantize(values: np.ndarray, qparam: QParam) -> np.ndarray:
    return (values.astype(np.float32) - np.float32(qparam.zero_point)) * np.float32(qparam.scale)


def _softplus(values: np.ndarray) -> np.ndarray:
    return np.logaddexp(0.0, values)


class TabMEngine:
    """Loads the exported blob once and runs one window at a time."""

    def __init__(self, model_dir: Path | str | None = None, members: int = MAX_MEMBERS) -> None:
        self.model_dir = Path(model_dir) if model_dir is not None else model_dir_from_env()
        descriptor = json.loads((self.model_dir / "tabm_int8.json").read_text(encoding="utf-8"))
        self.descriptor = descriptor
        self.blob = np.frombuffer((self.model_dir / descriptor["blob"]).read_bytes(), dtype=np.int8)
        if self.blob.size != descriptor["blob_size"]:
            raise ValueError(
                f"{descriptor['blob']} is {self.blob.size} bytes, the descriptor says "
                f"{descriptor['blob_size']}: rerun tools/export_tabm_esp32.py"
            )
        self.members = max(1, min(int(members), descriptor["members"]))
        self.name = str(descriptor["model_name"])
        self.version = (
            str(descriptor["model_version"])
            if self.members == descriptor["members"]
            else f"{descriptor['model_version']}-k{self.members}"
        )
        self.infer_every_s = int(descriptor.get("infer_every_s", 5))
        self.window_s = int(descriptor.get("window_s", WINDOW_S))
        self.tau = np.asarray(descriptor["tau"], dtype=np.float64)
        self.gamma_max = float(descriptor["gamma_max"])
        self.co_danger_ppm = float(descriptor["co_danger_ppm"])
        self.alarm_smooth_s = int(descriptor["alarm_smooth_s"])

        self.x_mean = np.asarray(descriptor["x_mean"], dtype=np.float32)
        self.x_std = np.asarray(descriptor["x_std"], dtype=np.float32)
        self.feature_mean = np.asarray(descriptor["feature_mean"], dtype=np.float32)
        self.feature_std = np.asarray(descriptor["feature_std"], dtype=np.float32)
        self.y_max = np.asarray(descriptor["y_max"], dtype=np.float32)
        self.uct_cut = np.asarray(descriptor["uct_cut"], dtype=np.float64)

        self._input = _qparam(descriptor["input"])
        self._blocks = [
            Block(
                din=int(block["din"]),
                r=_qparam(block["r"]),
                mul=_qparam(block["mul"]),
                w_scale=float(block["w_scale"]),
                matmul=_qparam(block["matmul"]),
                s=_qparam(block["s"]),
                mul1=_qparam(block["mul1"]),
                bias=_qparam(block["bias"]),
                out=_qparam(block["out"]),
            )
            for block in descriptor["block_params"]
        ]
        head = descriptor["head"]
        self._head = {
            "matmul": _qparam(head["matmul"]),
            "add": _qparam(head["add"]),
            "w_scale": float(head["w_scale"]),
        }
        self._weights = self._prepare_weights(descriptor)

    # ------------------------------------------------------------------ graph
    def _tensor(self, offset: int, shape: tuple[int, ...], dtype=np.int8) -> np.ndarray:
        return self.blob[offset : offset + int(np.prod(shape))].reshape(shape).astype(dtype)

    def _prepare_weights(self, descriptor: dict) -> list[dict]:
        """Convert each int8 weight matrix once: float32 for BLAS (exact, see the module doc),
        plus the int32 row sums that remove the activation zero point."""
        weights = []
        for block, params in zip(self._blocks, descriptor["block_params"], strict=True):
            r = _dequantize(self._tensor(params["r_offset"], (32, block.din)), block.r)
            w = self._tensor(params["w_offset"], (1024, block.din), np.int32)
            s = _dequantize(self._tensor(params["s_offset"], (32, 1024)), block.s)
            bias = _dequantize(self._tensor(params["bias_offset"], (32, 1024)), block.bias)
            weights.append(
                {
                    "w_t": np.ascontiguousarray(w.T, dtype=np.float32),
                    "w_rowsum": w.sum(axis=1),
                    "r": r,
                    "s": s,
                    "bias": bias,
                }
            )
        head = descriptor["head"]
        head_w = self._tensor(head["w_offset"], (32, 10, 1024), np.int32)
        weights.append(
            {
                # (members, 1024, 10): one small matmul per member, batched by numpy.
                "head_w_t": np.ascontiguousarray(head_w.transpose(0, 2, 1), dtype=np.float32),
                "head_rowsum": head_w.sum(axis=2),
                "head_bias": _dequantize(
                    self._tensor(head["bias_offset"], (32, 10)), _qparam(head["bias"])
                ),
            }
        )
        return weights

    @staticmethod
    def _int_matmul(act: np.ndarray, weights: np.ndarray) -> np.ndarray:
        """int8 activations @ int8 weights with an exact int32 result (float32 BLAS inside)."""
        return np.matmul(act.astype(np.float32), weights).astype(np.int32)

    def _forward(self, features: np.ndarray) -> np.ndarray:
        """(144,) standardised features -> (10,) raw outputs, integer arithmetic like the C++."""
        members = self.members
        act = _quantize(features, self._input)[None, :]  # (members, din) after the broadcast
        act_qp = self._input
        for block, cache in zip(self._blocks, self._weights, strict=False):
            r = cache["r"][:members]
            u = _quantize(_dequantize(act, act_qp) * r, block.mul)
            acc = self._int_matmul(u, cache["w_t"])  # (members, 1024)
            acc -= block.mul.zero_point * cache["w_rowsum"]
            v = _quantize(
                acc.astype(np.float32) * np.float32(block.mul.scale * block.w_scale), block.matmul
            )
            wq = _quantize(_dequantize(v, block.matmul) * cache["s"][:members], block.mul1)
            act = _quantize(_dequantize(wq, block.mul1) + cache["bias"][:members], block.out)
            act_qp = block.out

        head = self._weights[-1]
        acc = self._int_matmul(act[:, None, :], head["head_w_t"][:members])[:, 0, :]
        acc -= act_qp.zero_point * head["head_rowsum"][:members]
        v = _quantize(
            acc.astype(np.float32) * np.float32(act_qp.scale * self._head["w_scale"]),
            self._head["matmul"],
        )
        out = _quantize(
            _dequantize(v, self._head["matmul"]) + head["head_bias"][:members], self._head["add"]
        )
        return _dequantize(out, self._head["add"]).mean(axis=0)

    # ------------------------------------------------------------ preprocessing
    def features(self, window: np.ndarray) -> np.ndarray:
        """(60, 16) raw readings, oldest first -> (144,) standardised features."""
        normalised = ((window - self.x_mean) / self.x_std).astype(np.float32)
        last = normalised[-1]
        t = np.arange(normalised.shape[0], dtype=np.float32)
        t -= t.mean()
        slope = np.einsum("wc,w->c", normalised, t) / float((t**2).sum())
        parts = [
            last,
            normalised.mean(axis=0),
            normalised.std(axis=0),
            normalised.min(axis=0),
            normalised.max(axis=0),
            slope,
            last - normalised[-1 - 5],
            last - normalised[-1 - 15],
            last - normalised[-1 - 30],
        ]
        return ((np.concatenate(parts) - self.feature_mean) / self.feature_std).astype(np.float32)

    def predict(self, window: np.ndarray) -> Estimate:
        raw = self._forward(self.features(window))
        layout = raw.reshape(5, 2)  # logit, gamma, nu, alpha, beta
        logit, gamma = layout[0], layout[1]
        nu = _softplus(layout[2]) + 1e-6
        alpha = _softplus(layout[3]) + 1.0
        beta = _softplus(layout[4]) + 1e-6
        prob = 1.0 / (1.0 + np.exp(-logit))
        present = prob >= self.tau
        ppm_cond = np.clip(gamma, 0.0, self.gamma_max) * self.y_max
        scale = np.sqrt(beta * (1 + nu) / (alpha * nu))
        return Estimate(
            prob=prob,
            present=present,
            ppm=np.where(present, ppm_cond, 0.0),
            uct_ppm=(scale + 1.0 / np.sqrt(nu)) * self.y_max,
        )


class TabMAlarmFilter:
    """Causal 5 s means over the readings' sequence numbers, exactly like src/tabm_int8.cpp."""

    def __init__(self, engine: TabMEngine) -> None:
        self.engine = engine
        self.size = engine.alarm_smooth_s
        self.prob = np.zeros((self.size, 2), dtype=np.float64)
        self.co_ppm = np.zeros(self.size, dtype=np.float64)
        self.sequence = np.zeros(self.size, dtype=np.int64)
        self.count = 0
        self.next = 0

    def update(self, estimate: Estimate, sequence: int) -> str:
        self.prob[self.next] = estimate.prob
        self.co_ppm[self.next] = estimate.ppm[0]
        self.sequence[self.next] = sequence
        self.next = (self.next + 1) % self.size
        self.count = min(self.count + 1, self.size)

        keep = self.sequence[: self.count] + self.size > sequence  # older than the 5 s window
        used = int(keep.sum()) or 1
        if float(self.co_ppm[: self.count][keep].sum()) / used >= self.engine.co_danger_ppm:
            return "danger"
        if float(self.prob[: self.count][keep][:, 0].sum()) / used >= self.engine.tau[0]:
            return "warning"
        return "normal"


def pad_window(rows: list[np.ndarray], size: int) -> np.ndarray:
    """(n, 16) readings, oldest first -> (size, 16): a short window is padded with its oldest
    reading, i.e. the sensors are taken as steady before the node's first sample."""
    window = np.stack(rows)
    missing = size - len(window)
    if missing <= 0:
        return window
    return np.concatenate([np.repeat(window[:1], missing, axis=0), window])


class NodeTabMInference:
    """Per-node state: the 60 s window, the inference cadence and the alarm filter.

    The first reading is predicted at once (on a padded window, see `pad_window`); after that the
    model runs on every `infer_every_s`-th sequence number, as the firmware does. A sequence that
    goes backwards means the node restarted (simulator or board reboot): its old window and alarm
    history no longer describe the same air, so the node starts over, again without a warm-up.
    """

    def __init__(self, engine: TabMEngine, infer_every_s: int | None = None) -> None:
        self.engine = engine
        self.infer_every_s = max(1, int(infer_every_s or engine.infer_every_s))
        self.last_sequence: int | None = None
        self._restart()

    def _restart(self) -> None:
        self.window: deque[np.ndarray] = deque(maxlen=self.engine.window_s)
        self.alarm = TabMAlarmFilter(self.engine)
        self.predicted = False

    def _note_sequence(self, sequence: int) -> None:
        if self.last_sequence is not None and sequence < self.last_sequence:
            self._restart()
        self.last_sequence = sequence

    def prime(self, sensors: list[float], sequence: int | None = None) -> None:
        """Feed the window without running the model (a device is predicting on its own)."""
        if sequence is not None:
            self._note_sequence(sequence)
        self.window.append(np.asarray(sensors, dtype=np.float32))

    def step(self, sensors: list[float], sequence: int) -> tuple[Estimate | None, str | None, int]:
        self._note_sequence(sequence)
        self.window.append(np.asarray(sensors, dtype=np.float32))
        if self.predicted and sequence % self.infer_every_s != 0:
            return None, None, len(self.window)
        self.predicted = True
        estimate = self.engine.predict(pad_window(list(self.window), self.engine.window_s))
        return estimate, self.alarm.update(estimate, sequence), len(self.window)


def model_dir_from_env() -> Path:
    return Path(os.getenv("TABM_MODEL_DIR", str(DEFAULT_MODEL_DIR)))


def members_from_env(default: int = MAX_MEMBERS) -> int:
    return int(os.getenv("TABM_MEMBERS", str(default)))


__all__ = [
    "Estimate",
    "NodeTabMInference",
    "TabMAlarmFilter",
    "TabMEngine",
    "members_from_env",
    "model_dir_from_env",
    "pad_window",
]

"""Edge model: the 16-channel MOX window becomes CO / C2H4 ppm and an alarm level.

One call per sensor reading (1 Hz):

    16 sensors ─► last 60 s window ─► 144 features (9 per channel) ─► standardise ─► Ridge
    ─► undo the target compression ─► CO / C2H4 ppm ─► 5 s average + notebook thresholds ─► alarm

Pure Python on purpose: the edge device needs no numpy. Training lives in
tools/train_edge_model.py and writes the coefficients to models/edge_ridge_v1.json.
"""

import json
import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .sensors import SENSORS

# Alarm policy of the training notebook.
CO_ALARM_PPM = 50.0  # WARNING on (OSHA PEL TWA)
CO_ALARM_OFF_PPM = 40.0  # hysteresis: warning turns off only below this
ALARM_SMOOTH_S = 5  # causal moving average before comparing with thresholds
CO_DANGER_PPM = 200.0  # DANGER (NIOSH ceiling)
ETH_PRESENT_PPM = 3.0  # C2H4 considered present

WINDOW_S = 60
FEATURES_PER_CHANNEL = (
    "last",  # current reading
    "mean",
    "min",
    "max",
    "std",
    "slope10",  # change per second over the last 10 s
    "slope30",  # change per second over the last 30 s
    "rise",  # last - window minimum: how far above the recent clean baseline
    "deconv",  # last + tau * slope10: where the slow MOX response is heading
)
# MOX response time constants (rise / recovery) used by the "deconv" feature.
RISE_TAU_S = 25.0
FALL_TAU_S = 70.0
# Targets are compressed (c / max) ** 0.6 before fitting, which mirrors the MOX power-law
# response and makes the relation to the features close to linear.
TARGET_MAX = {"co": 533.33, "ethylene": 20.0}
TARGET_POWER = 0.6
MODEL_PATH = Path(__file__).with_name("models") / "edge_ridge_v1.json"


def feature_names() -> list[str]:
    return [f"{sensor.channel}.{name}" for sensor in SENSORS for name in FEATURES_PER_CHANNEL]


def _slope(series: Sequence[float], span: int) -> float:
    span = min(span, len(series) - 1)
    return (series[-1] - series[-1 - span]) / span if span > 0 else 0.0


def channel_features(series: Sequence[float]) -> list[float]:
    n = len(series)
    last = series[-1]
    mean = sum(series) / n
    low = min(series)
    high = max(series)
    # List comprehension instead of a generator: sum() keeps its compensated (Neumaier)
    # summation, so the result is bit-identical, but there is no generator frame per item.
    squares = [(value - mean) ** 2 for value in series]
    std = math.sqrt(sum(squares) / n)
    slope10 = _slope(series, 10)
    slope30 = _slope(series, 30)
    tau = RISE_TAU_S if slope10 > 0 else FALL_TAU_S
    return [last, mean, low, high, std, slope10, slope30, last - low, last + tau * slope10]


def extract_features(window: Sequence[Sequence[float]]) -> list[float]:
    """window: 16-value readings, oldest first (at least one)."""
    features: list[float] = []
    # Transpose the window once (zip runs in C) instead of walking every reading once per
    # channel: 16 passes over the deque become a single pass, which is most of the model cost.
    for series in zip(*window, strict=False):
        features.extend(channel_features(series))
    return features


def compress(value: float, gas: str) -> float:
    return (max(0.0, value) / TARGET_MAX[gas]) ** TARGET_POWER


def expand(value: float, gas: str) -> float:
    return TARGET_MAX[gas] * max(0.0, value) ** (1 / TARGET_POWER)


@dataclass
class RidgeModel:
    name: str
    version: str
    trained_on: str
    mean: list[float]
    std: list[float]
    coef: dict[str, list[float]]
    intercept: dict[str, float]
    alpha: float = 1.0
    metrics: dict = field(default_factory=dict)
    # Mean raw features in clean air: contributions are measured against this, so a channel
    # only counts as "driving" the estimate when it moved away from its clean baseline.
    reference: list[float] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "RidgeModel":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: Path = MODEL_PATH) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Skip the private caches (e.g. the standardised clean-air reference): the file must keep
        # loading back through __init__.
        payload = {key: value for key, value in self.__dict__.items() if not key.startswith("_")}
        Path(path).write_text(json.dumps(payload, indent=1), encoding="utf-8")

    def standardise(self, features: Sequence[float]) -> list[float]:
        return [(value - m) / s for value, m, s in zip(features, self.mean, self.std, strict=True)]

    def predict(self, features: Sequence[float]) -> "Estimate":
        z = self.standardise(features)
        ppm = {}
        for gas in ("co", "ethylene"):
            raw = self.intercept[gas] + sum(c * v for c, v in zip(self.coef[gas], z, strict=True))
            ppm[gas] = expand(raw, gas)
        # Explaining clean air is noise: name channels only when there is gas to explain.
        meaningful = ppm["co"] >= 10 or ppm["ethylene"] >= 1
        return Estimate(
            co_ppm=ppm["co"],
            ethylene_ppm=ppm["ethylene"],
            top_channels=self.top_channels(z) if meaningful else [],
        )

    def top_channels(self, z: Sequence[float], limit: int = 3) -> list[str]:
        """Channels that pushed the gas estimate up the most, relative to clean air."""
        reference = self._reference_z(z)
        per_channel = len(FEATURES_PER_CHANNEL)
        # Sum the two coefficients once instead of twice per feature inside the loop.
        weights = [co + eth for co, eth in zip(self.coef["co"], self.coef["ethylene"], strict=True)]
        scores = []
        for index, sensor in enumerate(SENSORS):
            score = sum(
                weights[i] * (z[i] - reference[i])
                for i in range(index * per_channel, (index + 1) * per_channel)
            )
            scores.append((score, sensor.channel))
        scores.sort(reverse=True)
        # Below ~0.02 in compressed units (≈ 7 ppm CO) a channel is just noise.
        return [channel for score, channel in scores[:limit] if score > 0.02]

    def _reference_z(self, z: Sequence[float]) -> list[float]:
        """Standardised clean-air baseline, computed once: it never changes at runtime."""
        cached = self.__dict__.get("_reference_z_cache")
        if cached is None:
            cached = self.standardise(self.reference) if self.reference else [0.0] * len(z)
            self.__dict__["_reference_z_cache"] = cached
        return cached


@dataclass(frozen=True)
class Estimate:
    co_ppm: float
    ethylene_ppm: float
    top_channels: list[str]


def classify_alarm(co_smooth: float, ethylene_smooth: float, warning_latched: bool) -> str:
    if co_smooth >= CO_DANGER_PPM:
        return "danger"
    co_warning = co_smooth >= (CO_ALARM_OFF_PPM if warning_latched else CO_ALARM_PPM)
    if co_warning or ethylene_smooth >= ETH_PRESENT_PPM:
        return "warning"
    return "normal"


class AlarmFilter:
    """5 s causal average + hysteresis, as the notebook's "_hyst" alarm variant."""

    def __init__(self) -> None:
        self.co: deque[float] = deque(maxlen=ALARM_SMOOTH_S)
        self.ethylene: deque[float] = deque(maxlen=ALARM_SMOOTH_S)
        self.warning_latched = False

    def update(self, co_ppm: float, ethylene_ppm: float) -> str:
        self.co.append(co_ppm)
        self.ethylene.append(ethylene_ppm)
        co_smooth = sum(self.co) / len(self.co)
        eth_smooth = sum(self.ethylene) / len(self.ethylene)
        level = classify_alarm(co_smooth, eth_smooth, self.warning_latched)
        self.warning_latched = co_smooth >= (
            CO_ALARM_OFF_PPM if self.warning_latched else CO_ALARM_PPM
        )
        return level


class NodeInference:
    """Per-node state: the rolling window and the alarm filter."""

    def __init__(self, model: RidgeModel, window_s: int = WINDOW_S) -> None:
        self.model = model
        self.window: deque[list[float]] = deque(maxlen=window_s)
        self.alarm = AlarmFilter()

    def prime(self, sensors: Sequence[float]) -> None:
        """Feed the window without running the model (a device is predicting on its own)."""
        self.window.append(list(sensors))

    def step(self, sensors: Sequence[float]) -> tuple[Estimate, str, int]:
        self.window.append(list(sensors))
        estimate = self.model.predict(extract_features(self.window))
        level = self.alarm.update(estimate.co_ppm, estimate.ethylene_ppm)
        return estimate, level, len(self.window)

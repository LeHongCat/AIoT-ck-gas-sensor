import logging
import math
import os
import random
import signal
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from .config import MqttConfig, env_bool
from .contracts import (
    ActuatorCommand,
    ActuatorState,
    DemoScenario,
    DemoState,
    GroundTruth,
    SensorReading,
)
from .edge_model import CO_ALARM_OFF_PPM, ETH_PRESENT_PPM
from .topics import mqtt_topic

LOG = logging.getLogger("gas-simulator")

# Ranges of the UCI 322 ethylene_CO recording (notebook section 4).
CO_MAX_PPM = 533.33
ETH_MAX_PPM = 20.0


def dataset_rows(path: Path) -> Iterator[tuple[list[float], float, float]]:
    """Stream the recording and aggregate it to one event per second.

    Accepts both layouts of the 19 columns (t_s, CO, Ethylene, S1..S16): the original 100 Hz file
    (whitespace separated) and `demo_data/demo_1hz.csv` (comma separated, already 1 Hz, with a
    header that is skipped because it does not parse as numbers).
    """
    current_second: int | None = None
    sums = [0.0] * 16
    count = 0
    last_co = 0.0
    last_ethylene = 0.0

    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            parts = line.replace(",", " ").split()
            if len(parts) != 19:
                continue
            try:
                values = [float(value) for value in parts]
            except ValueError:
                continue
            second = math.floor(values[0])
            if current_second is not None and second != current_second and count:
                yield [value / count for value in sums], last_co, last_ethylene
                sums = [0.0] * 16
                count = 0
            current_second = second
            last_co, last_ethylene = values[1], values[2]
            for index, value in enumerate(values[3:]):
                sums[index] += value
            count += 1
    if count:
        yield [value / count for value in sums], last_co, last_ethylene


# ------------------------------------------------------------------ sensor model
@dataclass(frozen=True)
class Channel:
    sensor: str
    base: float  # clean-air reading after warm-up
    co_gain: float  # added reading at 533.33 ppm CO
    eth_gain: float  # added reading at 20 ppm C2H4


# Calibrated against the notebook figures: S1-S8 rest around 1300-1900 and peak up to ~8700,
# S9/S10 and S13/S14 respond weakly, S11/S12 and S15/S16 reach 6000-8000 under CO.
CHANNELS = (
    Channel("TGS2602", 1400, 1500, 1800),
    Channel("TGS2602", 1600, 900, 4600),
    Channel("TGS2600", 1500, 4800, 1500),
    Channel("TGS2600", 1700, 5600, 1700),
    Channel("TGS2610", 1300, 1000, 1400),
    Channel("TGS2610", 1800, 1400, 1600),
    Channel("TGS2620", 1600, 6000, 1600),
    Channel("TGS2620", 1900, 6600, 1800),
    Channel("TGS2602", 350, 150, 360),
    Channel("TGS2602", 420, 120, 700),
    Channel("TGS2600", 1500, 4200, 900),
    Channel("TGS2600", 1600, 4700, 1000),
    Channel("TGS2610", 900, 300, 300),
    Channel("TGS2610", 1100, 350, 420),
    Channel("TGS2620", 1700, 5700, 1100),
    Channel("TGS2620", 1800, 6200, 1200),
)
RISE_TAU_S = 25.0
FALL_TAU_S = 70.0


class SensorModel:
    """First-order MOX response: fast rise, slow recovery, slight drift and noise."""

    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.state = [channel.base for channel in CHANNELS]
        self.elapsed = 0.0

    def step(self, co_ppm: float, ethylene_ppm: float, dt: float = 1.0) -> list[float]:
        drift = 1 + 0.04 * math.sin(2 * math.pi * self.elapsed / 5400)
        co = (max(0.0, co_ppm) / CO_MAX_PPM) ** 0.6
        eth = (max(0.0, ethylene_ppm) / ETH_MAX_PPM) ** 0.6
        readings = []
        for index, channel in enumerate(CHANNELS):
            target = channel.base * drift + channel.co_gain * co + channel.eth_gain * eth
            tau = RISE_TAU_S if target > self.state[index] else FALL_TAU_S
            self.state[index] += (target - self.state[index]) * (1 - math.exp(-dt / tau))
            readings.append(self.state[index] * (1 + self.rng.gauss(0, 0.004)))
        self.elapsed += dt
        return readings


# ------------------------------------------------------------------ leaks and kitchen air
@dataclass(frozen=True)
class Episode:
    co_ppm: float
    ethylene_ppm: float
    label: str


# Source strength of each leak, on the dataset set-point grid (CO 200 + k * 13.33 ppm,
# C2H4 k * 1.667 ppm). co_warning is the only level below 200 ppm; it exists to demonstrate the
# WARNING band. The room never simply "holds" these values: see KitchenAir.
EPISODES = {
    "co_warning": Episode(106.67, 0.0, "CO vượt ngưỡng cảnh báo"),
    "co_spike": Episode(533.33, 0.0, "CO tăng đột biến"),
    "ethylene_spike": Episode(0.0, 16.67, "Ethylene tăng đột biến"),
    "mixture": Episode(493.33, 13.33, "Rò rỉ hỗn hợp CO + C2H4"),
}
AUTO_CYCLE = ("co_warning", "ethylene_spike", "mixture", "co_spike")

# Kitchen air as one well-mixed volume, rates per second. A leak mixes in at FILL_RATE; air is
# replaced by natural draft, the hood fans and the roof exhaust. With both fans running the
# room settles at ~45% of the leak strength and clears with a ~16 s time constant.
FILL_RATE = 1 / 20
NATURAL_VENTILATION = 1 / 150
HOOD_VENTILATION = 1 / 45
ROOF_VENTILATION = 1 / 30


def ventilation_rate(fan_on: bool, exhaust_on: bool) -> float:
    return (
        NATURAL_VENTILATION
        + (HOOD_VENTILATION if fan_on else 0.0)
        + (ROOF_VENTILATION if exhaust_on else 0.0)
    )


def ventilation_label(fan_on: bool, exhaust_on: bool) -> str:
    if fan_on and exhaust_on:
        return "full"
    return "hood" if fan_on else "roof" if exhaust_on else "natural"


@dataclass
class Leak:
    scenario: str
    until: float
    auto: bool = False
    # The gas valve closed while it leaked: the source is cut off for the rest of the episode.
    isolated: bool = False

    @property
    def episode(self) -> Episode:
        return EPISODES[self.scenario]


class KitchenAir:
    """True gas concentration in the kitchen: rises while a leak emits, falls with ventilation."""

    def __init__(self) -> None:
        self.co = 0.0
        self.ethylene = 0.0

    @staticmethod
    def _approach(value: float, source: float, vent: float, dt: float) -> float:
        if source > 0:
            rate = FILL_RATE + vent
            target = FILL_RATE * source / rate
        else:
            rate, target = vent, 0.0
        return target + (value - target) * math.exp(-rate * dt)

    def step(
        self, leak: Leak | None, fan_on: bool, exhaust_on: bool, dt: float = 1.0
    ) -> tuple[float, float]:
        vent = ventilation_rate(fan_on, exhaust_on)
        emitting = leak is not None and not leak.isolated
        self.co = self._approach(self.co, leak.episode.co_ppm if emitting else 0.0, vent, dt)
        self.ethylene = self._approach(
            self.ethylene, leak.episode.ethylene_ppm if emitting else 0.0, vent, dt
        )
        return self.co, self.ethylene

    def is_safe(self) -> bool:
        return self.co < CO_ALARM_OFF_PPM and self.ethylene < ETH_PRESENT_PPM

    def eta_safe(self, fan_on: bool, exhaust_on: bool) -> float:
        """Seconds until both gases are under the alarm thresholds, if nothing emits."""
        vent = ventilation_rate(fan_on, exhaust_on)
        times = [0.0]
        if self.co > CO_ALARM_OFF_PPM:
            times.append(math.log(self.co / CO_ALARM_OFF_PPM) / vent)
        if self.ethylene > ETH_PRESENT_PPM:
            times.append(math.log(self.ethylene / ETH_PRESENT_PPM) / vent)
        return max(times)


class GasSchedule:
    """Which leak is active: requested by the demo UI or started automatically."""

    def __init__(self, auto_every: int = 0, auto_seconds: int = 90) -> None:
        self.auto_every = auto_every
        self.auto_seconds = auto_seconds
        self.tick = 0
        self.leak: Leak | None = None
        self.lock = threading.Lock()

    def trigger(self, scenario: str, duration_s: float, now: float) -> None:
        with self.lock:
            # "clear" cuts the source; the air still has to be ventilated out.
            self.leak = None if scenario == "clear" else Leak(scenario, now + duration_s)

    def advance(self, now: float, valve_closed: bool, room_safe: bool) -> Leak | None:
        with self.lock:
            if (
                self.auto_every > 0
                and self.leak is None
                and self.tick % self.auto_every == self.auto_every - self.auto_seconds
            ):
                scenario = AUTO_CYCLE[(self.tick // self.auto_every) % len(AUTO_CYCLE)]
                self.leak = Leak(scenario, now + self.auto_seconds, auto=True)
            self.tick += 1
            leak = self.leak
            if leak is not None and valve_closed:
                leak.isolated = True
            if leak is not None and (now >= leak.until or (leak.isolated and room_safe)):
                self.leak = leak = None
            return leak


def leak_phase(leak: Leak | None, air: KitchenAir) -> str:
    if leak is not None:
        return "isolated" if leak.isolated else "leaking"
    return "clean" if air.is_safe() else "clearing"


# ------------------------------------------------------------------ simulator service
class Simulator:
    def __init__(self) -> None:
        self.config = MqttConfig()
        self.node_id = os.getenv("SIMULATOR_NODE_ID", "virtual-gas-01")
        self.interval = float(os.getenv("SIMULATOR_INTERVAL_SECONDS", "1"))
        self.loop = env_bool("SIMULATOR_LOOP", True)
        # The recording's CO / C2H4 columns (or the synthetic room air) as a reference stream.
        self.publish_truth = env_bool("SIMULATOR_PUBLISH_GROUND_TRUTH", True)
        self.leak_every = int(os.getenv("SIMULATOR_LEAK_EVERY_EVENTS", "420"))
        self.dataset_path = Path(os.getenv("DATASET_PATH", "data/ethylene_CO.txt"))
        self.schedule = GasSchedule(self.leak_every)
        self.air = KitchenAir()
        self.leak: Leak | None = None
        # The simulator is also the device: these are the outputs it last applied.
        self.outputs = {"fan_on": False, "exhaust_on": False, "gas_valve_closed": False}
        self.sensor_model = SensorModel()
        self.stop_event = threading.Event()
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"simulator-{self.node_id}",
            protocol=mqtt.MQTTv5,
        )
        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.error("MQTT connection rejected: %s", reason_code)
            return
        client.subscribe(mqtt_topic(self.node_id, "commands"), qos=self.config.qos)
        client.subscribe(mqtt_topic(self.node_id, "demo-scenario"), qos=self.config.qos)
        # The retained ACK tells a restarted device which outputs it last applied.
        client.subscribe(mqtt_topic(self.node_id, "actuator-state"), qos=self.config.qos)

    def _on_message(self, client, userdata, message) -> None:
        if message.topic.endswith("/demo-scenario"):
            try:
                request = DemoScenario.model_validate_json(message.payload)
                self.schedule.trigger(request.scenario, request.duration_s, time.monotonic())
                LOG.info("Demo scenario %s for %ss", request.scenario, request.duration_s)
            except Exception:
                LOG.exception("Rejected demo scenario")
            return
        if message.topic.endswith("/actuator-state"):
            try:
                state = ActuatorState.model_validate_json(message.payload)
                self.outputs = {
                    "fan_on": state.fan_on,
                    "exhaust_on": state.exhaust_on,
                    "gas_valve_closed": state.gas_valve_closed,
                }
            except Exception:
                LOG.exception("Ignored unreadable actuator state")
            return
        try:
            command = ActuatorCommand.model_validate_json(message.payload)
            self.outputs = {
                "fan_on": command.fan_on,
                "exhaust_on": command.exhaust_on,
                "gas_valve_closed": command.gas_valve_closed,
            }
            state = ActuatorState(
                node_id=self.node_id,
                observed_at=datetime.now(UTC),
                sequence=command.sequence,
                command_id=command.event_id,
                mode=command.mode,
                source=command.source,
                fan_on=command.fan_on,
                exhaust_on=command.exhaust_on,
                gas_valve_closed=command.gas_valve_closed,
                alarm_on=command.alarm_on,
                suppression_on=command.suppression_on,
                alarm_muted=command.alarm_muted,
            )
            client.publish(
                mqtt_topic(self.node_id, "actuator-state"),
                state.model_dump_json(),
                qos=self.config.qos,
                retain=True,
            )
            LOG.info("Applied actuator command %s (%s)", command.event_id, command.mode)
        except Exception:
            LOG.exception("Rejected actuator command on %s", message.topic)

    def _air_step(self) -> tuple[list[float], float, float]:
        outputs = self.outputs
        self.leak = self.schedule.advance(
            time.monotonic(), outputs["gas_valve_closed"], self.air.is_safe()
        )
        co, ethylene = self.air.step(self.leak, outputs["fan_on"], outputs["exhaust_on"])
        return self.sensor_model.step(co, ethylene), co, ethylene

    def _rows(self) -> Iterator[tuple[list[float], float, float]]:
        if self.dataset_path.is_file():
            LOG.info("Replaying %s at %.3fs/event", self.dataset_path, self.interval)
            self.schedule.auto_every = 0  # real data brings its own gas episodes
            for sensors, co, ethylene in dataset_rows(self.dataset_path):
                # A requested demo leak temporarily replaces the replay until the kitchen is
                # safe again; the sensor model continues from the last real readings.
                if self.schedule.leak is not None or not self.air.is_safe():
                    yield self._air_step()
                else:
                    self.sensor_model.state = list(sensors)
                    yield sensors, co, ethylene
            return
        LOG.warning(
            "Dataset not found at %s; using calibrated synthetic data (episode every %ss)",
            self.dataset_path,
            self.leak_every,
        )
        while True:
            yield self._air_step()

    def _publish_demo_state(self, observed_at: datetime, sequence: int) -> None:
        leak = self.leak
        fan_on, exhaust_on = self.outputs["fan_on"], self.outputs["exhaust_on"]
        phase = leak_phase(leak, self.air)
        emitting = leak is not None and not leak.isolated
        state = DemoState(
            node_id=self.node_id,
            observed_at=observed_at,
            sequence=sequence,
            scenario=leak.scenario if leak else None,
            auto=leak.auto if leak else False,
            phase=phase,
            remaining_s=round(max(0.0, leak.until - time.monotonic()), 1) if leak else None,
            source_co_ppm=leak.episode.co_ppm if emitting else 0.0,
            source_ethylene_ppm=leak.episode.ethylene_ppm if emitting else 0.0,
            room_co_ppm=round(self.air.co, 2),
            room_ethylene_ppm=round(self.air.ethylene, 3),
            ventilation=ventilation_label(fan_on, exhaust_on),
            eta_safe_s=None if emitting else round(self.air.eta_safe(fan_on, exhaust_on), 1),
        )
        # Retained so a freshly opened UI immediately knows the current situation.
        self.client.publish(
            mqtt_topic(self.node_id, "demo-state"), state.model_dump_json(), qos=0, retain=True
        )

    def run(self) -> None:
        self.client.connect(self.config.host, self.config.port, keepalive=30)
        self.client.loop_start()
        sequence = 0
        try:
            while not self.stop_event.is_set():
                produced = False
                for sensors, co_ppm, ethylene_ppm in self._rows():
                    if self.stop_event.is_set():
                        break
                    produced = True
                    started = time.perf_counter()
                    observed_at = datetime.now(UTC)
                    reading = SensorReading(
                        node_id=self.node_id,
                        observed_at=observed_at,
                        sequence=sequence,
                        sensors=sensors,
                    )
                    result = self.client.publish(
                        mqtt_topic(self.node_id, "readings"),
                        reading.model_dump_json(),
                        qos=self.config.qos,
                    )
                    result.wait_for_publish(timeout=5)

                    if self.publish_truth:
                        # Same observed_at and sequence as the reading: pairs 1:1 with the
                        # prediction of this second. Only 16 sensor values reach the model.
                        truth = GroundTruth(
                            node_id=self.node_id,
                            observed_at=observed_at,
                            sequence=sequence,
                            co_ppm=max(0.0, co_ppm),
                            ethylene_ppm=max(0.0, ethylene_ppm),
                        )
                        self.client.publish(
                            mqtt_topic(self.node_id, "ground-truth"),
                            truth.model_dump_json(),
                            qos=self.config.qos,
                        )
                    self._publish_demo_state(observed_at, sequence)
                    sequence += 1
                    elapsed = time.perf_counter() - started
                    if self.stop_event.wait(max(0.0, self.interval - elapsed)):
                        break
                if not self.loop or not produced:
                    break
        finally:
            self.client.disconnect()
            self.client.loop_stop()

    def stop(self, *_args) -> None:
        self.stop_event.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    simulator = Simulator()
    signal.signal(signal.SIGTERM, simulator.stop)
    signal.signal(signal.SIGINT, simulator.stop)
    simulator.run()


if __name__ == "__main__":
    main()

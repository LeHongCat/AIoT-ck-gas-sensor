"""Control-policy cases C01-C11 and the S09 / S11 timings, against the running stack.

Crafted predictions and operator requests are published on MQTT for bench-ctl-* nodes. They take
the real path (Bridge -> Kafka -> Safety Controller -> MQTT command) and this harness plays the
device: it records every command and ACKs it (or, for C09, fails to). Each case stores its input,
the expected and the actual outputs, timings and PASS / FAIL in control_cases.json / .csv.

C01-C03 also run the edge alarm filter (TabMAlarmFilter, the same rules as the firmware) offline on
crafted model outputs, to test the 0.48 / 200 ppm boundaries without depending on model accuracy.

    docker compose --profile esp32 up -d --wait
    uv run python scripts/bench/control_cases.py --out-dir docs/bench/run
"""

import argparse
import csv
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt

from aiot_pipeline.contracts import ControlRequest, Prediction, SensorReading
from aiot_pipeline.tabm_model import Estimate, TabMAlarmFilter, TabMEngine
from aiot_pipeline.topics import mqtt_topic

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    MQTT_HOST,
    MQTT_PORT,
    ROOT,
    Device,
    clear_retained,
    log,
    manifest,
    prom_value,
    service_action,
    summary,
    write_json,
)

OUTPUTS = ("fan_on", "exhaust_on", "gas_valve_closed", "alarm_on", "suppression_on")
POLICY = {
    "normal": dict(
        fan_on=False, exhaust_on=False, gas_valve_closed=False, alarm_on=False, suppression_on=False
    ),
    "warning": dict(
        fan_on=True, exhaust_on=True, gas_valve_closed=False, alarm_on=True, suppression_on=False
    ),
    "danger": dict(
        fan_on=True, exhaust_on=True, gas_valve_closed=True, alarm_on=True, suppression_on=False
    ),
}
PREFIX = f"bench-ctl-{uuid.uuid4().hex[:4]}"  # fresh node ids: the controller keeps per-node state


class Harness:
    def __init__(self) -> None:
        self.device = Device(PREFIX).start()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{PREFIX}-pub-{uuid.uuid4().hex[:6]}",
            protocol=mqtt.MQTTv5,
        )
        self.client.connect(MQTT_HOST, MQTT_PORT)
        self.client.loop_start()
        self.sequences: dict[str, int] = {}
        self.timings = {
            "prediction_to_command": [],
            "prediction_to_ack": [],
            "request_to_command": [],
        }
        self.nodes: set[str] = set()

    def stop(self) -> None:
        self.device.stop()
        self.client.loop_stop()
        self.client.disconnect()

    def _seq(self, node: str) -> int:
        self.sequences[node] = self.sequences.get(node, 0) + 1
        return self.sequences[node]

    def predict(self, node: str, level: str, model: str = "bench-ctl") -> float:
        self.nodes.add(node)
        co = {"normal": 5.0, "warning": 80.0, "danger": 300.0}[level]
        event = Prediction(
            node_id=node,
            observed_at=datetime.now(UTC),
            sequence=self._seq(node),
            co_ppm=co,
            ethylene_ppm=0.0,
            model_name=model,
            model_version="v1",
            alarm_level=level,
        )
        sent = time.time()
        self.client.publish(
            mqtt_topic(node, "predictions"), event.model_dump_json(), qos=1
        ).wait_for_publish(5)
        return sent

    def request(self, node: str, mode: str, **outputs) -> float:
        self.nodes.add(node)
        event = ControlRequest(
            node_id=node,
            observed_at=datetime.now(UTC),
            sequence=self._seq(node),
            mode=mode,
            **outputs,
        )
        sent = time.time()
        self.client.publish(
            mqtt_topic(node, "control-requests"), event.model_dump_json(), qos=1
        ).wait_for_publish(5)
        return sent

    def reading(self, node: str, sensors: list[float], sequence: int) -> None:
        event = SensorReading(
            node_id=node, observed_at=datetime.now(UTC), sequence=sequence, sensors=sensors
        )
        self.client.publish(mqtt_topic(node, "readings"), event.model_dump_json(), qos=1)

    def command(self, node: str, since: float, timeout: float = 8.0, kind: str = "prediction"):
        got = self.device.wait_command(node, since, timeout)
        if got is None:
            return None
        received, cmd = got
        key = "prediction_to_command" if kind == "prediction" else "request_to_command"
        self.timings[key].append(received - since)
        ack = self.ack_time(cmd["event_id"])
        if kind == "prediction" and ack is not None:
            self.timings["prediction_to_ack"].append(ack - since)
        return {
            "received_s": round(received - since, 4),
            "ack_s": None if ack is None else round(ack - since, 4),
            **cmd,
        }

    def ack_time(self, command_id: str, timeout: float = 2.0) -> float | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.device.lock:
                for stamp, ack in self.device.acks:
                    if ack["command_id"] == command_id:
                        return stamp
            time.sleep(0.005)
        return None

    def quiet(self, node: str, since: float, seconds: float) -> list:
        time.sleep(seconds)
        return self.device.commands_after(node, since)


def outputs(cmd: dict | None) -> dict | None:
    return None if cmd is None else {key: cmd[key] for key in OUTPUTS}


def case(cid: str, scenario: str, steps: list[dict], extra: dict | None = None) -> dict:
    passed = all(step["pass"] for step in steps)
    return {
        "id": cid,
        "scenario": scenario,
        "result": "PASS" if passed else "FAIL",
        "steps": steps,
        **(extra or {}),
    }


def step(name: str, expected, actual, ok: bool, **timing) -> dict:
    return {"step": name, "expected": expected, "actual": actual, "pass": bool(ok), **timing}


# ---- offline alarm filter (C01-C03) ------------------------------------------------------------


def filter_run(
    engine: TabMEngine, prob: list[tuple[float, float]], co_ppm: list[float], every: int = 1
) -> list[str]:
    alarm = TabMAlarmFilter(engine)
    levels = []
    for seq, ((p_co, p_eth), co) in enumerate(zip(prob, co_ppm, strict=True)):
        if seq % every:
            continue
        p = np.array([p_co, p_eth])
        estimate = Estimate(
            prob=p, present=p >= engine.tau, ppm=np.array([co, 0.0]), uct_ppm=np.zeros(2)
        )
        levels.append((seq, alarm.update(estimate, seq)))
    return levels


def offline_cases(engine: TabMEngine) -> list[dict]:
    tau, danger = float(engine.tau[0]), engine.co_danger_ppm
    cases = []
    boundary = []
    for label, p, co, expect in [
        ("P(CO) = τ − 1e-6, CO 0", tau - 1e-6, 0.0, "normal"),
        ("P(CO) = τ, CO 0", tau, 0.0, "warning"),
        ("P(CO) = τ + 1e-6, CO 0", tau + 1e-6, 0.0, "warning"),
        ("P(CO) ≥ τ, CO 199.99 ppm", 0.9, 199.99, "warning"),
        ("P(CO) ≥ τ, CO 200 ppm", 0.9, danger, "danger"),
        ("P(CO) ≥ τ, CO 200.01 ppm", 0.9, danger + 0.01, "danger"),
        ("P(CO) < τ, CO 250 ppm (danger first)", 0.1, 250.0, "danger"),
    ]:
        level = filter_run(engine, [(p, 0.0)] * 5, [co] * 5)[-1][1]
        boundary.append(step(label, expect, level, level == expect))
    cases.append(
        case(
            "C01a",
            f"Edge alarm filter at the boundaries (τ = {tau:.4f}, danger = {danger:g} ppm, mean of 5 s)",
            boundary,
        )
    )

    # C02: oscillation around τ and a step, at 1 s and 5 s inference cadence.
    osc_steps = []
    signal = [(tau + (0.05 if (s // 2) % 2 else -0.05), 0.0) for s in range(120)]
    for every in (1, 5):
        levels = filter_run(engine, signal, [0.0] * 120, every)
        flips = sum(1 for a, b in zip(levels, levels[1:], strict=False) if a[1] != b[1])
        osc_steps.append(
            step(
                f"P(CO) = τ ± 0.05 alternating every 2 s, cadence {every} s",
                "recorded",
                f"{flips} level changes in {len(levels)} predictions",
                True,
                flips=flips,
                predictions=len(levels),
            )
        )
        rise = [(0.1, 0.0)] * 30 + [(0.9, 0.0)] * 60 + [(0.1, 0.0)] * 30
        levels = filter_run(engine, rise, [0.0] * 120, every)
        on = next(s for s, lv in levels if lv == "warning" and s >= 30)
        off = next(s for s, lv in levels if lv == "normal" and s >= 90)
        osc_steps.append(
            step(
                f"P(CO) step 0.1 → 0.9 at s=30, back at s=90, cadence {every} s",
                "warning after the step, normal after the drop",
                f"warning at s={on}, normal at s={off}",
                on >= 30 and off >= 90,
                rise_delay_s=on - 30,
                fall_delay_s=off - 90,
            )
        )
    cases.append(
        case(
            "C02",
            "Oscillation around the threshold and step response, 1 s vs 5 s cadence",
            osc_steps,
        )
    )

    eth = filter_run(engine, [(0.01, 0.99)] * 30, [0.0] * 30)
    cases.append(
        case(
            "C03",
            "Only C2H4 present (P(C2H4) = 0.99, P(CO) = 0.01)",
            [
                step(
                    "30 predictions",
                    "normal (C2H4 shown but no alarm)",
                    sorted({lv for _, lv in eth}),
                    {lv for _, lv in eth} == {"normal"},
                )
            ],
        )
    )
    return cases


# ---- live cases --------------------------------------------------------------------------------


def c01_policy(h: Harness) -> dict:
    node = f"{PREFIX}-c01"
    steps = []
    for level in ("normal", "warning", "danger", "warning", "normal"):
        sent = h.predict(node, level)
        cmd = h.command(node, sent)
        ok = cmd is not None and outputs(cmd) == POLICY[level] and cmd["mode"] == "auto"
        steps.append(
            step(
                f"prediction {level}",
                POLICY[level],
                outputs(cmd),
                ok,
                command_s=cmd and cmd["received_s"],
                ack_s=cmd and cmd["ack_s"],
            )
        )
        time.sleep(1)
    return case("C01b", "Safety Controller output for each alarm level (policy table)", steps)


def timing_loop(h: Harness, n: int) -> dict:
    node = f"{PREFIX}-c00"
    misses = 0
    for i in range(n):
        level = "warning" if i % 2 == 0 else "normal"
        sent = h.predict(node, level)
        if h.command(node, sent) is None:
            misses += 1
        time.sleep(1.5)
    return {"node": node, "level_changes": n, "missing_commands": misses}


def c04_override(h: Harness, ttl: float) -> dict:
    node = f"{PREFIX}-c04"
    steps = []
    h.command(node, h.predict(node, "warning"))
    sent = h.request(node, "manual", fan_on=True)
    cmd = h.command(node, sent, kind="request")
    manual = dict(POLICY["normal"], fan_on=True)
    steps.append(
        step(
            "manual request: only the hood fan",
            {"mode": "manual", **manual},
            cmd and {"mode": cmd["mode"], **outputs(cmd)},
            cmd is not None and cmd["mode"] == "manual" and outputs(cmd) == manual,
            command_s=cmd and cmd["received_s"],
            ack_s=cmd and cmd["ack_s"],
        )
    )
    # predictions keep coming every second: Auto takes over at the first one after the TTL
    takeover = None
    while time.time() - sent < ttl + 10:
        tick = h.predict(node, "warning")
        got = h.device.wait_command(node, tick, 0.9)
        if got and got[1]["mode"] == "auto":
            takeover = got[0] - sent
            break
        time.sleep(max(0.0, 1 - (time.time() - tick)))
    steps.append(
        step(
            f"warning every 1 s during the override (TTL {ttl:g} s)",
            f"auto command ≈ {ttl:g}–{ttl + 1:g} s after the request",
            None if takeover is None else f"auto after {takeover:.2f} s",
            takeover is not None and ttl <= takeover <= ttl + 1.5,
            takeover_s=takeover and round(takeover, 3),
        )
    )

    node_b = f"{PREFIX}-c04b"
    h.command(node_b, h.predict(node_b, "warning"))
    sent = h.request(node_b, "manual", fan_on=True)
    h.command(node_b, sent, kind="request")
    later = h.quiet(node_b, sent + 1, ttl + 10)
    steps.append(
        step(
            f"no prediction for {ttl + 10:g} s after the request",
            "no auto command while no prediction arrives",
            f"{len(later)} commands",
            len(later) == 0,
        )
    )
    tick = h.predict(node_b, "warning")
    cmd = h.command(node_b, tick)
    steps.append(
        step(
            "first prediction after the TTL",
            {"mode": "auto", **POLICY["warning"]},
            cmd and {"mode": cmd["mode"], **outputs(cmd)},
            cmd is not None and cmd["mode"] == "auto" and outputs(cmd) == POLICY["warning"],
            command_s=cmd and cmd["received_s"],
        )
    )
    return case("C04", "Direct override, its expiry, and expiry while predictions stop", steps)


def c05_danger_in_override(h: Harness) -> dict:
    node = f"{PREFIX}-c05"
    h.command(node, h.predict(node, "warning"))
    h.command(node, h.request(node, "manual"), kind="request")
    sent = h.predict(node, "danger")
    cmd = h.command(node, sent)
    ok = cmd is not None and cmd["mode"] == "auto" and outputs(cmd) == POLICY["danger"]
    return case(
        "C05",
        "Danger during a direct override",
        [
            step(
                "danger prediction while the override (all off) runs",
                {"mode": "auto", **POLICY["danger"]},
                cmd and {"mode": cmd["mode"], **outputs(cmd)},
                ok,
                command_s=cmd and cmd["received_s"],
                ack_s=cmd and cmd["ack_s"],
            )
        ],
    )


def c06_suppression(h: Harness, ttl: float) -> dict:
    node = f"{PREFIX}-c06"
    steps = []
    h.command(node, h.predict(node, "warning"))
    sent = h.request(node, "manual", suppression_on=True)
    cmd = h.command(node, sent, kind="request")
    want = dict(POLICY["normal"], suppression_on=True, gas_valve_closed=True, alarm_on=True)
    steps.append(
        step(
            "manual suppression request",
            want,
            outputs(cmd),
            cmd is not None and outputs(cmd) == want,
            command_s=cmd and cmd["received_s"],
        )
    )
    tick = h.predict(node, "danger")
    cmd = h.command(node, tick)
    want = dict(POLICY["danger"], suppression_on=True)
    steps.append(
        step(
            "danger while the discharge runs",
            {"mode": "auto", **want},
            cmd and {"mode": cmd["mode"], **outputs(cmd)},
            cmd is not None and cmd["mode"] == "auto" and outputs(cmd) == want,
            command_s=cmd and cmd["received_s"],
        )
    )
    time.sleep(max(0.0, sent + ttl + 1 - time.time()))
    tick = h.predict(node, "danger")
    cmd = h.command(node, tick)
    steps.append(
        step(
            "danger after the discharge deadline",
            POLICY["danger"],
            outputs(cmd),
            cmd is not None and outputs(cmd) == POLICY["danger"],
        )
    )
    return case(
        "C06", "Fire suppression on request, interlock (valve + siren), kept through danger", steps
    )


def c07_mute(h: Harness) -> dict:
    node = f"{PREFIX}-c07"
    steps = []
    h.command(node, h.predict(node, "warning"))
    sent = h.request(node, "mute")
    cmd = h.command(node, sent, kind="request")
    want = dict(POLICY["warning"], alarm_on=False)
    steps.append(
        step(
            "mute during warning",
            {"mode": "auto", "alarm_muted": True, **want},
            cmd and {"mode": cmd["mode"], "alarm_muted": cmd.get("alarm_muted"), **outputs(cmd)},
            cmd is not None
            and cmd["mode"] == "auto"
            and cmd.get("alarm_muted") is True
            and outputs(cmd) == want,
            command_s=cmd and cmd["received_s"],
        )
    )
    since = time.time()
    for _ in range(3):
        h.predict(node, "warning")
        time.sleep(0.5)
    extra = h.quiet(node, since, 1.5)
    steps.append(
        step(
            "three more warnings while muted", "no new command", f"{len(extra)} commands", not extra
        )
    )
    tick = h.predict(node, "danger")
    cmd = h.command(node, tick)
    steps.append(
        step(
            "new danger",
            {"alarm_muted": False, **POLICY["danger"]},
            cmd and {"alarm_muted": cmd.get("alarm_muted"), **outputs(cmd)},
            cmd is not None
            and not cmd.get("alarm_muted")
            and outputs(cmd) == POLICY["danger"]
            and "re-armed" in cmd["reason"],
            command_s=cmd and cmd["received_s"],
            reason=cmd and cmd["reason"],
        )
    )
    sent = h.request(node, "mute")
    cmd = h.command(node, sent, kind="request")
    want = dict(POLICY["danger"], alarm_on=False)
    steps.append(
        step("mute during danger", want, outputs(cmd), cmd is not None and outputs(cmd) == want)
    )
    since = time.time()
    h.predict(node, "danger")
    extra = h.quiet(node, since, 1.5)
    steps.append(
        step(
            "same danger continues",
            "no new command (still muted)",
            f"{len(extra)} commands",
            not extra,
        )
    )
    sent = h.request(node, "unmute")
    cmd = h.command(node, sent, kind="request")
    steps.append(
        step(
            "unmute",
            POLICY["danger"],
            outputs(cmd),
            cmd is not None and outputs(cmd) == POLICY["danger"],
        )
    )
    return case("C07", "Siren mute, re-arm by a new danger, mute during danger, unmute", steps)


def c08_dedupe(h: Harness) -> dict:
    node = f"{PREFIX}-c08"
    before = prom_value(f'sum(aiot_control_commands_deduplicated_total{{node_id="{node}"}})') or 0.0
    since = time.time()
    for _ in range(20):
        h.predict(node, "warning")
        time.sleep(0.2)
    commands = h.quiet(node, since, 2)
    time.sleep(7)  # one Prometheus scrape
    after = prom_value(f'sum(aiot_control_commands_deduplicated_total{{node_id="{node}"}})') or 0.0
    with h.device.lock:
        pairs = [(c["event_id"], c["node_id"]) for _, c in h.device.commands]
        acks = {a["command_id"]: a["node_id"] for _, a in h.device.acks}
    matched = sum(1 for cid, n in pairs if acks.get(cid) == n)
    return case(
        "C08",
        "Unchanged desired state and command-ACK pairing",
        [
            step(
                "20 identical warning predictions",
                "1 command, 19 deduplicated",
                f"{len(commands)} commands, {after - before:g} deduplicated",
                len(commands) == 1 and after - before == 19,
            ),
            step(
                "every command ACKed by the same node with its command_id (all cases so far)",
                f"{len(pairs)} / {len(pairs)}",
                f"{matched} / {len(pairs)}",
                matched == len(pairs),
            ),
        ],
    )


def ack_timeouts(node: str) -> float:
    return prom_value(f'sum(aiot_actuator_ack_timeouts_total{{node_id="{node}"}})') or 0.0


def detect_timeout(node: str, before: float, since: float, limit: float = 25.0) -> float | None:
    while time.time() - since < limit:
        if ack_timeouts(node) > before:
            return time.time() - since
        time.sleep(0.5)
    return None


def c09_ack_faults(h: Harness) -> dict:
    steps = []
    for mode, delay in (("silent", 0), ("late", 7.0), ("wrong_id", 0), ("wrong_node", 0)):
        node = f"{PREFIX}-c09-{mode.replace('_', '')}"
        h.device.mode = "ack"
        h.command(node, h.predict(node, "normal"))
        time.sleep(1)
        h.device.mode, h.device.delay = mode, delay
        before = ack_timeouts(node)
        sent = h.predict(node, "warning")
        cmd = h.device.wait_command(node, sent, 8)
        detected = detect_timeout(node, before, sent)
        expect_timeout = mode != "wrong_node"
        expected = (
            "ACK timeout counted (after the 5 s deadline + one scrape)"
            if expect_timeout
            else "matched by command_id"
        )
        actual = f"timeout seen after {detected:.1f} s" if detected else "no timeout counted"
        steps.append(
            step(
                f"device {mode}" + (f" ({delay:g} s)" if delay else ""),
                expected,
                actual,
                cmd is not None and (detected is not None) == expect_timeout,
                detection_s=detected and round(detected, 2),
            )
        )
        h.device.mode = "ack"
        time.sleep(1)
    return case(
        "C09",
        "Missing, late, wrong-id and wrong-node ACKs",
        steps,
        {
            "note": "The bridge matches an ACK by command_id only; an ACK published under another node id still counts as the commanded node's ACK (wrong_node row).",
        },
    )


def c10_restart(h: Harness) -> dict:
    node = f"{PREFIX}-c10"
    steps = []
    h.command(node, h.predict(node, "warning"))
    h.command(node, h.request(node, "mute"), kind="request")
    h.command(node, h.request(node, "manual", fan_on=True), kind="request")
    action = service_action("restart", "safety-controller")
    ready = None
    tick_first = None
    while time.time() - action["returned_at"] < 60:
        tick = h.predict(node, "warning")
        tick_first = tick_first or tick
        got = h.device.wait_command(node, tick, 1.0)
        if got:
            ready = (got[0] - action["issued_at"], got[1])
            break
    cmd = ready and ready[1]
    steps.append(
        step(
            "warning after the controller restarted (it had mute + override)",
            {"mode": "auto", "alarm_muted": False, **POLICY["warning"]},
            cmd and {"mode": cmd["mode"], "alarm_muted": cmd.get("alarm_muted"), **outputs(cmd)},
            cmd is not None
            and cmd["mode"] == "auto"
            and not cmd.get("alarm_muted")
            and outputs(cmd) == POLICY["warning"],
            restart_call_s=round(action["returned_at"] - action["issued_at"], 2),
            first_command_after_restart_s=ready and round(ready[0], 2),
        )
    )
    retained = []
    probe = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"{PREFIX}-probe-{uuid.uuid4().hex[:4]}"
    )
    probe.on_message = lambda c, u, m: retained.append((m.retain, json.loads(m.payload)))
    probe.connect(MQTT_HOST, MQTT_PORT)
    probe.subscribe(mqtt_topic(f"{PREFIX}-c01", "actuator-state"), qos=1)
    probe.loop_start()
    time.sleep(2)
    probe.loop_stop()
    probe.disconnect()
    age = None
    if retained:
        age = time.time() - datetime.fromisoformat(retained[0][1]["observed_at"]).timestamp()
    steps.append(
        step(
            "new subscriber to an idle node's actuator-state",
            "receives the retained (old) ACK, with its observed_at",
            None if age is None else f"retained={retained[0][0]}, age {age:.0f} s",
            bool(retained) and retained[0][0],
            retained_age_s=age and round(age, 1),
        )
    )
    return case(
        "C10",
        "Controller restart (state is in memory: fail-safe) and retained ACK",
        steps,
        {
            "note": "A retained ACK only shows the last acknowledged state and when it was acknowledged; it is not evidence that the device is online.",
        },
    )


def c11_handover(h: Harness, timeout: float) -> dict:
    node = f"{PREFIX}-c11"
    rows = [
        [float(v) for v in r] for r in list(csv.reader(open(ROOT / "demo_data/demo_1hz.csv")))[1:]
    ]
    seen: list[tuple[float, dict]] = []
    watch = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"{PREFIX}-watch-{uuid.uuid4().hex[:4]}"
    )
    watch.on_message = lambda c, u, m: seen.append((time.time(), json.loads(m.payload)))
    watch.connect(MQTT_HOST, MQTT_PORT)
    watch.subscribe(mqtt_topic(node, "predictions"), qos=1)
    watch.loop_start()
    time.sleep(1)
    phases = {}
    plan = [
        ("gateway only", 20, False),
        ("device predicts every 5 s", 30, True),
        ("device stops", timeout + 20, False),
    ]
    seq = 0
    for name, seconds, device in plan:
        phases[name] = time.time()
        for _ in range(int(seconds)):
            tick = time.time()
            h.reading(node, rows[seq % len(rows)][3:], seq)
            if device and seq % 5 == 0:
                h.predict(node, "normal", model="bench-device")
            seq += 1
            time.sleep(max(0.0, 1 - (time.time() - tick)))
    time.sleep(2)
    watch.loop_stop()
    watch.disconnect()
    gateway = [(t, e) for t, e in seen if e["model_name"] != "bench-device"]
    a, b, c = phases["gateway only"], phases["device predicts every 5 s"], phases["device stops"]
    in_a = sum(1 for t, _ in gateway if a <= t < b)
    in_b = sum(1 for t, _ in gateway if b + 2 <= t < c)
    resumed = next((t - c for t, _ in gateway if t >= c), None)
    steps = [
        step(
            "readings only, 20 s",
            "gateway predicts every second",
            f"{in_a} gateway predictions",
            in_a >= 18,
        ),
        step(
            "device publishes its own predictions",
            "gateway steps aside (no duplicate predictions)",
            f"{in_b} gateway predictions",
            in_b == 0,
        ),
        step(
            "device stops predicting",
            f"gateway resumes after ≈ {timeout:g} s",
            None if resumed is None else f"resumed after {resumed:.1f} s",
            resumed is not None and timeout - 6 <= resumed <= timeout + 6,
            resume_s=resumed and round(resumed, 1),
        ),
    ]
    return case(
        "C11",
        "Gateway hand-over to a node that predicts on its own",
        steps,
        {
            "note": "The gateway ignoring its own echo (MQTT 5 No Local) is covered by the load test: predictions stored 100 % up to 40 nodes.",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", default="docs/bench/run")
    parser.add_argument("--timing-samples", type=int, default=40)
    parser.add_argument(
        "--skip-restart", action="store_true", help="skip C10 (restarts the controller)"
    )
    args = parser.parse_args()
    out = Path(args.out_dir)
    run = manifest("control_cases", vars(args))
    env = run["containers"]
    ttl = float(env.get("safety-controller", {}).get("env", {}).get("DIRECT_OVERRIDE_SECONDS", 30))
    handover = float(
        env.get("edge-inference", {}).get("env", {}).get("EDGE_ON_DEVICE_TIMEOUT_S", 30)
    )
    write_json(out / "run_manifest_control.json", run)

    cases = offline_cases(TabMEngine())
    h = Harness()
    try:
        for name, fn in [
            ("C01b", lambda: c01_policy(h)),
            ("C04", lambda: c04_override(h, ttl)),
            ("C05", lambda: c05_danger_in_override(h)),
            ("C06", lambda: c06_suppression(h, ttl)),
            ("C07", lambda: c07_mute(h)),
            ("C08", lambda: c08_dedupe(h)),
            ("C09", lambda: c09_ack_faults(h)),
            ("C11", lambda: c11_handover(h, handover)),
        ]:
            log(f"running {name}")
            try:
                cases.append(fn())
            except Exception as error:  # keep the other cases
                cases.append(
                    {
                        "id": name,
                        "scenario": "harness error",
                        "result": "ERROR",
                        "steps": [],
                        "error": repr(error),
                    }
                )
            log(f"{name}: {cases[-1]['result']}")
            write_json(out / "control_cases.json", {"run_id": run["run_id"], "cases": cases})
        log("timing loop (S09 / S11)")
        loop = timing_loop(h, args.timing_samples)
        if not args.skip_restart:
            log("running C10 (restarts safety-controller)")
            cases.append(c10_restart(h))
            log(f"C10: {cases[-1]['result']}")
    finally:
        h.stop()
    timings = {
        "S09_prediction_published_to_command_at_device": summary(
            h.timings["prediction_to_command"]
        ),
        "S11_prediction_published_to_ack_published": summary(h.timings["prediction_to_ack"]),
        "request_published_to_command_at_device": summary(h.timings["request_to_command"]),
        "loop": loop,
        "raw_seconds": {k: [round(v, 5) for v in vals] for k, vals in h.timings.items()},
    }
    write_json(
        out / "control_cases.json", {"run_id": run["run_id"], "timings": timings, "cases": cases}
    )
    with (out / "control_cases.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case", "scenario", "step", "expected", "actual", "pass"])
        for c in cases:
            for s in c["steps"]:
                writer.writerow(
                    [
                        c["id"],
                        c["scenario"],
                        s["step"],
                        json.dumps(s["expected"], ensure_ascii=False),
                        json.dumps(s["actual"], ensure_ascii=False),
                        s["pass"],
                    ]
                )
    clear_retained(sorted(h.nodes | {f"{h.device.other_node}"}))
    log(json.dumps(timings))
    log(" ".join(f"{c['id']}={c['result']}" for c in cases))


if __name__ == "__main__":
    main()

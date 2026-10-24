"""Recovery cases R01-R07: stop one part of the running stack under load and measure the recovery.

Background traffic: N replay nodes (bench-rec-*) publish a reading and a ground truth every second
from the host and ACK commands. For each case the harness records, on the host clock:

* when the fault was injected and when the component was back (docker stop / start returned);
* detection: when Prometheus first showed the matching alert as pending / firing;
* recovery: first record of each stream in Kafka published after the component was back;
* loss and duplicates per stream in Kafka (sequence numbers) and, for readings, in InfluxDB;
* backlog drained: when the last record published before the restart reached Kafka / InfluxDB;
* whether the control path (crafted prediction -> command -> ACK) still works during the fault.

    docker compose --profile esp32 up -d --wait
    uv run python scripts/bench/recovery_cases.py --out-dir docs/bench/run
"""

import argparse
import csv
import json
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from confluent_kafka import Consumer

from aiot_pipeline.contracts import GroundTruth, Prediction, SensorReading
from aiot_pipeline.topics import KafkaTopic, mqtt_topic

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    KAFKA,
    MQTT_HOST,
    MQTT_PORT,
    ROOT,
    Device,
    clear_retained,
    influx_sequences,
    log,
    manifest,
    prom,
    service_action,
    wait_healthy,
    write_json,
)

PREFIX = "bench-rec"
STREAMS = {
    str(KafkaTopic.SENSOR): "readings",
    str(KafkaTopic.GROUND_TRUTH): "ground-truth",
    str(KafkaTopic.PREDICTION): "predictions",
    str(KafkaTopic.DEAD_LETTER): "dead-letter",
}


class Traffic:
    """N nodes, one reading + one ground truth per second each; keeps the publish time per seq."""

    def __init__(self, nodes: int) -> None:
        self.names = [f"{PREFIX}-{i + 1:02d}" for i in range(nodes)]
        self.rows = [
            [float(v) for v in r]
            for r in list(csv.reader(open(ROOT / "demo_data/demo_1hz.csv")))[1:]
        ]
        self.published: dict[int, float] = {}
        self.seq = 0
        self.stop_event = threading.Event()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{PREFIX}-traffic-{uuid.uuid4().hex[:4]}"
        )
        self.client.max_queued_messages_set(0)
        self.client.reconnect_delay_set(1, 2)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "Traffic":
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        self.client.loop_start()
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(5)
        self.client.loop_stop()
        self.client.disconnect()

    def _run(self) -> None:
        start = time.time()
        while not self.stop_event.is_set():
            seq = self.seq
            observed = datetime.now(UTC)
            for i, name in enumerate(self.names):
                row = self.rows[(i * 397 + seq) % len(self.rows)]
                reading = SensorReading(
                    node_id=name, observed_at=observed, sequence=seq, sensors=row[3:]
                )
                truth = GroundTruth(
                    node_id=name,
                    observed_at=observed,
                    sequence=seq,
                    co_ppm=row[1],
                    ethylene_ppm=row[2],
                )
                self.client.publish(mqtt_topic(name, "readings"), reading.model_dump_json(), qos=1)
                self.client.publish(
                    mqtt_topic(name, "ground-truth"), truth.model_dump_json(), qos=1
                )
            self.published[seq] = time.time()
            self.seq += 1
            self.stop_event.wait(max(0.0, start + self.seq - time.time()))


class KafkaWatch:
    """Records (topic, node, seq) -> first arrival time and duplicate counts for bench-rec nodes."""

    def __init__(self) -> None:
        self.arrivals: dict[tuple[str, str, int], float] = {}
        self.duplicates: dict[str, int] = {t: 0 for t in STREAMS}
        self.dead_letters: list[dict] = []
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "KafkaWatch":
        self.thread.start()
        self.ready.wait(60)
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(10)

    def _run(self) -> None:
        consumer = Consumer(
            {
                "bootstrap.servers": KAFKA,
                "group.id": f"{PREFIX}-{uuid.uuid4().hex[:6]}",
                "auto.offset.reset": "latest",
                "enable.auto.commit": True,  # resume from our position after a broker restart
                "auto.commit.interval.ms": 500,
                "fetch.wait.max.ms": 10,
                "reconnect.backoff.max.ms": 1000,
            }
        )
        consumer.subscribe(list(STREAMS))
        while not consumer.assignment() and not self.stop_event.is_set():
            consumer.poll(0.2)
        self.ready.set()
        while not self.stop_event.is_set():
            message = consumer.poll(0.2)
            if message is None or message.error():
                continue
            now = time.time()
            if message.topic() == str(KafkaTopic.DEAD_LETTER):
                record = json.loads(message.value())
                if PREFIX in json.dumps(record):
                    self.dead_letters.append({"received": now, **record})
                continue
            event = json.loads(message.value())
            if not event["node_id"].startswith(PREFIX):
                continue
            key = (message.topic(), event["node_id"], event["sequence"])
            if key in self.arrivals:
                self.duplicates[message.topic()] += 1
            else:
                self.arrivals[key] = now
        consumer.close()


class AlertWatch:
    """Polls Prometheus once a second; first time each alert is pending / firing."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        self.first: dict[str, float] = {}
        self.errors = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "AlertWatch":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop_event.set()
        self.thread.join(5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                for r in prom('ALERTS{alertstate=~"pending|firing"}'):
                    name = r["metric"]["alertname"]
                    if name in self.names:
                        self.first.setdefault(f"{name}:{r['metric']['alertstate']}", time.time())
            except Exception:  # Prometheus itself may be the component under test
                self.errors += 1
            self.stop_event.wait(1)


class Lab:
    def __init__(self, nodes: int) -> None:
        self.device = Device(PREFIX).start()
        self.kafka = KafkaWatch().start()
        self.traffic = Traffic(nodes).start()
        self.ctl = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{PREFIX}-ctl-{uuid.uuid4().hex[:4]}"
        )
        self.ctl.connect(MQTT_HOST, MQTT_PORT)
        self.ctl.loop_start()
        self.ctl_seq = 0
        self.probe_nodes: list[str] = []
        self.ctl.reconnect_delay_set(1, 2)
        self.device.client.reconnect_delay_set(1, 2)

    def stop(self) -> None:
        self.traffic.stop()
        self.kafka.stop()
        self.device.stop()
        self.ctl.loop_stop()
        self.ctl.disconnect()

    def control_probe(self, level: str, timeout: float = 15.0) -> dict:
        """Crafted prediction for a control-only node: does a command (and ACK) come back?"""
        self.ctl_seq += 1
        # A fresh node per probe: its first prediction always yields a command (no dedupe).
        node = f"{PREFIX}-ctl{self.ctl_seq:02d}"
        self.probe_nodes.append(node)
        event = Prediction(
            node_id=node,
            observed_at=datetime.now(UTC),
            sequence=self.ctl_seq,
            co_ppm=80.0 if level == "warning" else 5.0,
            ethylene_ppm=0.0,
            model_name="bench-rec",
            model_version="v1",
            alarm_level=level,
        )
        sent = time.time()
        self.ctl.publish(mqtt_topic(node, "predictions"), event.model_dump_json(), qos=1)
        got = self.device.wait_command(node, sent, timeout)
        return {"level": level, "command_after_s": None if got is None else round(got[0] - sent, 3)}

    def window(self, start: float, end: float) -> list[int]:
        return [seq for seq, t in self.traffic.published.items() if start <= t < end]

    def stream_report(self, topic: str, seqs: list[int], back: float | None) -> dict:
        names = self.traffic.names
        arrivals = self.kafka.arrivals
        expected = [(n, s) for n in names for s in seqs]
        missing = [(n, s) for n, s in expected if (topic, n, s) not in arrivals]
        lost_seqs = sorted({s for _, s in missing})
        report = {
            "expected": len(expected),
            "missing": len(missing),
            "missing_pct": round(100 * len(missing) / len(expected), 2) if expected else None,
            "missing_span_s": self._span(lost_seqs),
            "duplicates": self.kafka.duplicates.get(topic, 0),
        }
        if back is not None:
            after = [
                arrivals[(topic, n, s)] - back
                for (n, s) in expected
                if (topic, n, s) in arrivals and self.traffic.published.get(s, 0) >= back
            ]
            report["first_new_record_after_back_s"] = round(min(after), 2) if after else None
            before = [
                arrivals[(topic, n, s)]
                for (n, s) in expected
                if (topic, n, s) in arrivals and self.traffic.published.get(s, 0) < back
            ]
            report["backlog_drained_after_back_s"] = (
                round(max(before) - back, 2) if before and max(before) > back else 0.0
            )
        return report

    def _span(self, seqs: list[int]) -> float | None:
        if not seqs:
            return 0.0
        times = [self.traffic.published[s] for s in seqs if s in self.traffic.published]
        return round(max(times) - min(times) + 1, 1) if times else None

    def influx_report(self, seqs: list[int], start: float) -> dict:
        stored = influx_sequences("sensor_reading", PREFIX, start)
        want = set(seqs)
        found = sum(len(want & set(stored.get(n, []))) for n in self.traffic.names)
        total = len(want) * len(self.traffic.names)
        return {"expected": total, "stored": found, "missing": total - found}


def influx_drain_time(
    lab: Lab, seqs: list[int], start: float, since: float, limit: float = 120
) -> float | None:
    total = len(seqs) * len(lab.traffic.names)
    while time.time() - since < limit:
        if lab.influx_report(seqs, start)["stored"] >= total:
            return round(time.time() - since, 2)
        time.sleep(1)
    return None


def outage(
    lab: Lab,
    rid: str,
    scenario: str,
    services: list[str],
    hold: float,
    alerts: tuple[str, ...],
    settle: float = 40,
    probe: bool = True,
    influx: bool = False,
) -> dict:
    log(f"{rid}: {scenario}")
    begin = time.time() - 10
    with AlertWatch(alerts) as watch:
        stops = [service_action("stop", s) for s in services]
        fault = stops[0]["issued_at"]
        probe_during = lab.control_probe("warning") if probe else None
        time.sleep(max(0.0, fault + hold - time.time()))
        starts = [service_action("start", s) for s in services]
        back = max(s["returned_at"] for s in starts)
        healthy = {s: wait_healthy(s) for s in services}
        time.sleep(settle)
        probe_after = lab.control_probe("normal") if probe else None
    end = back + settle - 15  # leave time for the last records in the window to arrive
    seqs = lab.window(begin, end)
    result = {
        "id": rid,
        "scenario": scenario,
        "services": services,
        "hold_s": hold,
        "fault_issued_at": fault,
        "stop_calls_s": [round(s["returned_at"] - s["issued_at"], 2) for s in stops],
        "start_calls_s": [round(s["returned_at"] - s["issued_at"], 2) for s in starts],
        "healthy_after_start_s": {
            k: (round(v, 2) if v is not None else None) for k, v in healthy.items()
        },
        "alerts_first_seen_after_fault_s": {
            k: round(t - fault, 1) for k, t in sorted(watch.first.items())
        },
        "prometheus_poll_errors": watch.errors,
        "control_probe_during_fault": probe_during,
        "control_probe_after": probe_after,
        "harness_note": "control probes use a fresh node each; the Kafka watcher commits offsets",
        "kafka": {
            STREAMS[t]: lab.stream_report(t, seqs, back)
            for t in STREAMS
            if t != str(KafkaTopic.DEAD_LETTER)
        },
        "window_seqs": [seqs[0], seqs[-1]] if seqs else None,
    }
    if influx:
        result["influx_readings_drained_after_back_s"] = influx_drain_time(
            lab, seqs, begin - 30, back - 0.0
        )
    time.sleep(5)
    result["influx_readings"] = lab.influx_report(seqs, begin - 30)
    log(
        f"{rid}: {json.dumps({k: result[k] for k in ('alerts_first_seen_after_fault_s', 'control_probe_during_fault')})}"
    )
    return result


def r05_malformed(lab: Lab) -> dict:
    log("R05: malformed messages")
    node = f"{PREFIX}-bad"
    client = lab.ctl
    good = SensorReading(
        node_id=node, observed_at=datetime.now(UTC), sequence=1, sensors=[1.0] * 16
    )
    base = json.loads(good.model_dump_json())
    cases = {
        "invalid JSON": b"{not json",
        "15 channels": json.dumps({**base, "sensors": [1.0] * 15}).encode(),
        "node_id differs from topic": json.dumps({**base, "node_id": f"{node}-x"}).encode(),
        "observed_at without timezone": json.dumps(
            {**base, "observed_at": "2026-10-24T10:00:00"}
        ).encode(),
        "unknown field": json.dumps({**base, "extra": 1}).encode(),
    }
    sent = time.time()
    for payload in cases.values():
        client.publish(mqtt_topic(node, "readings"), payload, qos=1)
    valid = SensorReading(
        node_id=node, observed_at=datetime.now(UTC), sequence=2, sensors=[1.0] * 16
    )
    client.publish(mqtt_topic(node, "readings"), valid.model_dump_json(), qos=1)
    time.sleep(8)
    letters = [d for d in lab.kafka.dead_letters if d["received"] >= sent]
    valid_seen = (str(KafkaTopic.SENSOR), node, 2) in lab.kafka.arrivals
    return {
        "id": "R05",
        "scenario": "Malformed readings, then a valid one",
        "sent_invalid": len(cases),
        "dead_letters": len(letters),
        "dead_letter_errors": [d.get("error", "")[:160] for d in letters],
        "valid_after_invalid_reached_kafka": valid_seen,
        "result": "PASS" if len(letters) == len(cases) and valid_seen else "FAIL",
    }


def ui_and_observability_back() -> dict:
    import urllib.request

    marks = {}
    for name, url in (
        ("demo-ui", "http://localhost:8080/health"),
        ("grafana", "http://localhost:3000/api/health"),
        ("prometheus", "http://localhost:9090/-/ready"),
    ):
        start = time.time()
        while time.time() - start < 120:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        marks[name] = round(time.time() - start, 2)
                        break
            except Exception:
                time.sleep(0.5)
    return marks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nodes", type=int, default=5)
    parser.add_argument("--out-dir", default="docs/bench/run")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    args = parser.parse_args()
    out = Path(args.out_dir)
    run = manifest("recovery_cases", vars(args))
    write_json(out / "run_manifest_recovery.json", run)
    only = set(filter(None, args.only.split(",")))
    lab = Lab(args.nodes)
    results = []
    try:
        log("warm-up 40 s")
        time.sleep(40)
        plan = [
            (
                "R01",
                "Stop Influx Sink 60 s",
                ["influx-sink"],
                60,
                ("AiotServiceDown", "AiotConsumerLagHigh"),
                dict(influx=True),
            ),
            ("R02", "Stop Bridge 20 s", ["mqtt-kafka-bridge"], 20, ("AiotServiceDown",), {}),
            (
                "R03",
                "Stop Kafka 30 s",
                ["kafka"],
                30,
                ("AiotServiceDown", "AiotConsumerLagHigh"),
                dict(influx=True),
            ),
            (
                "R04",
                "Stop Mosquitto 20 s",
                ["mosquitto"],
                20,
                ("AiotMqttBridgeDisconnected", "AiotSensorStale", "AiotServiceDown"),
                {},
            ),
            (
                "R06",
                "Stop edge-inference 60 s",
                ["edge-inference"],
                60,
                ("AiotEdgeInferenceStale", "AiotServiceDown"),
                {},
            ),
            (
                "R07",
                "Stop Kitchen UI, Grafana and Prometheus 30 s",
                ["demo-ui", "grafana", "prometheus"],
                30,
                (),
                {},
            ),
        ]
        for rid, scenario, services, hold, alerts, options in plan:
            if only and rid not in only:
                continue
            result = outage(lab, rid, scenario, services, hold, alerts, **options)
            if rid == "R07":
                result["observable_again_after_start_s"] = ui_and_observability_back()
            results.append(result)
            write_json(out / "recovery_cases.json", {"run_id": run["run_id"], "cases": results})
            time.sleep(20)
        if not only or "R05" in only:
            results.append(r05_malformed(lab))
            write_json(out / "recovery_cases.json", {"run_id": run["run_id"], "cases": results})
    finally:
        lab.stop()
        clear_retained([*lab.traffic.names, *lab.probe_nodes])
    log("done")


if __name__ == "__main__":
    main()

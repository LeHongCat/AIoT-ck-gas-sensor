"""Load benchmark: N replay nodes at 1 Hz against the running stack, stepped up until it saturates.

Each step starts N nodes (bench-rR-nNNN-001 ...) that behave like the simulator: every second each
node publishes a SensorReading and a GroundTruth from demo_data/demo_1hz.csv (each node from its
own row offset) and ACKs every command it receives. All nodes publish at the same moment each
second (burst), as in the first benchmark. While the step runs the harness measures, on the host
clock that also stamps observed_at:

* S01 events published per type; S02 predictions counted at MQTT, Kafka and InfluxDB;
* S03 gateway latency: reading published -> its prediction received back on MQTT;
* S04 Kafka latency: reading / ground truth / prediction published -> host Kafka consumer;
* S12 completeness, duplicates and out-of-order records per stream (event_id / sequence);
* D10/D11 prediction cadence per node (interval between two predictions of the same node);
* S15/D12 CPU and memory per container (docker stats every 5 s, mean and peak);

and afterwards reads Prometheus (S05 InfluxDB persistence, S08 controller sample age, S10
command -> ACK, S13 consumer lag series, S14 error counters) and InfluxDB (stored sequences).

Run it against the stack without the simulator (the ESP32 profile), from the repository root:

    docker compose --profile esp32 up -d --wait
    uv run python scripts/bench/load_test.py --nodes 1,10,20,40,60,100 --repeat 3 --out-dir docs/bench/run
"""

import argparse
import csv
import json
import multiprocessing as mp
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from confluent_kafka import Consumer

from aiot_pipeline.contracts import GroundTruth, SensorReading
from aiot_pipeline.topics import KafkaTopic, mqtt_topic

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ACK_FIELDS,
    DockerSampler,
    age_s,
    clear_retained,
    docker,
    influx_sequences,
    log,
    manifest,
    now_iso,
    prom,
    prom_range,
    prom_value,
    series_stats,
    service_action,
    summary,
    wait_healthy,
    write_json,
)

PYTHON_SERVICES = ("mqtt-kafka-bridge", "edge-inference", "influx-sink", "safety-controller")

ROW_STRIDE = 397  # node i starts i * 397 rows into the file


# ---- step processes (top level: Windows starts them with spawn) ----------------------------------


def run_nodes(args, prefix: str, nodes: int, seconds: int, out) -> None:
    """Publish readings + ground truth for every node each second; ACK every command."""
    rows = [[float(v) for v in r] for r in list(csv.reader(open(args.csv)))[1:]]
    names = [f"{prefix}-{i + 1:03d}" for i in range(nodes)]
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{prefix}-nodes")
    client.max_queued_messages_set(0)
    acks = {"commands": 0}

    def on_message(c, userdata, message) -> None:
        command = json.loads(message.payload)
        if not command["node_id"].startswith(prefix):
            return
        acks["commands"] += 1
        ack = {key: command.get(key, False) for key in ACK_FIELDS}
        ack.update(
            schema_version=1,
            event_id=str(uuid.uuid4()),
            node_id=command["node_id"],
            observed_at=now_iso(),
            sequence=command["sequence"],
            event_type="actuator.state",
            command_id=command["event_id"],
        )
        c.publish(
            mqtt_topic(command["node_id"], "actuator-state"), json.dumps(ack), qos=1, retain=True
        )

    client.on_message = on_message
    client.connect(args.mqtt_host, args.mqtt_port)
    client.subscribe("aiot/v1/nodes/+/commands", qos=1)
    client.loop_start()
    published = {"readings": 0, "ground-truth": 0}
    late_ticks = 0
    start = time.time() + 1
    for second in range(seconds):
        delay = start + second - time.time()
        if delay < 0:
            late_ticks += 1
        time.sleep(max(0.0, delay))
        for i, name in enumerate(names):
            if args.stagger:  # spread the nodes evenly over the second instead of one burst
                time.sleep(max(0.0, start + second + i / nodes - time.time()))
            row = rows[(i * ROW_STRIDE + second) % len(rows)]
            observed = datetime.now(UTC)
            reading = SensorReading(
                node_id=name, observed_at=observed, sequence=second, sensors=row[3:]
            )
            truth = GroundTruth(
                node_id=name,
                observed_at=observed,
                sequence=second,
                co_ppm=row[1],
                ethylene_ppm=row[2],
            )
            client.publish(mqtt_topic(name, "readings"), reading.model_dump_json(), qos=1)
            client.publish(mqtt_topic(name, "ground-truth"), truth.model_dump_json(), qos=1)
            published["readings"] += 1
            published["ground-truth"] += 1
    time.sleep(2)
    client.loop_stop()
    client.disconnect()
    out.put(
        {
            "published": published,
            "publisher_late_ticks": late_ticks,
            "commands_received_by_nodes": acks["commands"],
            "node_names": names,
        }
    )


def watch_mqtt(args, prefix: str, measure_from: float, measure_to_holder, stop, out) -> None:
    """Gateway latency, prediction cadence per node and Mosquitto's dropped-message counter."""
    latencies: list[float] = []
    arrivals: dict[str, list[float]] = {}
    event_ids: set[str] = set()
    duplicates = 0
    dropped: list[float] = []

    def on_message(c, userdata, message) -> None:
        nonlocal duplicates
        if message.topic.startswith("$SYS"):
            dropped.append(float(message.payload))
            return
        event = json.loads(message.payload)
        if not event["node_id"].startswith(prefix):
            return
        now = time.time()
        if event["event_id"] in event_ids:
            duplicates += 1
        event_ids.add(event["event_id"])
        arrivals.setdefault(event["node_id"], []).append(now)
        if now >= measure_from:
            latencies.append(age_s(event["observed_at"]))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{prefix}-watch")
    client.on_message = on_message
    client.connect(args.mqtt_host, args.mqtt_port)
    client.subscribe("aiot/v1/nodes/+/predictions", qos=0)
    client.subscribe("$SYS/broker/publish/messages/dropped", qos=0)
    client.loop_start()
    stop.wait()
    client.loop_stop()
    measure_to = measure_to_holder.value
    intervals = []
    max_gap = 0.0
    for times in arrivals.values():
        inside = [t for t in times if measure_from <= t <= measure_to]
        gaps = [b - a for a, b in zip(inside, inside[1:], strict=False)]
        intervals.extend(gaps)
        if inside:
            edges = [inside[0] - measure_from, measure_to - inside[-1]]
            max_gap = max([max_gap, *gaps, *edges])
    period = args.infer_every
    out.put(
        {
            "who": "mqtt",
            "gateway_latency": summary(latencies),
            "predictions_mqtt_in_window": sum(
                1 for times in arrivals.values() for t in times if measure_from <= t <= measure_to
            ),
            "predictions_mqtt_duplicates": duplicates,
            "prediction_interval": summary(intervals, scale=1.0, unit="s"),
            "prediction_interval_over_1_5x_pct": round(
                100 * sum(1 for g in intervals if g > 1.5 * period) / len(intervals), 2
            )
            if intervals
            else None,
            "prediction_max_gap_s": round(max_gap, 2),
            "nodes_with_predictions": len(arrivals),
            "mosquitto_dropped": (dropped[-1] - dropped[0]) if len(dropped) > 1 else 0,
        }
    )


def watch_kafka(args, prefix: str, measure_from: float, ready, stop, out) -> None:
    """Latency to a Kafka consumer on the host, and completeness/duplicates/order per stream."""
    consumer = Consumer(
        {
            "bootstrap.servers": args.kafka,
            "group.id": f"{prefix}-{uuid.uuid4().hex[:6]}",
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
            "fetch.wait.max.ms": 10,
        }
    )
    topics = [KafkaTopic.SENSOR, KafkaTopic.GROUND_TRUTH, KafkaTopic.PREDICTION]
    consumer.subscribe([str(t) for t in topics])
    while not consumer.assignment():
        consumer.poll(0.2)
    ready.set()
    stats = {
        str(t): {"lat": [], "ids": set(), "dup": 0, "ooo": 0, "last": {}, "seqs": {}}
        for t in topics
    }
    while not stop.is_set():
        message = consumer.poll(0.2)
        if message is None or message.error():
            continue
        event = json.loads(message.value())
        node = event["node_id"]
        if not node.startswith(prefix):
            continue
        s = stats[message.topic()]
        if event["event_id"] in s["ids"]:
            s["dup"] += 1
        s["ids"].add(event["event_id"])
        if event["sequence"] < s["last"].get(node, -1):
            s["ooo"] += 1
        s["last"][node] = event["sequence"]
        s["seqs"].setdefault(node, set()).add(event["sequence"])
        if time.time() >= measure_from:
            s["lat"].append(age_s(event["observed_at"]))
    consumer.close()
    out.put(
        {
            "who": "kafka",
            "kafka": {
                topic: {
                    "latency": summary(s["lat"]),
                    "records": len(s["ids"]),
                    "duplicates": s["dup"],
                    "out_of_order": s["ooo"],
                    "sequences": {node: sorted(seqs) for node, seqs in s["seqs"].items()},
                }
                for topic, s in stats.items()
            },
        }
    )


# ---- after the step ----------------------------------------------------------------------------


def prometheus_figures(at: float, window: int) -> dict:
    w = f"{window}s"

    def quantile(metric: str, q: float, match: str = "") -> float | None:
        return prom_value(
            f"histogram_quantile({q}, sum by (le) (increase({metric}_bucket{match}[{w}])))", at
        )

    def increase(expr: str) -> float:
        return prom_value(f"sum(increase({expr}[{w}]))", at) or 0.0

    figures = {
        "inference_mean_ms": prom_value(
            f"1000 * sum(increase(aiot_inference_seconds_sum[{w}]))"
            f" / sum(increase(aiot_inference_seconds_count[{w}]))",
            at,
        ),
        "predictions_made": increase("aiot_predictions_total"),
        "bridge_msgs_per_s": prom_value(
            f'sum(rate(aiot_mqtt_messages_received_total{{job="mqtt-kafka-bridge"}}[{w}]))', at
        ),
        "controller_sample_age_p50_s": quantile("aiot_control_loop_lag_seconds", 0.5),
        "controller_sample_age_p95_s": quantile("aiot_control_loop_lag_seconds", 0.95),
        "command_ack_p50_s": quantile("aiot_actuator_ack_latency_seconds", 0.5),
        "command_ack_p95_s": quantile("aiot_actuator_ack_latency_seconds", 0.95),
        "commands_published": increase("aiot_control_commands_total"),
        "commands_deduplicated": increase("aiot_control_commands_deduplicated_total"),
        "errors": {
            "ack_timeouts": increase("aiot_actuator_ack_timeouts_total"),
            "mqtt_invalid": increase("aiot_mqtt_invalid_messages_total"),
            "kafka_delivery_failures": increase("aiot_kafka_delivery_failures_total"),
            "sink_invalid": increase("aiot_sink_invalid_events_total"),
            "influx_write_failures": increase("aiot_influx_write_failures_total"),
            "dead_letter_records": increase(
                'kafka_topic_partition_current_offset{topic="gas.dead-letter.v1"}'
            ),
        },
        "influx_write_p95_s": quantile("aiot_influx_write_duration_seconds", 0.95),
    }
    for measurement in ("sensor_reading", "gas_ground_truth", "gas_prediction"):
        for q in (0.5, 0.95):
            figures[f"persist_{measurement}_p{int(q * 100)}_s"] = quantile(
                "aiot_pipeline_latency_seconds", q, f'{{measurement="{measurement}"}}'
            )
    figures["consumer_lag"] = {
        r["metric"].get("consumergroup"): series_stats(
            [(float(t), float(v)) for t, v in r["values"]]
        )
        for r in prom_range("max by (consumergroup) (aiot:consumer_lag:sum)", at - window, at, 5)
    }
    return figures


def completeness(expected: list[int], got: dict[str, list[int]], names: list[str]) -> dict:
    wanted = set(expected)
    found = sum(len(wanted & set(got.get(name, []))) for name in names)
    total = len(wanted) * len(names)
    return {
        "expected": total,
        "found": found,
        "pct": round(100 * found / total, 2) if total else None,
    }


def metric_series(service: str) -> int | None:
    """Number of Prometheus series the service exports (node_id labels are never removed)."""
    out = docker(
        "exec",
        f"aiot-gas-monitoring-{service}-1",
        "python",
        "-c",
        "import urllib.request; b = urllib.request.urlopen('http://localhost:8000/metrics').read()"
        ".decode(); print(sum(1 for l in b.splitlines() if l and not l.startswith('#')))",
    ).stdout.strip()
    return int(out) if out.isdigit() else None


def reset_services() -> dict:
    """Restart the Python services so a step starts without the series of earlier steps."""
    started = time.time()
    for service in PYTHON_SERVICES:
        service_action("restart", service)
    healthy = {s: wait_healthy(s) for s in PYTHON_SERVICES}
    time.sleep(15)  # consumer groups rejoin, first scrape
    return {
        "seconds": round(time.time() - started, 1),
        "healthy": all(v is not None for v in healthy.values()),
    }


def run_step(args, run: int, nodes: int) -> dict:
    prefix = f"bench-r{run}-n{nodes:03d}"
    seconds = args.warmup + args.step
    ctx = mp.get_context("spawn" if os.name == "nt" else "fork")
    stop, ready, results = ctx.Event(), ctx.Event(), ctx.Queue()
    measure_to_holder = ctx.Value("d", 0.0)
    initial_series = {s: metric_series(s) for s in PYTHON_SERVICES}
    initial_lag = {
        r["metric"].get("consumergroup"): float(r["value"][1])
        for r in prom("max by (consumergroup) (aiot:consumer_lag:sum)")
    }
    start = time.time() + 8  # spawn needs a few seconds to import the modules
    measure_from = start + args.warmup
    watchers = [
        ctx.Process(
            target=watch_mqtt, args=(args, prefix, measure_from, measure_to_holder, stop, results)
        ),
        ctx.Process(target=watch_kafka, args=(args, prefix, measure_from, ready, stop, results)),
    ]
    for process in watchers:
        process.start()
    ready.wait(60)
    delay = start - time.time()
    if delay > 0:
        time.sleep(delay)
    publisher = ctx.Process(target=run_nodes, args=(args, prefix, nodes, seconds, results))
    publisher.start()
    time.sleep(max(0.0, measure_from - time.time()))
    with DockerSampler() as sampler:
        publisher.join()
        measure_to = time.time() - 3
    measure_to_holder.value = measure_to
    time.sleep(args.drain)  # predictions and records still in flight
    stop.set()
    step = {
        "run": run,
        "nodes": nodes,
        "node_prefix": prefix,
        "burst": not args.stagger,
        "warmup_s": args.warmup,
        "measure_s": round(measure_to - measure_from, 1),
        "infer_every_s": args.infer_every,
        "initial_consumer_lag": initial_lag,
        "initial_metric_series": initial_series,
    }
    collected = [results.get(timeout=120) for _ in range(3)]
    for process in [*watchers, publisher]:
        process.join(30)
    for item in collected:
        item.pop("who", None)
        step.update(item)
    names = step.pop("node_names")
    measured_seconds = list(range(args.warmup, seconds))
    scheduled = [s for s in measured_seconds if s % args.infer_every == 0]
    step["events_per_s_published"] = round(sum(step["published"].values()) / (seconds), 2)
    step.update(prometheus_figures(measure_to, int(measure_to - measure_from)))
    time.sleep(2)
    kafka = step.pop("kafka")
    step["kafka"] = {}
    for topic, data in kafka.items():
        expect = scheduled if topic == str(KafkaTopic.PREDICTION) else measured_seconds
        sequences = data.pop("sequences")
        data["completeness_in_window"] = completeness(expect, sequences, names)
        step["kafka"][topic] = data
    step["influx"] = {}
    for measurement in ("sensor_reading", "gas_ground_truth", "gas_prediction"):
        stored = influx_sequences(measurement, prefix, measure_from - args.warmup)
        expect = scheduled if measurement == "gas_prediction" else measured_seconds
        step["influx"][measurement] = completeness(expect, stored, names)
    step["predictions_expected_by_schedule"] = len(scheduled) * nodes
    step["final_metric_series"] = {s: metric_series(s) for s in PYTHON_SERVICES}
    step["containers"] = sampler.summary()
    step["retained_nodes"] = names
    return step


def headline(step: dict) -> dict:
    kafka_reading = step["kafka"][str(KafkaTopic.SENSOR)]["latency"]
    return {
        "run": step["run"],
        "nodes": step["nodes"],
        "events/s": step["events_per_s_published"],
        "pred_made": step["predictions_made"],
        "gw_p95_ms": step["gateway_latency"].get("p95_ms"),
        "kafka_p95_ms": kafka_reading.get("p95_ms"),
        "persist_p95_s": step.get("persist_sensor_reading_p95_s"),
        "stored_read_%": step["influx"]["sensor_reading"]["pct"],
        "stored_pred_%": step["influx"]["gas_prediction"]["pct"],
        "dropped": step["mosquitto_dropped"],
        "edge_cpu": step["containers"].get("edge-inference", {}).get("cpu_cores_mean"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nodes", default="1,10,20,40,60,100", help="comma-separated node counts")
    parser.add_argument("--repeat", type=int, default=1, help="independent sweeps")
    parser.add_argument("--warmup", type=int, default=30, help="seconds published before measuring")
    parser.add_argument("--step", type=int, default=120, help="seconds measured per step")
    parser.add_argument("--rest", type=int, default=30, help="idle seconds between steps")
    parser.add_argument(
        "--drain", type=int, default=5, help="seconds to wait for in-flight records"
    )
    parser.add_argument("--infer-every", type=int, default=1, help="gateway TABM_INFER_EVERY_S")
    parser.add_argument("--label", default="default", help="configuration label for the results")
    parser.add_argument(
        "--stagger", action="store_true", help="spread node publishes over each second"
    )
    parser.add_argument(
        "--reset-services",
        action="store_true",
        help="restart the Python services before every step (clean metric and consumer state)",
    )
    parser.add_argument("--csv", default="demo_data/demo_1hz.csv")
    parser.add_argument("--mqtt-host", default="localhost")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--kafka", default="localhost:29092")
    parser.add_argument("--out-dir", default="docs/bench/run")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    run_manifest = manifest("load_test", vars(args))
    write_json(out_dir / f"run_manifest_load_{args.label}.json", run_manifest)
    steps = []
    retained: list[str] = []
    first = True
    for run in range(1, args.repeat + 1):
        for nodes in (int(n) for n in args.nodes.split(",")):
            if not first:
                time.sleep(args.rest)
            first = False
            reset = reset_services() if args.reset_services else None
            log(
                f"[{args.label}] run {run}: {nodes} nodes, {args.warmup}+{args.step} s, reset={reset}"
            )
            step = run_step(args, run, nodes)
            step["services_reset_before_step"] = reset
            step["label"] = args.label
            retained.extend(step.pop("retained_nodes"))
            steps.append(step)
            log(json.dumps(headline(step)))
            write_json(
                out_dir / f"load_{args.label}.json",
                {"run_id": run_manifest["run_id"], "steps": steps},
            )
    clear_retained(retained)
    log(f"done; cleared {len(retained)} retained ACK topics")


if __name__ == "__main__":
    main()

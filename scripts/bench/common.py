"""Shared helpers for the system benchmarks in scripts/bench (load, control, recovery, UI, startup).

Every run writes a manifest (code, model, data, machine and container limits) next to its results,
measures percentiles from raw samples, and reads Prometheus, InfluxDB and `docker stats` the same
way, so the result files of different scripts can be compared.
"""

import csv
import hashlib
import io
import json
import math
import platform
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from aiot_pipeline.topics import mqtt_topic

ROOT = Path(__file__).resolve().parents[2]
MQTT_HOST, MQTT_PORT = "localhost", 1883
KAFKA = "localhost:29092"
PROMETHEUS = "http://localhost:9090"
INFLUX = "http://localhost:8086"
INFLUX_TOKEN = "dev-super-secret-token-change-me"
INFLUX_BUCKET = "gas_monitoring"
PROJECT = "aiot-gas-monitoring"
ACK_FIELDS = (
    "mode",
    "source",
    "fan_on",
    "exhaust_on",
    "gas_valve_closed",
    "alarm_on",
    "suppression_on",
    "alarm_muted",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def age_s(observed_at: str) -> float:
    return time.time() - datetime.fromisoformat(observed_at).timestamp()


def summary(values: list[float], scale: float = 1000.0, unit: str = "ms") -> dict:
    """n, mean, p50, p95, p99, max of raw samples (seconds in, `unit` out).

    Percentiles: statistics.quantiles(n=100, method="inclusive") on the raw samples (linear
    interpolation between order statistics, numpy's default), never outside [min, max].
    """
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    q = (
        statistics.quantiles(ordered, n=100, method="inclusive")
        if len(ordered) > 1
        else [ordered[0]] * 99
    )
    return {
        "n": len(ordered),
        f"mean_{unit}": round(statistics.fmean(ordered) * scale, 2),
        f"p50_{unit}": round(q[49] * scale, 2),
        f"p95_{unit}": round(q[94] * scale, 2),
        f"p99_{unit}": round(q[98] * scale, 2),
        f"max_{unit}": round(ordered[-1] * scale, 2),
    }


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


def log(message: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {message}", flush=True)


# ---- Prometheus / InfluxDB ---------------------------------------------------------------------


def prom(expr: str, at: float | None = None) -> list:
    params = {"query": expr}
    if at is not None:
        params["time"] = f"{at:.3f}"
    url = f"{PROMETHEUS}/api/v1/query?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)["data"]["result"]


def prom_value(expr: str, at: float | None = None) -> float | None:
    result = prom(expr, at)
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) else round(value, 6)


def prom_range(expr: str, start: float, end: float, step: float = 5.0) -> list[dict]:
    params = {"query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step}"}
    url = f"{PROMETHEUS}/api/v1/query_range?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)["data"]["result"]


def series_stats(values: list[tuple[float, float]]) -> dict:
    if not values:
        return {"n": 0}
    numbers = [v for _, v in values]
    return {
        "n": len(numbers),
        "mean": round(statistics.fmean(numbers), 2),
        "peak": round(max(numbers), 2),
        "last": round(numbers[-1], 2),
    }


def influx_rows(flux: str) -> list[dict]:
    request = urllib.request.Request(
        f"{INFLUX}/api/v2/query?org=aiot",
        data=flux.encode(),
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        text = response.read().decode()
    return [row for row in csv.DictReader(io.StringIO(text)) if row.get("_value") not in (None, "")]


def influx_sequences(measurement: str, node_prefix: str, start: float) -> dict[str, list[int]]:
    """Stored `sequence` values per node for nodes starting with `node_prefix`."""
    flux = (
        'import "strings"\n'
        f'from(bucket: "{INFLUX_BUCKET}") |> range(start: {int(start) - 30}) '
        f'|> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "sequence" '
        f'and strings.hasPrefix(v: r.node_id, prefix: "{node_prefix}")) '
        '|> keep(columns: ["node_id", "_value"])'
    )
    stored: dict[str, list[int]] = {}
    for row in influx_rows(flux):
        stored.setdefault(row["node_id"], []).append(int(float(row["_value"])))
    return stored


# ---- docker ------------------------------------------------------------------------------------


def docker(*args: str, check: bool = False, timeout: float = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check
        )
    except subprocess.TimeoutExpired as error:
        # A sleeping host makes the clock jump; report an empty result instead of aborting a run.
        return subprocess.CompletedProcess(error.cmd, 124, stdout="", stderr=f"timeout: {error}")


def container(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def service_action(action: str, service: str) -> dict:
    """docker stop/start/restart of one service, with wall-clock marks around the call."""
    issued = time.time()
    result = docker(action, container(service))
    return {
        "action": action,
        "service": service,
        "issued_at": issued,
        "returned_at": time.time(),
        "ok": result.returncode == 0,
        "stderr": result.stderr.strip()[-300:],
    }


def wait_healthy(service: str, timeout: float = 180) -> float | None:
    """Seconds until `docker inspect` reports healthy (or running, if no healthcheck)."""
    started = time.time()
    while time.time() - started < timeout:
        out = docker(
            "inspect",
            "-f",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container(service),
        ).stdout.split()
        if out and out[0] == "running" and (len(out) == 1 or out[1] == "healthy"):
            return time.time() - started
        time.sleep(0.25)
    return None


def _mib(text: str) -> float:
    text = text.strip()
    for unit, factor in (
        ("KiB", 1 / 1024),
        ("MiB", 1),
        ("GiB", 1024),
        ("kB", 1 / 1024),
        ("B", 1 / 1048576),
    ):
        if text.endswith(unit):
            return float(text[: -len(unit)]) * factor
    return float("nan")


def sample_docker(stop: threading.Event, samples: dict, interval: float = 5.0) -> None:
    """Every `interval` s: CPU (cores = CPUPerc / 100) and memory (MiB) of every container."""
    while not stop.is_set():
        output = docker(
            "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
        ).stdout
        stamp = time.time()
        for line in output.splitlines():
            name, cpu, memory = line.split("\t")
            if not name.startswith(PROJECT):
                continue
            name = name.removeprefix(f"{PROJECT}-").removesuffix("-1")
            samples.setdefault(name, []).append(
                (stamp, float(cpu.rstrip("%")) / 100, _mib(memory.split("/")[0]))
            )
        stop.wait(interval)


def summarize_docker(samples: dict) -> dict:
    out = {}
    for name, values in sorted(samples.items()):
        cpu = [c for _, c, _ in values]
        mem = [m for _, _, m in values]
        out[name] = {
            "samples": len(values),
            "cpu_cores_mean": round(statistics.fmean(cpu), 3),
            "cpu_cores_peak": round(max(cpu), 3),
            "mem_mib_mean": round(statistics.fmean(mem), 1),
            "mem_mib_peak": round(max(mem), 1),
        }
    out["_total"] = {
        "cpu_cores_mean": round(sum(v["cpu_cores_mean"] for v in out.values()), 3),
        "mem_mib_mean": round(sum(v["mem_mib_mean"] for k, v in out.items() if k != "_total"), 1),
    }
    return out


class DockerSampler:
    def __init__(self, interval: float = 5.0) -> None:
        self.samples: dict = {}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=sample_docker, args=(self.stop_event, self.samples, interval), daemon=True
        )

    def __enter__(self) -> "DockerSampler":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop_event.set()
        self.thread.join(30)

    def summary(self) -> dict:
        return summarize_docker(self.samples)


def clock_offset_ms(tries: int = 15) -> dict:
    """Container clock minus host clock, from Prometheus' time() over HTTP (smallest round trip).

    All containers of Docker Desktop share the clock of its VM, so this bounds the error of the
    latency histograms the services export (container clock minus a host-stamped observed_at).
    """
    best = None
    url = f"{PROMETHEUS}/api/v1/query?query=time()"
    for _ in range(tries):
        before = time.time()
        with urllib.request.urlopen(url, timeout=10) as response:
            server = float(json.load(response)["data"]["result"][0])
        after = time.time()
        rtt = after - before
        if best is None or rtt < best[0]:
            best = (rtt, server - (before + after) / 2)
    return {
        "method": "Prometheus time() over HTTP",
        "offset_ms": round(best[1] * 1000, 1),
        "uncertainty_ms": round(best[0] * 500, 1),
    }


# ---- manifest ----------------------------------------------------------------------------------


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def container_config() -> dict:
    names = docker("ps", "--format", "{{.Names}}").stdout.split()
    config = {}
    for name in sorted(n for n in names if n.startswith(PROJECT)):
        info = json.loads(docker("inspect", name).stdout)[0]
        host = info["HostConfig"]
        env = dict(e.split("=", 1) for e in info["Config"].get("Env") or [] if "=" in e)
        config[name.removeprefix(f"{PROJECT}-").removesuffix("-1")] = {
            "image": info["Config"]["Image"],
            "cpus": (host.get("NanoCpus") or 0) / 1e9 or None,
            "memory_mib": (host.get("Memory") or 0) / 1048576 or None,
            "env": {
                k: v
                for k, v in env.items()
                if k.startswith(
                    ("TABM_", "EDGE_", "INFLUX_BATCH", "INFLUX_FLUSH", "DIRECT_", "DEMO_")
                )
            },
        }
    return config


def manifest(tool: str, parameters: dict) -> dict:
    info = json.loads(docker("info", "--format", "{{json .}}").stdout or "{}")
    model_files = [
        ROOT / "model" / "TabM_seed42_int8_static.onnx",
        ROOT / "model" / "preprocessing.json",
        ROOT / "model" / "thresholds.json",
        ROOT / "wokwi/esp32-replay/esp32-replay/model/tabm_int8.bin",
        ROOT / "wokwi/esp32-replay/esp32-replay/model/tabm_int8.json",
    ]
    dataset = ROOT / "demo_data" / "demo_1hz.csv"
    return {
        "run_id": f"{tool}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
        "started_utc": now_iso(),
        "tool": tool,
        "parameters": parameters,
        "code": {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "uncommitted_files": _git("status", "--porcelain").splitlines(),
        },
        "model": {
            str(p.relative_to(ROOT)): {
                "sha256": sha256(p),
                "bytes": p.stat().st_size if p.exists() else None,
            }
            for p in model_files
        },
        "dataset": {"path": "demo_data/demo_1hz.csv", "sha256": sha256(dataset)},
        "machine": {
            "host_platform": platform.platform(),
            "host_python": sys.version.split()[0],
            "docker_server": info.get("ServerVersion"),
            "docker_os": info.get("OperatingSystem"),
            "docker_kernel": info.get("KernelVersion"),
            "docker_cpus": info.get("NCPU"),
            "docker_mem_gib": round((info.get("MemTotal") or 0) / 2**30, 2),
        },
        "clock_offset": clock_offset_ms(),
        "containers": container_config(),
        "notes": [
            "Latencies measured by the harness use the host clock that also stamps observed_at.",
            "Histograms exported by containers subtract observed_at from the container clock; "
            "see clock_offset.",
            "docker stats CPUPerc/100 = cores; memory = container working set reported by docker.",
        ],
    }


# ---- MQTT test device --------------------------------------------------------------------------


class Device:
    """Plays one or more actuator nodes: records every command and acknowledges it.

    `mode` changes the ACK for fault tests: "ack" (normal), "silent" (no ACK), "late" (ACK after
    `delay` s), "wrong_id" (random command_id), "wrong_node" (ACK under another node's id).
    """

    def __init__(self, prefix: str, client_id: str | None = None) -> None:
        self.prefix = prefix
        self.mode = "ack"
        self.delay = 0.0
        self.other_node = f"{prefix}-other"
        self.commands: list[tuple[float, dict]] = []
        self.acks: list[tuple[float, dict]] = []
        self.lock = threading.Lock()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or f"{prefix}-device-{uuid.uuid4().hex[:6]}",
            protocol=mqtt.MQTTv5,
        )
        self.client.on_message = self._on_message
        self.client.on_connect = lambda c, *a: c.subscribe("aiot/v1/nodes/+/commands", qos=1)

    def start(self) -> "Device":
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        self.client.loop_start()
        time.sleep(0.5)
        return self

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def _on_message(self, client, userdata, message) -> None:
        received = time.time()
        command = json.loads(message.payload)
        if not command.get("node_id", "").startswith(self.prefix):
            return
        with self.lock:
            self.commands.append((received, command))
        if self.mode == "silent":
            return
        if self.mode == "late":
            threading.Timer(self.delay, self._ack, args=(command,)).start()
        else:
            self._ack(command)

    def _ack(self, command: dict) -> None:
        node = command["node_id"] if self.mode != "wrong_node" else self.other_node
        ack = {key: command.get(key, False) for key in ACK_FIELDS}
        ack.update(
            schema_version=1,
            event_id=str(uuid.uuid4()),
            node_id=node,
            observed_at=now_iso(),
            sequence=command["sequence"],
            event_type="actuator.state",
            command_id=command["event_id"] if self.mode != "wrong_id" else str(uuid.uuid4()),
        )
        self.client.publish(mqtt_topic(node, "actuator-state"), json.dumps(ack), qos=1, retain=True)
        with self.lock:
            self.acks.append((time.time(), ack))

    def commands_after(self, node: str, since: float) -> list[tuple[float, dict]]:
        with self.lock:
            return [(t, c) for t, c in self.commands if c["node_id"] == node and t >= since]

    def wait_command(
        self, node: str, since: float, timeout: float = 10.0
    ) -> tuple[float, dict] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.commands_after(node, since)
            if found:
                return found[0]
            time.sleep(0.005)
        return None


def clear_retained(prefix_nodes: list[str], suffixes=("actuator-state",)) -> None:
    """Remove retained messages the benchmark left on the broker."""
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"bench-cleanup-{uuid.uuid4().hex[:6]}"
    )
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    for node in prefix_nodes:
        for suffix in suffixes:
            client.publish(mqtt_topic(node, suffix), b"", qos=1, retain=True).wait_for_publish(5)
    client.loop_stop()
    client.disconnect()

"""S06 / S07: one node published from the host -> Kitchen UI backend (SSE client) and browser DOM.

The kitchen screen must be pinned to the benchmark node, so it shows exactly what we publish:

    $env:DEMO_NODE_ID = "bench-ui-001"; docker compose --profile esp32 up -d --wait demo-ui
    uv run --with websocket-client python scripts/bench/ui_latency.py --out-dir docs/bench/run

Measured on the host clock that also stamps observed_at (same machine, same clock):

* sse_*: observed_at -> an SSE client on the host receives the first snapshot carrying that
  reading / prediction (backend consumes Kafka, renders the snapshot, pushes it every 200 ms);
* dom_*: observed_at -> app.js finished renderState() with that record (headless Edge, CDP);
* frame_*: observed_at -> two requestAnimationFrame callbacks later (≈ the frame that shows it).
"""

import argparse
import csv
import json
import os
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import websocket

from aiot_pipeline.contracts import GroundTruth, SensorReading
from aiot_pipeline.topics import mqtt_topic

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    MQTT_HOST,
    MQTT_PORT,
    ROOT,
    clear_retained,
    log,
    manifest,
    summary,
    write_json,
)

NODE = "bench-ui-001"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
INSTRUMENT = """
(() => {
  window.__lat = { reading: [], prediction: [], frame_reading: [], frame_prediction: [] };
  const seen = { reading: -1, prediction: -1 };
  const original = renderState;
  renderState = function (state) {
    original(state);
    const done = Date.now();
    for (const [kind, event] of [["reading", state.latest.sensor], ["prediction", state.latest.prediction]]) {
      if (!event || event.sequence === seen[kind]) continue;
      seen[kind] = event.sequence;
      const observed = Date.parse(event.observed_at);
      window.__lat[kind].push([event.sequence, done - observed]);
      requestAnimationFrame(() => requestAnimationFrame(() => window.__lat["frame_" + kind].push([event.sequence, Date.now() - observed])));
    }
  };
  return "instrumented";
})()
"""


def publish(stop: threading.Event, seconds: int, marks: dict) -> None:
    rows = [
        [float(v) for v in r] for r in list(csv.reader(open(ROOT / "demo_data/demo_1hz.csv")))[1:]
    ]
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"bench-ui-pub-{uuid.uuid4().hex[:4]}"
    )
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    start = time.time() + 1
    for seq in range(seconds):
        if stop.is_set():
            break
        time.sleep(max(0.0, start + seq - time.time()))
        row = rows[seq % len(rows)]
        observed = datetime.now(UTC)
        reading = SensorReading(node_id=NODE, observed_at=observed, sequence=seq, sensors=row[3:])
        truth = GroundTruth(
            node_id=NODE, observed_at=observed, sequence=seq, co_ppm=row[1], ethylene_ppm=row[2]
        )
        client.publish(mqtt_topic(NODE, "readings"), reading.model_dump_json(), qos=1)
        client.publish(mqtt_topic(NODE, "ground-truth"), truth.model_dump_json(), qos=1)
        marks[seq] = time.time()
    client.loop_stop()
    client.disconnect()


def sse(stop: threading.Event, samples: dict, backend: list) -> None:
    seen = {"reading": -1, "prediction": -1}
    with urllib.request.urlopen("http://localhost:8080/api/stream", timeout=60) as response:
        for raw in response:
            if stop.is_set():
                break
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            received = time.time()
            state = json.loads(line[5:])
            if state.get("node_id") != NODE:
                continue
            for kind, event in (
                ("reading", state["latest"].get("sensor")),
                ("prediction", state["latest"].get("prediction")),
            ):
                if event and event["sequence"] != seen[kind]:
                    seen[kind] = event["sequence"]
                    observed = datetime.fromisoformat(event["observed_at"]).timestamp()
                    samples[kind].append((event["sequence"], received - observed))
            stream = state.get("stream", {})
            backend.append({k: v.get("latency_ms") for k, v in stream.items()})


class Browser:
    def __init__(self, port: int = 9440) -> None:
        profile = Path(os.environ.get("TEMP", "/tmp")) / f"bench-edge-{uuid.uuid4().hex[:6]}"
        self.proc = subprocess.Popen(
            [
                EDGE,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                # app.js renders in requestAnimationFrame: keep a headless page from being treated as hidden
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--window-size=1600,940",
                "about:blank",
            ],
        )
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=60, suppress_origin=True
        )
        self.seq = 0

    def cmd(self, method: str, **params):
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                return message.get("result", {})

    def js(self, expression: str):
        result = self.cmd(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self.ws.close()
        self.proc.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--measure", type=int, default=120)
    parser.add_argument("--out-dir", default="docs/bench/run")
    args = parser.parse_args()
    state = json.load(urllib.request.urlopen("http://localhost:8080/api/state"))
    run = manifest("ui_latency", vars(args))
    if not (
        state.get("node_pinned")
        and run["containers"].get("demo-ui", {}).get("env", {}).get("DEMO_NODE_ID") == NODE
    ):
        raise SystemExit(f"Pin the kitchen screen first: DEMO_NODE_ID={NODE} (see the docstring)")
    write_json(Path(args.out_dir) / "run_manifest_ui.json", run)

    stop = threading.Event()
    marks: dict = {}
    samples = {"reading": [], "prediction": []}
    backend: list = []
    total = args.warmup + args.measure + 5
    threads = [
        threading.Thread(target=publish, args=(stop, total, marks), daemon=True),
        threading.Thread(target=sse, args=(stop, samples, backend), daemon=True),
    ]
    browser = Browser()
    browser.cmd("Page.enable")
    browser.cmd("Emulation.setFocusEmulationEnabled", enabled=True)
    browser.cmd("Page.bringToFront")
    browser.cmd("Page.navigate", url="http://localhost:8080/")
    time.sleep(6)
    log(browser.js(INSTRUMENT))
    for thread in threads:
        thread.start()
    time.sleep(total + 3)
    stop.set()
    dom = browser.js("JSON.stringify(window.__lat)")
    browser.close()
    dom = json.loads(dom)
    first = args.warmup
    last = args.warmup + args.measure

    def pick(pairs) -> list[float]:
        return [value for seq, value in pairs if first <= seq < last]

    result = {
        "run_id": run["run_id"],
        "node": NODE,
        "measured_sequences": [first, last - 1],
        "sse_reading": summary(pick(samples["reading"])),
        "sse_prediction": summary(pick(samples["prediction"])),
        "dom_reading": summary([v / 1000 for v in pick(dom["reading"])]),
        "dom_prediction": summary([v / 1000 for v in pick(dom["prediction"])]),
        "frame_reading": summary([v / 1000 for v in pick(dom["frame_reading"])]),
        "frame_prediction": summary([v / 1000 for v in pick(dom["frame_prediction"])]),
        "backend_ema_ms_last": backend[-1] if backend else None,
        "published": len(marks),
    }
    write_json(Path(args.out_dir) / "ui_latency.json", result)
    clear_retained([NODE])
    log(json.dumps({k: v for k, v in result.items() if k.startswith(("sse", "dom", "frame"))}))


if __name__ == "__main__":
    main()

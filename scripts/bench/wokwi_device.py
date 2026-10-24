"""D07 / D08 / D10 / D11 (and optionally R04) for the ESP32-S3 node running in Wokwi.

Start the pipeline in ESP32 mode and the Wokwi simulation (VS Code, wokwi/esp32-replay/esp32-replay,
*Wokwi: Start Simulator*), wait until the board publishes, then from the repository root:

    ./scripts/demo.ps1 esp32
    uv run python scripts/bench/wokwi_device.py --minutes 15 --out-dir docs/bench/run
    uv run python scripts/bench/wokwi_device.py --minutes 6 --broker-outage 20 --out-dir docs/bench/run

The board's own clock (millis) measures inference_ms; the host clock measures arrival intervals.
Wokwi simulates the CPU, so simulated time can run slower than wall time: the ratio is reported
from the reading sequence numbers (one per simulated second) against the host clock.
"""

import argparse
import json
import statistics
import sys
import threading
import time
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    MQTT_HOST,
    MQTT_PORT,
    log,
    manifest,
    service_action,
    wait_healthy,
    write_json,
)


def quant(values: list[float]) -> dict:
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
        "p50": round(q[49], 3),
        "p95": round(q[94], 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", default="esp32-edge-01")
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument(
        "--period", type=float, default=5.0, help="configured inference period (kInferEveryS)"
    )
    parser.add_argument(
        "--broker-outage", type=float, default=0, help="stop Mosquitto this many s half-way (R04)"
    )
    parser.add_argument(
        "--wait", type=float, default=0, help="minutes to wait for the board before recording"
    )
    parser.add_argument("--out-dir", default="docs/bench/run")
    args = parser.parse_args()
    run = manifest("wokwi_device", vars(args))
    readings: list[tuple[float, int]] = []
    predictions: list[tuple[float, dict]] = []
    diagnostics: list[tuple[float, dict]] = []
    lock = threading.Lock()

    def on_message(client, userdata, message) -> None:
        now = time.time()
        try:
            event = json.loads(message.payload)
        except ValueError:
            return
        suffix = message.topic.rsplit("/", 1)[-1]
        with lock:
            if suffix == "readings":
                readings.append((now, int(event["sequence"])))
            elif suffix == "predictions" and event.get("model_name") != "bench-ctl":
                predictions.append((now, event))
            elif suffix == "diagnostics":
                diagnostics.append((now, event))

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"bench-wokwi-{uuid.uuid4().hex[:4]}"
    )
    client.on_message = on_message
    client.on_connect = lambda c, *a: c.subscribe(f"aiot/v1/nodes/{args.node}/+", qos=1)
    client.reconnect_delay_set(1, 2)
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    if args.wait:
        log(f"waiting up to {args.wait:g} min for the first reading of {args.node}")
        deadline = time.time() + args.wait * 60
        while not readings and time.time() < deadline:
            time.sleep(1)
    start = time.time()
    end = start + args.minutes * 60
    outage = None
    log(f"recording {args.node} for {args.minutes:g} min")
    while time.time() < end:
        if args.broker_outage and outage is None and time.time() >= start + (end - start) / 2:
            stop = service_action("stop", "mosquitto")
            time.sleep(args.broker_outage)
            back = service_action("start", "mosquitto")
            wait_healthy("mosquitto")
            outage = {"stop": stop, "start": back}
            log("broker outage done")
        time.sleep(1)
    client.loop_stop()
    client.disconnect()

    if not readings:
        raise SystemExit(f"No reading from {args.node}: is the Wokwi simulation running?")
    seqs = [s for _, s in readings]
    wall = readings[-1][0] - readings[0][0]
    sim = seqs[-1] - seqs[0]
    pred_times = [t for t, _ in predictions]
    pred_seqs = [int(e["sequence"]) for _, e in predictions]
    intervals = [b - a for a, b in zip(pred_times, pred_times[1:], strict=False)]
    seq_steps = [b - a for a, b in zip(pred_seqs, pred_seqs[1:], strict=False)]
    expected = sim / args.period + 1
    result = {
        "run_id": run["run_id"],
        "node": args.node,
        "recorded_s": round(time.time() - start, 1),
        "readings": len(readings),
        "simulated_s_per_wall_s": round(sim / wall, 3) if wall else None,
        "reading_sequence_gaps": sum(1 for a, b in zip(seqs, seqs[1:], strict=False) if b - a != 1),
        "predictions": len(predictions),
        "predictions_expected_by_schedule": round(expected, 1),
        "model": sorted(
            {f"{e.get('model_name')}/{e.get('model_version')}" for _, e in predictions}
        ),
        "D08_inference_ms_on_board": quant(
            [float(e["inference_ms"]) for _, e in predictions if e.get("inference_ms") is not None]
        ),
        "D10_prediction_interval_wall_s": quant(intervals),
        "D10_prediction_sequence_step": quant([float(s) for s in seq_steps]),
        "D11_over_1_5x_period_pct": round(
            100 * sum(1 for g in intervals if g > 1.5 * args.period) / len(intervals), 2
        )
        if intervals
        else None,
        "D11_max_gap_wall_s": round(max(intervals), 2) if intervals else None,
        "D11_skipped_windows_reported": diagnostics[-1][1].get("skipped_windows")
        if diagnostics
        else None,
        "D07_memory": {
            key: min(d[key] for _, d in diagnostics if key in d)
            for key in (
                "free_heap",
                "min_free_heap",
                "max_alloc_heap",
                "min_free_psram",
                "inference_stack_free_min",
                "loop_stack_free_min",
            )
            if any(key in d for _, d in diagnostics)
        },
        "D07_static": {
            k: diagnostics[-1][1].get(k)
            for k in ("heap_size", "psram_size", "inference_stack_size", "cpu_mhz")
        }
        if diagnostics
        else None,
        "diagnostics_messages": len(diagnostics),
    }
    if outage:
        back = outage["start"]["returned_at"]
        after = [t for t, _ in readings if t > back]
        before = [s for t, s in readings if t < outage["stop"]["issued_at"]]
        resumed = [s for t, s in readings if t > back]
        result["R04_broker_outage"] = {
            "held_s": args.broker_outage,
            "first_reading_after_start_s": round(after[0] - back, 2) if after else None,
            "last_seq_before": before[-1] if before else None,
            "first_seq_after": resumed[0] if resumed else None,
            "note": "the firmware blocks in connectMqtt() while the broker is down, so the replay pauses (no sequence gap) instead of losing readings",
        }
    write_json(Path(args.out_dir) / f"wokwi_device{'_r04' if outage else ''}.json", result)
    write_json(Path(args.out_dir) / "run_manifest_wokwi.json", run)
    log(json.dumps({k: v for k, v in result.items() if k != "run_id"}, ensure_ascii=False))


if __name__ == "__main__":
    main()

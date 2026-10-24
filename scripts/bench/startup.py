"""S16: cold start of the demo stack, repeated: compose up -> healthy -> first data on each stage.

Each run does `docker compose --profile demo down` (volumes kept) and then times, from the moment
`up -d --wait` is issued:

* compose_ready: `up -d --wait` returned (every healthcheck passed);
* first_reading_ui / first_prediction_ui: the Kitchen UI backend (/api/state, polled every 0.2 s)
  holds a reading / prediction stamped after the start;
* first_reading_influx: InfluxDB holds a simulator reading stamped after the start.

    uv run python scripts/bench/startup.py --runs 3 --out-dir docs/bench/run
"""

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, influx_rows, log, manifest, write_json  # noqa: E402


def compose(*args: str, timeout: float = 600) -> float:
    started = time.time()
    subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return time.time() - started


def stamp(event: dict | None) -> float | None:
    return None if not event else datetime.fromisoformat(event["observed_at"]).timestamp()


def one_run(index: int) -> dict:
    compose("--profile", "demo", "--profile", "esp32", "down")
    time.sleep(5)
    start = time.time()
    proc = subprocess.Popen(
        ["docker", "compose", "--profile", "demo", "up", "-d", "--wait"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marks: dict[str, float] = {}
    while time.time() - start < 300 and len(marks) < 4:
        if "compose_ready" not in marks and proc.poll() is not None:
            marks["compose_ready"] = time.time() - start
        try:
            state = json.load(urllib.request.urlopen("http://localhost:8080/api/state", timeout=1))
            for key, event in (
                ("first_reading_ui", state["latest"].get("sensor")),
                ("first_prediction_ui", state["latest"].get("prediction")),
            ):
                observed = stamp(event)
                if key not in marks and observed and observed >= start:
                    marks[key] = time.time() - start
        except Exception:
            pass
        if "first_reading_influx" not in marks and "first_reading_ui" in marks:
            try:
                rows = influx_rows(
                    'from(bucket: "gas_monitoring") |> range(start: -5m) '
                    '|> filter(fn: (r) => r._measurement == "sensor_reading" and r._field == "sequence") '
                    '|> last() |> keep(columns: ["_time", "_value"])'
                )
                if (
                    rows
                    and datetime.fromisoformat(rows[0]["_time"].replace("Z", "+00:00")).timestamp()
                    >= start
                ):
                    marks["first_reading_influx"] = time.time() - start
            except Exception:
                pass
        time.sleep(0.2)
    proc.wait(300)
    marks.setdefault("compose_ready", time.time() - start)
    result = {"run": index, **{k: round(v, 2) for k, v in marks.items()}}
    log(json.dumps(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out-dir", default="docs/bench/run")
    args = parser.parse_args()
    runs = [one_run(i + 1) for i in range(args.runs)]
    run = manifest("startup", vars(args))
    write_json(Path(args.out_dir) / "startup.json", {"run_id": run["run_id"], "runs": runs})
    write_json(Path(args.out_dir) / "run_manifest_startup.json", run)


if __name__ == "__main__":
    main()

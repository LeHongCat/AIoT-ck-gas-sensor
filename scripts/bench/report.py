"""Turn the JSON results of scripts/bench into the Markdown tables of the benchmark report.

    uv run python scripts/bench/report.py docs/bench/system-2026-10-24 > tables.md

Load figures are given as the median of the repeated runs, with min-max in brackets when there is
more than one run. Nothing here re-measures: it only formats the stored results.
"""

import json
import statistics
import sys
from pathlib import Path

SENSOR, TRUTH, PRED = "gas.sensor.readings.v1", "gas.ground-truth.v1", "gas.predictions.v1"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def fmt(values: list, digits: int = 1, unit: str = "") -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "—"
    med = statistics.median(values)
    text = f"{med:.{digits}f}{unit}"
    if len(values) > 1 and max(values) != min(values):
        text += f" ({min(values):.{digits}f}–{max(values):.{digits}f})"
    return text


def by_nodes(steps: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for step in steps:
        grouped.setdefault(step["nodes"], []).append(step)
    return dict(sorted(grouped.items()))


def load_tables(name: str, data: dict) -> str:
    steps = data["steps"]
    rows = by_nodes(steps)
    runs = len({s["run"] for s in steps})
    out = [f"### Thử tải `{name}` — {runs} lượt, trung vị (min–max)\n"]
    out.append(
        "| Nút | Sự kiện vào/s | Dự đoán/s (MQTT) | Gateway p50 / p95 (ms) | Kafka p95 reading (ms) "
        "| Lưu InfluxDB p50 / p95 reading (s) | Lưu InfluxDB p95 dự đoán (s) |"
    )
    out.append("|---:|---:|---:|---:|---:|---:|---:|")
    for nodes, group in rows.items():
        out.append(
            f"| {nodes} | {fmt([s['events_per_s_published'] for s in group])} "
            f"| {fmt([s['predictions_mqtt_in_window'] / s['measure_s'] for s in group])} "
            f"| {fmt([s['gateway_latency'].get('p50_ms') for s in group], 0)} / "
            f"{fmt([s['gateway_latency'].get('p95_ms') for s in group], 0)} "
            f"| {fmt([s['kafka'][SENSOR]['latency'].get('p95_ms') for s in group], 0)} "
            f"| {fmt([s.get('persist_sensor_reading_p50_s') for s in group], 2)} / "
            f"{fmt([s.get('persist_sensor_reading_p95_s') for s in group], 2)} "
            f"| {fmt([s.get('persist_gas_prediction_p95_s') for s in group], 2)} |"
        )
    out.append("\n**Độ đầy đủ (trong cửa sổ đo), lỗi và độ chậm consumer**\n")
    out.append(
        "| Nút | Reading: Kafka / Influx (%) | Ground truth: Kafka / Influx (%) | Dự đoán / lịch: Kafka / Influx (%) "
        "| Trùng / sai thứ tự (Kafka) | Mosquitto bỏ | DLQ / ACK quá hạn / lỗi ghi | Lag đỉnh Sink / Controller |"
    )
    out.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for nodes, group in rows.items():
        dup = sum(s["kafka"][t]["duplicates"] for s in group for t in (SENSOR, TRUTH, PRED))
        ooo = sum(s["kafka"][t]["out_of_order"] for s in group for t in (SENSOR, TRUTH, PRED))
        err = [
            (
                s["errors"]["dead_letter_records"],
                s["errors"]["ack_timeouts"],
                s["errors"]["influx_write_failures"],
            )
            for s in group
        ]
        lag = lambda g, s: s["consumer_lag"].get(g, {}).get("peak")  # noqa: E731
        out.append(
            f"| {nodes} "
            f"| {fmt([s['kafka'][SENSOR]['completeness_in_window']['pct'] for s in group], 2)} / "
            f"{fmt([s['influx']['sensor_reading']['pct'] for s in group], 2)} "
            f"| {fmt([s['kafka'][TRUTH]['completeness_in_window']['pct'] for s in group], 2)} / "
            f"{fmt([s['influx']['gas_ground_truth']['pct'] for s in group], 2)} "
            f"| {fmt([s['kafka'][PRED]['completeness_in_window']['pct'] for s in group], 2)} / "
            f"{fmt([s['influx']['gas_prediction']['pct'] for s in group], 2)} "
            f"| {dup} / {ooo} "
            f"| {fmt([s['mosquitto_dropped'] for s in group], 0)} "
            f"| {sum(e[0] for e in err):g} / {sum(e[1] for e in err):g} / {sum(e[2] for e in err):g} "
            f"| {fmt([lag('influx-sink-v1', s) for s in group], 0)} / {fmt([lag('safety-controller-v1', s) for s in group], 0)} |"
        )
    out.append("\n**Nhịp dự đoán của gateway và vòng điều khiển**\n")
    out.append(
        "| Nút | Khoảng cách 2 dự đoán p50 / p95 (s) | > 1,5 chu kỳ (%) | Khoảng ngừng dài nhất (s) "
        "| Tuổi mẫu tại Controller p50 / p95 (s) | Lệnh → ACK p50 / p95 (ms) | Lệnh phát / bị chặn trùng |"
    )
    out.append("|---:|---:|---:|---:|---:|---:|---:|")
    for nodes, group in rows.items():
        ack50 = [
            None if s["command_ack_p50_s"] is None else s["command_ack_p50_s"] * 1000 for s in group
        ]
        ack95 = [
            None if s["command_ack_p95_s"] is None else s["command_ack_p95_s"] * 1000 for s in group
        ]
        out.append(
            f"| {nodes} | {fmt([s['prediction_interval'].get('p50_s') for s in group], 2)} / "
            f"{fmt([s['prediction_interval'].get('p95_s') for s in group], 2)} "
            f"| {fmt([s['prediction_interval_over_1_5x_pct'] for s in group], 2)} "
            f"| {fmt([s['prediction_max_gap_s'] for s in group], 1)} "
            f"| {fmt([s['controller_sample_age_p50_s'] for s in group], 2)} / {fmt([s['controller_sample_age_p95_s'] for s in group], 2)} "
            f"| {fmt(ack50, 1)} / {fmt(ack95, 1)} "
            f"| {fmt([s['commands_published'] for s in group], 0)} / {fmt([s['commands_deduplicated'] for s in group], 0)} |"
        )
    out.append("\n**CPU (lõi) và RAM (MiB) theo dịch vụ: trung bình / đỉnh**\n")
    services = sorted({k for s in steps for k in s["containers"] if not k.startswith("_")})
    out.append("| Nút | " + " | ".join(services) + " | Tổng CPU / RAM TB |")
    out.append("|---:|" + "---:|" * (len(services) + 1))
    for nodes, group in rows.items():
        cells = []
        for svc in services:
            vals = [s["containers"].get(svc) for s in group if s["containers"].get(svc)]
            cells.append(
                f"{fmt([v['cpu_cores_mean'] for v in vals], 2)} / {fmt([v['cpu_cores_peak'] for v in vals], 2)} lõi; "
                f"{fmt([v['mem_mib_mean'] for v in vals], 0)} / {fmt([v['mem_mib_peak'] for v in vals], 0)} MiB"
            )
        total = [s["containers"]["_total"] for s in group]
        out.append(
            f"| {nodes} | "
            + " | ".join(cells)
            + f" | {fmt([t['cpu_cores_mean'] for t in total], 2)} / {fmt([t['mem_mib_mean'] for t in total], 0)} |"
        )
    return "\n".join(out) + "\n"


def control_tables(data: dict) -> str:
    out = [
        "### Kịch bản điều khiển C01–C11\n",
        "| Mã | Kịch bản | Bước | Kỳ vọng | Thực tế | Kết quả |",
        "|---|---|---|---|---|---|",
    ]
    for case in data["cases"]:
        for i, step in enumerate(case["steps"]):
            exp = (
                step["expected"]
                if isinstance(step["expected"], str)
                else json.dumps(step["expected"], ensure_ascii=False)
            )
            act = (
                step["actual"]
                if isinstance(step["actual"], str)
                else json.dumps(step["actual"], ensure_ascii=False)
            )
            out.append(
                f"| {case['id'] if i == 0 else ''} | {case['scenario'] if i == 0 else ''} | {step['step']} "
                f"| {exp} | {act} | {'ĐẠT' if step['pass'] else 'KHÔNG ĐẠT'} |"
            )
    t = data["timings"]
    out.append("\n**Thời gian trên đường điều khiển (đồng hồ host)**\n")
    out.append("| Đại lượng | n | p50 (ms) | p95 (ms) | max (ms) |")
    out.append("|---|---:|---:|---:|---:|")
    for key, label in (
        (
            "S09_prediction_published_to_command_at_device",
            "S09 · dự đoán phát lên MQTT → thiết bị nhận lệnh",
        ),
        (
            "S11_prediction_published_to_ack_published",
            "S11 · dự đoán phát lên MQTT → thiết bị phát ACK",
        ),
        ("request_published_to_command_at_device", "Yêu cầu operator → thiết bị nhận lệnh"),
    ):
        s = t[key]
        out.append(
            f"| {label} | {s.get('n')} | {s.get('p50_ms')} | {s.get('p95_ms')} | {s.get('max_ms')} |"
        )
    return "\n".join(out) + "\n"


def recovery_tables(data: dict) -> str:
    out = ["### Phục hồi R01–R07\n"]
    out.append(
        "| Mã | Sự cố | Phát hiện (alert, s sau khi gây lỗi) | Khỏe lại sau start (s) | Reading mất trong Kafka "
        "| Dự đoán mất | Bản ghi mới đầu tiên sau khi chạy lại (s) | Hết tồn đọng (s) | Reading thiếu trong InfluxDB | Điều khiển trong lúc lỗi |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for c in data["cases"]:
        if c["id"] == "R05":
            continue
        r, p = c["kafka"]["readings"], c["kafka"]["predictions"]
        alerts = (
            ", ".join(f"{k} {v}" for k, v in c["alerts_first_seen_after_fault_s"].items()) or "—"
        )
        probe = c.get("control_probe_during_fault") or {}
        drain = c.get("influx_readings_drained_after_back_s")
        out.append(
            f"| {c['id']} | {c['scenario']} | {alerts} | {', '.join(f'{k} {v}' for k, v in c['healthy_after_start_s'].items())} "
            f"| {r['missing']} / {r['expected']} ({r['missing_span_s']} s) | {p['missing']} / {p['expected']} "
            f"| {r.get('first_new_record_after_back_s')} | Kafka {r.get('backlog_drained_after_back_s')}"
            + (f"; Influx {drain}" if drain is not None else "")
            + f" | {c['influx_readings']['missing']} / {c['influx_readings']['expected']} "
            f"| {'lệnh sau ' + str(probe.get('command_after_s')) + ' s' if probe.get('command_after_s') is not None else 'không có lệnh trong 15 s'} |"
        )
    for c in data["cases"]:
        if c["id"] == "R05":
            out.append(
                f"\n**R05 · bản tin sai:** gửi {c['sent_invalid']} bản tin sai → {c['dead_letters']} bản ghi DLQ; "
                f"bản tin hợp lệ gửi ngay sau vẫn vào Kafka: {c['valid_after_invalid_reached_kafka']}. Kết quả: {c['result']}.\n"
            )
            for err in c["dead_letter_errors"]:
                out.append(f"- `{err}`")
    return "\n".join(out) + "\n"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    folder = Path(sys.argv[1])
    parts = []
    for path in sorted(folder.glob("load_*.json")):
        parts.append(load_tables(path.stem.removeprefix("load_"), load(path)))
    if (data := load(folder / "control_cases.json")) is not None:
        parts.append(control_tables(data))
    if (data := load(folder / "recovery_cases.json")) is not None:
        parts.append(recovery_tables(data))
    if (data := load(folder / "ui_latency.json")) is not None:
        rows = [
            "### Độ trễ tới giao diện (1 nút)\n",
            "| Đại lượng | n | p50 (ms) | p95 (ms) | max (ms) |",
            "|---|---:|---:|---:|---:|",
        ]
        for key in (
            "sse_reading",
            "sse_prediction",
            "dom_reading",
            "dom_prediction",
            "frame_reading",
            "frame_prediction",
        ):
            s = data[key]
            rows.append(
                f"| {key} | {s.get('n')} | {s.get('p50_ms')} | {s.get('p95_ms')} | {s.get('max_ms')} |"
            )
        parts.append("\n".join(rows) + "\n")
    if (data := load(folder / "startup.json")) is not None:
        rows = [
            "### Khởi động nguội\n",
            "| Lượt | compose sẵn sàng (s) | Reading đầu tiên ở UI (s) | Dự đoán đầu tiên ở UI (s) | Reading đầu tiên trong InfluxDB (s) |",
            "|---:|---:|---:|---:|---:|",
        ]
        for r in data["runs"]:
            rows.append(
                f"| {r['run']} | {r.get('compose_ready')} | {r.get('first_reading_ui')} | {r.get('first_prediction_ui')} | {r.get('first_reading_influx')} |"
            )
        parts.append("\n".join(rows) + "\n")
    print("\n".join(parts))


if __name__ == "__main__":
    main()

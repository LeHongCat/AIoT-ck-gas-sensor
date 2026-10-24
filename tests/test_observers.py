import json

from aiot_pipeline.config import MqttConfig
from aiot_pipeline.observers import MqttObserver, summarize_payload, summarize_prometheus


def test_payload_preview_is_compact_and_readable() -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "event_id": "3fe63cf3-e7b8-424c-96d3-246bc812b7b7",
            "observed_at": "2026-10-17T13:35:12.645536Z",
            "sensors": [606.1688, 625.19],
            "co_ppm": 12.3456,
        }
    ).encode()
    preview = summarize_payload(payload)
    assert "schema_version" not in preview
    assert '"event_id":"3fe63cf3…"' in preview
    assert '"observed_at":"13:35:12.645"' in preview
    assert '"sensors":[606,625]' in preview
    assert '"co_ppm":12.35' in preview


def test_observer_tracks_topics_rates_and_roles() -> None:
    observer = MqttObserver(MqttConfig())
    for second in range(5):
        observer.observe("aiot/v1/nodes/n1/readings", b'{"sequence":1}', False, 100.0 + second)
    observer.observe("aiot/v1/nodes/n1/commands", b'{"fan_on":true}', False, 104.0)
    observer.observe("$SYS/broker/clients/connected", b"6", False, 104.0)
    observer.observe("$SYS/broker/uptime", b"360 seconds", False, 104.0)
    snapshot = observer.snapshot(now=105.0)
    readings, commands = snapshot["topics"]
    assert readings["suffix"] == "readings" and readings["count"] == 5
    assert readings["rate"] == 0.5
    assert readings["kafka_topic"] == "gas.sensor.readings.v1"
    assert commands["publisher"] == "Safety Controller"
    assert snapshot["stats"] == {"clients_connected": 6.0, "uptime_seconds": 360.0}


def test_prometheus_summary_handles_nan_and_groups_series() -> None:
    series = [
        {"metric": {"__name__": "aiot:mqtt_ingress:rate1m"}, "value": [0, "3.5"]},
        {"metric": {"__name__": "aiot:ack_latency:p95"}, "value": [0, "NaN"]},
        {
            "metric": {"__name__": "aiot:pipeline_latency:p95", "measurement": "sensor_reading"},
            "value": [0, "0.61"],
        },
        {"metric": {"__name__": "up", "job": "influx-sink"}, "value": [0, "1"]},
        {"metric": {"__name__": "aiot:consumer_lag:sum", "consumergroup": "g"}, "value": [0, "2"]},
    ]
    alerts = [
        {"metric": {"alertname": "AiotGasWarning", "severity": "warning"}},
        {"metric": {"alertname": "AiotGasDanger", "severity": "critical", "node_id": "n1"}},
    ]
    summary = summarize_prometheus(series, alerts)
    assert summary["rates"]["mqtt_ingress"] == 3.5
    assert summary["latency"]["ack_p95"] is None
    assert summary["latency"]["edge_to_influx_p95"] == 0.61
    assert summary["up"] == {"influx-sink": 1.0}
    assert summary["lag"] == {"g": 2.0}
    assert summary["alerts"][0]["name"] == "AiotGasDanger"


def test_observer_exposes_latest_demo_state() -> None:
    observer = MqttObserver(MqttConfig())
    payload = json.dumps({"phase": "isolated", "room_co_ppm": 120.5, "eta_safe_s": 18.2}).encode()
    observer.observe("aiot/v1/nodes/n1/demo-state", payload, True, 50.0)
    demo = observer.snapshot(now=51.0)["demo"]
    assert demo["phase"] == "isolated" and demo["room_co_ppm"] == 120.5
    assert demo["received_at"] == 50.0

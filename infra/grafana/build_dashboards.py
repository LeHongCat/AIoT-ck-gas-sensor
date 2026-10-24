"""Generate the provisioned Grafana dashboards for the AIoT gas pipeline.

Run after changing a panel, then commit the JSON:

    uv run python infra/grafana/build_dashboards.py

tests/test_dashboards.py fails when the committed JSON no longer matches this script.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aiot_pipeline.sensors import SENSORS  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("dashboards")
PROM = {"type": "prometheus", "uid": "prometheus-operations"}
FLUX = {"type": "influxdb", "uid": "influxdb-gas"}

MINT, AMBER, RED, BLUE, PURPLE, GREY = (
    "#10966a",
    "#c48014",
    "#d63b31",
    "#2672c9",
    "#7a55d6",
    "#8a9995",
)
ORANGE = "#d9711a"
CHANNEL_TEXT = {s.channel: f"{s.channel} · {s.sensor_type} · {s.target}" for s in SENSORS}
TOPIC_TEXT = {
    "readings": "aiot/v1/nodes/+/readings  →  gas.sensor.readings.v1",
    "predictions": "aiot/v1/nodes/+/predictions  →  gas.predictions.v1",
    "ground-truth": "aiot/v1/nodes/+/ground-truth  →  gas.ground-truth.v1",
    "control-requests": "aiot/v1/nodes/+/control-requests  →  gas.control-requests.v1",
    "commands": "aiot/v1/nodes/+/commands  →  gas.actuator-commands.v1",
    "actuator-state": "aiot/v1/nodes/+/actuator-state  →  gas.actuator-state.v1",
}
TOPIC_ROLE = {
    "readings": "…/readings  ·  Edge gateway → Bridge",
    "predictions": "…/predictions  ·  Edge model → Bridge",
    "ground-truth": "…/ground-truth  ·  Nguồn phát lại (nhãn) → Bridge",
    "control-requests": "…/control-requests  ·  Kitchen UI → Bridge",
    "commands": "…/commands  ·  Safety Controller → thiết bị + Bridge",
    "actuator-state": "…/actuator-state  ·  Thiết bị (ACK) → Bridge",
}


class Layout:
    def __init__(self) -> None:
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_h = 0
        self.next_id = 1

    def row(self, title: str) -> None:
        self.newline()
        self.panels.append(
            {
                "type": "row",
                "title": title,
                "collapsed": False,
                "id": self._id(),
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y},
                "panels": [],
            }
        )
        self.y += 1

    def newline(self) -> None:
        self.y += self.row_h
        self.x = 0
        self.row_h = 0

    def add(self, panel: dict, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.newline()
        panel["id"] = self._id()
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.x += w
        self.row_h = max(self.row_h, h)
        self.panels.append(panel)
        return panel

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1


def prom(expr: str, legend: str = "__auto", ref: str = "A", **extra) -> dict:
    return {
        "datasource": PROM,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
        "range": True,
        **extra,
    }


def flux(query: str, ref: str = "A") -> dict:
    return {"datasource": FLUX, "query": query, "refId": ref}


def steps(*pairs) -> dict:
    return {
        "mode": "absolute",
        "steps": [{"color": color, "value": value} for value, color in pairs],
    }


def stat(
    title,
    targets,
    *,
    ds=PROM,
    unit="none",
    thresholds=None,
    decimals=None,
    graph="area",
    color_mode="background",
    text_mode="auto",
    mappings=None,
    description="",
    no_value=None,
    overrides=None,
    calcs=("lastNotNull",),
    orientation="auto",
):
    defaults = {
        "color": {"mode": "thresholds"},
        "unit": unit,
        "mappings": mappings or [],
        "thresholds": thresholds or steps((None, MINT)),
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if no_value:
        defaults["noValue"] = no_value
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "colorMode": color_mode,
            "graphMode": graph,
            "justifyMode": "center",
            "orientation": orientation,
            "textMode": text_mode,
            "wideLayout": True,
            "showPercentChange": False,
            "percentChangeColorMode": "standard",
            "reduceOptions": {"calcs": list(calcs), "fields": "", "values": False},
        },
    }


def timeseries(
    title,
    targets,
    *,
    ds=PROM,
    unit="none",
    description="",
    overrides=None,
    thresholds=None,
    threshold_style="off",
    fill=12,
    draw="line",
    stack=False,
    min_=None,
    decimals=None,
    legend_calcs=("mean", "max", "lastNotNull"),
    line_width=2,
    gradient="opacity",
    interpolation="smooth",
    span_nulls=True,
):
    custom = {
        "drawStyle": draw,
        "lineWidth": line_width,
        "fillOpacity": fill,
        "gradientMode": gradient,
        "lineInterpolation": interpolation,
        "showPoints": "never",
        "spanNulls": span_nulls,
        "axisBorderShow": False,
        "thresholdsStyle": {"mode": threshold_style},
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
    }
    if draw == "bars":
        custom["barAlignment"] = 0
    defaults = {
        "color": {"mode": "palette-classic"},
        "unit": unit,
        "custom": custom,
        "thresholds": thresholds or steps((None, MINT)),
    }
    if min_ is not None:
        defaults["min"] = min_
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "calcs": list(legend_calcs),
                "showLegend": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def state_timeline(
    title, targets, *, ds=PROM, mappings=None, overrides=None, description="", row_height=0.8
):
    return {
        "type": "state-timeline",
        "title": title,
        "description": description,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "fixed", "fixedColor": GREY},
                "mappings": mappings or [],
                "thresholds": steps((None, GREY)),
                "custom": {"fillOpacity": 85, "lineWidth": 0},
            },
            "overrides": overrides or [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "auto",
            "alignValue": "center",
            "rowHeight": row_height,
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }


def value_map(*items) -> list[dict]:
    """items: (value, text, color)"""
    return [
        {
            "type": "value",
            "options": {
                str(value): {"text": text, "color": color, "index": i}
                for i, (value, text, color) in enumerate(items)
            },
        }
    ]


def color_override(name: str, color: str, extra: list | None = None, matcher="byName") -> dict:
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]
    return {"matcher": {"id": matcher, "options": name}, "properties": props + (extra or [])}


def text_panel(title: str, content: str) -> dict:
    return {
        "type": "text",
        "title": title,
        "options": {"mode": "markdown", "content": content},
        "transparent": True,
    }


def table(
    title,
    targets,
    *,
    ds=PROM,
    overrides=None,
    transformations=None,
    description="",
    no_value="Không có dữ liệu",
    sort_by=None,
):
    panel = {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "left", "cellOptions": {"type": "auto"}},
                "noValue": no_value,
            },
            "overrides": overrides or [],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "fields": ""},
        },
        "transformations": transformations or [],
    }
    if sort_by:
        panel["options"]["sortBy"] = [{"displayName": sort_by, "desc": False}]
    return panel


def field(name, *props) -> dict:
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": key, "value": value} for key, value in props],
    }


def text_map(mapping: dict) -> list[dict]:
    return [
        {
            "type": "value",
            "options": {
                key: {"text": text, "index": i} for i, (key, text) in enumerate(mapping.items())
            },
        }
    ]


# ------------------------------------------------------------------ live pipeline map
def canvas_map() -> dict:
    """One box per stage, with live numbers; borders and arrows turn red when a stage stops."""
    scale = {"horizontal": "scale", "vertical": "scale"}
    elements: list[dict] = []
    boxes: dict[str, dict] = {}

    def place(left, top, width, height):
        return {
            "left": left,
            "top": top,
            "right": round(100 - left - width, 2),
            "bottom": round(100 - top - height, 2),
        }

    def node(name, title, caption, metric, left, top, width=10.5, height=34, accent=MINT):
        box = {
            "type": "rectangle",
            "name": name,
            "config": {
                "text": {"mode": "fixed", "fixed": title},
                "color": {"fixed": "#13241f"},
                "size": 14,
                "align": "center",
                "valign": "top",
            },
            "background": {"color": {"fixed": "#ffffff"}},
            "border": {"color": {"field": metric, "fixed": accent}, "width": 3, "radius": 14},
            "constraint": scale,
            "placement": place(left, top, width, height),
            "connections": [],
        }
        elements.append(box)
        boxes[name] = box
        elements.append(
            {
                "type": "metric-value",
                "name": f"{name} · số liệu",
                "config": {
                    "text": {"mode": "field", "field": metric, "fixed": ""},
                    "color": {"field": metric, "fixed": accent},
                    "size": 22,
                    "align": "center",
                    "valign": "middle",
                },
                "background": {"color": {"fixed": "transparent"}},
                "border": {"color": {"fixed": "transparent"}, "width": 0},
                "constraint": scale,
                "placement": place(left + 0.4, top + height * 0.36, width - 0.8, height * 0.34),
            }
        )
        elements.append(
            {
                "type": "text",
                "name": f"{name} · mô tả",
                "config": {
                    "text": {"mode": "fixed", "fixed": caption},
                    "color": {"fixed": "#52665f"},
                    "size": 11,
                    "align": "center",
                    "valign": "middle",
                },
                "background": {"color": {"fixed": "transparent"}},
                "border": {"color": {"fixed": "transparent"}, "width": 0},
                "constraint": scale,
                "placement": place(left + 0.4, top + height * 0.72, width - 0.8, height * 0.24),
            }
        )

    def label(text, left, top, width=4, height=8, color="#52665f"):
        elements.append(
            {
                "type": "text",
                "name": f"nhãn · {text}",
                "config": {
                    "text": {"mode": "fixed", "fixed": text},
                    "color": {"fixed": color},
                    "size": 11,
                    "align": "center",
                    "valign": "middle",
                },
                "background": {"color": {"fixed": "transparent"}},
                "border": {"color": {"fixed": "transparent"}, "width": 0},
                "constraint": scale,
                "placement": place(left, top, width, height),
            }
        )

    def connect(source, target, metric, src=(1, 0), dst=(-1, 0), color=MINT):
        boxes[source]["connections"].append(
            {
                "source": {"x": src[0], "y": src[1]},
                "target": {"x": dst[0], "y": dst[1]},
                "targetName": target,
                "path": "straight",
                "color": {"field": metric, "fixed": color},
                "size": {"fixed": 3, "min": 1, "max": 10},
                "direction": "forward",
                "lineStyle": {"style": "dashed", "animate": True},
            }
        )

    lefts = [1, 15.25, 29.5, 43.75, 58, 72.25, 86.5]
    top_a, top_b = 6, 60
    node(
        "Cảm biến + Edge", "Cảm biến + Edge model", "16 kênh → TabM INT8", "nodes", lefts[0], top_a
    )
    node(
        "MQTT",
        "MQTT · Mosquitto",
        "aiot/v1/nodes/+/… · QoS 1",
        "mqtt",
        lefts[1],
        top_a,
        accent=PURPLE,
    )
    node("Bridge", "Bridge", "kiểm contract · DLQ", "bridge", lefts[2], top_a)
    node("Kafka", "Apache Kafka", "topic gas.* · replay", "kafka", lefts[3], top_a, accent=BLUE)
    node("Influx sink", "Influx sink", "batch 0.5 s → ghi", "sink", lefts[4], top_a)
    node("InfluxDB", "InfluxDB", "lưu time-series", "influx", lefts[5], top_a)
    node("Grafana", "Grafana", "bạn đang xem", "grafana", lefts[6], top_a)
    node(
        "Thiết bị",
        "Thiết bị an toàn",
        "ACK p95 · quạt/van/còi",
        "ack",
        lefts[1],
        top_b,
        accent=ORANGE,
    )
    node(
        "Safety Controller",
        "Safety Controller",
        "alarm hiện tại",
        "alarm",
        lefts[3],
        top_b,
        accent=ORANGE,
    )
    node("Kitchen UI", "Kitchen UI", "lag consumer · SSE", "ui_lag", lefts[4], top_b)

    for (source, target, metric), text, left in zip(
        [
            ("Cảm biến + Edge", "MQTT", "mqtt"),
            ("MQTT", "Bridge", "bridge"),
            ("Bridge", "Kafka", "kafka"),
            ("Kafka", "Influx sink", "sink"),
            ("Influx sink", "InfluxDB", "influx"),
            ("InfluxDB", "Grafana", "grafana"),
        ],
        ["publish", "subscribe", "produce", "consume", "write", "query"],
        [
            lefts[0] + 10.5,
            lefts[1] + 10.5,
            lefts[2] + 10.5,
            lefts[3] + 10.5,
            lefts[4] + 10.5,
            lefts[5] + 10.5,
        ],
        strict=True,
    ):
        connect(source, target, metric)
        label(text, left, top_a + 8, width=3.75)
    connect("Kafka", "Safety Controller", "alarm", src=(0, -1), dst=(0, 1), color=ORANGE)
    label("predictions", lefts[3] + 5.5, 44, width=6)
    connect("Safety Controller", "Thiết bị", "ack", src=(-1, 0), dst=(1, 0), color=ORANGE)
    label("lệnh qua MQTT", lefts[2] + 0.5, top_b + 8, width=10, color=ORANGE)
    connect("Thiết bị", "MQTT", "ack", src=(0, 1), dst=(0, -1), color=ORANGE)
    label("ACK", lefts[1] + 5.5, 44, width=4, color=ORANGE)
    connect("Kafka", "Kitchen UI", "ui_lag", src=(1, -1), dst=(-1, 0.5))
    label("live", lefts[4] - 3.5, 47, width=4)
    elements.append(
        {
            "type": "metric-value",
            "name": "Độ trễ end-to-end",
            "config": {
                "text": {"mode": "field", "field": "e2e", "fixed": ""},
                "color": {"field": "e2e", "fixed": MINT},
                "size": 18,
                "align": "center",
                "valign": "middle",
            },
            "background": {"color": {"fixed": "#ffffff"}},
            "border": {"color": {"field": "e2e", "fixed": MINT}, "width": 2, "radius": 12},
            "constraint": scale,
            "placement": place(lefts[5], top_b + 6, 24, 22),
        }
    )
    label("Cảm biến → InfluxDB · p95", lefts[5], top_b - 2, width=24, height=8, color="#33463f")

    rate = steps((None, RED), (0.05, MINT))
    overrides = [
        field(
            "nodes",
            ("unit", "suffix: node"),
            ("decimals", 0),
            ("thresholds", steps((None, RED), (1, MINT))),
        ),
        field("mqtt", ("unit", "suffix: msg/s"), ("decimals", 1), ("thresholds", rate)),
        field("bridge", ("unit", "suffix: msg/s"), ("decimals", 1), ("thresholds", rate)),
        field("kafka", ("unit", "suffix: rec/s"), ("decimals", 1), ("thresholds", rate)),
        field("sink", ("unit", "suffix: rec/s"), ("decimals", 1), ("thresholds", rate)),
        field("influx", ("unit", "suffix: pt/s"), ("decimals", 1), ("thresholds", rate)),
        field(
            "grafana",
            ("mappings", value_map((0, "DOWN", RED), (1, "UP", MINT))),
            ("thresholds", steps((None, RED), (1, MINT))),
        ),
        field(
            "alarm",
            ("mappings", value_map((0, "NORMAL", MINT), (1, "WARNING", AMBER), (2, "DANGER", RED))),
            ("thresholds", steps((None, MINT), (1, AMBER), (2, RED))),
        ),
        field(
            "ack",
            ("unit", "s"),
            ("decimals", 1),
            ("thresholds", steps((None, MINT), (0.25, AMBER), (1, RED))),
        ),
        field(
            "ui_lag",
            ("unit", "suffix: rec lag"),
            ("decimals", 0),
            ("thresholds", steps((None, MINT), (50, AMBER), (100, RED))),
        ),
        field(
            "e2e",
            ("unit", "s"),
            ("decimals", 0),
            ("thresholds", steps((None, MINT), (1, AMBER), (3, RED))),
        ),
    ]
    return {
        "type": "canvas",
        "title": "Bản đồ pipeline",
        "description": "Mỗi ô là một tầng của pipeline với số liệu đang chạy qua nó. Viền và mũi tên "
        "xanh = dữ liệu đang chảy, đỏ = tầng đó dừng. Đường cam là vòng điều khiển "
        "(Kafka → Safety Controller → MQTT → thiết bị → ACK).",
        "datasource": PROM,
        "targets": [
            prom(
                "count(aiot:sensor_age:max < 10) or vector(0)",
                "nodes",
                "A",
                instant=True,
                range=False,
            ),
            prom("aiot:mqtt_ingress:rate1m", "mqtt", "B", instant=True, range=False),
            prom("aiot:kafka_produced:rate1m", "bridge", "C", instant=True, range=False),
            prom("aiot:kafka_appended:rate1m", "kafka", "D", instant=True, range=False),
            prom("aiot:sink_consumed:rate1m", "sink", "E", instant=True, range=False),
            prom("aiot:influx_written:rate1m", "influx", "F", instant=True, range=False),
            prom('max(up{job="grafana"})', "grafana", "G", instant=True, range=False),
            prom("max(aiot_alarm_level) or vector(0)", "alarm", "H", instant=True, range=False),
            prom("(aiot:ack_latency:p95 > 0) or vector(0)", "ack", "I", instant=True, range=False),
            prom(
                'max(aiot:consumer_lag:sum{consumergroup="kitchen-demo-ui-v1"}) or vector(0)',
                "ui_lag",
                "J",
                instant=True,
                range=False,
            ),
            prom(
                'aiot:pipeline_latency:p95{measurement="sensor_reading"}',
                "e2e",
                "K",
                instant=True,
                range=False,
            ),
        ],
        "fieldConfig": {
            "defaults": {"color": {"mode": "thresholds"}, "thresholds": steps((None, MINT))},
            "overrides": overrides,
        },
        "options": {
            "inlineEditing": False,
            "showAdvancedTypes": True,
            "panZoom": False,
            "infinitePan": False,
            "root": {
                "type": "frame",
                "name": "Element root",
                "elements": elements,
                "background": {"color": {"fixed": "transparent"}},
                "border": {"color": {"fixed": "transparent"}},
                "constraint": {"horizontal": "left", "vertical": "top"},
            },
        },
    }


def siren_switch() -> dict:
    """Siren switch on the gas dashboard: the controller's siren state and two one-click actions.

    The actions POST the same request as the kitchen screen's switch (UI → MQTT control-requests
    → Kafka → Safety Controller), so they are audited and logged like it; demo_ui allows
    Grafana's origin for that call (CORS).
    """
    scale = {"horizontal": "scale", "vertical": "scale"}

    def place(left, top, width, height):
        return {
            "left": left,
            "top": top,
            "right": round(100 - left - width, 2),
            "bottom": round(100 - top - height, 2),
        }

    def text(name, value, left, top, width, height, size=12, color="#52665f", field=None):
        return {
            "type": "metric-value" if field else "text",
            "name": name,
            "config": {
                "text": {"mode": "field", "field": field, "fixed": ""}
                if field
                else {"mode": "fixed", "fixed": value},
                "color": {"field": field, "fixed": color} if field else {"fixed": color},
                "size": size,
                "align": "center",
                "valign": "middle",
            },
            "background": {"color": {"fixed": "transparent"}},
            "border": {"color": {"fixed": "transparent"}, "width": 0},
            "constraint": scale,
            "placement": place(left, top, width, height),
        }

    def button(name, label, mode, color, left, confirmation):
        return {
            "type": "rectangle",
            "name": name,
            "config": {
                "text": {"mode": "fixed", "fixed": label},
                "color": {"fixed": "#ffffff"},
                "size": 14,
                "align": "center",
                "valign": "middle",
            },
            "background": {"color": {"fixed": color}},
            "border": {"color": {"fixed": color}, "width": 1, "radius": 10},
            "constraint": scale,
            "placement": place(left, 54, 46, 38),
            "links": [],
            "actions": [
                {
                    "title": label.capitalize(),
                    "type": "fetch",
                    "oneClick": True,
                    "confirmation": confirmation,
                    "fetch": {
                        "method": "POST",
                        "url": f"{KITCHEN_UI}/api/control",
                        "body": json.dumps({"node_id": "${siren_node}", "mode": mode}),
                        "headers": [["Content-Type", "application/json"]],
                        "queryParams": [],
                    },
                }
            ],
        }

    def icon(name, path, left):
        return {
            "type": "icon",
            "name": name,
            "config": {
                "path": {"mode": "fixed", "fixed": f"img/icons/unicons/{path}.svg"},
                "fill": {"field": "Trạng thái", "fixed": GREY},
            },
            "background": {"color": {"fixed": "transparent"}},
            "border": {"color": {"fixed": "transparent"}, "width": 0},
            "constraint": scale,
            "placement": place(left, 6, 11, 40),
        }

    elements = [
        icon("Đèn", "lightbulb", 2),
        icon("Chuông", "bell", 14),
        text("Đèn & chuông · trạng thái", "", 26, 4, 46, 44, size=18, field="Trạng thái"),
        text("Công tắc", "", 72, 4, 26, 44, size=13, field="Chuông"),
        button(
            "Nút tắt chuông",
            "TẮT CHUÔNG",
            "mute",
            AMBER,
            2,
            "Tắt chuông cảnh báo của node ${siren_node}? Model, dữ liệu và log vẫn chạy; "
            "DANGER mới sẽ tự bật lại chuông.",
        ),
        button(
            "Nút bật chuông",
            "BẬT CHUÔNG",
            "unmute",
            MINT,
            52,
            "Bật lại chuông cảnh báo của node ${siren_node}?",
        ),
    ]
    return {
        "type": "canvas",
        "title": "Chuông cảnh báo",
        "description": "Tắt / bật còi của node đang chọn (biến Node chuông). Lệnh đi Kitchen UI → MQTT → "
        "Kafka → Safety Controller như công tắc trên màn hình bếp: chế độ vẫn AUTO, quạt và van "
        "gas vẫn theo alarm, model và log vẫn chạy. DANGER mới tự bật lại chuông. Cần quyền Editor.",
        "datasource": PROM,
        "targets": [
            prom(
                'max(aiot_siren_muted{node_id="${siren_node}"})',
                "Chuông",
                "A",
                instant=True,
                range=False,
            ),
            prom(
                # 0 waiting · 1 silenced by an operator · 2 light flashing and siren sounding
                'max(aiot_actuator_desired_output{node_id="${siren_node}", output="alarm_on"}) * 2 '
                '+ max(aiot_siren_muted{node_id="${siren_node}"})',
                "Trạng thái",
                "B",
                instant=True,
                range=False,
            ),
        ],
        "fieldConfig": {
            "defaults": {"color": {"mode": "thresholds"}, "thresholds": steps((None, MINT))},
            "overrides": [
                field(
                    "Chuông",
                    ("mappings", value_map((0, "Công tắc BẬT", MINT), (1, "Công tắc TẮT", AMBER))),
                ),
                field(
                    "Trạng thái",
                    (
                        "mappings",
                        value_map(
                            (0, "Đèn tắt · chuông chờ", GREY),
                            (1, "Đèn & chuông TẮT TAY", AMBER),
                            (2, "Đèn chớp · chuông kêu", RED),
                            (3, "Đèn chớp · chuông kêu", RED),
                        ),
                    ),
                ),
            ],
        },
        "options": {
            "inlineEditing": False,
            "showAdvancedTypes": True,
            "panZoom": False,
            "infinitePan": False,
            "root": {
                "type": "frame",
                "name": "Element root",
                "elements": elements,
                "background": {"color": {"fixed": "transparent"}},
                "border": {"color": {"fixed": "transparent"}},
                "constraint": {"horizontal": "left", "vertical": "top"},
            },
        },
    }


def dashboard(
    uid,
    title,
    description,
    panels,
    *,
    variables,
    links,
    annotations,
    tags,
    refresh="5s",
    time_from="now-30m",
):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": tags,
        "editable": True,
        "graphTooltip": 1,
        "fiscalYearStartMonth": 0,
        "id": None,
        "liveNow": False,
        "refresh": refresh,
        "schemaVersion": 42,
        "version": 1,
        "style": "light",
        "timezone": "browser",
        "weekStart": "",
        "time": {"from": time_from, "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
        "templating": {"list": variables},
        "links": links,
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                },
                *annotations,
            ]
        },
        "panels": panels,
    }


DANGER_ANNOTATION = {
    "datasource": PROM,
    "enable": True,
    "iconColor": RED,
    "name": "Gas DANGER",
    "expr": "max by (node_id) (aiot_alarm_level) == 2",
    "step": "5s",
    "titleFormat": "DANGER · {{node_id}}",
    "textFormat": "Van gas khóa, xả khí + còi bật",
    "useValueForTime": False,
    "hide": False,
}
OVERRIDE_ANNOTATION = {
    "datasource": PROM,
    "enable": True,
    "iconColor": PURPLE,
    "name": "Direct override",
    "expr": "max by (node_id) (aiot_direct_override_active) == 1",
    "step": "5s",
    "titleFormat": "Override · {{node_id}}",
    "textFormat": "Operator can thiệp trực tiếp",
    "useValueForTime": False,
    "hide": False,
}


def link(title, url, icon="dashboard", blank=False, tooltip=""):
    return {
        "asDropdown": False,
        "icon": icon,
        "includeVars": not blank,
        "keepTime": not blank,
        "tags": [],
        "targetBlank": blank,
        "title": title,
        "tooltip": tooltip,
        "type": "link",
        "url": url,
    }


KITCHEN_UI = "http://localhost:8080"

LINKS = [
    link("Kitchen UI", KITCHEN_UI, "external link", True, "Giao diện bếp công nghiệp"),
    link("Gas Monitoring", "/d/aiot-gas-monitoring", tooltip="Khí, model, actuator"),
    link("Pipeline Operations", "/d/aiot-pipeline-operations", tooltip="MQTT, Kafka, InfluxDB"),
    link("Prometheus alerts", "http://localhost:9090/alerts", "external link", True),
]

ALARM_MAP = value_map((0, "NORMAL", "green"), (1, "WARNING", "orange"), (2, "DANGER", "red"))
ONOFF_MAP = value_map((0, "Tắt", GREY), (1, "Bật", MINT))
VALVE_MAP = value_map((0, "Mở", MINT), (1, "Khóa", RED))
MODE_MAP = value_map((0, "AUTO", BLUE), (1, "DIRECT", PURPLE))


# --------------------------------------------------------------------------- operations
def operations() -> dict:
    L = Layout()
    node = 'node_id=~"$node"'

    L.row("① Pipeline")
    L.add(
        text_panel(
            "",
            (
                "**Cách đọc:** mỗi ô là một tầng; số trong ô là lượng dữ liệu đang chạy qua. "
                "**Xanh** = đang chảy · **đỏ** = dừng · đường **cam** = vòng điều khiển an toàn. "
                "Kéo xuống để xem **từng cảm biến gửi gì** (②) và **từng topic MQTT** (③)."
            ),
        ),
        24,
        2,
    )
    L.add(canvas_map(), 24, 12)
    L.add(
        stat(
            "Edge → InfluxDB p95",
            [prom('aiot:pipeline_latency:p95{measurement="sensor_reading"}')],
            unit="s",
            decimals=2,
            thresholds=steps((None, MINT), (1, AMBER), (3, RED)),
            description="observed_at trên thiết bị → batch được InfluxDB xác nhận.",
            no_value="—",
        ),
        4,
        4,
    )
    L.add(
        stat(
            "Command → ACK p95",
            [prom("aiot:ack_latency:p95 > 0")],
            unit="s",
            decimals=1,
            thresholds=steps((None, MINT), (0.25, AMBER), (1, RED)),
            description="Lệnh MQTT tới thiết bị → ActuatorState ACK (bridge đo theo command_id).",
            no_value="chưa có lệnh",
        ),
        4,
        4,
    )
    L.add(
        stat(
            "Control loop lag p95",
            [prom("aiot:control_loop_lag:p95")],
            unit="s",
            decimals=1,
            thresholds=steps((None, MINT), (0.5, AMBER), (2, RED)),
            description="Tuổi prediction khi Safety Controller ra quyết định.",
        ),
        4,
        4,
    )
    L.add(
        stat(
            "Sensor freshness",
            [prom('max(aiot:sensor_age:max{node_id=~"$node"})')],
            unit="s",
            decimals=1,
            thresholds=steps((None, MINT), (5, AMBER), (10, RED)),
            description="Thời gian từ reading mới nhất (theo observed_at).",
            graph="none",
        ),
        4,
        4,
    )
    L.add(
        stat(
            "Consumer lag",
            [prom("sum(aiot:consumer_lag:sum)")],
            unit="short",
            decimals=0,
            thresholds=steps((None, MINT), (50, AMBER), (100, RED)),
            description="Tổng lag mọi consumer group (clamp từng partition trước khi cộng).",
        ),
        4,
        4,
    )
    L.add(
        stat(
            "Cảnh báo đang bật",
            [prom('count(ALERTS{alertstate="firing"}) or vector(0)')],
            unit="short",
            decimals=0,
            graph="none",
            thresholds=steps((None, MINT), (1, AMBER), (2, RED)),
            description="Số alert Prometheus đang firing — chi tiết ở bảng bên dưới.",
        ),
        4,
        4,
    )

    sensors_section(L, node)
    mqtt_section(L, node)

    L.row("④ Dịch vụ & cảnh báo")
    up_jobs = "mqtt-kafka-bridge|edge-inference|influx-sink|safety-controller|kafka-exporter|influxdb|grafana|prometheus"
    L.add(
        stat(
            "Service health",
            [prom(f'up{{job=~"{up_jobs}"}}', "{{job}}")],
            mappings=value_map((0, "DOWN", "red"), (1, "UP", "green")),
            thresholds=steps((None, RED), (1, "green")),
            graph="none",
            text_mode="value_and_name",
            description="Prometheus scrape mỗi 5s.",
        ),
        12,
        7,
    )
    alerts = {
        "type": "table",
        "title": "Alert",
        "datasource": PROM,
        "description": "Rule trong infra/prometheus/rules/aiot.yml",
        "targets": [
            prom('ALERTS{alertstate="firing"}', "", instant=True, format="table", range=False)
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "left", "cellOptions": {"type": "auto"}},
                "noValue": "Không có alert — pipeline ổn định",
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "severity"},
                    "properties": [
                        {
                            "id": "custom.cellOptions",
                            "value": {"type": "color-background", "mode": "basic"},
                        },
                        {
                            "id": "mappings",
                            "value": value_map(
                                ("critical", "CRITICAL", "red"), ("warning", "WARNING", "orange")
                            ),
                        },
                    ],
                }
            ],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "fields": ""},
        },
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "Value": True,
                        "__name__": True,
                        "alertstate": True,
                        "instance": True,
                    },
                    "indexByName": {
                        "severity": 0,
                        "alertname": 1,
                        "plane": 2,
                        "job": 3,
                        "node_id": 4,
                    },
                    "renameByName": {
                        "alertname": "Alert",
                        "plane": "Plane",
                        "job": "Job",
                        "node_id": "Node",
                        "severity": "Mức",
                    },
                },
            }
        ],
    }
    L.add(alerts, 12, 7)

    L.row("⑤ Throughput & latency")
    L.add(
        timeseries(
            "Throughput",
            [
                prom("aiot:mqtt_ingress:rate1m", "MQTT ingress", "A"),
                prom("aiot:kafka_produced:rate1m", "Kafka produced (ACK)", "B"),
                prom("aiot:sink_consumed:rate1m", "Sink consumed", "C"),
                prom("aiot:influx_written:rate1m", "Influx written", "D"),
            ],
            unit="suffix: ev/s",
            fill=8,
            overrides=[
                color_override("MQTT ingress", BLUE),
                color_override("Kafka produced (ACK)", PURPLE),
                color_override("Sink consumed", AMBER),
                color_override("Influx written", MINT),
            ],
            description="Bốn đường gần trùng nhau nghĩa là không có tầng nào làm rơi hay dồn dữ liệu.",
        ),
        12,
        10,
    )
    L.add(
        timeseries(
            "Latency từng chặng p95",
            [
                prom("aiot:bridge_event_age:p95", "Edge → Bridge", "A"),
                prom("aiot:influx_write:p95", "Influx write", "B"),
                prom(
                    'aiot:pipeline_latency:p50{measurement="sensor_reading"}',
                    "Edge → InfluxDB p50",
                    "C",
                ),
                prom(
                    'aiot:pipeline_latency:p95{measurement="sensor_reading"}',
                    "Edge → InfluxDB p95",
                    "D",
                ),
                prom("aiot:control_loop_lag:p95", "Prediction → quyết định", "E"),
            ],
            unit="s",
            fill=0,
            min_=0,
            thresholds=steps((None, MINT), (3, RED)),
            threshold_style="dashed",
            overrides=[
                color_override("Edge → Bridge", BLUE),
                color_override("Influx write", AMBER),
                color_override("Edge → InfluxDB p50", "#2f9e78"),
                color_override("Edge → InfluxDB p95", MINT),
                color_override("Prediction → quyết định", PURPLE),
            ],
            description="Phần lớn latency end-to-end đến từ khoảng flush batch của sink "
            "(INFLUX_FLUSH_SECONDS). Đường đỏ là SLO 3s.",
        ),
        12,
        10,
    )

    L.row("⑥ Control plane")
    L.add(
        state_timeline(
            "Alarm & chế độ",
            [
                prom(f"max by (node_id) (aiot_alarm_level{{{node}}})", "Alarm · {{node_id}}", "A"),
                prom(
                    f"max by (node_id) (aiot_direct_override_active{{{node}}})",
                    "Chế độ · {{node_id}}",
                    "B",
                ),
                prom(
                    f"max by (node_id) (aiot_siren_muted{{{node}}})",
                    "Chuông · {{node_id}}",
                    "C",
                ),
            ],
            mappings=ALARM_MAP,
            overrides=[
                {
                    "matcher": {"id": "byFrameRefID", "options": "B"},
                    "properties": [{"id": "mappings", "value": MODE_MAP}],
                },
                {
                    "matcher": {"id": "byFrameRefID", "options": "C"},
                    "properties": [
                        {
                            "id": "mappings",
                            "value": value_map((0, "Bật", MINT), (1, "TẮT TAY", AMBER)),
                        }
                    ],
                },
            ],
            description="Danger luôn hủy override và chuyển về AUTO. Chuông tắt tay chỉ tắt còi; "
            "DANGER mới tự bật lại chuông.",
        ),
        12,
        7,
    )
    L.add(
        state_timeline(
            "Actuator mong muốn",
            [
                prom(f"max by (output) (aiot_actuator_desired_output{{{node}}})", "{{output}}"),
            ],
            mappings=ONOFF_MAP,
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "gas_valve_closed"},
                    "properties": [
                        {"id": "mappings", "value": VALVE_MAP},
                        {"id": "displayName", "value": "Van gas"},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "fan_on"},
                    "properties": [{"id": "displayName", "value": "Quạt hút"}],
                },
                {
                    "matcher": {"id": "byName", "options": "exhaust_on"},
                    "properties": [{"id": "displayName", "value": "Xả khí"}],
                },
                {
                    "matcher": {"id": "byName", "options": "alarm_on"},
                    "properties": [{"id": "displayName", "value": "Còi"}],
                },
                {
                    "matcher": {"id": "byName", "options": "suppression_on"},
                    "properties": [
                        {"id": "displayName", "value": "Dập lửa"},
                        {
                            "id": "mappings",
                            "value": value_map((0, "Sẵn sàng", GREY), (1, "PHUN", BLUE)),
                        },
                    ],
                },
            ],
        ),
        12,
        7,
    )
    L.add(
        timeseries(
            "Lệnh actuator",
            [
                prom(
                    f"sum by (mode, trigger) (increase(aiot_control_commands_total{{{node}}}[$__rate_interval]))",
                    "{{mode}} · {{trigger}}",
                ),
            ],
            unit="short",
            draw="bars",
            fill=80,
            stack=True,
            decimals=0,
            legend_calcs=("sum",),
            gradient="none",
            overrides=[
                color_override("auto · normal", MINT),
                color_override("auto · warning", AMBER),
                color_override("auto · danger", RED),
                color_override("manual · operator", PURPLE),
                color_override("auto · cancel", BLUE),
            ],
            description="Chỉ phát khi trạng thái mong muốn đổi — không có lệnh lặp mỗi giây.",
        ),
        10,
        8,
    )
    L.add(
        timeseries(
            "Command → ACK",
            [
                prom("aiot:ack_latency:p95", "ACK p95", "A"),
                prom(f"sum(aiot_actuator_pending_commands{{{node}}})", "Lệnh chờ ACK", "B"),
                prom(
                    f"sum(increase(aiot_actuator_ack_timeouts_total{{{node}}}[$__rate_interval]))",
                    "Timeout",
                    "C",
                ),
            ],
            unit="s",
            fill=10,
            min_=0,
            span_nulls=False,
            overrides=[
                color_override("ACK p95", MINT),
                color_override(
                    "Lệnh chờ ACK",
                    AMBER,
                    [
                        {"id": "unit", "value": "short"},
                        {"id": "custom.axisPlacement", "value": "right"},
                    ],
                ),
                color_override(
                    "Timeout",
                    RED,
                    [
                        {"id": "unit", "value": "short"},
                        {"id": "custom.drawStyle", "value": "bars"},
                        {"id": "custom.axisPlacement", "value": "right"},
                    ],
                ),
            ],
            description="ACK timeout 5s. Thiết bị chỉ được xem là đã đổi trạng thái khi có ACK.",
        ),
        10,
        8,
    )
    dedup = (
        f"sum(increase(aiot_control_commands_deduplicated_total{{{node}}}[15m])) / "
        f"(sum(increase(aiot_control_commands_deduplicated_total{{{node}}}[15m])) + "
        f"sum(increase(aiot_control_commands_total{{{node}}}[15m])))"
    )
    L.add(
        stat(
            "Lệnh trùng bị chặn",
            [prom(dedup)],
            unit="percentunit",
            decimals=1,
            thresholds=steps((None, AMBER), (0.8, MINT)),
            graph="none",
            description="15 phút gần nhất: tỉ lệ prediction không cần phát lệnh MQTT nhờ deduplicate desired state.",
        ),
        4,
        8,
    )

    L.row("⑦ Bridge")
    L.add(
        stat(
            "Bridge ↔ Mosquitto",
            [prom('aiot_mqtt_connected{job="mqtt-kafka-bridge"}', "Bridge")],
            mappings=value_map((0, "DISCONNECTED", "red"), (1, "CONNECTED", "green")),
            thresholds=steps((None, RED), (1, "green")),
            graph="none",
        ),
        6,
        7,
    )
    L.add(
        stat(
            "Dead-letter",
            [prom("aiot:dead_letter:increase5m")],
            decimals=0,
            graph="none",
            thresholds=steps((None, MINT), (1, RED)),
            unit="short",
            description="Payload bị bridge từ chối trong 5 phút gần nhất.",
        ),
        6,
        7,
    )
    L.add(
        timeseries(
            "Payload lỗi & sequence gap",
            [
                prom(
                    "sum by (reason) (increase(aiot_mqtt_invalid_messages_total[$__rate_interval]))",
                    "invalid: {{reason}}",
                    "A",
                ),
                prom(
                    f"sum by (node_id, event_type) (increase(aiot_sequence_gaps_total{{{node}}}[$__rate_interval]))",
                    "gap: {{node_id}}/{{event_type}}",
                    "B",
                ),
            ],
            unit="short",
            draw="bars",
            fill=70,
            gradient="none",
            legend_calcs=("sum",),
            description="Payload lỗi được đẩy sang gas.dead-letter.v1, không chặn pipeline.",
        ),
        12,
        7,
    )

    L.row("⑧ Kafka")
    L.add(
        stat(
            "Brokers",
            [prom("kafka_brokers", "brokers")],
            graph="none",
            thresholds=steps((None, RED), (1, "green")),
        ),
        4,
        5,
    )
    L.add(
        stat(
            "Consumer groups",
            [prom("count(aiot:consumer_lag:sum)", "groups")],
            decimals=0,
            graph="none",
            thresholds=steps((None, BLUE)),
            description="influx-sink-v1 · safety-controller-v1 · kitchen-demo-ui-v1",
        ),
        4,
        5,
    )
    L.add(
        stat(
            "Partitions",
            [
                prom(
                    'sum by (topic) (kafka_topic_partitions{topic=~"gas[.].*"})',
                    "{{topic}}",
                )
            ],
            color_mode="value",
            text_mode="value_and_name",
            graph="none",
            thresholds=steps((None, BLUE)),
        ),
        16,
        5,
    )
    L.add(
        timeseries(
            "Consumer lag",
            [prom("aiot:consumer_lag:sum", "{{consumergroup}}")],
            unit="short",
            fill=10,
            min_=0,
            thresholds=steps((None, MINT), (100, RED)),
            threshold_style="dashed",
            description="kitchen-demo-ui-v1 · influx-sink-v1 · safety-controller-v1",
        ),
        12,
        7,
    )
    L.add(
        timeseries(
            "Topic append rate",
            [
                prom(
                    'sum by (topic) (rate(kafka_topic_partition_current_offset{topic=~"gas[.].*"}[1m]))',
                    "{{topic}}",
                )
            ],
            unit="suffix: rec/s",
            fill=6,
        ),
        12,
        7,
    )
    L.add(
        timeseries(
            "Bridge → Kafka delivery",
            [
                prom(
                    "sum by (topic) (rate(aiot_kafka_messages_produced_total[1m]))",
                    "ok {{topic}}",
                    "A",
                ),
                prom(
                    "sum by (topic) (rate(aiot_kafka_delivery_failures_total[1m]))",
                    "fail {{topic}}",
                    "B",
                ),
            ],
            unit="suffix: msg/s",
            fill=6,
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": "fail.*"},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": RED}}],
                }
            ],
        ),
        24,
        7,
    )

    L.row("⑨ InfluxDB")
    L.add(
        timeseries(
            "Latency end-to-end p95",
            [prom("aiot:pipeline_latency:p95", "{{measurement}}")],
            unit="s",
            fill=0,
            min_=0,
            description="Từ observed_at tới khi InfluxDB xác nhận batch.",
        ),
        12,
        7,
    )
    L.add(
        timeseries(
            "Kafka → InfluxDB",
            [
                prom(
                    'sum by (topic) (rate(aiot_kafka_messages_consumed_total{job="influx-sink"}[1m]))',
                    "consume {{topic}}",
                    "A",
                ),
                prom(
                    'sum by (measurement) (rate(aiot_influx_points_written_total{job="influx-sink"}[1m]))',
                    "write {{measurement}}",
                    "B",
                ),
            ],
            unit="suffix: ev/s",
            fill=4,
            description="Record sink đọc từ Kafka (consume) và point ghi vào InfluxDB (write) mỗi giây.",
        ),
        12,
        7,
    )
    L.add(
        stat(
            "Influx write p95",
            [prom("aiot:influx_write:p95")],
            unit="s",
            decimals=3,
            thresholds=steps((None, MINT), (0.25, AMBER), (1, RED)),
        ),
        6,
        5,
    )
    L.add(
        stat(
            "Batch size p95",
            [
                prom(
                    'histogram_quantile(0.95, sum by (le) (rate(aiot_influx_batch_size_bucket{job="influx-sink"}[5m])))'
                )
            ],
            decimals=0,
            thresholds=steps((None, BLUE)),
        ),
        6,
        5,
    )
    L.add(
        stat(
            "Lần ghi cuối",
            [prom('time() - aiot_influx_last_success_timestamp_seconds{job="influx-sink"}')],
            unit="s",
            decimals=1,
            graph="none",
            thresholds=steps((None, MINT), (5, AMBER), (15, RED)),
        ),
        6,
        5,
    )
    L.add(
        stat(
            "Sink errors",
            [
                prom(
                    'sum(increase(aiot_influx_write_failures_total{job="influx-sink"}[5m]))',
                    "write failures",
                    "A",
                ),
                prom(
                    'sum(increase(aiot_sink_invalid_events_total{job="influx-sink"}[5m]))',
                    "invalid events",
                    "B",
                ),
            ],
            decimals=0,
            graph="none",
            text_mode="value_and_name",
            thresholds=steps((None, MINT), (1, RED)),
        ),
        6,
        5,
    )

    variables = [
        {
            "name": "node",
            "label": "Node",
            "type": "query",
            "datasource": PROM,
            "query": {
                "query": 'label_values(aiot_last_event_timestamp_seconds{job="mqtt-kafka-bridge"}, node_id)',
                "refId": "PrometheusVariableQueryEditor-VariableQuery",
                "qryType": 1,
            },
            "definition": 'label_values(aiot_last_event_timestamp_seconds{job="mqtt-kafka-bridge"}, node_id)',
            "includeAll": True,
            "allValue": ".*",
            "multi": True,
            "refresh": 2,
            "sort": 1,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "options": [],
        }
    ]
    return dashboard(
        "aiot-pipeline-operations",
        "AIoT Pipeline Operations",
        "Sức khỏe end-to-end: luồng dữ liệu, latency, control plane, MQTT, Kafka, InfluxDB.",
        L.panels,
        variables=variables,
        links=LINKS,
        annotations=[DANGER_ANNOTATION, OVERRIDE_ANNOTATION],
        tags=["aiot", "pipeline"],
        time_from="now-30m",
    )


def sensors_section(L: "Layout", node: str) -> None:
    L.row("② Thiết bị & cảm biến")
    L.add(
        stat(
            "Node online",
            [
                prom("count(aiot:sensor_age:max < 10) or vector(0)", "online", "A"),
                prom("count(aiot:sensor_age:max) or vector(0)", "đã biết", "B"),
            ],
            decimals=0,
            graph="none",
            text_mode="value_and_name",
            thresholds=steps((None, RED), (1, MINT)),
            description="Node có sensor reading trong 10 giây gần nhất / mọi node từng gửi.",
        ),
        4,
        6,
    )
    L.add(
        stat(
            "Kênh cảm biến",
            [prom(f"count(aiot_sensor_reading{{{node}}}) or vector(0)", "kênh")],
            decimals=0,
            graph="none",
            thresholds=steps((None, RED), (16, MINT)),
            description="16 kênh MOX mỗi node: 4 × TGS2602/2600/2610/2620.",
        ),
        4,
        6,
    )
    L.add(
        stat(
            "Gói/giây",
            [
                prom(
                    'sum by (event_type) (rate(aiot_mqtt_messages_received_total{job="mqtt-kafka-bridge",'
                    'event_type=~"readings|predictions"}[1m]))',
                    "{{event_type}}",
                )
            ],
            unit="suffix: /s",
            decimals=1,
            color_mode="value",
            text_mode="value_and_name",
            graph="none",
            thresholds=steps((None, RED), (0.05, MINT)),
            description="Mỗi node gửi readings (16 kênh) và predictions (ppm + alarm).",
        ),
        8,
        6,
    )
    L.add(
        stat(
            "Nồng độ khí",
            [prom(f'max by (gas) (aiot_gas_ppm{{source="prediction",{node}}})', "{{gas}}")],
            unit="ppm",
            decimals=1,
            color_mode="value",
            text_mode="value_and_name",
            graph="none",
            thresholds=steps((None, MINT), (50, AMBER), (200, RED)),
            description="CO và C₂H₄ mới nhất do edge model dự đoán.",
        ),
        8,
        6,
    )

    nodes = table(
        "Danh sách node",
        [
            prom(
                'time() - max by (node_id) (aiot_last_event_timestamp_seconds{event_type="readings"})',
                "",
                "A",
                instant=True,
                range=False,
                format="table",
            ),
            prom(
                "max by (node_id) (aiot_alarm_level)",
                "",
                "B",
                instant=True,
                range=False,
                format="table",
            ),
            prom(
                "sum by (node_id) (increase(aiot_sequence_gaps_total[1h])) or "
                'max by (node_id) (aiot_last_event_timestamp_seconds{event_type="readings"}) * 0',
                "",
                "C",
                instant=True,
                range=False,
                format="table",
            ),
            prom(
                "max by (node_id) (aiot_direct_override_active)",
                "",
                "D",
                instant=True,
                range=False,
                format="table",
            ),
        ],
        description="Mỗi dòng là một Edge node. Tuổi dữ liệu > 10 s nghĩa là node đã ngừng gửi.",
        transformations=[
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True},
                    "renameByName": {
                        "node_id": "Node",
                        "Value #A": "Gói mới nhất (s trước)",
                        "Value #B": "Alarm",
                        "Value #C": "Mất gói (1 giờ)",
                        "Value #D": "Chế độ",
                    },
                    "indexByName": {
                        "node_id": 0,
                        "Value #A": 1,
                        "Value #B": 2,
                        "Value #D": 3,
                        "Value #C": 4,
                    },
                },
            },
        ],
        overrides=[
            field(
                "Gói mới nhất (s trước)",
                ("unit", "s"),
                ("decimals", 1),
                ("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
                ("thresholds", steps((None, "green"), (5, "orange"), (10, "red"))),
            ),
            field(
                "Alarm",
                ("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
                (
                    "mappings",
                    value_map(
                        (0, "NORMAL", "green"), (1, "WARNING", "orange"), (2, "DANGER", "red")
                    ),
                ),
            ),
            field(
                "Chế độ",
                ("mappings", value_map((0, "AUTO", BLUE), (1, "DIRECT", PURPLE))),
                ("custom.cellOptions", {"type": "color-text"}),
            ),
            field(
                "Mất gói (1 giờ)",
                ("decimals", 0),
                ("custom.cellOptions", {"type": "color-text"}),
                ("thresholds", steps((None, "green"), (1, "orange"))),
            ),
        ],
    )
    L.add(nodes, 24, 5)

    channels = table(
        "Giá trị 16 kênh",
        [
            prom(
                f"aiot_sensor_reading{{{node}}}", "", "A", instant=True, range=False, format="table"
            ),
            prom(
                f"100 * (aiot_sensor_reading{{{node}}} / min_over_time(aiot_sensor_reading{{{node}}}[30m]) - 1)",
                "",
                "B",
                instant=True,
                range=False,
                format="table",
            ),
        ],
        description="Giá trị thô của từng kênh MOX (độ dẫn tăng khi có khí) và mức tăng so với nền "
        "thấp nhất 30 phút. Kênh tăng > 50% là đang phản ứng mạnh với khí.",
        transformations=[
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "__name__": True,
                        "instance": True,
                        "job": True,
                    },
                    "renameByName": {
                        "channel": "Kênh · loại · vị trí",
                        "node_id": "Node",
                        "sensor_type": "Loại",
                        "Value #A": "Giá trị",
                        "Value #B": "Tăng so với nền",
                    },
                    "indexByName": {
                        "channel": 0,
                        "Value #A": 1,
                        "Value #B": 2,
                        "sensor_type": 3,
                        "node_id": 4,
                    },
                },
            },
            {"id": "sortBy", "options": {"sort": [{"field": "Tăng so với nền", "desc": True}]}},
        ],
        overrides=[
            field(
                "Kênh · loại · phát hiện",
                ("mappings", text_map(CHANNEL_TEXT)),
                ("custom.width", 330),
            ),
            field(
                "Giá trị",
                ("decimals", 0),
                ("min", 0),
                ("max", 9000),
                (
                    "custom.cellOptions",
                    {"type": "gauge", "mode": "gradient", "valueDisplayMode": "text"},
                ),
                ("color", {"mode": "continuous-GrYlRd"}),
            ),
            field(
                "Tăng so với nền",
                ("unit", "percent"),
                ("decimals", 0),
                ("custom.cellOptions", {"type": "color-background", "mode": "gradient"}),
                (
                    "thresholds",
                    steps((None, "green"), (15, "#f4d35e"), (50, "orange"), (150, "red")),
                ),
            ),
            field("Loại", ("custom.width", 90)),
            field("Node", ("custom.width", 140)),
        ],
    )
    L.add(channels, 12, 15)
    L.add(
        state_timeline(
            "Phản ứng từng kênh",
            [
                prom(
                    # Zero-padded "ch" label (S01..S16) keeps the rows in channel order.
                    "label_replace(label_replace("
                    f"aiot_sensor_reading{{{node}}} / min_over_time(aiot_sensor_reading{{{node}}}[30m]), "
                    '"ch", "S0$1", "channel", "S([1-9])"), "ch", "$1", "channel", "(S1[0-6])")',
                    "{{ch}} · {{sensor_type}}",
                )
            ],
            description="Giá trị / nền 30 phút của từng kênh. Mỗi hàng là một kênh. Xanh = như nền sạch, vàng/cam/đỏ = kênh đang tăng do có khí. "
            "Nhìn theo cột để thấy cả mảng 16 cảm biến cùng phản ứng.",
            row_height=0.85,
        ),
        12,
        15,
    )
    timeline = L.panels[-1]
    timeline["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    timeline["fieldConfig"]["defaults"]["thresholds"] = steps(
        (None, "#2f9e78"), (1.15, "#f4d35e"), (1.5, "orange"), (2.5, "red")
    )
    timeline["fieldConfig"]["defaults"]["decimals"] = 2
    timeline["options"]["showValue"] = "never"
    timeline["options"]["mergeValues"] = True
    timeline["options"]["legend"] = {
        "showLegend": True,
        "displayMode": "list",
        "placement": "bottom",
    }


def mqtt_section(L: "Layout", node: str) -> None:
    L.row("③ MQTT")
    broker = 'aiot_mqtt_broker_stat{job="mqtt-kafka-bridge",name="%s"}'
    for title, name, desc, w in [
        (
            "Client",
            "clients_connected",
            "Edge/simulator, bridge, controller, UI, observer.",
            4,
        ),
        ("Subscription", "subscriptions", "Tổng số subscription đang mở trên broker.", 4),
        (
            "Retained message",
            "retained_messages",
            "ACK thiết bị được giữ lại để client mới biết trạng thái.",
            4,
        ),
    ]:
        L.add(
            stat(
                title,
                [prom(broker % name, title)],
                decimals=0,
                graph="none",
                color_mode="value",
                thresholds=steps((None, PURPLE)),
                description=desc,
            ),
            w,
            5,
        )
    L.add(
        stat(
            "Broker nhận / gửi",
            [
                prom(broker % "load_received_1min", "nhận"),
                prom(broker % "load_sent_1min", "gửi", "B"),
            ],
            decimals=0,
            graph="none",
            color_mode="value",
            text_mode="value_and_name",
            thresholds=steps((None, PURPLE)),
            description="msg/phút, trung bình 1 phút do Mosquitto tự báo ($SYS), gồm cả message hệ thống.",
        ),
        6,
        5,
    )
    L.add(
        stat(
            "Uptime broker",
            [prom(broker % "uptime_seconds", "uptime")],
            unit="s",
            decimals=0,
            graph="none",
            color_mode="value",
            thresholds=steps((None, PURPLE)),
        ),
        6,
        5,
    )

    topics = table(
        "MQTT topic → Kafka topic",
        [
            prom(
                'sum by (event_type) (rate(aiot_mqtt_messages_received_total{job="mqtt-kafka-bridge"}[1m]))',
                "",
                "A",
                instant=True,
                range=False,
                format="table",
            ),
            prom(
                'sum by (event_type) (rate(aiot_mqtt_bytes_received_total{job="mqtt-kafka-bridge"}[1m]))',
                "",
                "B",
                instant=True,
                range=False,
                format="table",
            ),
            prom(
                'sum by (event_type) (rate(aiot_mqtt_bytes_received_total{job="mqtt-kafka-bridge"}[5m])) / '
                '(sum by (event_type) (rate(aiot_mqtt_messages_received_total{job="mqtt-kafka-bridge"}[5m])) > 0)',
                "",
                "C",
                instant=True,
                range=False,
                format="table",
            ),
            prom(
                'time() - max by (event_type) (aiot_last_event_timestamp_seconds{job="mqtt-kafka-bridge"})',
                "",
                "D",
                instant=True,
                range=False,
                format="table",
            ),
            prom(
                'sum by (event_type) (increase(aiot_mqtt_messages_received_total{job="mqtt-kafka-bridge"}[1h]))',
                "",
                "E",
                instant=True,
                range=False,
                format="table",
            ),
        ],
        description="Bridge chỉ subscribe các suffix trong allow-list. Mỗi MQTT topic map 1-1 sang một Kafka topic.",
        transformations=[
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True},
                    "renameByName": {
                        "event_type": "MQTT topic → Kafka topic",
                        "Value #A": "msg/s",
                        "Value #B": "bytes/s",
                        "Value #C": "Kích thước payload",
                        "Value #D": "Lần nhận cuối",
                        "Value #E": "Message (1 giờ)",
                    },
                    "indexByName": {
                        "event_type": 0,
                        "Value #A": 1,
                        "Value #B": 2,
                        "Value #C": 3,
                        "Value #D": 4,
                        "Value #E": 5,
                    },
                },
            },
        ],
        overrides=[
            field(
                "MQTT topic → Kafka topic",
                ("mappings", text_map(TOPIC_TEXT)),
                ("custom.width", 470),
            ),
            field(
                "msg/s",
                ("decimals", 2),
                ("custom.cellOptions", {"type": "color-text"}),
                ("thresholds", steps((None, GREY), (0.01, MINT))),
            ),
            field("bytes/s", ("unit", "Bps"), ("decimals", 0)),
            field("Kích thước payload", ("unit", "decbytes"), ("decimals", 0)),
            field(
                "Lần nhận cuối",
                ("unit", "s"),
                ("decimals", 1),
                ("custom.cellOptions", {"type": "color-text"}),
                ("thresholds", steps((None, MINT), (10, AMBER), (300, GREY))),
            ),
            field("Message (1 giờ)", ("decimals", 0)),
        ],
    )
    L.add(topics, 16, 9)
    L.add(
        table(
            "Publisher / subscriber",
            [
                prom(
                    'sum by (event_type) (rate(aiot_mqtt_messages_received_total{job="mqtt-kafka-bridge"}[1m]))',
                    "",
                    "A",
                    instant=True,
                    range=False,
                    format="table",
                )
            ],
            transformations=[
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True, "Value": True},
                        "renameByName": {"event_type": "Topic suffix"},
                    },
                }
            ],
            overrides=[field("Topic suffix", ("mappings", text_map(TOPIC_ROLE)))],
            description="Publisher → subscriber của từng suffix MQTT.",
        ),
        8,
        9,
    )
    L.add(
        timeseries(
            "MQTT msg/s",
            [
                prom(
                    'sum by (event_type) (rate(aiot_mqtt_messages_received_total{job="mqtt-kafka-bridge"}[1m]))',
                    "{{event_type}}",
                )
            ],
            unit="suffix: msg/s",
            fill=6,
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "Latency Edge → Bridge p95",
            [
                prom(
                    'histogram_quantile(0.95, sum by (le, event_type) (rate(aiot_event_age_seconds_bucket{event_type=~"readings|predictions"}[1m])))',
                    "{{event_type}}",
                )
            ],
            unit="s",
            fill=0,
            min_=0,
            description="observed_at trên thiết bị → bridge nhận từ Mosquitto. Đây là chi phí của chặng MQTT.",
        ),
        8,
        8,
    )
    L.add(
        timeseries(
            "Mosquitto vào / ra",
            [
                prom(
                    'rate(aiot_mqtt_broker_stat{job="mqtt-kafka-bridge",name=~"messages_received|messages_sent"}[1m])',
                    "broker {{name}}",
                )
            ],
            unit="suffix: msg/s",
            fill=6,
            description="Ra > vào vì một message có thể đi tới nhiều subscriber (bridge, thiết bị, observer).",
        ),
        8,
        8,
    )


# --------------------------------------------------------------------------- gas monitoring
RANGE = "from(bucket: v.defaultBucket) |> range(start: v.timeRangeStart, stop: v.timeRangeStop)"
CO_STEPS = steps((None, "green"), (50, "orange"), (200, "red"))
ETH_STEPS = steps((None, "green"), (3, "orange"))
NODE = "r.node_id =~ /^${node:regex}$/"


ACTUATOR_LABEL = (
    "|> map(fn: (r) => ({_time: r._time, _value: if r._value then 1 else 0, "
    '_field: if r._field == "fan_on" then "Quạt hút" else if r._field == "exhaust_on" then "Xả khí" '
    'else if r._field == "gas_valve_closed" then "Van gas" else if r._field == "suppression_on" then "Dập lửa" '
    'else if r._field == "alarm_muted" then "Chuông tắt tay" else "Còi"}))'
)
ACTUATOR_FILTER = (
    'r._measurement == "actuator_state" and (r._field == "fan_on" or r._field == "exhaust_on" '
    f'or r._field == "gas_valve_closed" or r._field == "alarm_on" or r._field == "suppression_on" '
    f'or r._field == "alarm_muted") and {NODE}'
)
MUTED_MAP = value_map((0, "—", GREY), (1, "TẮT TAY", AMBER))


def predicted(field: str) -> str:
    """Edge-model estimate of one gas, one series per node."""
    return (
        f'{RANGE} |> filter(fn: (r) => r._measurement == "gas_prediction" and r._field == "{field}" and {NODE}) '
        '|> keep(columns: ["_time", "_value", "node_id"]) |> group(columns: ["node_id"]) |> sort(columns: ["_time"]) '
        "|> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false) "
        '|> map(fn: (r) => ({_time: r._time, _value: r._value, node_id: r.node_id, _field: "Dự đoán"})) '
        '|> group(columns: ["node_id", "_field"])'
    )


def latest(measurement: str, field: str) -> str:
    return (
        f'{RANGE} |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}" and {NODE}) '
        '|> keep(columns: ["_time", "_value", "node_id"]) |> group(columns: ["node_id"]) |> sort(columns: ["_time"]) '
        "|> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)"
    )


def gas() -> dict:
    L = Layout()
    L.row("Trạng thái hiện tại")
    for title, measurement, field_name, decimals, thresholds, description in (
        (
            "CO dự đoán",
            "gas_prediction",
            "co_ppm",
            1,
            CO_STEPS,
            "TabM: TB 5 s của CO ≥ 200 ppm → danger.",
        ),
        ("C₂H₄ dự đoán", "gas_prediction", "ethylene_ppm", 2, ETH_STEPS, "Thang dataset 0–20 ppm."),
    ):
        L.add(
            stat(
                title,
                [flux(latest(measurement, field_name))],
                ds=FLUX,
                unit="ppm",
                decimals=decimals,
                thresholds=thresholds,
                description=description,
            ),
            5,
            5,
        )
    L.add(
        stat(
            "Alarm level",
            [
                flux(
                    f'{RANGE} |> filter(fn: (r) => r._measurement == "gas_prediction" and r._field == "co_ppm" and {NODE}) '
                    '|> map(fn: (r) => ({_time: r._time, node_id: r.node_id, _value: if r.alarm_level == "danger" then 2 '
                    'else if r.alarm_level == "warning" then 1 else 0})) |> group(columns: ["node_id"]) |> sort(columns: ["_time"]) |> last()'
                )
            ],
            ds=FLUX,
            mappings=ALARM_MAP,
            graph="none",
            thresholds=steps((None, "green"), (1, "orange"), (2, "red")),
        ),
        4,
        5,
    )
    L.add(
        stat(
            "Actuator",
            [
                flux(
                    f"from(bucket: v.defaultBucket) |> range(start: -30d) |> filter(fn: (r) => {ACTUATOR_FILTER}) "
                    f'{ACTUATOR_LABEL} |> group(columns: ["_field"]) |> sort(columns: ["_time"]) |> last()'
                )
            ],
            ds=FLUX,
            mappings=ONOFF_MAP,
            graph="none",
            text_mode="value_and_name",
            thresholds=steps((None, GREY)),
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "Van gas"},
                    "properties": [{"id": "mappings", "value": VALVE_MAP}],
                },
                {
                    "matcher": {"id": "byName", "options": "Dập lửa"},
                    "properties": [
                        {
                            "id": "mappings",
                            "value": value_map((0, "Sẵn sàng", GREY), (1, "PHUN", BLUE)),
                        }
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Chuông tắt tay"},
                    "properties": [{"id": "mappings", "value": MUTED_MAP}],
                },
            ],
            description="Trạng thái thiết bị đã xác nhận (ActuatorState), không phải lệnh.",
        ),
        10,
        5,
    )

    L.row("Dự đoán")
    for title, field_name, thresholds in (
        (
            "CO (ppm)",
            "co_ppm",
            steps((None, "transparent"), (200, "rgba(255,107,98,0.12)")),
        ),
        ("C₂H₄ (ppm)", "ethylene_ppm", steps((None, "transparent"))),
    ):
        L.add(
            timeseries(
                title,
                [flux(predicted(field_name))],
                ds=FLUX,
                unit="ppm",
                fill=8,
                threshold_style="dashed+area",
                thresholds=thresholds,
                overrides=[
                    color_override("Dự đoán", MINT, [{"id": "custom.lineWidth", "value": 2}])
                ],
                min_=0,
            ),
            12,
            9,
        )

    L.row("An toàn & điều khiển")
    L.add(
        state_timeline(
            "Lịch sử alarm",
            [
                flux(
                    f'{RANGE} |> filter(fn: (r) => r._measurement == "gas_prediction" and r._field == "co_ppm" and {NODE}) '
                    '|> map(fn: (r) => ({_time: r._time, node_id: r.node_id, _value: if r.alarm_level == "danger" then 2 '
                    'else if r.alarm_level == "warning" then 1 else 0})) |> group(columns: ["node_id"]) |> sort(columns: ["_time"]) '
                    "|> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false)"
                )
            ],
            ds=FLUX,
            mappings=ALARM_MAP,
            row_height=0.7,
        ),
        16,
        5,
    )
    L.add(siren_switch(), 8, 5)
    L.add(
        state_timeline(
            "Lịch sử actuator",
            [
                flux(
                    f"from(bucket: v.defaultBucket) |> range(start: -30d, stop: v.timeRangeStop) "
                    f"|> filter(fn: (r) => {ACTUATOR_FILTER}) {ACTUATOR_LABEL} "
                    '|> group(columns: ["_field"]) |> sort(columns: ["_time"])'
                )
            ],
            ds=FLUX,
            mappings=ONOFF_MAP,
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "Van gas"},
                    "properties": [{"id": "mappings", "value": VALVE_MAP}],
                },
                {
                    "matcher": {"id": "byName", "options": "Dập lửa"},
                    "properties": [
                        {
                            "id": "mappings",
                            "value": value_map((0, "Sẵn sàng", GREY), (1, "PHUN", BLUE)),
                        }
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Chuông tắt tay"},
                    "properties": [{"id": "mappings", "value": MUTED_MAP}],
                },
            ],
            description="Mỗi đoạn là một ActuatorState ACK; giữ nguyên tới ACK kế tiếp. "
            "Chuông tắt tay: operator tắt còi, chế độ vẫn AUTO.",
        ),
        24,
        7,
    )
    audit = {
        "type": "table",
        "title": "Audit trail lệnh",
        "datasource": FLUX,
        "description": "Command do Safety Controller phát (auto hoặc direct override). "
        "alarm_muted = operator đã tắt chuông; lệnh vẫn ở chế độ AUTO.",
        "targets": [
            flux(
                f'{RANGE} |> filter(fn: (r) => r._measurement == "actuator_command" and {NODE}) '
                '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") |> group() '
                '|> sort(columns: ["_time"], desc: true) |> limit(n: 100) '
                '|> keep(columns: ["_time", "node_id", "mode", "source", "reason", "fan_on", "exhaust_on", '
                '"gas_valve_closed", "alarm_on", "alarm_muted", "suppression_on"])'
            )
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "left", "cellOptions": {"type": "auto"}},
                "noValue": "Chưa có lệnh trong khung thời gian",
            },
            "overrides": [
                *[
                    {
                        "matcher": {"id": "byName", "options": name},
                        "properties": [
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {
                                "id": "mappings",
                                "value": value_map(("true", on, on_color), ("false", off, GREY)),
                            },
                            {"id": "custom.width", "value": 90},
                        ],
                    }
                    for name, on, off, on_color in [
                        ("fan_on", "Bật", "Tắt", MINT),
                        ("exhaust_on", "Bật", "Tắt", MINT),
                        ("alarm_on", "Bật", "Tắt", AMBER),
                        ("alarm_muted", "Tắt tay", "—", AMBER),
                        ("gas_valve_closed", "Khóa", "Mở", RED),
                        ("suppression_on", "PHUN", "—", BLUE),
                    ]
                ],
                {
                    "matcher": {"id": "byName", "options": "mode"},
                    "properties": [
                        {
                            "id": "custom.cellOptions",
                            "value": {"type": "color-background", "mode": "basic"},
                        },
                        {
                            "id": "mappings",
                            "value": value_map(
                                ("auto", "AUTO", BLUE), ("manual", "DIRECT", PURPLE)
                            ),
                        },
                        {"id": "custom.width", "value": 90},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "_time"},
                    "properties": [{"id": "custom.width", "value": 180}],
                },
            ],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "fields": ""},
        },
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "indexByName": {
                        "_time": 0,
                        "node_id": 1,
                        "mode": 2,
                        "source": 3,
                        "fan_on": 4,
                        "exhaust_on": 5,
                        "gas_valve_closed": 6,
                        "alarm_on": 7,
                        "alarm_muted": 8,
                        "suppression_on": 9,
                        "reason": 10,
                    },
                    "renameByName": {
                        "_time": "Thời điểm",
                        "node_id": "Node",
                        "source": "Nguồn",
                        "reason": "Lý do",
                    },
                },
            }
        ],
    }
    L.add(audit, 24, 8)

    L.row("Log")
    prediction_log = {
        "type": "table",
        "title": "Log dự đoán",
        "description": "Mỗi dòng là một lần model đọc cửa sổ 60 s của 16 kênh.",
        "datasource": FLUX,
        "targets": [
            flux(
                f'{RANGE} |> filter(fn: (r) => r._measurement == "gas_prediction" and {NODE}) '
                '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") |> group() '
                '|> keep(columns: ["_time", "node_id", "alarm_level", "sequence", "co_ppm", "ethylene_ppm", '
                '"inference_ms", "window_s"]) '
                '|> sort(columns: ["_time"], desc: true) |> limit(n: 60)'
            )
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "left", "cellOptions": {"type": "auto"}},
                "noValue": "Chưa có dự đoán",
            },
            "overrides": [
                field(
                    "alarm_level",
                    ("custom.cellOptions", {"type": "color-background", "mode": "basic"}),
                    (
                        "mappings",
                        value_map(
                            ("normal", "NORMAL", "green"),
                            ("warning", "WARNING", "orange"),
                            ("danger", "DANGER", "red"),
                        ),
                    ),
                ),
                field(
                    "co_ppm",
                    ("decimals", 1),
                    ("unit", "ppm"),
                    ("custom.cellOptions", {"type": "color-text"}),
                    ("thresholds", steps((None, "green"), (50, "orange"), (200, "red"))),
                ),
                field(
                    "ethylene_ppm",
                    ("decimals", 2),
                    ("unit", "ppm"),
                    ("custom.cellOptions", {"type": "color-text"}),
                    ("thresholds", steps((None, "green"), (3, "orange"))),
                ),
                field("inference_ms", ("decimals", 2), ("unit", "ms")),
                field("window_s", ("unit", "s"), ("decimals", 0)),
                field("sequence", ("decimals", 0)),
            ],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "fields": ""},
        },
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "indexByName": {
                        "_time": 0,
                        "sequence": 1,
                        "node_id": 2,
                        "co_ppm": 3,
                        "ethylene_ppm": 4,
                        "alarm_level": 5,
                        "inference_ms": 6,
                        "window_s": 7,
                    },
                    "renameByName": {
                        "_time": "Thời điểm",
                        "sequence": "seq",
                        "node_id": "Node",
                        "co_ppm": "CO dự đoán",
                        "ethylene_ppm": "C₂H₄ dự đoán",
                        "alarm_level": "Alarm",
                        "inference_ms": "Suy luận",
                        "window_s": "Cửa sổ",
                    },
                },
            }
        ],
    }
    L.add(prediction_log, 24, 9)
    channel_order = {f"s{i}": i + 1 for i in range(1, 17)}
    sensor_log = {
        "type": "table",
        "title": "Log cảm biến",
        "description": "Giá trị MOX của từng kênh mỗi giây (sensor_reading). Màu càng nóng, độ dẫn càng cao do có khí.",
        "datasource": FLUX,
        "targets": [
            flux(
                f'{RANGE} |> filter(fn: (r) => r._measurement == "sensor_reading" and {NODE}) '
                '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") |> group() '
                '|> sort(columns: ["_time"], desc: true) |> limit(n: 40)'
            )
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "align": "center",
                    "cellOptions": {"type": "color-background", "mode": "gradient"},
                },
                "color": {"mode": "continuous-GrYlRd"},
                "min": 0,
                "max": 9000,
                "decimals": 0,
            },
            "overrides": [
                field("_time", ("custom.cellOptions", {"type": "auto"}), ("custom.width", 170)),
                field("sequence", ("custom.cellOptions", {"type": "auto"}), ("custom.width", 70)),
                field("node_id", ("custom.cellOptions", {"type": "auto"}), ("custom.width", 120)),
            ],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "fields": ""},
        },
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "_measurement": True,
                        "_start": True,
                        "_stop": True,
                        "schema_version": True,
                    },
                    "indexByName": {"_time": 0, "sequence": 1, **channel_order, "node_id": 18},
                    "renameByName": {
                        "_time": "Thời điểm",
                        "sequence": "seq",
                        "node_id": "Node",
                        **{f"s{i}": f"S{i}" for i in range(1, 17)},
                    },
                },
            }
        ],
    }
    L.add(sensor_log, 24, 9)

    L.row("Hiệu năng model")
    L.add(
        timeseries(
            "Inference latency",
            [
                flux(
                    f'{RANGE} |> filter(fn: (r) => r._measurement == "gas_prediction" and r._field == "inference_ms" and {NODE}) '
                    '|> keep(columns: ["_time", "_value"]) |> sort(columns: ["_time"]) '
                    '|> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false) |> keep(columns: ["_time", "_value"]) |> set(key: "_field", value: "mean") |> group(columns: ["_field"])',
                    "A",
                ),
                flux(
                    f'{RANGE} |> filter(fn: (r) => r._measurement == "gas_prediction" and r._field == "inference_ms" and {NODE}) '
                    '|> keep(columns: ["_time", "_value"]) |> sort(columns: ["_time"]) '
                    '|> aggregateWindow(every: v.windowPeriod, fn: max, createEmpty: false) |> keep(columns: ["_time", "_value"]) |> set(key: "_field", value: "max") |> group(columns: ["_field"])',
                    "B",
                ),
            ],
            ds=FLUX,
            unit="ms",
            fill=10,
            min_=0,
            overrides=[
                color_override("mean", MINT),
                color_override("max", AMBER, [{"id": "custom.fillOpacity", "value": 0}]),
            ],
            description="Thời gian model suy luận một cửa sổ (đo trên chính tiến trình edge) và tần suất "
            "dự đoán, dùng để phát hiện node chậm hoặc tụt nhịp.",
        ),
        24,
        8,
    )

    L.row("Cảm biến MOX")
    order = {f"s{i}": i - 1 for i in range(1, 17)}
    L.add(
        {
            "type": "bargauge",
            "title": "Giá trị hiện tại",
            "datasource": FLUX,
            "targets": [
                flux(
                    f"from(bucket: v.defaultBucket) |> range(start: -2m) "
                    f'|> filter(fn: (r) => r._measurement == "sensor_reading" and r._field =~ /^s([1-9]|1[0-6])$/ and {NODE}) '
                    '|> last() |> keep(columns: ["_measurement", "_field", "_value"]) |> group() '
                    '|> pivot(rowKey: ["_measurement"], columnKey: ["_field"], valueColumn: "_value")'
                )
            ],
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "continuous-GrYlRd"},
                    "unit": "none",
                    "decimals": 0,
                    "min": 0,
                    "max": 9000,
                    "thresholds": steps((None, MINT)),
                },
                "overrides": [],
            },
            "options": {
                "orientation": "horizontal",
                "displayMode": "gradient",
                "valueMode": "color",
                "namePlacement": "left",
                "showUnfilled": True,
                "sizing": "auto",
                "minVizHeight": 8,
                "maxVizHeight": 300,
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            },
            "transformations": [
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"_measurement": True},
                        "indexByName": order,
                        "renameByName": {f"s{i}": f"S{i}" for i in range(1, 17)},
                    },
                }
            ],
        },
        8,
        15,
    )
    L.add(
        timeseries(
            "Lịch sử 16 kênh",
            [
                flux(
                    'import "strings"\n'
                    f'{RANGE} |> filter(fn: (r) => r._measurement == "sensor_reading" and r._field =~ /^s([1-9]|1[0-6])$/ and {NODE}) '
                    "|> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false) "
                    '|> keep(columns: ["_time", "_value", "_field"]) '
                    '|> map(fn: (r) => ({r with _field: "S" + strings.trimPrefix(v: r._field, prefix: "s")})) '
                    '|> group(columns: ["_field"])'
                )
            ],
            ds=FLUX,
            unit="none",
            fill=0,
            line_width=1,
            legend_calcs=("lastNotNull",),
            gradient="none",
        ),
        16,
        15,
    )
    # Legend as list on the right keeps 16 series readable.
    L.panels[-1]["options"]["legend"] = {
        "displayMode": "list",
        "placement": "right",
        "calcs": [],
        "showLegend": True,
    }

    # Node lists come from the stored readings, not schema.tagValues(): InfluxDB keeps a deleted
    # node's id in its series index, so purged test nodes would stay in the dropdowns.
    latest_reading_per_node = (
        "from(bucket: v.defaultBucket)\n"
        "  |> range(start: -30d)\n"
        '  |> filter(fn: (r) => r._measurement == "sensor_reading" and r._field == "s1")\n'
        "  |> last()\n"
        "  |> group()\n"
    )
    variables = [
        {
            "name": "node",
            "label": "Node",
            "type": "query",
            "datasource": FLUX,
            "query": latest_reading_per_node + "  |> map(fn: (r) => ({_value: r.node_id}))",
            "definition": "node_id có reading trong 30 ngày",
            "includeAll": True,
            "allValue": ".*",
            "multi": True,
            "refresh": 2,
            "sort": 1,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
            "options": [],
        },
        {
            # The siren switch acts on exactly one node. Nodes are listed by their latest reading,
            # newest first, and Grafana selects the first one: the node streaming right now, which
            # is the node the kitchen screen follows. Alphabetical order picked whichever id sorted
            # first (esp32-edge-01 before virtual-gas-01), so the switch muted a node not on screen.
            "name": "siren_node",
            "label": "Node chuông",
            "type": "query",
            "datasource": FLUX,
            "query": latest_reading_per_node
            + '  |> sort(columns: ["_time"], desc: true)\n'
            + "  |> map(fn: (r) => ({_value: r.node_id}))",
            "definition": "node_id theo reading mới nhất",
            "includeAll": False,
            "multi": False,
            "refresh": 2,
            "sort": 0,
            "options": [],
        },
    ]
    return dashboard(
        "aiot-gas-monitoring",
        "AIoT Gas Monitoring",
        "CO/Ethylene, chất lượng model, alarm, actuator ACK, audit trail và 16 kênh MOX.",
        L.panels,
        variables=variables,
        links=LINKS,
        annotations=[DANGER_ANNOTATION, OVERRIDE_ANNOTATION],
        tags=["aiot", "gas"],
        time_from="now-30m",
    )


if __name__ == "__main__":
    for name, board in [("pipeline-operations.json", operations()), ("gas-monitoring.json", gas())]:
        (OUT / name).write_text(
            json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("wrote", OUT / name, len(board["panels"]), "panels")

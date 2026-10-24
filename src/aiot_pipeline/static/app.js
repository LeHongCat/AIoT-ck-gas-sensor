const $ = (id) => document.getElementById(id);

const HISTORY_SECONDS = 180; // trend and sensor sparklines
const ACCURACY_SECONDS = 600; // prediction ↔ ground-truth pairs behind the accuracy figures
const KEEP_SECONDS = { sensor: HISTORY_SECONDS, prediction: ACCURACY_SECONDS, ground_truth: ACCURACY_SECONDS };
// A gas counts as present at these reference concentrations (PRESENT_PPM of the notebook).
const PRESENT_PPM = { co: 50, ethylene: 1 };
// Scales and thresholds of the training notebook.
const CO_MAX = 533.33;
const ETH_MAX = 20;
const CO_WARNING = 50;
const CO_DANGER = 200;
const ETH_PRESENT = 3;
// Meters are linear (distance ∝ ppm); index.html puts labels, ticks and threshold marks at value / max.
const METER_MAX = { co: CO_MAX, ethylene: ETH_MAX };
const SIREN_ACK_TIMEOUT_MS = 8000;

const STREAM_TYPES = [
  ["sensor", "gas.sensor.readings.v1", "Sensor"],
  ["prediction", "gas.predictions.v1", "Prediction"],
  ["ground_truth", "gas.ground-truth.v1", "Ground truth"],
  ["control_request", "gas.control-requests.v1", "Operator request"],
  ["actuator_command", "gas.actuator-commands.v1", "Lệnh actuator"],
  ["actuator_state", "gas.actuator-state.v1", "Thiết bị ACK"],
];
const OUTPUT_KEYS = ["fan_on", "exhaust_on", "gas_valve_closed", "alarm_on", "suppression_on"];
const ALARM_TEXT = {
  normal: ["NORMAL", "Không khí an toàn"],
  warning: ["WARNING", "CO ≥ 50 ppm hoặc C₂H₄ ≥ 3 ppm"],
  danger: ["DANGER", "CO ≥ 200 ppm · khóa gas, sơ tán"],
  waiting: ["CHỜ DỮ LIỆU", "Chờ dự đoán đầu tiên từ edge"],
};
// Model descriptions from /api/config, keyed by the model_name predictions carry.
let modelCards = {};
let shownModel = null;
// Fallback when /api/config is unavailable; the backend serves the same table (sensors.py).
const TYPE_TARGETS = {
  TGS2602: "VOC, mùi, NH₃/H₂S",
  TGS2600: "Khí ô nhiễm: CO, H₂",
  TGS2610: "LPG: propan, butan",
  TGS2620: "Hơi dung môi, cồn, CO",
};
const TYPE_ORDER = ["TGS2602", "TGS2600", "TGS2610", "TGS2620"];

let latestState = null;
let transport = "connecting";
let pollTimer = null;
let lastAckLatency = null;
const sensorNodes = [...document.querySelectorAll(".sensor-node")].sort((a, b) => a.dataset.index - b.dataset.index);
let sensors = sensorNodes.map((node, index) => ({
  channel: `S${index + 1}`,
  index,
  sensor_type: node.dataset.sensor,
  target: TYPE_TARGETS[node.dataset.sensor],
  location: "",
}));
const history = { sensor: [], prediction: [], ground_truth: [] };

function formatNumber(value, digits = 1) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
}

function compact(value) {
  if (!Number.isFinite(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : value.toFixed(0);
}

function timeLabel(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString("vi-VN");
}

function ageLabel(seconds) {
  if (seconds == null) return "chờ";
  if (seconds < 1) return "vừa xong";
  if (seconds < 60) return `${Math.round(seconds)}s trước`;
  return `${Math.floor(seconds / 60)} phút trước`;
}

function alarmFrom(data) {
  if (data?.alarm_level) return data.alarm_level;
  if (!data) return "waiting";
  if (data.co_ppm >= CO_DANGER) return "danger";
  if (data.co_ppm >= CO_WARNING || data.ethylene_ppm >= ETH_PRESENT) return "warning";
  return "normal";
}

function presenceClass(reading) {
  if (!reading) return "—";
  const co = reading.co_ppm >= CO_WARNING;
  const eth = reading.ethylene_ppm >= ETH_PRESENT;
  return co && eth ? "MIX" : co ? "CO" : eth ? "C₂H₄" : "AIR";
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]);
}

async function fetchJson(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

// ------------------------------------------------------------------ toasts
function toast(kind, title, text = "", ttl = 4200) {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.innerHTML = `<b>${escapeHtml(title)}</b>${text ? `<span>${escapeHtml(text)}</span>` : ""}`;
  $("toastStack").appendChild(item);
  requestAnimationFrame(() => item.classList.add("show"));
  setTimeout(() => {
    item.classList.remove("show");
    setTimeout(() => item.remove(), 300);
  }, ttl);
}

// ------------------------------------------------------------------ history
function pushHistory(kind, row) {
  const rows = history[kind];
  const last = rows[rows.length - 1];
  if (last) {
    // The same record again: /api/history stamps it in µs, Date.parse() of the live snapshot in
    // ms, so compare the sequence (and allow the sub-ms difference) instead of `===`.
    if (row[1] === last[1] && Math.abs(row[0] - last[0]) < 0.5) return false;
    if (row[0] < last[0] - 0.001) rows.length = 0; // producer clock went backwards (restart)
  }
  rows.push(row);
  const cutoff = row[0] - KEEP_SECONDS[kind];
  while (rows.length && rows[0][0] < cutoff) rows.shift();
  return true;
}

function ingest(state) {
  const { sensor, prediction, ground_truth: truth } = state.latest;
  const ts = (event) => new Date(event.observed_at).getTime() / 1000;
  if (truth) pushHistory("ground_truth", [ts(truth), truth.sequence, truth.co_ppm, truth.ethylene_ppm]);
  const fresh = sensor ? pushHistory("sensor", [ts(sensor), sensor.sequence, ...sensor.sensors]) : false;
  if (prediction) {
    pushHistory("prediction", [
      ts(prediction), prediction.sequence, prediction.co_ppm, prediction.ethylene_ppm, prediction.alarm_level,
      prediction.inference_ms, (prediction.top_channels || []).join(","), prediction.window_s,
    ]);
  }
  return fresh;
}

// ------------------------------------------------------------------ safety log
const safetyLog = [];
const previous = { alarm: null, actuator: null, evacuating: false, commandId: null, suppression: false, purge: false, muted: null };
const counters = { danger: 0, commands: 0 };

function logEvent(kind, title, detail = "", at = Date.now() / 1000) {
  safetyLog.unshift({ kind, title, detail, at });
  if (safetyLog.length > 40) safetyLog.pop();
  renderLog();
}

function renderLog() {
  $("logStats").innerHTML = `
    <span><b>${counters.danger}</b> lần danger</span>
    <span><b>${counters.commands}</b> lệnh</span>
`;
  if (!safetyLog.length) return;
  $("safetyLog").innerHTML = safetyLog.slice(0, 14).map((item) => `
    <li class="${item.kind}"><time>${timeLabel(item.at)}</time><b>${escapeHtml(item.title)}</b><span>${escapeHtml(item.detail)}</span></li>`).join("");
}

function describeOutputs(state) {
  const parts = [];
  if (state.fan_on) parts.push("quạt");
  if (state.exhaust_on) parts.push("xả mái");
  if (state.gas_valve_closed) parts.push("khóa gas");
  if (state.alarm_on) parts.push("còi");
  if (state.suppression_on) parts.push("phun dập lửa");
  return parts.length ? parts.join(" · ") : "tất cả tắt, van gas mở";
}

function trackSafety(state, alarm) {
  const reading = state.latest.prediction;
  if (alarm !== "waiting" && alarm !== previous.alarm) {
    if (previous.alarm !== null) {
      const detail = reading ? `CO ${formatNumber(reading.co_ppm)} ppm · C₂H₄ ${formatNumber(reading.ethylene_ppm, 2)} ppm` : "";
      const title = { normal: "Trở về NORMAL", warning: "Cảnh báo WARNING", danger: "NGUY HIỂM · DANGER" }[alarm];
      if (alarm === "danger") counters.danger += 1;
      logEvent(alarm, title, detail);
      if (alarm !== "normal") toast(alarm, title, detail);
    }
    previous.alarm = alarm;
  }
  const command = state.latest.actuator_command;
  if (command && command.event_id !== previous.commandId) {
    if (previous.commandId !== null) {
      counters.commands += 1;
      logEvent(command.mode === "manual" ? "operator" : "command", command.mode === "manual" ? "Lệnh can thiệp trực tiếp" : "Safety Controller phát lệnh", command.reason);
    }
    previous.commandId = command.event_id;
  }
  const ack = state.latest.actuator_state;
  if (ack) {
    const muted = Boolean(ack.alarm_muted);
    if (previous.muted !== null && muted !== previous.muted) {
      const rearmed = !muted && /re-armed/.test(command?.reason || "");
      const title = muted ? "Chuông cảnh báo đã tắt tay" : rearmed ? "DANGER mới · chuông tự bật lại" : "Chuông cảnh báo bật lại";
      logEvent(muted ? "operator" : rearmed ? "danger" : "operator", title, muted ? "model, dữ liệu và log vẫn chạy" : "");
      toast(muted ? "warning" : rearmed ? "danger" : "info", title);
    }
    previous.muted = muted;
    const signature = OUTPUT_KEYS.map((key) => Number(Boolean(ack[key]))).join("") + ack.mode;
    if (previous.actuator !== null && signature !== previous.actuator) {
      logEvent("ack", `Thiết bị ACK · ${ack.mode === "manual" ? "DIRECT" : "AUTO"}`, describeOutputs(ack), new Date(ack.observed_at).getTime() / 1000);
    }
    previous.actuator = signature;
  }
}

// ------------------------------------------------------------------ telemetry
function meterPercent(value, max) {
  return Math.min(100, (Math.max(0, Number(value) || 0) / max) * 100);
}

function renderTelemetry(state) {
  const prediction = state.latest.prediction;
  const reading = prediction;
  const alarm = alarmFrom(reading);

  $("coValue").textContent = reading ? formatNumber(reading.co_ppm) : "—";
  $("ethyleneValue").textContent = reading ? formatNumber(reading.ethylene_ppm, 2) : "—";
  // The truth of the very reading the prediction was made on, else the newest one.
  const truth = (prediction && truthOf(prediction.sequence)) || state.latest.ground_truth;
  for (const [gas, key, digits] of [["co", "co_ppm", 1], ["ethylene", "ethylene_ppm", 2]]) {
    $(`${gas}Truth`).textContent = truth ? `${formatNumber(truth[key], digits)} ppm` : "—";
    $(`${gas}Delta`).textContent = truth && prediction ? `${formatNumber(prediction[key] - truth[key], digits)} ppm` : "—";
    $(`${gas}TruthMark`).style.left = `${meterPercent(truth?.[key], METER_MAX[gas])}%`;
    $(`${gas}TruthMark`).style.opacity = truth ? 1 : 0;
    $(`${gas}Meter`).style.width = `${meterPercent(reading?.[key], METER_MAX[gas])}%`;
  }
  $("coMeter").style.background = (reading?.co_ppm || 0) >= CO_DANGER ? "var(--red)" : (reading?.co_ppm || 0) >= CO_WARNING ? "var(--amber)" : "var(--mint)";
  $("ethyleneMeter").style.background = (reading?.ethylene_ppm || 0) >= ETH_PRESENT ? "var(--purple)" : "var(--mint)";
  $("modelTag").textContent = prediction ? `${prediction.model_name} ${prediction.model_version}` : "—";
  const presence = presenceClass(reading);
  $("presenceTag").textContent = presence;
  $("presenceTag").dataset.kind = presence === "C₂H₄" ? "ETH" : presence;
  const [levelText, meaning] = ALARM_TEXT[alarm] || ALARM_TEXT.waiting;
  $("gasHero").dataset.level = alarm;
  $("alarmText").textContent = levelText;
  $("alarmMeaning").textContent = meaning;
  $("inference").textContent = prediction?.inference_ms != null ? `${formatNumber(prediction.inference_ms)} ms` : "—";
  $("observedAt").textContent = reading ? `${reading.sequence} · ${new Date(reading.observed_at).toLocaleTimeString("vi-VN")}` : "—";

  return { alarm, reading, co: reading?.co_ppm || 0, ethylene: reading?.ethylene_ppm || 0 };
}

function truthOf(sequence) {
  const rows = history.ground_truth;
  for (let i = rows.length - 1; i >= 0 && i >= rows.length - 30; i -= 1) {
    if (rows[i][1] === sequence) return { observed_at: rows[i][0], sequence, co_ppm: rows[i][2], ethylene_ppm: rows[i][3] };
  }
  return null;
}

function renderTrend() {
  const lastOf = (rows) => (rows.length ? rows[rows.length - 1][0] : -Infinity);
  const newest = Math.max(lastOf(history.prediction), lastOf(history.ground_truth));
  const now = Number.isFinite(newest) ? newest : Date.now() / 1000;
  const start = now - HISTORY_SECONDS;
  const pred = history.prediction.filter((row) => row[0] >= start);
  const truth = history.ground_truth.filter((row) => row[0] >= start);
  const top = Math.max(260, ...pred.map((row) => row[2] * 1.12), ...truth.map((row) => row[2] * 1.12));
  const x = (t) => ((t - start) / HISTORY_SECONDS) * 360;
  const y = (value) => 120 - (value / top) * 116;
  const yEth = (value) => 120 - (value / ETH_MAX) * 116;
  // A 3-minute trend has ~180 useful pixels, so drawing more samples only costs strings.
  const points = (rows, index, scale) => {
    if (!rows.length) return "";
    const stride = Math.max(1, Math.ceil(rows.length / 180));
    const out = [];
    for (let i = 0; i < rows.length; i += stride) {
      out.push(`${x(rows[i][0]).toFixed(1)},${scale(rows[i][index]).toFixed(1)}`);
    }
    const lastRow = rows[rows.length - 1];
    if ((rows.length - 1) % stride !== 0) {
      out.push(`${x(lastRow[0]).toFixed(1)},${scale(lastRow[index]).toFixed(1)}`);
    }
    return out.join(" ");
  };
  $("predLine").setAttribute("points", points(pred, 2, y));
  $("ethLine").setAttribute("points", points(pred, 3, yEth));
  $("truthLine").setAttribute("points", points(truth, 2, y));
  $("ethTruthLine").setAttribute("points", points(truth, 3, yEth));
  const yWarning = y(CO_WARNING);
  const yDanger = y(CO_DANGER);
  for (const [id, value] of [["lineWarning", yWarning], ["lineDanger", yDanger]]) {
    $(id).setAttribute("y1", value);
    $(id).setAttribute("y2", value);
  }
  $("bandDanger").setAttribute("height", Math.max(0, yDanger));
  $("bandWarning").setAttribute("y", yDanger);
  $("bandWarning").setAttribute("height", Math.max(0, yWarning - yDanger));
  $("labelDanger").style.top = `${(yDanger / 120) * 100}%`;
  $("labelWarning").style.top = `${(yWarning / 120) * 100}%`;
}

// ------------------------------------------------------------------ sensors
const sensorItems = [];
// Lowest reading seen this session: the clean-air baseline even if the page opened mid-episode.
const sessionBaseline = Array(16).fill(Infinity);
const sensorView = Array.from({ length: 16 }, () => ({ value: NaN, base: NaN, max: NaN, rise: 0, heat: 0 }));

function buildSensorGroups() {
  const root = $("sensorGroups");
  root.innerHTML = "";
  sensorItems.length = 0;
  for (const type of TYPE_ORDER) {
    const members = sensors.filter((sensor) => sensor.sensor_type === type);
    const group = document.createElement("section");
    group.className = "sensor-group";
    group.dataset.sensor = type;
    group.innerHTML = `
      <header><span class="sg-chip">${type}</span><b>${escapeHtml(members[0]?.target || TYPE_TARGETS[type])}</b><small>${members.map((sensor) => sensor.channel).join(" · ")}</small></header>
      <div class="sg-items"></div>`;
    const list = group.querySelector(".sg-items");
    for (const sensor of members) {
      const item = document.createElement("article");
      item.className = "sensor-item";
      item.dataset.index = sensor.index;
      item.dataset.sensor = type;
      item.innerHTML = `
        <svg class="si-icon"><use href="#sensorCan"></use></svg>
        <div class="si-head"><b>${sensor.channel}</b><small>${escapeHtml(sensor.location || type)}</small></div>
        <strong class="si-value">—</strong>
        <span class="si-delta">chờ dữ liệu</span>
        <svg class="si-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true"><path class="area" d=""></path><polyline points=""></polyline></svg>
        <span class="si-bar"><i></i></span>`;
      list.appendChild(item);
      sensorItems[sensor.index] = {
        item,
        value: item.querySelector(".si-value"),
        delta: item.querySelector(".si-delta"),
        line: item.querySelector("polyline"),
        area: item.querySelector("path.area"),
        bar: item.querySelector(".si-bar i"),
      };
    }
    root.appendChild(group);
  }
}

function renderSensors(state, fresh) {
  const sensor = state.latest.sensor;
  const rows = history.sensor;
  $("sensorCount").textContent = state.counts.sensor.toLocaleString("vi-VN");
  if (!rows.length) return;
  const start = rows[0][0];
  const span = Math.max(1, rows[rows.length - 1][0] - start);
  for (let i = 0; i < 16; i += 1) {
    const values = rows.map((row) => row[i + 2]);
    const min = Math.min(...values);
    const max = Math.max(...values);
    sessionBaseline[i] = Math.min(sessionBaseline[i], min);
    const base = sessionBaseline[i];
    const current = values[values.length - 1];
    // The span floor keeps clean-air noise from looking like a gas response.
    const range = Math.max(max - min, base * 0.35, 1);
    const heat = Math.max(0, Math.min(1, (current - base) / Math.max(max - base, base * 0.35, 1)));
    const rise = base > 0 ? ((current - base) / base) * 100 : 0;
    Object.assign(sensorView[i], { value: current, base, max, rise, heat });
    const level = heat > 0.8 && rise > 40 ? "high" : heat > 0.45 && rise > 15 ? "mid" : "low";

    const view = sensorItems[i];
    if (view) {
      view.value.textContent = formatNumber(current, 0);
      view.delta.textContent = `${rise >= 0 ? "▲" : "▼"} ${formatNumber(Math.abs(rise), 0)}% so với nền ${formatNumber(base, 0)}`;
      const points = rows.map((row, k) => `${(((row[0] - start) / span) * 100).toFixed(1)},${(29 - ((values[k] - min) / range) * 26).toFixed(1)}`);
      view.line.setAttribute("points", points.join(" "));
      view.area.setAttribute("d", `M0,30 L${points.join(" L")} L100,30 Z`);
      view.bar.style.width = `${Math.max(4, heat * 100).toFixed(0)}%`;
      view.item.dataset.level = level;
    }

    const node = sensorNodes[i];
    node.classList.toggle("live", Boolean(sensor));
    node.dataset.level = level;
    node.style.setProperty("--heat", heat.toFixed(2));
    node.querySelector(".sn-level").style.strokeDasharray = `${Math.max(3, heat * 100).toFixed(1)} 100`;
    if (fresh) {
      node.classList.remove("ping");
      void node.offsetWidth;
      node.classList.add("ping");
    }
  }
}

// Tooltip explaining a sensor: what it is, where it hangs, what it reads now.
function showSensorTip(index, anchor) {
  const sensor = sensors[index];
  const view = sensorView[index];
  const tip = $("sensorTip");
  tip.innerHTML = `
    <header><svg><use href="#sensorCan"></use></svg><div><b>${sensor.channel} · ${sensor.sensor_type}</b><small>${escapeHtml(sensor.location)}</small></div></header>
    <p>Phát hiện: <b>${escapeHtml(sensor.target)}</b></p>
    <dl>
      <div><dt>Giá trị</dt><dd>${formatNumber(view.value, 0)}</dd></div>
      <div><dt>Nền sạch</dt><dd>${formatNumber(view.base, 0)}</dd></div>
      <div><dt>Thay đổi</dt><dd>${view.rise >= 0 ? "+" : ""}${formatNumber(view.rise, 0)}%</dd></div>
      <div><dt>Đỉnh 3 phút</dt><dd>${formatNumber(view.max, 0)}</dd></div>
    </dl>`;
  const rect = anchor.getBoundingClientRect();
  tip.classList.add("show");
  const width = tip.offsetWidth;
  const left = Math.min(window.innerWidth - width - 12, Math.max(12, rect.left + rect.width / 2 - width / 2));
  const top = rect.top - tip.offsetHeight - 10 < 8 ? rect.bottom + 10 : rect.top - tip.offsetHeight - 10;
  tip.style.left = `${left}px`;
  tip.style.top = `${top + window.scrollY}px`;
}

function hideSensorTip() {
  $("sensorTip").classList.remove("show");
}

function bindSensorTips() {
  document.addEventListener("mouseover", (event) => {
    const target = event.target.closest(".sensor-node, .sensor-item");
    if (target) showSensorTip(Number(target.dataset.index), target);
  });
  document.addEventListener("mouseout", (event) => {
    if (event.target.closest(".sensor-node, .sensor-item") && !event.relatedTarget?.closest?.(".sensor-node, .sensor-item")) hideSensorTip();
  });
  document.addEventListener("focusin", (event) => {
    const target = event.target.closest(".sensor-node");
    if (target) showSensorTip(Number(target.dataset.index), target);
  });
  document.addEventListener("focusout", hideSensorTip);
}

// ------------------------------------------------------------------ MQTT inspector
const topicViews = new Map();
function renderMqtt(state) {
  const mqtt = state.mqtt;
  if (!mqtt) return;
  $("mqttBroker").textContent = `${mqtt.broker} · QoS ${mqtt.qos} · subscribe aiot/v1/nodes/+/+`;
  $("mqttState").textContent = mqtt.connected ? "đang nghe broker" : "mất kết nối";
  $("mqttState").dataset.state = mqtt.connected ? "live" : "down";
  const stats = mqtt.stats || {};
  const chips = [
    [`${formatNumber(mqtt.rate, 1)}`, "msg/s"],
    [mqtt.topics.length, "topic"],
    [stats.clients_connected ?? "—", "client"],
    [stats.subscriptions ?? "—", "subscription"],
    [stats.retained_messages ?? "—", "retained"],
  ];
  $("brokerStats").innerHTML = chips.map(([value, label]) => `<span><b>${value}</b>${label}</span>`).join("");

  const list = $("topicList");
  if (mqtt.topics.length && list.querySelector(".empty")) list.innerHTML = "";
  for (const topic of mqtt.topics) {
    let view = topicViews.get(topic.topic);
    if (!view) {
      const item = document.createElement("article");
      item.className = `topic-item t-${topic.suffix}`;
      const base = topic.topic.slice(0, topic.topic.length - topic.suffix.length);
      item.innerHTML = `
        <div class="ti-head"><code><span>${escapeHtml(base)}</span><b>${escapeHtml(topic.suffix)}</b></code><em class="ti-rate"></em></div>
        <div class="ti-route">${escapeHtml(topic.publisher)} <i>→</i> ${escapeHtml(topic.subscriber)}${topic.kafka_topic ? ` <i>→</i> <span class="kafka">${escapeHtml(topic.kafka_topic)}</span>` : ""}</div>
        <pre class="ti-payload"></pre>
        <div class="ti-meta"></div>`;
      list.appendChild(item);
      view = { item, rate: item.querySelector(".ti-rate"), payload: item.querySelector(".ti-payload"), meta: item.querySelector(".ti-meta"), count: -1 };
      topicViews.set(topic.topic, view);
    }
    view.rate.textContent = `${formatNumber(topic.rate, 1)}/s`;
    view.payload.textContent = topic.preview;
    view.meta.textContent = `${topic.bytes} B · ${topic.age_s < 1 ? "vừa xong" : `${formatNumber(topic.age_s, 0)}s trước`} · ${topic.count.toLocaleString("vi-VN")} message${topic.retained ? " · retained" : ""}`;
    view.item.classList.toggle("idle", topic.age_s > 15);
    if (view.count !== -1 && topic.count !== view.count) {
      view.item.classList.remove("flash");
      void view.item.offsetWidth;
      view.item.classList.add("flash");
    }
    view.count = topic.count;
  }
}

// ------------------------------------------------------------------ stream health
function renderStream(state) {
  $("topicStatus").textContent = state.kafka_error ? "Kafka lỗi" : state.events.length ? "Đang consume" : "Đang kết nối…";
  $("topicStatus").dataset.state = state.kafka_error ? "error" : state.events.length ? "live" : "idle";
  const stream = state.stream || {};
  $("streamTable").innerHTML = `
    <div class="stream-row head"><span>Kafka topic</span><span>msg/s</span><span>Edge → UI</span><span>Tổng</span></div>
    ${STREAM_TYPES.map(([key, topic, label]) => {
      const stat = stream[key] || {};
      const age = stat.last_seen ? state.server_time - stat.last_seen : null;
      const live = age != null && age < 5;
      const latency = stat.latency_ms;
      const tone = latency == null ? "" : latency > 1500 ? "bad" : latency > 500 ? "warn" : "good";
      return `<div class="stream-row ${live ? "live" : ""}" data-type="${key}">
        <span><i></i><b>${label}</b><small>${topic}</small></span>
        <span>${formatNumber(stat.rate ?? 0, 1)}</span>
        <span class="latency ${tone}">${latency == null ? "—" : `${Math.round(latency)} ms`}</span>
        <span>${(state.counts[key] || 0).toLocaleString("vi-VN")}</span>
      </div>`;
    }).join("")}`;
  if (!state.events.length) return;
  $("eventList").innerHTML = state.events.slice(0, 6).map((event) => `
    <div class="event ${event.type}">
      <i class="event-dot"></i>
      <div><strong>${event.topic}</strong><small>partition ${event.partition} · offset ${event.offset} · seq ${event.sequence}${event.replayed ? " · replay" : event.latency_ms != null ? ` · ${Math.round(event.latency_ms)} ms` : ""}</small></div>
      <time>${timeLabel(event.received_at)}</time>
    </div>`).join("");
}

function sensorAge(state) {
  const observed = state?.latest.sensor?.observed_at;
  return observed ? Math.max(0, Date.now() / 1000 - new Date(observed).getTime() / 1000) : null;
}

function renderFreshness(state) {
  const age = sensorAge(state);
  const live = age != null && age < 5;
  $("freshnessText").textContent = live ? `Fresh · ${ageLabel(age)}` : age == null ? "Chờ sensor" : `Stale · ${ageLabel(age)}`;
  $("freshnessDot").parentElement.classList.toggle("live", live);
  $("freshnessDot").parentElement.classList.toggle("stale", age != null && !live);
}

// ------------------------------------------------------------------ KPI strip
// ------------------------------------------------------------------ 16 sensors → model → logs
let modelInfo = null;
const LOG_ROWS = 10;

// The edge model card and the alarm wording follow the model that actually predicts: the gateway
// TabM, the ESP32's TabM (same model, fewer members) or the Ridge fallback.
function applyModelCard(card, prediction = null) {
  if (!card) return;
  modelInfo = card;
  const version = prediction ? ` · ${prediction.model_version}` : "";
  // "seed42-k4": the ESP32 runs the first 4 of the 32 ensemble members.
  const members = /-k(\d+)$/.exec(prediction?.model_version || "");
  const summary = members ? card.summary.replace(/\d+ member/, `${members[1]}/32 member`) : card.summary;
  $("modelInfo").textContent = `${card.title}${version} · ${summary}`;
  $("modelInfo").title = card.trained_on ? `Huấn luyện: ${card.trained_on}` : "";
  $("mfFeatures").textContent = `${card.features.length} đặc trưng × 16 kênh: ${card.features.join(" · ")}`;
  $("mfModelName").textContent = `④ ${card.title}`;
  $("mfModelSub").textContent = summary;
  if (card.alarm) {
    ALARM_TEXT.warning[1] = card.alarm.warning;
    ALARM_TEXT.danger[1] = card.alarm.danger;
    $("alarmPolicy").title = `WARNING: ${card.alarm.warning} · DANGER: ${card.alarm.danger} · ${card.alarm.note}`;
  }
}

function followModel(prediction) {
  const model = prediction && `${prediction.model_name}/${prediction.model_version}`;
  if (!model || model === shownModel) return;
  shownModel = model;
  applyModelCard(modelCards[prediction.model_name], prediction);
}

function heatOf(index, value) {
  const base = sessionBaseline[index];
  if (!Number.isFinite(base) || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, (value - base) / Math.max(base * 1.5, 1)));
}

function renderModelFlow(state) {
  const sensor = state.latest.sensor;
  const prediction = state.latest.prediction;
  const age = sensorAge(state);
  const readingsRate = (state.mqtt?.topics || []).filter((topic) => topic.suffix === "readings").reduce((sum, topic) => sum + topic.rate, 0);
  $("mfInput").textContent = sensor ? `seq ${sensor.sequence} · ${formatNumber(readingsRate, 1)}/s${age != null && age > 5 ? " · mất tín hiệu" : ""}` : "chờ";
  $("mfWindow").textContent = prediction?.window_s ? `${prediction.window_s}/${modelInfo?.window_s ?? 60} giây` : "—";
  $("mfInfer").textContent = prediction?.inference_ms != null ? `${formatNumber(prediction.inference_ms, 2)} ms` : "—";
  $("mfPredict").textContent = prediction ? `CO ${formatNumber(prediction.co_ppm, 0)} · C₂H₄ ${formatNumber(prediction.ethylene_ppm, 1)} · ${prediction.alarm_level.toUpperCase()}` : "—";
  const written = state.pipeline?.rates?.influx_written;
  $("mfLog").textContent = `${(state.counts.prediction || 0).toLocaleString("vi-VN")} dự đoán${written != null ? ` · Influx ${formatNumber(written, 1)} pt/s` : ""}`;
  document.querySelector(".model-flow").dataset.alarm = prediction?.alarm_level || "waiting";

  const sensorRows = history.sensor.slice(-LOG_ROWS).reverse();
  if (sensorRows.length) {
    const head = `<div class="sl-row head"><span>Thời điểm</span><span>seq</span><div class="cells">${sensors.map((sensor) => `<b>${sensor.channel.slice(1)}</b>`).join("")}</div></div>`;
    $("sensorLog").innerHTML = head + sensorRows.map((row) => `
      <div class="sl-row"><time>${timeLabel(row[0])}</time><span>#${row[1]}</span><div class="cells">${row.slice(2, 18).map((value, index) =>
        `<i style="--h:${heatOf(index, value).toFixed(2)}" title="${sensors[index].channel} · ${sensors[index].sensor_type} · ${escapeHtml(sensors[index].location)}: ${formatNumber(value, 0)}">${compact(value)}</i>`).join("")}</div></div>`).join("");
  }

  const predictionRows = history.prediction.slice(-LOG_ROWS).reverse();
  if (predictionRows.length) {
    const truthBySeq = new Map(history.ground_truth.map((row) => [row[1], row]));
    const pair = (value, truth, digits) => `<b>${formatNumber(value, digits)}</b> <small>/ ${truth == null ? "—" : formatNumber(truth, digits)}</small>`;
    $("predictionLog").innerHTML = `<div class="pl-row head"><span>Thời điểm</span><span>seq</span><span>CO dự đoán / thực tế</span><span>C₂H₄ dự đoán / thực tế</span><span>Alarm</span><span>Suy luận</span><span>Cửa sổ</span></div>` +
      predictionRows.map((row) => {
        const truth = truthBySeq.get(row[1]);
        return `<div class="pl-row ${row[4] || ""}">
          <time>${timeLabel(row[0])}</time><span>#${row[1]}</span>
          <span>${pair(row[2], truth?.[2], 0)}</span>
          <span>${pair(row[3], truth?.[3], 1)}</span>
          <span><em class="alarm-chip ${row[4] || ""}">${(row[4] || "—").toUpperCase()}</em></span>
          <span>${row[5] != null ? `${formatNumber(row[5], row[5] >= 100 ? 0 : 2)} ms` : "—"}</span>
          <span>${row[7] != null ? `${row[7]} s` : "—"}</span>
        </div>`;
      }).join("");
  }
}

// Prediction ↔ ground truth over the last ACCURACY_SECONDS, paired by sequence (same reading).
function renderAccuracy() {
  const truthBySeq = new Map(history.ground_truth.map((row) => [row[1], row]));
  let pairs = 0, coError = 0, ethError = 0, coHit = 0, ethHit = 0;
  for (const row of history.prediction) {
    const truth = truthBySeq.get(row[1]);
    if (!truth) continue;
    pairs += 1;
    coError += Math.abs(row[2] - truth[2]);
    ethError += Math.abs(row[3] - truth[3]);
    coHit += Number((row[2] >= PRESENT_PPM.co) === (truth[2] >= PRESENT_PPM.co));
    ethHit += Number((row[3] >= PRESENT_PPM.ethylene) === (truth[3] >= PRESENT_PPM.ethylene));
  }
  const share = (hits) => `${formatNumber((hits / pairs) * 100, 1)} %`;
  $("accCoMae").textContent = pairs ? `${formatNumber(coError / pairs)} ppm` : "—";
  $("accEthMae").textContent = pairs ? `${formatNumber(ethError / pairs, 2)} ppm` : "—";
  $("accCoHit").textContent = pairs ? share(coHit) : "—";
  $("accEthHit").textContent = pairs ? share(ethHit) : "—";
  $("accPairs").textContent = pairs.toLocaleString("vi-VN");
}

// ------------------------------------------------------------------ gas status (top of page)
function renderGasHero(state, alarm, actuator) {
  const chips = [];
  if (actuator) {
    chips.push('<span class="mode auto">AUTO</span>');
    const active = [
      ["fan_on", "fan", "Chụp hút"],
      ["exhaust_on", "air", "Quạt mái"],
      ["gas_valve_closed", "valve", "Khóa gas"],
      ["alarm_on", "siren", "Còi"],
      ["suppression_on", "drop", "Dập lửa"],
    ].filter(([key]) => actuator[key]);
    if (active.length) {
      for (const [key, icon, label] of active) chips.push(`<span class="on ${key}"><i class="ic ${icon}"></i>${label}</span>`);
    } else {
      chips.push('<span class="idle">Thiết bị chờ</span>');
    }
    if (actuator.alarm_muted) chips.push('<span class="muted"><i class="ic siren"></i>Chuông tắt</span>');
  }
  $("alarmActions").innerHTML = chips.join("");
}

// ------------------------------------------------------------------ device state
// The record already contains the gas: a command cannot change the readings, so the
// screen only reports what the device acknowledged.
const ACTUATOR_ROWS = [
  ["fan_on", "Quạt chụp hút", "Tắt", "Bật"],
  ["exhaust_on", "Quạt hút mái", "Tắt", "Bật"],
  ["gas_valve_closed", "Van gas tổng", "Đang mở", "Đã khóa"],
  ["alarm_on", "Còi & đèn chớp", "Tắt", "Bật"],
  ["suppression_on", "Dập lửa", "Sẵn sàng", "Đang phun"],
];

function renderControlPanel(actuator) {
  const list = $("actuatorStateList");
  if (!list) return;
  if (!actuator) {
    list.innerHTML = '<li class="idle">Chưa nhận ACK từ thiết bị</li>';
    return;
  }
  list.innerHTML = ACTUATOR_ROWS.map(([key, name, off, on]) => {
    const active = Boolean(actuator[key]);
    const muted = key === "alarm_on" && !active && actuator.alarm_muted;
    return `<li class="${active ? "on" : muted ? "muted" : ""}"><span>${name}</span><b>${active ? on : muted ? "Tắt tay" : off}</b></li>`;
  }).join("");
}

// ------------------------------------------------------------------ siren switch
// Muting goes UI → MQTT control-requests → Kafka → Safety Controller, which keeps AUTO and only
// drops alarm_on; the switch flips when the device ACK carries alarm_muted.
let sirenPending = null; // { muted, since } until the ACK confirms it

function renderSiren(state) {
  const ack = state?.latest.actuator_state;
  const muted = Boolean(ack?.alarm_muted);
  if (sirenPending && (muted === sirenPending.muted || Date.now() - sirenPending.since > SIREN_ACK_TIMEOUT_MS)) {
    if (muted !== sirenPending.muted) toast("danger", "Chưa có ACK", "Thiết bị chưa xác nhận lệnh chuông");
    sirenPending = null;
  }
  const sounding = Boolean(ack?.alarm_on);
  const view = sirenPending ? "pending" : muted ? "muted" : sounding ? "sounding" : "armed";
  $("sirenCard").dataset.state = view;
  $("sirenBadge").dataset.state = view;
  $("sirenBadgeText").textContent = {
    pending: "Đang gửi lệnh chuông…",
    muted: "Chuông tắt tay",
    sounding: "Đèn chớp · chuông kêu",
    armed: "Đèn & chuông chờ",
  }[view];
  $("sirenSwitch").setAttribute("aria-checked", String(!muted));
  $("sirenSwitch").disabled = Boolean(sirenPending) || !state?.node_id;
  $("sirenSwitchText").textContent = muted ? "TẮT" : "BẬT";
  $("sirenStatus").textContent = sirenPending
    ? sirenPending.muted ? "Đang tắt chuông…" : "Đang bật lại chuông…"
    : muted ? "Tắt tay · model, dữ liệu, log vẫn chạy · DANGER mới tự bật lại"
    : sounding ? "Đèn đang chớp · chuông đang kêu"
    : "Đèn tắt · chuông chờ · tự bật khi WARNING / DANGER";
}

$("sirenSwitch").addEventListener("click", async () => {
  const state = latestState;
  const node = state?.node_id;
  if (!node || sirenPending) return;
  const mute = !state.latest.actuator_state?.alarm_muted;
  sirenPending = { muted: mute, since: Date.now() };
  renderSiren(state);
  try {
    await fetchJson("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: node, mode: mute ? "mute" : "unmute" }),
    });
  } catch (error) {
    sirenPending = null;
    toast("danger", "Không gửi được lệnh chuông", String(error.message || error));
    renderSiren(latestState);
  }
});

// "prediction alarm level is danger; siren muted by operator" (safety_controller.py)
// → "Alarm DANGER · chuông tắt tay".
const REASON_NOTES = [
  [/suppression/, "giữ dập lửa"],
  [/siren muted/, "chuông tắt tay"],
  [/siren unmuted/, "bật lại chuông"],
  [/re-armed the siren/, "DANGER bật lại chuông"],
  [/cancelled/, "hủy can thiệp"],
];

function reasonLabel(reason) {
  const text = reason || "";
  const level = /alarm level is (\w+)/.exec(text);
  const notes = REASON_NOTES.filter(([pattern]) => pattern.test(text)).map(([, note]) => note);
  if (level) return [`Alarm ${level[1].toUpperCase()}`, ...notes].join(" · ");
  if (/direct intervention for/.test(text)) return "Can thiệp trực tiếp";
  return text || "—";
}

function renderCommand(state) {
  const command = state.latest.actuator_command;
  const ack = state.latest.actuator_state;
  const clock = (iso) => new Date(iso).toLocaleTimeString("vi-VN");
  $("commandMode").textContent = command ? (command.mode === "manual" ? "DIRECT" : "AUTO") : "—";
  $("commandTime").textContent = command ? clock(command.observed_at) : "—";
  $("commandReason").textContent = command ? reasonLabel(command.reason) : "—";
  $("ackTime").textContent = ack ? clock(ack.observed_at) : "—";
  // The ACK carries the id of the command it applied: the round trip of exactly that command.
  const matched = command && ack && ack.command_id === command.event_id;
  lastAckLatency = matched ? Math.max(0, (new Date(ack.observed_at) - new Date(command.observed_at)) / 1000) : null;
  $("ackLatency").textContent = lastAckLatency == null ? "—" : `${Math.round(lastAckLatency * 1000)} ms`;
  const node = state.node_id || ack?.node_id;
  $("automationTitle").textContent = ack?.mode === "manual" ? "Can thiệp trực tiếp" : "Chế độ tự động";
  $("automationStatus").textContent = node ? `Node ${node}${ack ? ` · ACK ${clock(ack.observed_at)}` : ""}` : "—";
}

function renderActuators(state) {
  const actuator = state.latest.actuator_state;
  renderCommand(state);
  if (!actuator) {
    renderControlPanel(null);
    return null;
  }
  $("controlAck").textContent = `${actuator.mode === "manual" ? "DIRECT" : "AUTO"} · ACK ${new Date(actuator.observed_at).toLocaleTimeString("vi-VN")}`;
  $("controlAck").classList.add("live");
  renderControlPanel(actuator);
  return actuator;
}

$("labelToggle").addEventListener("click", (event) => {
  const next = event.currentTarget.getAttribute("aria-pressed") !== "true";
  event.currentTarget.setAttribute("aria-pressed", String(next));
  window.KitchenScene?.setLabels(next);
});

// ------------------------------------------------------------------ transport
let pendingState = null;
let frameHandle = 0;
let renderedPrediction = undefined;

function apply(state) {
  // SSE delivers several records per second (readings, predictions, ACK). Coalesce them into one
  // render per animation frame: a burst can no longer queue up synchronous DOM work.
  latestState = state;
  pendingState = state;
  if (!frameHandle) frameHandle = requestAnimationFrame(flushState);
}

function flushState() {
  frameHandle = 0;
  const state = pendingState;
  pendingState = null;
  if (state) renderState(state);
}

let shownNode;

function renderState(state) {
  if (state.node_id !== shownNode) {
    // The screen switched to another node (simulator ↔ ESP32): drop the old node's trends and
    // load the new node's own history, which the backend kept while it was not on screen.
    if (shownNode !== undefined) {
      history.sensor.length = 0;
      history.prediction.length = 0;
      history.ground_truth.length = 0;
      sessionBaseline.fill(Infinity);
      seedHistory();
    }
    shownNode = state.node_id;
  }
  followModel(state.latest.prediction);
  const fresh = ingest(state);
  const { alarm, co, ethylene } = renderTelemetry(state);
  const prediction = state.latest.prediction;
  const trendKey = `${prediction?.sequence}/${state.latest.ground_truth?.sequence}`;
  if (trendKey !== renderedPrediction) {
    renderedPrediction = trendKey;
    renderTrend();
  }
  renderSensors(state, fresh);
  renderStream(state);
  renderMqtt(state);
  renderFreshness(state);
  const actuator = renderActuators(state);
  renderSiren(state);
  trackSafety(state, alarm);
  renderGasHero(state, alarm, actuator);
  renderModelFlow(state);
  renderAccuracy();
  $("enoseNode").textContent = state.node_id ? `Node ${state.node_id}` : "—";
  const suppression = Boolean(actuator?.suppression_on);
  const evacuating = alarm === "danger" || suppression;
  $("evacBanner").classList.toggle("show", evacuating);
  $("evacTitle").textContent = "SƠ TÁN KHẨN CẤP";
  $("evacText").textContent = `Van gas tổng đã khóa · xả khí${actuator?.alarm_on ? " + còi" : ""} đang bật${actuator?.alarm_muted ? " · chuông tắt tay" : ""} · tập trung tại điểm an toàn`;
  if (evacuating !== previous.evacuating) {
    logEvent(evacuating ? "danger" : "normal", evacuating ? "Bắt đầu sơ tán nhân sự" : "Kết thúc sơ tán", evacuating ? (suppression ? "phun dập lửa" : "gas danger") : "");
    previous.evacuating = evacuating;
  }
  window.KitchenScene?.update({
    level: alarm,
    co,
    ethylene,
    fan: Boolean(actuator?.fan_on),
    exhaust: Boolean(actuator?.exhaust_on),
    valveClosed: Boolean(actuator?.gas_valve_closed),
    siren: Boolean(actuator?.alarm_on),
    muted: Boolean(actuator?.alarm_muted),
    suppression,
  });
  window.PipelineFlow?.update(state, {
    alarm,
    sensorAge: sensorAge(state),
    channels: state.latest.sensor?.sensors?.length ?? 0,
    ackLatency: lastAckLatency,
    transport: transport === "sse" ? "SSE" : transport === "polling" ? "polling" : "đang nối",
  });
}

function setTransport(mode) {
  transport = mode;
  const labels = { sse: "LIVE · SSE PUSH", polling: "LIVE · POLLING 1s", reconnecting: "ĐANG KẾT NỐI LẠI", connecting: "ĐANG KẾT NỐI" };
  $("transportText").textContent = labels[mode];
  $("transportBadge").dataset.state = mode;
}

async function poll() {
  try {
    apply(await fetchJson("/api/state"));
  } catch (error) {
    $("topicStatus").textContent = "UI backend offline";
    $("topicStatus").dataset.state = "error";
  }
}

function startPolling() {
  if (pollTimer) return;
  setTransport("polling");
  poll();
  pollTimer = setInterval(poll, 1000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function connect() {
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  const source = new EventSource("/api/stream");
  let fallback = null;
  source.onmessage = (message) => {
    clearTimeout(fallback);
    fallback = null;
    stopPolling();
    setTransport("sse");
    apply(JSON.parse(message.data));
  };
  source.onerror = () => {
    if (transport === "sse") setTransport("reconnecting");
    // EventSource retries on its own; poll meanwhile so the screen never freezes.
    fallback ||= setTimeout(startPolling, 3000);
  };
}

async function seedHistory() {
  try {
    const seed = await fetchJson("/api/history");
    // The backend keeps 10 minutes: enough clean air to find each channel's baseline.
    for (const row of seed.sensor || []) {
      for (let i = 0; i < 16; i += 1) sessionBaseline[i] = Math.min(sessionBaseline[i], row[i + 2]);
    }
    for (const kind of Object.keys(history)) for (const row of seed[kind] || []) pushHistory(kind, row);
    renderTrend();  // draw the seeded history before the next live record arrives
  } catch (error) {
    // History is a convenience; the live stream fills the charts on its own.
  }
}

async function boot() {
  renderLog();
  setTransport("connecting");
  try {
    const config = await fetchJson("/api/config");
    if (Array.isArray(config.sensors) && config.sensors.length === 16) sensors = config.sensors;
    modelCards = config.models || {};
    applyModelCard(config.model);
    if (config.node_id) ALARM_TEXT.waiting[1] = `Chờ dự đoán đầu tiên từ node ${config.node_id}`;
    if (config.grafana_url) {
      $("linkPipeline").href = `${config.grafana_url}/d/aiot-pipeline-operations`;
      $("linkGas").href = `${config.grafana_url}/d/aiot-gas-monitoring`;
    }
  } catch (error) {
    // Older backend without /api/config: keep defaults.
  }
  sensorNodes.forEach((node, index) => node.setAttribute("aria-label", `Cảm biến ${sensors[index].channel} ${sensors[index].sensor_type} · ${sensors[index].location}`));
  buildSensorGroups();
  bindSensorTips();
  await seedHistory();
  connect();
  // Clocks move even when no new record arrives.
  setInterval(() => {
    renderFreshness(latestState);
    renderControlPanel(latestState?.latest.actuator_state ?? null);
    renderSiren(latestState);
  }, 1000);
}

boot();

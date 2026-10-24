/*
 * Live pipeline map: Sensors → Edge → MQTT (Mosquitto) → Bridge → Kafka → consumers.
 * Every dot is spawned by a real record the UI just received, and travels the route that
 * record actually took, through its MQTT topic and its Kafka topic.
 */
(() => {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("flowSvg");
  if (!svg) return;
  svg.setAttribute("viewBox", "0 0 1040 384");
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function el(tag, attrs = {}, parent = svg, text = null) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text != null) node.textContent = text;
    parent.appendChild(node);
    return node;
  }

  // ------------------------------------------------------------------ layout
  const MQTT_ROWS = [
    ["readings", "Edge → Bridge"],
    ["predictions", "Edge model → Bridge"],
    ["ground-truth", "Nguồn (nhãn) → Bridge"],
    ["control-requests", "Kitchen UI → Bridge"],
    ["commands", "Controller → Thiết bị"],
    ["actuator-state", "Thiết bị (ACK) → Bridge"],
  ];
  const KAFKA_ROWS = [
    ["sensor", "gas.sensor.readings.v1"],
    ["prediction", "gas.predictions.v1"],
    ["ground_truth", "gas.ground-truth.v1"],
    ["control_request", "gas.control-requests.v1"],
    ["actuator_command", "gas.actuator-commands.v1"],
    ["actuator_state", "gas.actuator-state.v1"],
  ];
  const mqttY = (index) => 66 + index * 43 + 18;
  const kafkaY = (index) => 66 + index * 39 + 16;
  const MQ = Object.fromEntries(MQTT_ROWS.map(([suffix], index) => [suffix, mqttY(index)]));
  const KF = Object.fromEntries(KAFKA_ROWS.map(([type], index) => [type, kafkaY(index)]));

  const NODES = {
    sensors: { x: 10, y: 18, w: 160, h: 74, title: "16 cảm biến MOX", sub: "TGS2602/2600/2610/2620" },
    edge: { x: 10, y: 150, w: 160, h: 74, title: "Edge gateway", sub: "đọc 16 kênh · suy luận" },
    device: { x: 10, y: 290, w: 160, h: 74, title: "Thiết bị an toàn", sub: "quạt · van · còi · dập lửa" },
    bridge: { x: 478, y: 150, w: 132, h: 82, title: "MQTT → Kafka", sub: "Bridge · kiểm contract" },
    influx: { x: 884, y: 8, w: 150, h: 70, title: "Influx sink → DB", sub: "lưu time-series" },
    grafana: { x: 884, y: 92, w: 150, h: 54, title: "Grafana", sub: "" },
    controller: { x: 884, y: 162, w: 150, h: 78, title: "Safety Controller", sub: "Auto + override" },
    ui: { x: 884, y: 290, w: 150, h: 74, title: "Kitchen UI", sub: "màn hình này · SSE" },
  };

  // Visible edges
  const EDGES = {
    "sens-edge": "M90 92 L90 150",
    "edge-readings": `M170 176 C 198 176, 196 ${MQ.readings}, 222 ${MQ.readings}`,
    "edge-predictions": `M170 187 C 198 187, 196 ${MQ.predictions}, 222 ${MQ.predictions}`,
    "edge-gt": `M170 198 C 198 198, 196 ${MQ["ground-truth"]}, 222 ${MQ["ground-truth"]}`,
    "mqtt-bridge": "M436 191 L478 191",
    "bridge-kafka": "M610 191 L650 191",
    "kafka-influx": `M846 ${KF.sensor} C 866 ${KF.sensor}, 864 43, 884 43`,
    "influx-grafana": "M959 78 L959 92",
    "kafka-controller": `M846 ${KF.prediction} C 866 ${KF.prediction}, 864 194, 884 194`,
    "kafka-request-controller": `M846 ${KF.control_request} C 866 ${KF.control_request}, 864 214, 884 214`,
    "kafka-ui": "M846 288 C 866 288, 864 327, 884 327",
    "controller-commands": `M884 232 L868 232 L868 354 L452 354 L452 ${MQ.commands} L436 ${MQ.commands}`,
    "commands-device": `M222 ${MQ.commands} C 196 ${MQ.commands}, 196 316, 170 316`,
    "device-ack": `M170 338 C 196 338, 196 ${MQ["actuator-state"]}, 222 ${MQ["actuator-state"]}`,
    "ui-control": `M959 364 L959 377 L444 377 L444 ${MQ["control-requests"]} L436 ${MQ["control-requests"]}`,
  };
  const EDGE_KIND = {
    "controller-commands": "control", "commands-device": "control", "device-ack": "control",
    "ui-control": "operator", "edge-gt": "truth",
  };
  const EDGE_LABELS = [
    [560, 349, "lệnh actuator · MQTT", "middle"],
    [700, 372, "operator → MQTT", "middle"],
  ];

  const edgeLayer = el("g", { class: "fl-edges" });
  const nodeLayer = el("g", { class: "fl-nodes" });
  const hiddenLayer = el("g", { class: "fl-hidden" });
  const packetLayer = el("g", { class: "fl-packets" });
  const paths = {};

  for (const [id, d] of Object.entries(EDGES)) {
    el("path", { d, class: `fl-edge ${EDGE_KIND[id] || "data"}` }, edgeLayer);
    paths[id] = el("path", { d, class: `fl-flow ${EDGE_KIND[id] || "data"}`, id: `fl-${id}` }, edgeLayer);
  }
  for (const [x, y, text, anchor] of EDGE_LABELS) el("text", { x, y, class: "fl-edge-label", "text-anchor": anchor }, edgeLayer, text);

  function hidden(id, d) {
    paths[id] = el("path", { d, fill: "none", stroke: "none" }, hiddenLayer);
  }
  // Routes inside the broker / log: a record enters its topic row, then leaves.
  for (const [suffix] of MQTT_ROWS) {
    const y = MQ[suffix];
    hidden(`m-${suffix}`, `M222 ${y} L404 ${y} C 424 ${y}, 420 191, 436 191`);
    hidden(`mrev-${suffix}`, `M436 ${y} L222 ${y}`);
  }
  for (const [type] of KAFKA_ROWS) {
    const y = KF[type];
    hidden(`k-${type}`, `M650 191 C 676 191, 670 ${y}, 694 ${y} L846 ${y}`);
    hidden(`kdown-${type}`, `M846 ${y} L846 288`);
  }

  // ------------------------------------------------------------------ nodes
  const nodeEls = {};
  function box(id, spec) {
    const g = el("g", { class: "fl-node", "data-node": id, transform: `translate(${spec.x} ${spec.y})` }, nodeLayer);
    el("rect", { width: spec.w, height: spec.h, rx: 12, class: "fl-box" }, g);
    el("circle", { cx: 14, cy: 16, r: 4, class: "fl-dot" }, g);
    el("text", { x: 24, y: 20, class: "fl-title" }, g, spec.title);
    el("text", { x: 12, y: 37, class: "fl-sub" }, g, spec.sub);
    const metric = el("text", { x: 12, y: spec.h - 14, class: "fl-metric" }, g, "—");
    nodeEls[id] = { g, metric };
    return g;
  }
  for (const [id, spec] of Object.entries(NODES)) box(id, spec);

  // MQTT broker column
  const mqttGroup = el("g", { class: "fl-node fl-broker", "data-node": "mqtt" }, nodeLayer);
  el("rect", { x: 222, y: 8, width: 214, height: 360, rx: 14, class: "fl-box broker" }, mqttGroup);
  el("circle", { cx: 238, cy: 26, r: 4, class: "fl-dot" }, mqttGroup);
  el("text", { x: 248, y: 30, class: "fl-title big" }, mqttGroup, "MQTT · Mosquitto");
  const mqttSub = el("text", { x: 236, y: 49, class: "fl-sub" }, mqttGroup, "broker :1883 · 0 msg/s");
  const mqttRows = {};
  MQTT_ROWS.forEach(([suffix, roles], index) => {
    const y = mqttY(index) - 18;
    const row = el("g", { class: "fl-row" }, mqttGroup);
    el("rect", { x: 230, y, width: 198, height: 36, rx: 8, class: "fl-row-bg" }, row);
    el("text", { x: 240, y: y + 15, class: "fl-row-title" }, row, `…/${suffix}`);
    el("text", { x: 240, y: y + 29, class: "fl-row-sub" }, row, roles);
    const rate = el("text", { x: 420, y: y + 15, class: "fl-row-rate", "text-anchor": "end" }, row, "0/s");
    mqttRows[suffix] = { row, rate };
  });

  // Kafka column
  const kafkaGroup = el("g", { class: "fl-node fl-kafka", "data-node": "kafka" }, nodeLayer);
  el("rect", { x: 650, y: 8, width: 196, height: 300, rx: 14, class: "fl-box kafka" }, kafkaGroup);
  el("circle", { cx: 666, cy: 26, r: 4, class: "fl-dot" }, kafkaGroup);
  el("text", { x: 676, y: 30, class: "fl-title big" }, kafkaGroup, "Apache Kafka");
  const kafkaSub = el("text", { x: 664, y: 49, class: "fl-sub" }, kafkaGroup, "7 topic · lưu & replay");
  const kafkaRows = {};
  KAFKA_ROWS.forEach(([type, topic], index) => {
    const y = kafkaY(index) - 16;
    const row = el("g", { class: "fl-row" }, kafkaGroup);
    el("rect", { x: 658, y, width: 180, height: 32, rx: 8, class: "fl-row-bg" }, row);
    el("circle", { cx: 670, cy: y + 16, r: 3.5, class: `fl-type ${type}` }, row);
    el("text", { x: 680, y: y + 20, class: "fl-row-title small" }, row, topic);
    const rate = el("text", { x: 832, y: y + 20, class: "fl-row-rate", "text-anchor": "end" }, row, "0");
    kafkaRows[type] = { row, rate };
  });

  // Draw packets above everything.
  svg.appendChild(hiddenLayer);
  svg.appendChild(packetLayer);

  // ------------------------------------------------------------------ packets
  const CHAINS = {
    sensor: [
      ["sens-edge", "edge-readings", "m-readings", "mqtt-bridge", "bridge-kafka", "k-sensor", "kafka-influx", "influx-grafana"],
      ["sens-edge", "edge-readings", "m-readings", "mqtt-bridge", "bridge-kafka", "k-sensor", "kdown-sensor", "kafka-ui"],
    ],
    prediction: [["edge-predictions", "m-predictions", "mqtt-bridge", "bridge-kafka", "k-prediction", "kafka-controller"]],
    ground_truth: [["edge-gt", "m-ground-truth", "mqtt-bridge", "bridge-kafka", "k-ground_truth", "kdown-ground_truth", "kafka-ui"]],
    control_request: [["ui-control", "m-control-requests", "mqtt-bridge", "bridge-kafka", "k-control_request", "kafka-request-controller"]],
    actuator_command: [["controller-commands", "mrev-commands", "commands-device"]],
    actuator_state: [["device-ack", "m-actuator-state", "mqtt-bridge", "bridge-kafka", "k-actuator_state", "kdown-actuator_state", "kafka-ui"]],
  };
  const ROW_OF_SEGMENT = {};
  for (const [suffix] of MQTT_ROWS) {
    ROW_OF_SEGMENT[`m-${suffix}`] = () => mqttRows[suffix].row;
    ROW_OF_SEGMENT[`mrev-${suffix}`] = () => mqttRows[suffix].row;
  }
  for (const [type] of KAFKA_ROWS) ROW_OF_SEGMENT[`k-${type}`] = () => kafkaRows[type].row;
  const SPEED = 430; // svg units per second
  const packets = [];
  const lengths = {};
  const lengthOf = (id) => (lengths[id] ??= paths[id].getTotalLength());

  function flash(node) {
    node.classList.remove("hit");
    void node.getBBox();
    node.classList.add("hit");
  }

  function spawn(type) {
    if (REDUCED || packets.length > 60) return;
    for (const chain of CHAINS[type] || []) {
      const dot = el("circle", { r: 5, class: `fl-packet ${type}` }, packetLayer);
      packets.push({ dot, chain, index: 0, distance: 0 });
    }
  }

  let last = performance.now();
  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min(0.25, (now - last) / 1000);
    last = now;
    for (let i = packets.length - 1; i >= 0; i -= 1) {
      const packet = packets[i];
      packet.distance += SPEED * dt;
      let id = packet.chain[packet.index];
      while (id && packet.distance >= lengthOf(id)) {
        packet.distance -= lengthOf(id);
        packet.index += 1;
        id = packet.chain[packet.index];
        if (id && ROW_OF_SEGMENT[id]) flash(ROW_OF_SEGMENT[id]());
        if (!id) break;
      }
      if (!id) {
        const target = packet.chain[packet.chain.length - 1];
        const node = target.endsWith("grafana") ? "grafana" : target.endsWith("influx") ? "influx" : target.endsWith("controller") ? "controller" : target.endsWith("ui") ? "ui" : target.endsWith("device") ? "device" : target.endsWith("edge") ? "edge" : null;
        if (node && nodeEls[node]) flash(nodeEls[node].g);
        packet.dot.remove();
        packets.splice(i, 1);
        continue;
      }
      const point = paths[id].getPointAtLength(packet.distance);
      packet.dot.setAttribute("cx", point.x.toFixed(1));
      packet.dot.setAttribute("cy", point.y.toFixed(1));
    }
  }
  requestAnimationFrame(frame);

  // ------------------------------------------------------------------ live numbers
  const fmt = (value, digits = 1) => (Number.isFinite(value) ? value.toFixed(digits) : "—");
  const ms = (seconds) => (Number.isFinite(seconds) ? (seconds >= 1 ? `${seconds.toFixed(2)} s` : `${Math.round(seconds * 1000)} ms`) : "—");
  const lastCounts = {};

  function setNode(id, metric, state) {
    const node = id === "mqtt" ? mqttGroup : id === "kafka" ? kafkaGroup : nodeEls[id]?.g;
    if (!node) return;
    if (metric != null && nodeEls[id]) nodeEls[id].metric.textContent = metric;
    node.classList.toggle("ok", state === "ok");
    node.classList.toggle("warn", state === "warn");
    node.classList.toggle("down", state === "down");
  }

  function update(state, extra = {}) {
    // Spawn one dot per new record, capped so a backlog does not flood the map.
    for (const type of Object.keys(CHAINS)) {
      const count = state.counts?.[type] ?? 0;
      if (lastCounts[type] != null && count > lastCounts[type]) {
        for (let i = 0; i < Math.min(3, count - lastCounts[type]); i += 1) spawn(type);
      }
      lastCounts[type] = count;
    }

    const mqtt = state.mqtt || {};
    const pipeline = state.pipeline || {};
    const up = pipeline.up || {};
    const rates = pipeline.rates || {};
    const latency = pipeline.latency || {};
    const stream = state.stream || {};

    const bySuffix = {};
    for (const topic of mqtt.topics || []) bySuffix[topic.suffix] = (bySuffix[topic.suffix] || 0) + topic.rate;
    for (const [suffix] of MQTT_ROWS) mqttRows[suffix].rate.textContent = `${fmt(bySuffix[suffix] || 0)}/s`;
    mqttSub.textContent = `${mqtt.broker || "mosquitto:1883"} · QoS ${mqtt.qos ?? 1} · ${fmt(mqtt.rate || 0)} msg/s`;
    setNode("mqtt", null, mqtt.connected ? "ok" : "down");

    let kafkaTotal = 0;
    for (const [type] of KAFKA_ROWS) {
      const rate = stream[type]?.rate || 0;
      kafkaTotal += rate;
      kafkaRows[type].rate.textContent = fmt(rate);
    }
    const lagTotal = Object.values(pipeline.lag || {}).reduce((sum, value) => sum + (value || 0), 0);
    kafkaSub.textContent = `${fmt(kafkaTotal)} rec/s · lag ${pipeline.ok ? Math.round(lagTotal) : "—"}`;
    setNode("kafka", null, !pipeline.ok ? null : up["kafka-exporter"] === 0 ? "down" : lagTotal > 100 ? "warn" : "ok");

    const sensorAge = extra.sensorAge;
    setNode("sensors", `${extra.channels ?? 0}/16 kênh · ${sensorAge == null ? "chờ" : sensorAge < 5 ? "đang gửi" : "mất tín hiệu"}`, sensorAge == null ? null : sensorAge < 5 ? "ok" : "down");
    setNode("edge", `${state.latest?.sensor?.node_id || "—"} · seq ${state.latest?.sensor?.sequence ?? "—"}`, sensorAge == null ? null : sensorAge < 5 ? "ok" : "down");
    setNode("bridge", pipeline.ok ? `${fmt(rates.kafka_produced)} msg/s · DLQ ${Math.round(pipeline.dead_letter_5m || 0)}` : "Prometheus offline", !pipeline.ok ? null : up["mqtt-kafka-bridge"] === 0 ? "down" : (pipeline.dead_letter_5m || 0) > 0 ? "warn" : "ok");
    setNode("influx", pipeline.ok ? `${fmt(rates.influx_written)} pt/s · p95 ${ms(latency.edge_to_influx_p95)}` : "—", !pipeline.ok ? null : up["influx-sink"] === 0 || up.influxdb === 0 ? "down" : (latency.edge_to_influx_p95 || 0) > 3 ? "warn" : "ok");
    setNode("grafana", pipeline.ok ? (up.grafana === 1 ? "đang chạy" : "không phản hồi") : "—", !pipeline.ok ? null : up.grafana === 1 ? "ok" : "down");
    const ack = state.latest?.actuator_state;
    setNode("controller", extra.alarm ? `${String(extra.alarm).toUpperCase()} · ${ack?.mode === "manual" ? "DIRECT" : "AUTO"}` : "—", !pipeline.ok ? null : up["safety-controller"] === 0 ? "down" : "ok");
    setNode("device", extra.ackLatency != null ? `ACK sau ${ms(extra.ackLatency)}` : ack ? "đã ACK" : "chờ lệnh", ack ? "ok" : null);
    const uiLatency = stream.sensor?.latency_ms;
    setNode("ui", `${extra.transport || "—"} · ${uiLatency == null ? "—" : `${Math.round(uiLatency)} ms`}`, extra.transport === "SSE" || extra.transport === "polling" ? "ok" : "warn");
  }

  window.PipelineFlow = { update, spawn };
})();

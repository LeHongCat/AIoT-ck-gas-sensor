// Boots static/app.js against a minimal fake DOM and pushes one SSE snapshot through it.
// Run by tests/test_ui_smoke.py: a function that no longer exists, or a throw during boot(),
// leaves the real screen stuck on "CHỜ DỮ LIỆU", and this script fails instead.
//
//   node tests/ui_smoke.js src/aiot_pipeline/static/app.js
"use strict";
const fs = require("fs");
const vm = require("vm");

const elements = new Map();
const failures = [];

function makeElement(id = "") {
  const element = {
    id,
    textContent: "",
    innerHTML: "",
    title: "",
    href: "",
    style: { setProperty() {} },
    dataset: {},
    attributes: {},
    children: [],
    offsetWidth: 100,
    offsetHeight: 20,
    classList: {
      names: new Set(),
      add(...names) { names.forEach((name) => this.names.add(name)); },
      remove(...names) { names.forEach((name) => this.names.delete(name)); },
      toggle(name, force) { (force ?? !this.names.has(name)) ? this.names.add(name) : this.names.delete(name); },
      contains(name) { return this.names.has(name); },
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener() {},
    remove() {},
    closest() { return null; },
    querySelector() { return makeElement(); },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, right: 100, bottom: 20, width: 100, height: 20 }; },
  };
  element.parentElement = element;
  return element;
}

// The 16 e-nose buttons the script finds by class.
const SENSOR_TYPES = ["TGS2602", "TGS2602", "TGS2600", "TGS2600", "TGS2610", "TGS2610", "TGS2620", "TGS2620"];
const sensorNodes = Array.from({ length: 16 }, (_, index) => {
  const node = makeElement();
  node.dataset.index = String(index);
  node.dataset.sensor = SENSOR_TYPES[index % 8];
  return node;
});

const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  },
  querySelectorAll(selector) { return selector === ".sensor-node" ? sensorNodes : []; },
  querySelector() { return makeElement(); },
  createElement() { return makeElement(); },
  addEventListener() {},
};

let source = null;
class FakeEventSource {
  constructor(url) { this.url = url; source = this; }
  close() {}
}

const now = Date.now() / 1000;
const iso = (offset = 0) => new Date((now + offset) * 1000).toISOString();
const config = {
  sensors: [],
  grafana_url: "http://localhost:3000",
  model: { name: "tabm-int8", title: "TabM INT8", summary: "s", window_s: 60, features: ["last"], alarm: { warning: "w", danger: "d", note: "n" } },
  models: {},
  node_id: null,
};
config.models["tabm-int8"] = config.model;
// Two seeded predictions; the second is the one the snapshot carries again, stamped in µs by the
// backend and in ms by Date.parse(): it must count as the same record, not wipe the history.
const seeded = [
  [now - 1.000123, 6, 0, 8.2, "normal", 9, "", 7],
  [Math.floor(now * 1000) / 1000 + 0.000456, 7, 0, 8.3, "normal", 9, "", 8],
];
const truthRows = [[now - 1.000123, 6, 0, 8.33], [now, 7, 0, 8.33]];
const responses = { "/api/config": config, "/api/history": { sensor: [], prediction: seeded, ground_truth: truthRows } };

const context = {
  document,
  innerWidth: 1600,
  scrollY: 0,
  EventSource: FakeEventSource,
  fetch: async (path) => ({ ok: path in responses, status: path in responses ? 200 : 404, json: async () => responses[path] }),
  requestAnimationFrame: (callback) => setTimeout(callback, 0),
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval() {},
  console,
};
context.window = context; // in a browser `window` is the global object
vm.createContext(context);
process.on("unhandledRejection", (error) => failures.push(`unhandled rejection: ${error && error.stack}`));

const snapshot = {
  version: 3,
  node_id: "virtual-gas-01",
  node_pinned: false,
  latest: {
    sensor: { node_id: "virtual-gas-01", sequence: 7, observed_at: iso(), sensors: Array(16).fill(1500) },
    prediction: {
      node_id: "virtual-gas-01", sequence: 7, observed_at: iso(), co_ppm: 0, ethylene_ppm: 8.3,
      alarm_level: "normal", model_name: "tabm-int8", model_version: "seed42", inference_ms: 9, window_s: 8, top_channels: [],
    },
    ground_truth: { node_id: "virtual-gas-01", sequence: 7, observed_at: iso(), co_ppm: 0, ethylene_ppm: 8.33 },
    control_request: null,
    actuator_command: null,
    actuator_state: {
      node_id: "virtual-gas-01", sequence: 0, observed_at: iso(), mode: "auto", fan_on: false, exhaust_on: false,
      gas_valve_closed: false, alarm_on: false, suppression_on: false,
    },
  },
  counts: { sensor: 8, prediction: 8, control_request: 0, actuator_command: 1, actuator_state: 1 },
  events: [],
  stream: {},
  kafka_error: null,
  uptime_seconds: 5,
  server_time: now,
  mqtt: { broker: "mosquitto:1883", qos: 1, connected: true, rate: 2, stats: {}, topics: [] },
  pipeline: { rates: {} },
};

try {
  vm.runInContext(fs.readFileSync(process.argv[2], "utf-8"), context, { filename: process.argv[2] });
} catch (error) {
  failures.push(`app.js threw while loading: ${error.stack}`);
}

setTimeout(() => {
  if (!source) failures.push("boot() never opened the SSE stream (connect() was not reached)");
  else {
    try {
      source.onmessage({ data: JSON.stringify(snapshot) });
    } catch (error) {
      failures.push(`SSE message handler threw: ${error.stack}`);
    }
  }
  setTimeout(() => {
    const text = (id) => document.getElementById(id).textContent;
    if (source && text("alarmText") !== "NORMAL") failures.push(`alarm text is "${text("alarmText")}", expected NORMAL`);
    if (source && text("transportText") !== "LIVE · SSE PUSH") failures.push(`transport is "${text("transportText")}"`);
    if (source && !document.getElementById("actuatorStateList").innerHTML.includes("Van gas")) failures.push("device state list was not rendered");
    const points = (document.getElementById("predLine").attributes.points || "").split(" ").filter(Boolean);
    if (source && points.length !== 2) failures.push(`trend has ${points.length} points, expected the 2 seeded ones`);
    const truthPoints = (document.getElementById("truthLine").attributes.points || "").split(" ").filter(Boolean);
    if (source && truthPoints.length !== 2) failures.push(`ground-truth line has ${truthPoints.length} points, expected 2`);
    if (source && text("ethyleneTruth") !== "8.33 ppm") failures.push(`C2H4 ground truth shows "${text("ethyleneTruth")}"`);
    if (source && text("accPairs") !== "2") failures.push(`accuracy pairs "${text("accPairs")}", expected 2`);
    if (failures.length) {
      console.error(failures.join("\n"));
      process.exit(1);
    }
    console.log("ui smoke ok");
  }, 50);
}, 50);

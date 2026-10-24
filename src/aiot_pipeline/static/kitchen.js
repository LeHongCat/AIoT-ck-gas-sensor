/*
 * Industrial kitchen scene: a top-down floor plan with a working crew.
 *
 * The scene is driven by acknowledged pipeline state through KitchenScene.update():
 *   - fan_on       -> hood fans spin, smoke is pulled into the hood
 *   - exhaust_on   -> roof exhaust runs, duct airflow animates, haze clears faster
 *   - gas valve    -> closed valve shuts every burner; hot-line cooks stop and wait
 *   - alarm_on     -> wall sirens: flashing dome, rotating beams, sound waves (slash when muted)
 *   - danger level -> the crew evacuates through the emergency exit to the muster point
 */
(() => {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("kitchenSvg");
  const scene = document.getElementById("roomScene");
  if (!svg || !scene) return;

  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DEG = Math.PI / 180;
  const rand = (min, max) => min + Math.random() * (max - min);
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function el(tag, attrs = {}, parent = null, text = null) {
    const node = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text != null) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }

  // ------------------------------------------------------------------ floor plan
  // Walkable aisle graph. Stations connect to the nearest node.
  const NODES = {
    c0: [320, 262], c1: [392, 262], c2: [485, 262], c3: [555, 262], c4: [625, 262],
    c5: [735, 262], c6: [805, 262], c7: [940, 262], c8: [1070, 262], c9: [1195, 262],
    c10: [1275, 262],
    l1: [320, 360], l2: [320, 450], l3: [320, 570], l4: [320, 720], l5: [320, 790], l6: [320, 855],
    p0: [158, 360], p1: [158, 450], p2: [158, 570], p3: [158, 795],
    m1: [480, 450], m2: [650, 450], m3: [820, 450], m4: [900, 450], m5: [1100, 450],
    m6: [1275, 450], m7: [1370, 450], m8: [1490, 452],
    b1: [480, 720], b2: [780, 720], b3: [820, 720], b4: [1100, 720], b5: [1275, 720], b6: [1440, 720],
    s0: [1440, 560],
    d0: [820, 925], d1: [820, 1040],
    e0: [1440, 860], e1: [1540, 860], e2: [1608, 860],
  };
  const EDGES = [
    "c0-c1", "c1-c2", "c2-c3", "c3-c4", "c4-c5", "c5-c6", "c6-c7", "c7-c8", "c8-c9", "c9-c10",
    "c0-l1", "l1-l2", "l2-l3", "l3-l4", "l4-l5", "l5-l6",
    "l1-p0", "p0-p1", "p1-p2", "p3-l5",
    "l2-m1", "m1-m2", "m2-m3", "m3-m4", "m4-m5", "m5-m6", "m6-m7", "m7-m8",
    "c10-m6", "m6-b5",
    "l4-b1", "b1-b2", "b2-b3", "b3-b4", "b4-b5", "b5-b6",
    "m1-b1", "m5-b4", "m8-s0", "s0-b6",
    "b3-d0", "d0-d1",
    "b6-e0", "e0-e1", "e1-e2",
  ];
  const GRAPH = {};
  for (const edge of EDGES) {
    const [a, b] = edge.split("-");
    const cost = Math.hypot(NODES[a][0] - NODES[b][0], NODES[a][1] - NODES[b][1]);
    (GRAPH[a] ||= []).push([b, cost]);
    (GRAPH[b] ||= []).push([a, cost]);
  }

  // x, y = where the person stands; a = facing angle in degrees (0 = right, -90 = up).
  const STATIONS = {
    combi: { x: 392, y: 240, a: -90, name: "Lò combi" },
    wok1: { x: 485, y: 240, a: -90, name: "Wok 1", burners: ["wok1"], wok: "wok1" },
    wok2: { x: 555, y: 240, a: -90, name: "Wok 2", burners: ["wok2"], wok: "wok2" },
    wok3: { x: 625, y: 240, a: -90, name: "Wok 3", burners: ["wok3"], wok: "wok3" },
    range1: { x: 735, y: 240, a: -90, name: "Bếp Âu trái", burners: ["r0", "r3"] },
    range2: { x: 805, y: 240, a: -90, name: "Bếp Âu phải", burners: ["r2", "r5"] },
    griddle: { x: 940, y: 240, a: -90, name: "Bếp áp chảo", burners: ["griddle"] },
    fryer: { x: 1070, y: 240, a: -90, name: "Bếp chiên", burners: ["fryer"] },
    stock: { x: 1195, y: 240, a: -90, name: "Nồi ninh", burners: ["stock"] },
    passA: { x: 520, y: 287, a: 90, name: "Pass" },
    passB: { x: 690, y: 287, a: 90, name: "Pass" },
    passC: { x: 930, y: 287, a: 90, name: "Pass" },
    passD: { x: 1090, y: 287, a: 90, name: "Pass" },
    expo: { x: 815, y: 287, a: 90, name: "Pass trung tâm" },
    pickup: { x: 815, y: 386, a: -90, name: "Nhận món" },
    islandA: { x: 650, y: 516, a: 90, name: "Bàn chia nguội" },
    islandB: { x: 900, y: 516, a: 90, name: "Bàn chia nguội" },
    islandC: { x: 780, y: 645, a: -90, name: "Bàn chia nguội" },
    prepA1: { x: 138, y: 450, a: 180, name: "Bàn sơ chế A" },
    prepA2: { x: 138, y: 570, a: 180, name: "Bàn sơ chế A" },
    prepB1: { x: 270, y: 450, a: 180, name: "Bàn sơ chế B" },
    prepB2: { x: 270, y: 570, a: 180, name: "Bàn sơ chế B" },
    prepSink: { x: 115, y: 773, a: -90, name: "Bồn rửa rau" },
    dry: { x: 190, y: 857, a: 90, name: "Kho khô" },
    cooler: { x: 286, y: 236, a: 180, name: "Kho lạnh" },
    valve: { x: 302, y: 134, a: -70, name: "Van gas tổng" },
    hood: { x: 1005, y: 258, a: -90, name: "Chụp hút" },
    dish: { x: 1490, y: 424, a: -90, name: "Máy rửa" },
    potSink: { x: 1458, y: 570, a: 0, name: "Bồn cọ nồi" },
    dropTable: { x: 1370, y: 376, a: -90, name: "Bàn bát bẩn" },
    dining: { x: 820, y: 1040, a: 90, name: "Phòng ăn", offscreen: true },
  };
  const MUSTER = { x: 1650, y: 860 };
  const MUSTER_SLOTS = [];
  for (let row = -1; row <= 1; row += 1) {
    for (let col = -1; col <= 1; col += 1) {
      MUSTER_SLOTS.push({ x: MUSTER.x + col * 32, y: MUSTER.y + row * 38, a: 180 });
    }
  }

  // ------------------------------------------------------------------ crew
  const CREW = [
    {
      id: "minh", name: "Minh", role: "Bếp trưởng", jacket: "#262d31", apron: null,
      skin: "#d7a77f", hat: "toque", start: "expo",
      tasks: [
        { at: ["expo"], act: "inspect", dur: [6, 10], label: "Kiểm món ra pass" },
        { visit: ["wok1", "wok2", "wok3", "range1", "range2", "fryer"], act: "taste", dur: [3, 5], label: "Nếm thử" },
        { at: ["expo"], act: "plate", dur: [4, 7], label: "Hoàn thiện món", plate: true },
        { visit: ["islandA", "islandB"], act: "inspect", dur: [4, 6], label: "Kiểm bàn nguội" },
      ],
    },
    {
      id: "hung", name: "Hùng", role: "Bếp wok", jacket: "#f1f0ea", apron: "#2c3e50",
      skin: "#c48b62", hat: "cap", hatColor: "#1d1f21", start: "wok1",
      tasks: [
        { at: ["wok1", "wok2"], act: "wok", dur: [12, 18], label: "Đảo wok", gas: true },
        { at: ["passA", "passB"], act: "plate", dur: [3, 5], label: "Bày món", plate: true },
      ],
    },
    {
      id: "lan", name: "Lan", role: "Bếp wok", jacket: "#f1f0ea", apron: "#7a2e2e",
      skin: "#e2b896", hat: "net", hatColor: "#b33a3a", start: "wok3",
      tasks: [
        { at: ["wok3", "wok2"], act: "wok", dur: [11, 17], label: "Xào lửa lớn", gas: true },
        { at: ["passB", "passA"], act: "plate", dur: [3, 5], label: "Bày món", plate: true },
      ],
    },
    {
      id: "tuan", name: "Tuấn", role: "Bếp Âu", jacket: "#f1f0ea", apron: "#2c3e50",
      skin: "#b27a55", hat: "toque", start: "range1",
      tasks: [
        { at: ["range1", "range2"], act: "stir", dur: [10, 16], label: "Nấu sốt", gas: true },
        { at: ["griddle"], act: "flip", dur: [8, 12], label: "Áp chảo", gas: true },
        { at: ["passC"], act: "plate", dur: [3, 5], label: "Bày món", plate: true },
      ],
    },
    {
      id: "vy", name: "Vy", role: "Bếp chiên", jacket: "#f1f0ea", apron: "#3d5a40",
      skin: "#e8c09c", hat: "cap", hatColor: "#f4f4f0", start: "fryer",
      tasks: [
        { at: ["fryer"], act: "fry", dur: [10, 15], label: "Chiên giòn", gas: true },
        { at: ["passD"], act: "plate", dur: [3, 5], label: "Bày món", plate: true },
        { at: ["stock"], act: "stir", dur: [6, 9], label: "Ninh nước dùng", gas: true },
        { at: ["combi"], act: "oven", dur: [4, 6], label: "Kiểm lò combi" },
      ],
    },
    {
      id: "nam", name: "Nam", role: "Sơ chế", jacket: "#dfe7ec", apron: "#4a6b8a",
      skin: "#c99872", hat: "cap", hatColor: "#2d4560", start: "prepA1",
      tasks: [
        { at: ["prepA1", "prepA2"], act: "chop", dur: [12, 18], label: "Thái rau củ" },
        { at: ["cooler"], act: "fetch", dur: [3, 5], label: "Lấy nguyên liệu", door: true },
        { at: ["passA", "passC", "passD"], act: "drop", dur: [2, 3], label: "Tiếp nguyên liệu", carry: "crate" },
      ],
    },
    {
      id: "thao", name: "Thảo", role: "Bàn nguội", jacket: "#dfe7ec", apron: "#8a5a3c",
      skin: "#f0c9a3", hat: "net", hatColor: "#8c6fc4", start: "prepB1",
      tasks: [
        { at: ["prepB1", "prepB2"], act: "chop", dur: [10, 16], label: "Sơ chế thịt" },
        { at: ["prepSink"], act: "wash", dur: [6, 9], label: "Rửa rau" },
        { at: ["islandA", "islandB", "islandC"], act: "plate", dur: [8, 12], label: "Bày món nguội", carry: "bowl" },
        { at: ["dry"], act: "fetch", dur: [3, 4], label: "Lấy đồ khô" },
      ],
    },
    {
      id: "duc", name: "Đức", role: "Rửa bát", jacket: "#5d7d8f", apron: "#e0c34a",
      skin: "#a8714c", hat: "cap", hatColor: "#6d7478", gloves: "#f2cf3a", start: "dish",
      tasks: [
        { at: ["dish"], act: "wash", dur: [10, 15], label: "Chạy máy rửa" },
        { at: ["potSink"], act: "wash", dur: [8, 12], label: "Cọ nồi chảo" },
        { at: ["dropTable"], act: "sort", dur: [4, 6], label: "Phân loại bát" },
      ],
    },
    {
      id: "ha", name: "Hà", role: "Chạy món", jacket: "#16191b", apron: "#6b4b3a",
      skin: "#e6bb92", hat: "hair", hatColor: "#2a1d17", start: "pickup",
      tasks: [
        { at: ["pickup"], act: "pickup", dur: [2, 3], label: "Nhận món", needsPlates: true },
        { at: ["dining"], act: "away", dur: [5, 9], label: "Phục vụ bàn", carry: "tray" },
        { at: ["dropTable"], act: "drop", dur: [2, 3], label: "Trả bát bẩn", carry: "dirty" },
      ],
    },
  ];

  // ------------------------------------------------------------------ static drawing
  const defs = el("defs", {}, svg);
  defs.innerHTML = `
    <pattern id="kTile" width="40" height="40" patternUnits="userSpaceOnUse">
      <rect width="40" height="40" fill="#3a4144"/>
      <path d="M40 0H0V40" fill="none" stroke="#30373a" stroke-width="2"/>
      <rect x="2" y="2" width="36" height="36" fill="#ffffff" opacity=".015"/>
    </pattern>
    <pattern id="kMat" width="12" height="12" patternUnits="userSpaceOnUse">
      <rect width="12" height="12" fill="#22282a"/>
      <circle cx="6" cy="6" r="2.2" fill="#2e3538"/>
    </pattern>
    <pattern id="kHatch" width="10" height="10" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <rect width="10" height="10" fill="#e3b33b"/><rect width="5" height="10" fill="#26292b"/>
    </pattern>
    <pattern id="kGrate" width="8" height="8" patternUnits="userSpaceOnUse">
      <rect width="8" height="8" fill="#2b3134"/><path d="M0 4H8M4 0V8" stroke="#40484c" stroke-width="1.4"/>
    </pattern>
    <pattern id="kAsphalt" width="22" height="22" patternUnits="userSpaceOnUse">
      <rect width="22" height="22" fill="#1a1f21"/><circle cx="5" cy="7" r="1" fill="#252b2e"/><circle cx="15" cy="16" r="1.2" fill="#222829"/>
    </pattern>
    <linearGradient id="kSteel" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#d4dbdf"/><stop offset="1" stop-color="#9ea8ae"/>
    </linearGradient>
    <linearGradient id="kSteelSide" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#c3ccd1"/><stop offset=".5" stop-color="#dfe5e8"/><stop offset="1" stop-color="#aab4ba"/>
    </linearGradient>
    <linearGradient id="kCooler" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#d9e3e8"/><stop offset="1" stop-color="#b8c6cd"/>
    </linearGradient>
    <radialGradient id="kFlame">
      <stop offset="0" stop-color="#fff2b8" stop-opacity=".95"/>
      <stop offset=".45" stop-color="#ffab3d" stop-opacity=".85"/>
      <stop offset=".8" stop-color="#ff5a1f" stop-opacity=".35"/>
      <stop offset="1" stop-color="#ff3d00" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="kBlue">
      <stop offset=".55" stop-color="#2e7bff" stop-opacity="0"/>
      <stop offset=".8" stop-color="#58a6ff" stop-opacity=".85"/>
      <stop offset="1" stop-color="#9fd0ff" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="kLamp">
      <stop offset="0" stop-color="#ffcf7a" stop-opacity=".55"/><stop offset="1" stop-color="#ff9d2e" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="kHazeNormal"><stop offset="0" stop-color="#9fb4ad" stop-opacity=".9"/><stop offset="1" stop-color="#9fb4ad" stop-opacity="0"/></radialGradient>
    <radialGradient id="kHazeWarning"><stop offset="0" stop-color="#f4b860" stop-opacity=".95"/><stop offset="1" stop-color="#f4b860" stop-opacity="0"/></radialGradient>
    <linearGradient id="kSpray" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#e9f7ff" stop-opacity=".95"/><stop offset="1" stop-color="#9fd6ff" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="kFoam"><stop offset="0" stop-color="#ffffff" stop-opacity=".95"/><stop offset=".7" stop-color="#e3f2fb" stop-opacity=".8"/><stop offset="1" stop-color="#cfe7f5" stop-opacity="0"/></radialGradient>
    <marker id="kArrow" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" fill="#8ff0ff"/>
    </marker>
    <radialGradient id="kHazeDanger"><stop offset="0" stop-color="#ff6b62" stop-opacity="1"/><stop offset="1" stop-color="#ff6b62" stop-opacity="0"/></radialGradient>
    <filter id="kDrop" x="-10%" y="-10%" width="130%" height="140%">
      <feDropShadow dx="0" dy="4" stdDeviation="4" flood-color="#000" flood-opacity=".38"/>
    </filter>
  `;

  // Two stacked SVGs: the static plan (floor, equipment, drop shadows) is painted once; only
  // the live overlay (people, flames, fans, smoke) repaints every frame.
  const live = el("svg", { viewBox: "0 0 1720 1000", class: "k-live", "aria-hidden": "true" });
  svg.after(live);
  const layers = {};
  for (const name of ["floor", "fixtures", "hood"]) layers[name] = el("g", { class: `k-${name}` }, svg);
  for (const name of ["dynamic", "plates", "airflow", "crew", "motion", "particles", "spray", "haze", "alerts", "labels"]) {
    layers[name] = el("g", { class: `k-${name}` }, live);
  }
  const L = layers;

  function steel(x, y, w, h, parent, extra = {}) {
    return el("rect", { x, y, width: w, height: h, rx: 5, fill: "url(#kSteel)", stroke: "#6f7a80", "stroke-width": 1.5, ...extra }, parent);
  }
  function floorText(x, y, text, parent = L.floor, extra = {}) {
    return el("text", { x, y, class: "k-zone", "text-anchor": "middle", ...extra }, parent, text);
  }

  // Outside strip, floor, mats, walls
  el("rect", { x: 1580, y: 0, width: 140, height: 1000, fill: "url(#kAsphalt)" }, L.floor);
  el("rect", { x: 0, y: 0, width: 1580, height: 1000, fill: "#14181a" }, L.floor);
  el("rect", { x: 40, y: 40, width: 1520, height: 920, fill: "url(#kTile)" }, L.floor);
  el("rect", { x: 372, y: 216, width: 876, height: 64, rx: 6, fill: "url(#kMat)", opacity: ".95" }, L.floor);
  el("rect", { x: 1440, y: 405, width: 100, height: 40, rx: 5, fill: "url(#kMat)" }, L.floor);
  for (const [x, y] of [[820, 300], [790, 470], [1380, 520], [230, 720]]) {
    el("rect", { x: x - 14, y: y - 14, width: 28, height: 28, rx: 3, fill: "url(#kGrate)", stroke: "#262c2f" }, L.floor);
  }
  const evacRoute = el("path", {
    class: "k-evac-route",
    d: "M320 262 H1275 V720 H1440 V860 H1608 M320 262 V720 H1275 M815 386 V450 H1275",
  }, L.floor);
  evacRoute.setAttribute("fill", "none");
  for (const [x, y, w, h] of [[20, 20, 1560, 20], [20, 20, 20, 960], [20, 960, 740, 20], [880, 960, 700, 20], [1560, 20, 20, 800], [1560, 900, 20, 80]]) {
    el("rect", { x, y, width: w, height: h, fill: "#0c0f10" }, L.floor);
  }
  el("rect", { x: 40, y: 40, width: 1520, height: 920, fill: "none", stroke: "#566064", "stroke-width": 2 }, L.floor);

  floorText(1650, 760, "ĐIỂM TẬP TRUNG", L.floor, { class: "k-zone k-muster-text" });
  el("circle", { cx: MUSTER.x, cy: MUSTER.y, r: 66, class: "k-muster" }, L.floor);
  for (const x of [1592, 1612]) el("path", { d: `M${x} 846 l12 14 l-12 14`, class: "k-exit-arrow" }, L.floor);
  floorText(1650, 980, "Bên ngoài", L.floor, { class: "k-zone k-dim" });

  // Doors
  const exitDoor = el("line", { x1: 1560, y1: 822, x2: 1560, y2: 898, class: "k-door k-exit-door" }, L.fixtures);
  el("rect", { x: 1500, y: 905, width: 52, height: 22, rx: 3, class: "k-exit-sign" }, L.fixtures);
  el("text", { x: 1526, y: 921, class: "k-exit-sign-text", "text-anchor": "middle" }, L.fixtures, "EXIT");
  const diningDoors = [
    el("line", { x1: 762, y1: 960, x2: 818, y2: 960, class: "k-door" }, L.fixtures),
    el("line", { x1: 878, y1: 960, x2: 822, y2: 960, class: "k-door" }, L.fixtures),
  ];
  floorText(820, 994, "PHÒNG ĂN", L.floor, { class: "k-zone k-dim" });

  // Walk-in cooler
  el("rect", { x: 42, y: 42, width: 208, height: 248, rx: 4, fill: "url(#kCooler)", stroke: "#8394a0", "stroke-width": 2, filter: "url(#kDrop)" }, L.fixtures);
  for (let x = 72; x < 250; x += 32) el("line", { x1: x, y1: 46, x2: x, y2: 286, stroke: "#a9b8c0", "stroke-width": 1 }, L.fixtures);
  el("rect", { x: 88, y: 128, width: 116, height: 58, rx: 6, fill: "#22303a", opacity: ".92" }, L.fixtures);
  el("text", { x: 146, y: 152, class: "k-panel-title", "text-anchor": "middle" }, L.fixtures, "KHO LẠNH");
  el("text", { x: 146, y: 175, class: "k-panel-value", "text-anchor": "middle" }, L.fixtures, "2.4°C");
  const coolerDoor = el("line", { x1: 250, y1: 202, x2: 250, y2: 270, class: "k-door k-cooler-door" }, L.fixtures);

  // Gas cabinet, pipe, main valve
  el("rect", { x: 262, y: 44, width: 58, height: 34, rx: 3, fill: "url(#kHatch)", stroke: "#1b1d1e", "stroke-width": 2 }, L.fixtures);
  el("rect", { x: 270, y: 51, width: 42, height: 20, rx: 2, fill: "#1c2023" }, L.fixtures);
  el("text", { x: 291, y: 66, class: "k-gas-label", "text-anchor": "middle" }, L.fixtures, "GAS");
  const gasPipe = el("path", { d: "M291 78 V88 H1250", class: "k-gas-pipe" }, L.fixtures);
  for (const x of [392, 555, 770, 940, 1070, 1195]) el("line", { x1: x, y1: 88, x2: x, y2: 96, class: "k-gas-drop" }, L.fixtures);
  const valve = el("g", { class: "k-valve", transform: "translate(322 88)" }, L.fixtures);
  el("circle", { r: 11, class: "k-valve-body" }, valve);
  el("rect", { x: -3, y: -20, width: 6, height: 18, rx: 2, class: "k-valve-handle" }, valve);
  floorText(330, 118, "VAN TỔNG", L.fixtures, { class: "k-tag" });

  // Demo leak on the gas line between the wok range and the 6-burner range.
  const LEAK = { x: 664, y: 88 };
  // Leak marks live in the alerts layer so the hood fans never cover them.
  const leakMark = el("g", { class: "k-leak", transform: `translate(${LEAK.x} ${LEAK.y})` }, L.alerts);
  for (const delay of [0, 0.5, 1]) el("circle", { r: 10, class: "k-leak-ring", style: `animation-delay:${delay}s` }, leakMark);
  el("circle", { r: 6, class: "k-leak-core" }, leakMark);
  el("text", { x: 0, y: -18, class: "k-leak-text", "text-anchor": "middle" }, leakMark, "RÒ RỈ GAS");
  const isolatedMark = el("g", { class: "k-leak-isolated", transform: `translate(${LEAK.x} ${LEAK.y})` }, L.alerts);
  el("rect", { x: -54, y: -30, width: 108, height: 20, rx: 6 }, isolatedMark);
  el("text", { x: 0, y: -16, "text-anchor": "middle" }, isolatedMark, "ĐÃ CÔ LẬP");

  // Cooking line
  const line = el("g", { filter: "url(#kDrop)" }, L.fixtures);
  // combi oven
  steel(345, 96, 96, 108, line);
  el("rect", { x: 355, y: 108, width: 76, height: 56, rx: 4, fill: "#283034", stroke: "#59646a" }, line);
  el("rect", { x: 355, y: 172, width: 76, height: 22, rx: 3, fill: "#1f2629" }, line);
  const combiLed = el("circle", { cx: 420, cy: 183, r: 4, class: "k-led" }, line);
  el("text", { x: 381, y: 187, class: "k-tag k-tag-dark" }, line, "COMBI");
  // wok range
  steel(450, 96, 212, 108, line);
  // 6-burner range
  steel(670, 96, 202, 108, line);
  el("rect", { x: 678, y: 102, width: 186, height: 96, rx: 4, fill: "#2a3033" }, line);
  // griddle
  steel(880, 96, 122, 108, line);
  el("rect", { x: 888, y: 104, width: 106, height: 84, rx: 3, fill: "#34393c", stroke: "#4c5458" }, line);
  for (let x = 898; x < 990; x += 12) el("line", { x1: x, y1: 108, x2: x, y2: 184, stroke: "#3c4245", "stroke-width": 2 }, line);
  // fryers
  steel(1010, 96, 122, 108, line);
  // stock pot range
  steel(1140, 96, 112, 108, line);

  // Burners (flames live in the dynamic layer so the static line keeps its filter cache)
  const BURNERS = {};
  function burner(id, cx, cy, r, kind = "ring") {
    const group = el("g", { class: `k-burner k-burner-${kind}`, transform: `translate(${cx} ${cy})` }, L.dynamic);
    if (kind === "ring") {
      el("circle", { r: r + 2, fill: "#1b1f21", stroke: "#4b5357", "stroke-width": 3 }, group);
      const flame = el("g", { class: "k-flame" }, group);
      el("circle", { r: r + 10, fill: "url(#kFlame)", class: "k-flame-glow" }, flame);
      el("circle", { r: r - 1, fill: "none", stroke: "url(#kBlue)", "stroke-width": 7, class: "k-flame-ring" }, flame);
    } else {
      const flame = el("g", { class: "k-flame" }, group);
      el("rect", { x: -r.w / 2, y: -r.h / 2, width: r.w, height: r.h, rx: 6, fill: "url(#kFlame)", class: "k-flame-glow" }, flame);
    }
    BURNERS[id] = { group, lit: null, high: null };
    return group;
  }
  burner("wok1", 485, 146, 28);
  burner("wok2", 555, 146, 28);
  burner("wok3", 625, 146, 28);
  const rangeSpots = [[705, 126], [771, 126], [837, 126], [705, 174], [771, 174], [837, 174]];
  rangeSpots.forEach(([x, y], index) => burner(`r${index}`, x, y, 17));
  burner("griddle", 941, 146, { w: 102, h: 80 }, "plate");
  burner("fryer", 1071, 136, { w: 112, h: 74 }, "plate");
  burner("stock", 1196, 148, 42);

  // Cookware on top of the burners
  const WOKS = {};
  for (const id of ["wok1", "wok2", "wok3"]) {
    const x = { wok1: 485, wok2: 555, wok3: 625 }[id];
    const wok = el("g", { class: "k-wok", transform: `translate(${x} 146)` }, L.dynamic);
    el("line", { x1: 0, y1: 20, x2: 0, y2: 48, stroke: "#1a1a1a", "stroke-width": 6, "stroke-linecap": "round" }, wok);
    el("circle", { r: 25, fill: "#2b2f31", stroke: "#4c5257", "stroke-width": 3 }, wok);
    el("circle", { r: 15, fill: "#3a3127" }, wok);
    const food = el("g", { class: "k-wok-food" }, wok);
    for (let i = 0; i < 7; i += 1) {
      el("circle", { cx: rand(-9, 9), cy: rand(-9, 9), r: rand(2.2, 3.6), fill: ["#6fae4a", "#e0573b", "#f1d27a", "#c78a4a"][i % 4] }, food);
    }
    WOKS[id] = wok;
  }
  const pot = (x, y, r, liquid) => {
    el("circle", { cx: x, cy: y, r, fill: "url(#kSteelSide)", stroke: "#7c878d", "stroke-width": 2 }, L.dynamic);
    el("circle", { cx: x, cy: y, r: r - 5, fill: liquid }, L.dynamic);
  };
  pot(771, 126, 22, "#b97b3c");
  pot(837, 174, 15, "#8e3b2c");
  el("circle", { cx: 705, cy: 174, r: 19, fill: "#2c3033", stroke: "#50575c", "stroke-width": 2 }, L.dynamic);
  el("line", { x1: 705, y1: 193, x2: 705, y2: 215, stroke: "#1b1b1b", "stroke-width": 5, "stroke-linecap": "round" }, L.dynamic);
  pot(1196, 148, 40, "#a8743f");
  el("path", { d: "M1196 148 L1226 122", stroke: "#cfd6da", "stroke-width": 4, "stroke-linecap": "round" }, L.dynamic);
  const griddleFood = el("g", { class: "k-griddle-food" }, L.dynamic);
  for (const [x, y] of [[912, 128], [940, 158], [968, 126], [966, 166]]) el("circle", { cx: x, cy: y, r: 9, fill: "#7a4a2b", stroke: "#5a321b", "stroke-width": 2 }, griddleFood);
  for (const x of [1018, 1074]) {
    el("rect", { x, y: 104, width: 48, height: 68, rx: 4, fill: "#c99a2e", stroke: "#6b5a2e", "stroke-width": 2 }, L.dynamic);
  }
  const fryBubbles = el("g", { class: "k-bubbles" }, L.dynamic);
  for (let i = 0; i < 12; i += 1) {
    el("circle", { cx: (i % 2 ? 1098 : 1042) + rand(-14, 14), cy: rand(114, 162), r: rand(1.5, 3), class: "k-bubble", style: `animation-delay:${-rand(0, 1.2)}s` }, fryBubbles);
  }
  const baskets = el("g", { class: "k-baskets" }, L.dynamic);
  for (const x of [1022, 1078]) {
    el("rect", { x, y: 112, width: 40, height: 46, rx: 3, fill: "url(#kGrate)", stroke: "#9aa3a8", "stroke-width": 1.5, opacity: ".85" }, baskets);
    el("line", { x1: x + 20, y1: 158, x2: x + 20, y2: 196, stroke: "#222", "stroke-width": 4, "stroke-linecap": "round" }, baskets);
  }

  // Pass with heat lamps
  steel(380, 312, 820, 48, L.fixtures, { filter: "url(#kDrop)" });
  floorText(404, 342, "PASS", L.fixtures, { class: "k-tag k-tag-dark" });
  const PLATE_SLOTS = [];
  for (let x = 470; x <= 1160; x += 86) PLATE_SLOTS.push(x);
  for (const x of PLATE_SLOTS) el("circle", { cx: x, cy: 336, r: 30, fill: "url(#kLamp)", class: "k-lamp" }, L.alerts);

  // Central island
  steel(520, 540, 540, 80, L.fixtures, { filter: "url(#kDrop)" });
  el("text", { x: 790, y: 586, class: "k-tag k-tag-dark", "text-anchor": "middle" }, L.fixtures, "BÀN CHIA NGUỘI");
  for (const [x, y, c] of [[590, 566, "#7fb069"], [628, 594, "#e9c46a"], [1000, 566, "#e76f51"], [960, 596, "#90be6d"]]) {
    el("circle", { cx: x, cy: y, r: 13, fill: "#f4f4ef", stroke: "#c7cbc9" }, L.fixtures);
    el("circle", { cx: x, cy: y, r: 7, fill: c }, L.fixtures);
  }
  el("rect", { x: 690, y: 552, width: 44, height: 30, rx: 3, fill: "#b98a5b", stroke: "#8a643f" }, L.fixtures);
  el("rect", { x: 846, y: 552, width: 44, height: 30, rx: 3, fill: "#e8e2d4", stroke: "#b9b2a2" }, L.fixtures);

  // Prep zone
  steel(60, 380, 55, 260, L.fixtures, { filter: "url(#kDrop)" });
  steel(190, 380, 55, 260, L.fixtures, { filter: "url(#kDrop)" });
  for (const [x, y] of [[68, 420], [68, 540], [198, 420], [198, 540]]) {
    el("rect", { x, y, width: 39, height: 50, rx: 4, fill: "#c49a6c", stroke: "#8d6a45" }, L.fixtures);
  }
  for (const [x, y, c] of [[87, 492, "#7fb069"], [92, 610, "#9bc53d"], [217, 500, "#c8553d"], [222, 612, "#e9c46a"]]) {
    el("circle", { cx: x, cy: y, r: 7, fill: c }, L.fixtures);
    el("circle", { cx: x + 9, cy: y + 6, r: 5, fill: "#e76f51" }, L.fixtures);
  }
  floorText(186, 368, "SƠ CHẾ");
  steel(55, 690, 120, 56, L.fixtures, { filter: "url(#kDrop)" });
  el("rect", { x: 64, y: 698, width: 46, height: 40, rx: 6, fill: "#5d7f93", stroke: "#7f98a5" }, L.fixtures);
  el("rect", { x: 118, y: 698, width: 46, height: 40, rx: 6, fill: "#5d7f93", stroke: "#7f98a5" }, L.fixtures);
  // Dry storage shelves
  el("rect", { x: 55, y: 884, width: 270, height: 70, rx: 3, fill: "#5b5f5f", stroke: "#3f4444", filter: "url(#kDrop)" }, L.fixtures);
  for (let x = 62; x < 318; x += 32) {
    el("rect", { x, y: 892, width: 26, height: 22, rx: 2, fill: ["#b8915f", "#d9c9a3", "#8f6b45"][x % 3] }, L.fixtures);
    el("rect", { x: x + 2, y: 922, width: 24, height: 24, rx: 2, fill: ["#d9c9a3", "#a57c50", "#c9b48a"][x % 3] }, L.fixtures);
  }
  floorText(190, 878, "KHO KHÔ", L.floor, { class: "k-zone k-dim" });

  // Dish area
  steel(1320, 300, 100, 50, L.fixtures, { filter: "url(#kDrop)" });
  for (const [x, y] of [[1340, 316], [1356, 330], [1380, 318], [1400, 334]]) el("circle", { cx: x, cy: y, r: 9, fill: "#e3e1da", stroke: "#a8a59c" }, L.fixtures);
  steel(1430, 290, 120, 110, L.fixtures, { filter: "url(#kDrop)" });
  el("rect", { x: 1442, y: 302, width: 96, height: 62, rx: 4, fill: "#283136", stroke: "#5f6c72" }, L.fixtures);
  const dishLed = el("circle", { cx: 1530, cy: 384, r: 4, class: "k-led" }, L.fixtures);
  el("text", { x: 1490, y: 338, class: "k-panel-title", "text-anchor": "middle" }, L.fixtures, "MÁY RỬA");
  steel(1490, 450, 66, 240, L.fixtures, { filter: "url(#kDrop)" });
  for (const y of [462, 538, 614]) el("rect", { x: 1498, y, width: 50, height: 66, rx: 6, fill: "#5d7f93", stroke: "#7f98a5" }, L.fixtures);
  floorText(1380, 520, "RỬA BÁT", L.floor, { class: "k-zone" });
  steel(1300, 610, 120, 70, L.fixtures, { filter: "url(#kDrop)" });
  for (let x = 1310; x < 1412; x += 14) el("line", { x1: x, y1: 618, x2: x, y2: 672, stroke: "#8b959b", "stroke-width": 2 }, L.fixtures);

  // Hood, plenum, fans, duct and roof exhaust
  el("rect", { x: 335, y: 42, width: 920, height: 172, rx: 6, class: "k-hood" }, L.hood);
  el("rect", { x: 335, y: 42, width: 920, height: 38, rx: 4, fill: "#556066", stroke: "#7f8b91" }, L.hood);
  for (let x = 345; x < 1250; x += 16) el("line", { x1: x, y1: 46, x2: x, y2: 76, stroke: "#6b777d", "stroke-width": 1.5 }, L.hood);
  floorText(1238, 206, "CHỤP HÚT", L.hood, { class: "k-tag", "text-anchor": "end" });
  const ROTORS = [470, 700, 930, 1160];
  for (const x of ROTORS) {
    const fan = el("g", { transform: `translate(${x} 61)` }, L.motion);
    el("circle", { r: 18, fill: "#1f2629", stroke: "#a5b1b7", "stroke-width": 2 }, fan);
    const rotor = el("g", { class: "k-rotor k-hood-rotor" }, fan);
    for (let i = 0; i < 4; i += 1) el("path", { d: "M0 0 C6 -4 12 -2 14 3 C8 4 4 3 0 0Z", fill: "#c7d1d6", transform: `rotate(${i * 90})` }, rotor);
    el("circle", { r: 3, fill: "#dfe6ea" }, fan);
  }
  el("path", { d: "M1255 61 H1470 V90", class: "k-duct" }, L.hood);
  el("path", { d: "M1255 61 H1470 V90", class: "k-duct-flow" }, L.motion);
  const roofFan = el("g", { transform: "translate(1510 96)" }, L.motion);
  el("circle", { r: 42, fill: "#22292c", stroke: "#8a969c", "stroke-width": 3 }, roofFan);
  const roofRotor = el("g", { class: "k-rotor k-roof-rotor" }, roofFan);
  for (let i = 0; i < 6; i += 1) el("path", { d: "M0 0 C10 -6 26 -4 32 6 C18 8 8 6 0 0Z", fill: "#b9c4c9", transform: `rotate(${i * 60})` }, roofRotor);
  el("circle", { r: 6, fill: "#e6ecef" }, roofFan);
  floorText(1510, 158, "QUẠT HÚT MÁI", L.hood, { class: "k-tag" });

  // E-nose: the 16 MOX sensors share one board and one sampling chamber, mounted in the hood
  // plenum mid-line. Every gas appliance and the gas line sit under this hood, so their plume
  // (and a leak) is drawn past the board. Plan scale: 1 unit = 1 cm.
  const ENOSE = { x: 815, y: 61 };
  const enoseLink = el("g", { class: "k-enose-link" }, L.alerts);
  el("line", { x1: ENOSE.x, y1: ENOSE.y, x2: LEAK.x, y2: LEAK.y, class: "k-enose-ray" }, enoseLink);
  const leakDistance = Math.hypot(ENOSE.x - LEAK.x, ENOSE.y - LEAK.y) / 100;
  el("text", { x: (ENOSE.x + LEAK.x) / 2, y: (ENOSE.y + LEAK.y) / 2 + 34, class: "k-enose-dist", "text-anchor": "middle" },
    enoseLink, `${leakDistance.toFixed(1)} m`);
  const enose = el("g", { class: "k-enose", transform: `translate(${ENOSE.x} ${ENOSE.y})` }, L.motion);
  el("circle", { r: 30, class: "k-enose-wave" }, enose);
  el("rect", { x: -34, y: -15, width: 68, height: 30, rx: 6, class: "k-enose-box" }, enose);
  for (let row = 0; row < 4; row += 1) {
    for (let col = 0; col < 4; col += 1) {
      el("circle", { cx: -21 + col * 9, cy: -7.5 + row * 5, r: 2, class: "k-enose-can" }, enose);
    }
  }
  el("circle", { cx: 24, cy: 0, r: 4.5, class: "k-enose-led" }, enose);
  el("text", { x: ENOSE.x, y: 35, class: "k-enose-text", "text-anchor": "middle" }, L.motion, "E-NOSE · 16 CẢM BIẾN");

  // Hood fire-suppression nozzles above each appliance
  const NOZZLES = [392, 485, 625, 771, 941, 1071, 1196];
  for (const x of NOZZLES) {
    el("circle", { cx: x, cy: 104, r: 5, class: "k-nozzle" }, L.motion);
  }

  // Airflow streamlines: visible when the roof exhaust runs, strong during an emergency purge.
  const STREAMS = [
    "M150 620 C 220 420, 360 320, 470 200",
    "M200 840 C 300 640, 480 420, 600 205",
    "M560 690 C 600 520, 680 360, 700 205",
    "M820 930 C 830 700, 900 420, 930 205",
    "M1040 690 C 1060 520, 1120 360, 1160 205",
    "M1300 640 C 1360 460, 1430 300, 1490 150",
    "M1480 880 C 1520 620, 1530 380, 1515 150",
    "M380 300 C 420 250, 450 230, 470 200",
    "M1260 300 C 1300 220, 1400 170, 1470 120",
  ];
  for (const [index, d] of STREAMS.entries()) {
    el("path", { d, class: "k-stream", "marker-end": "url(#kArrow)", style: `animation-delay:${-index * 0.17}s` }, L.airflow);
  }
  el("path", { d: "M1510 140 L1510 40", class: "k-stream out", "marker-end": "url(#kArrow)" }, L.airflow);
  floorText(1510, 32, "THẢI RA MÁI", L.airflow, { class: "k-tag k-stream-label" });

  // Wet-chemical suppression: spray cones under every nozzle and foam on the hot line.
  for (const x of NOZZLES) {
    const cone = el("g", { class: "k-spray-cone" }, L.spray);
    el("path", { d: `M${x} 106 L${x - 42} 214 Q${x} 230 ${x + 42} 214 Z`, fill: "url(#kSpray)" }, cone);
    for (const dx of [-26, -9, 9, 26]) {
      el("line", { x1: x, y1: 108, x2: x + dx, y2: 212, class: "k-spray-jet" }, cone);
    }
  }
  const foam = el("g", { class: "k-foam" }, L.spray);
  for (let x = 360; x <= 1240; x += 34) {
    for (const y of [118, 160, 196]) {
      el("circle", { cx: x + rand(-10, 10), cy: y + rand(-12, 12), r: rand(16, 26), fill: "url(#kFoam)" }, foam);
    }
  }
  foam.style.opacity = "0";

  // Wall sirens ("đèn còi"): a red dome on a base, two light beams that rotate and sound waves
  // while the siren sounds; a slash while an operator has silenced it.
  const beacons = [[48, 330], [1552, 250], [700, 952], [1552, 760], [48, 820]].map(([x, y]) => {
    const group = el("g", { class: "k-beacon", transform: `translate(${x} ${y})` }, L.alerts);
    const beams = el("g", { class: "k-beacon-beams" }, group);
    for (const angle of [0, 180]) {
      el("path", { d: "M0 0 L150 -44 A156 156 0 0 1 150 44 Z", transform: `rotate(${angle})`, class: "k-beacon-beam" }, beams);
    }
    el("circle", { r: 46, class: "k-beacon-wave" }, group);
    for (const side of [1, -1]) {
      for (const r of [32, 44]) {
        const dx = side * r * 0.7;
        el("path", { d: `M${dx} ${-r * 0.7} A${r} ${r} 0 0 ${side > 0 ? 1 : 0} ${dx} ${r * 0.7}`, class: "k-beacon-sound" }, group);
      }
    }
    el("rect", { x: -20, y: -20, width: 40, height: 40, rx: 9, class: "k-beacon-base" }, group);
    el("circle", { r: 14, class: "k-beacon-core" }, group);
    el("circle", { cx: -5, cy: -5, r: 4, class: "k-beacon-glint" }, group);
    el("line", { x1: -22, y1: 22, x2: 22, y2: -22, class: "k-beacon-mute" }, group);
    return group;
  });

  // Gas haze over the hot line
  const haze = el("ellipse", { cx: 800, cy: 190, rx: 720, ry: 300, fill: "url(#kHazeNormal)", class: "k-haze", opacity: 0 }, L.haze);
  const leakHaze = el("ellipse", { cx: 640, cy: 150, rx: 340, ry: 170, fill: "url(#kHazeDanger)", class: "k-haze", opacity: 0 }, L.haze);

  // ------------------------------------------------------------------ dynamic world state
  const world = {
    level: "waiting",
    co: 0,
    fan: false,
    exhaust: false,
    valveClosed: false,
    siren: false,
    muted: false,
    suppression: false,
    purge: false,
    leak: null, // "leaking" | "isolated" | "clearing" | "clean" | null
    evacuating: false,
    allClearAt: 0,
    plates: 3,
    reservations: new Map(),
    labels: true,
  };

  const platesGroup = el("g", {}, L.plates);
  let renderedPlates = -1;
  function renderPlates() {
    if (renderedPlates === world.plates) return;
    renderedPlates = world.plates;
    platesGroup.replaceChildren();
    const foods = ["#e76f51", "#f4a261", "#90be6d", "#e9c46a", "#c8553d", "#7fb069", "#d4a373", "#ef476f"];
    for (let i = 0; i < world.plates; i += 1) {
      const x = PLATE_SLOTS[i];
      el("circle", { cx: x, cy: 336, r: 15, fill: "#f6f5f0", stroke: "#c3c6c3", "stroke-width": 1.5 }, platesGroup);
      el("circle", { cx: x, cy: 336, r: 8, fill: foods[i % foods.length] }, platesGroup);
      el("circle", { cx: x + 3, cy: 333, r: 3, fill: "#5f8f3a" }, platesGroup);
    }
  }

  // ------------------------------------------------------------------ routing
  function nearestNode(x, y) {
    let best = null;
    let bestDistance = Infinity;
    for (const [id, [nx, ny]] of Object.entries(NODES)) {
      const distance = (nx - x) ** 2 + (ny - y) ** 2;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = id;
      }
    }
    return best;
  }

  function shortestPath(start, goal) {
    const dist = { [start]: 0 };
    const prev = {};
    const open = new Set([start]);
    while (open.size) {
      let current = null;
      for (const id of open) if (current === null || dist[id] < dist[current]) current = id;
      open.delete(current);
      if (current === goal) break;
      for (const [next, cost] of GRAPH[current] || []) {
        const candidate = dist[current] + cost;
        if (candidate < (dist[next] ?? Infinity)) {
          dist[next] = candidate;
          prev[next] = current;
          open.add(next);
        }
      }
    }
    const path = [goal];
    while (path[0] !== start && prev[path[0]]) path.unshift(prev[path[0]]);
    return path;
  }

  function route(fromX, fromY, to) {
    const ids = shortestPath(nearestNode(fromX, fromY), nearestNode(to.x, to.y));
    const points = ids.map((id) => ({ x: NODES[id][0], y: NODES[id][1] }));
    // Skip the first node when we are already heading past it.
    if (points.length > 1) {
      const [a, b] = points;
      if (Math.hypot(fromX - b.x, fromY - b.y) < Math.hypot(a.x - b.x, a.y - b.y) && Math.hypot(fromX - a.x, fromY - a.y) < 60) {
        points.shift();
      }
    }
    points.push({ x: to.x, y: to.y });
    return points;
  }

  // ------------------------------------------------------------------ people
  function standPoint(stationId, visit = false) {
    const station = STATIONS[stationId];
    if (!visit) return { x: station.x, y: station.y, a: station.a };
    // Stand one step behind the person working there.
    const back = (station.a + 180) * DEG;
    return { x: station.x + Math.cos(back) * 38 + rand(-8, 8), y: station.y + Math.sin(back) * 38, a: station.a };
  }

  class Person {
    constructor(def, index) {
      Object.assign(this, def);
      this.index = index;
      const start = STATIONS[def.start];
      this.x = start.x;
      this.y = start.y;
      this.angle = start.a * DEG;
      this.targetAngle = this.angle;
      this.path = [];
      this.mode = "idle";
      this.timer = rand(0.2, 1.5);
      this.act = "idle";
      this.activity = "Chuẩn bị ca";
      this.taskIndex = Math.floor(Math.random() * def.tasks.length);
      this.queue = [];
      this.walkPhase = Math.random() * 6;
      this.t = Math.random() * 10;
      this.carry = null;
      this.hidden = false;
      this.speed = rand(78, 92);
      this.reserved = null;
      this.side = 0;
      this.progressAt = 0;
      this.build();
    }

    build() {
      this.root = el("g", { class: "k-person" }, L.crew);
      this.body = el("g", {}, this.root);
      el("ellipse", { cx: 4, cy: 5, rx: 22, ry: 20, class: "k-shadow" }, this.body);
      this.feet = [-1, 1].map((side) => el("ellipse", { cx: 3, cy: side * 7, rx: 7, ry: 4.5, fill: "#151718" }, this.body));
      const handColor = this.gloves || this.skin;
      this.arms = [-1, 1].map((side) => {
        const arm = el("line", { x1: 0, y1: side * 13, x2: 6, y2: side * 15, stroke: this.jacket, "stroke-width": 8.5, "stroke-linecap": "round", class: "k-arm" }, this.body);
        const hand = el("circle", { cx: 6, cy: side * 15, r: 4.4, fill: handColor, stroke: "rgba(0,0,0,.25)", "stroke-width": 1 }, this.body);
        return { arm, hand, side };
      });
      this.carryGroup = el("g", { transform: "translate(25 0)" }, this.body);
      el("ellipse", { rx: 11.5, ry: 18.5, fill: this.jacket, stroke: "rgba(0,0,0,.35)", "stroke-width": 1.2 }, this.body);
      if (this.apron) el("ellipse", { cx: 6.5, rx: 5, ry: 13.5, fill: this.apron }, this.body);
      el("circle", { cx: 1, r: 8.6, fill: this.skin }, this.body);
      this.drawHat();
      this.labelGroup = el("g", { class: "k-label" }, L.labels);
      this.labelBox = el("rect", { x: 0, y: -13, height: 22, rx: 11 }, this.labelGroup);
      this.labelDot = el("circle", { cx: 11, cy: -2, r: 4, fill: this.apron || this.jacket, stroke: "#fff", "stroke-width": 1 }, this.labelGroup);
      this.labelText = el("text", { x: 20, y: 3 }, this.labelGroup);
      this.labelName = el("tspan", { class: "k-label-name" }, this.labelText, this.name);
      this.labelAct = el("tspan", { class: "k-label-act" }, this.labelText, "");
      this.lastLabel = "";
    }

    drawHat() {
      const b = this.body;
      if (this.hat === "toque") {
        el("circle", { cx: 1, r: 11.5, fill: "#fbfbf8", stroke: "#cfd2cf", "stroke-width": 1.2 }, b);
        el("circle", { cx: 1, r: 6, fill: "none", stroke: "#d9dcd8", "stroke-width": 1.4 }, b);
        for (let i = 0; i < 8; i += 1) {
          const a = i * 45 * DEG;
          el("line", { x1: 1 + Math.cos(a) * 6, y1: Math.sin(a) * 6, x2: 1 + Math.cos(a) * 11, y2: Math.sin(a) * 11, stroke: "#dfe2de", "stroke-width": 1.2 }, b);
        }
      } else if (this.hat === "cap") {
        el("ellipse", { cx: 9, rx: 7, ry: 8, fill: this.hatColor, opacity: ".9" }, b);
        el("circle", { cx: 0, r: 9.4, fill: this.hatColor, stroke: "rgba(0,0,0,.3)" }, b);
        el("circle", { cx: 0, r: 2, fill: "rgba(255,255,255,.35)" }, b);
      } else if (this.hat === "net") {
        el("circle", { cx: 0, r: 9.6, fill: this.hatColor, stroke: "rgba(0,0,0,.25)" }, b);
        el("path", { d: "M-7 -5 Q0 -9 7 -5 M-8 1 Q0 -3 8 1 M-7 6 Q0 2 7 6", stroke: "rgba(255,255,255,.35)", fill: "none", "stroke-width": 1.2 }, b);
      } else {
        el("circle", { cx: -1, r: 9.2, fill: this.hatColor }, b);
        el("circle", { cx: -9, r: 4.5, fill: this.hatColor, stroke: "rgba(255,255,255,.12)" }, b);
      }
    }

    setCarry(kind) {
      if (this.carry === kind) return;
      this.carry = kind;
      const g = this.carryGroup;
      g.replaceChildren();
      if (kind === "crate") {
        el("rect", { x: -9, y: -14, width: 20, height: 28, rx: 3, fill: "#5c8a4f", stroke: "#3d5c34" }, g);
        for (const [x, y, c] of [[-3, -7, "#e76f51"], [4, 0, "#f4d35e"], [-2, 7, "#9bc53d"]]) el("circle", { cx: x, cy: y, r: 3.4, fill: c }, g);
      } else if (kind === "tray" || kind === "dirty") {
        el("circle", { r: 15, fill: "#2b2f31", stroke: "#666d70", "stroke-width": 1.5 }, g);
        for (const [x, y] of [[-5, -6], [5, -4], [0, 7]]) {
          el("circle", { cx: x, cy: y, r: 5.5, fill: kind === "tray" ? "#f6f5f0" : "#c9c6bc" }, g);
          if (kind === "tray") el("circle", { cx: x, cy: y, r: 2.6, fill: "#e76f51" }, g);
        }
      } else if (kind === "bowl") {
        el("circle", { r: 10, fill: "#f6f5f0", stroke: "#c3c6c3" }, g);
        el("circle", { r: 5.5, fill: "#90be6d" }, g);
      }
    }

    release() {
      if (this.reserved) world.reservations.delete(this.reserved);
      this.reserved = null;
    }

    walkTo(point, activity, speed = null) {
      this.path = route(this.x, this.y, point);
      this.goal = point;
      this.mode = "walk";
      this.activity = activity;
      this.speedNow = speed || this.speed;
      this.hidden = false;
    }

    chooseStation(task) {
      const options = [...(task.at || task.visit)].sort(() => Math.random() - 0.5);
      for (const id of options) {
        const key = task.visit ? `visit:${id}` : id;
        if (!world.reservations.has(key) || world.reservations.get(key) === this.id) return { id, key };
      }
      return null;
    }

    nextTask() {
      this.release();
      const fromQueue = this.queue.length > 0;
      const task = fromQueue ? this.queue.shift() : this.tasks[this.taskIndex++ % this.tasks.length];
      const choice = this.chooseStation(task);
      if (!choice) {
        this.mode = "idle";
        this.act = "idle";
        this.timer = rand(1.5, 3);
        this.activity = "Chờ vị trí trống";
        if (fromQueue) this.queue.unshift(task);
        else this.taskIndex -= 1;
        return;
      }
      world.reservations.set(choice.key, this.id);
      this.reserved = choice.key;
      this.task = task;
      this.stationId = choice.id;
      this.setCarry(task.carry || null);
      this.walkTo(standPoint(choice.id, Boolean(task.visit)), `→ ${task.label}`);
    }

    arrive() {
      if (this.evacuating) {
        this.mode = "muster";
        this.act = "idle";
        this.targetAngle = Math.PI;
        this.activity = "Đã tới điểm tập trung";
        return;
      }
      const task = this.task;
      this.targetAngle = this.goal.a * DEG;
      this.mode = "work";
      this.act = task.act;
      this.timer = rand(...task.dur);
      this.activity = task.label;
      this.waited = 0;
      if (task.act !== "drop" && task.act !== "away") this.setCarry(null);
      if (task.gas && world.valveClosed) {
        this.act = "wait";
        this.activity = "Van gas khóa · chờ";
      }
      if (task.act === "away") this.hidden = true;
    }

    complete() {
      const task = this.task;
      if (task?.needsPlates && world.plates === 0 && this.waited < 10) {
        this.waited += 1;
        this.timer = 1;
        this.act = "idle";
        this.activity = "Chờ món ra pass";
        return;
      }
      if (task?.plate && this.act !== "wait") world.plates = Math.min(PLATE_SLOTS.length, world.plates + 1);
      if (task?.needsPlates) {
        world.plates = Math.max(0, world.plates - Math.min(3, world.plates));
      }
      if (task?.act === "drop" || task?.act === "away") this.setCarry(null);
      if (task?.act === "pickup") this.setCarry("tray");
      if (task?.act === "fetch") this.setCarry("crate");
      this.hidden = false;
      this.nextTask();
    }

    evacuate(slot) {
      this.release();
      this.evacuating = true;
      this.queue = [];
      if (this.mode === "work" && this.act === "away") {
        // Already in the dining room on the safe side of the building.
        this.mode = "safe";
        this.activity = "An toàn · phòng ăn";
        return;
      }
      this.setCarry(null);
      this.act = "walk";
      this.walkTo(slot, "Sơ tán qua lối thoát hiểm", rand(135, 155));
    }

    resume() {
      this.evacuating = false;
      this.hidden = false;
      this.mode = "idle";
      this.act = "idle";
      this.timer = rand(0.3, 2.2);
      this.activity = "Quay lại vị trí";
    }

    update(dt) {
      this.t += dt;
      if (this.mode === "walk") {
        let remaining = this.speedNow * dt;
        while (remaining > 0 && this.path.length) {
          const target = this.path[0];
          const dx = target.x - this.x;
          const dy = target.y - this.y;
          const distance = Math.hypot(dx, dy);
          if (distance < 0.5) {
            this.path.shift();
            continue;
          }
          this.targetAngle = Math.atan2(dy, dx);
          const step = Math.min(distance, remaining);
          this.x += (dx / distance) * step;
          this.y += (dy / distance) * step;
          remaining -= step;
          if (step === distance) this.path.shift();
        }
        this.walkPhase += dt * this.speedNow / 7.5;
        if (!this.path.length) this.arrive();
      } else if (this.mode === "work" || this.mode === "idle") {
        this.timer -= dt;
        if (this.mode === "work" && this.task?.gas) {
          if (world.valveClosed && this.act !== "wait") {
            this.act = "wait";
            this.activity = "Van gas khóa · chờ";
          } else if (!world.valveClosed && this.act === "wait") {
            this.act = this.task.act;
            this.activity = this.task.label;
          }
        }
        if (this.timer <= 0) {
          if (this.mode === "idle") this.nextTask();
          else this.complete();
        }
      }
      // Turn smoothly toward the heading.
      let delta = this.targetAngle - this.angle;
      delta = Math.atan2(Math.sin(delta), Math.cos(delta));
      this.angle += clamp(delta, -9 * dt, 9 * dt);
    }

    pose() {
      const t = this.t;
      const s = Math.sin;
      if (this.mode === "walk") {
        const swing = s(this.walkPhase) * 8;
        if (this.carry) return [[19, -10], [19, 10]];
        return [[3 - swing, -16], [3 + swing, 16]];
      }
      switch (this.act) {
        case "wok": return [[21, -11], [15 + 8 * Math.max(0, s(t * 7)), 9 + 3 * Math.cos(t * 7)]];
        case "stir": return [[13, -11], [18 + 4 * s(t * 4), 6 + 5 * Math.cos(t * 4)]];
        case "chop": return [[19, -8], [17 + 3.5 * Math.abs(s(t * 13)), 8]];
        case "flip": return [[13, -13], [20, 4 + 9 * s(t * 3)]];
        case "fry": { const lift = 9 * Math.max(0, s(t * 1.6)); return [[16 + lift, -9], [16 + lift, 9]]; }
        case "plate": return [[18 + 2 * s(t * 3), -7 + 2 * Math.cos(t * 3)], [18 + 2 * Math.cos(t * 2), 7 + 3 * s(t * 2)]];
        case "wash": return [[18 + 4 * s(t * 9), -8], [18 + 4 * s(t * 9 + Math.PI), 8]];
        case "taste": return [[2, -16], [10 + 3 * s(t * 2), 3]];
        case "inspect": return [[-7, -9], [-7, 9]];
        case "oven": return [[6, -15], [21, 6]];
        case "fetch": case "sort": case "drop": case "pickup": return [[20 + 2 * s(t * 2.4), -9], [20 + 2 * Math.cos(t * 2.4), 9]];
        default: { const b = s(t * 1.7) * 0.8; return [[3 + b, -16], [3 - b, 16]]; }
      }
    }

    render() {
      const deg = this.angle / DEG;
      const breathe = this.mode === "walk" ? 1 : 1 + Math.sin(this.t * 1.7) * 0.012;
      // Side-step is visual only: the logical position stays on the aisle, so nobody can be
      // pushed backwards or pinned between two other people.
      const px = this.x - Math.sin(this.angle) * this.side;
      const py = this.y + Math.cos(this.angle) * this.side;
      this.body.setAttribute("transform", `translate(${px.toFixed(1)} ${py.toFixed(1)}) rotate(${deg.toFixed(1)}) scale(${breathe.toFixed(3)})`);
      const hands = this.pose();
      this.arms.forEach(({ arm, hand }, i) => {
        const [hx, hy] = hands[i];
        arm.setAttribute("x2", hx.toFixed(1));
        arm.setAttribute("y2", hy.toFixed(1));
        hand.setAttribute("cx", hx.toFixed(1));
        hand.setAttribute("cy", hy.toFixed(1));
      });
      const step = this.mode === "walk" ? Math.sin(this.walkPhase) * 8 : 2;
      this.feet[0].setAttribute("cx", (3 + step).toFixed(1));
      this.feet[1].setAttribute("cx", (3 - step).toFixed(1));
      const hidden = this.hidden || this.mode === "safe";
      this.root.style.opacity = hidden ? "0" : "1";
      // At the muster point the roster already says who is safe; labels would only pile up.
      const showLabel = world.labels && !hidden && this.mode !== "muster";
      this.labelGroup.style.display = showLabel ? "" : "none";
      if (showLabel) {
        const label = ` · ${this.activity}`;
        if (label !== this.lastLabel) {
          this.lastLabel = label;
          this.labelAct.textContent = label;
          this.labelBox.setAttribute("width", Math.ceil(this.labelText.getComputedTextLength() + 30));
        }
        // On the hot line the hood sits above people, so label below them.
        const labelY = py < 300 ? py + 40 : py - 34;
        this.labelGroup.setAttribute("transform", `translate(${(px - 26).toFixed(1)} ${labelY.toFixed(1)})`);
        this.labelGroup.classList.toggle("evac", Boolean(this.evacuating));
      }
    }
  }

  const people = CREW.map((def, index) => new Person(def, index));

  // ------------------------------------------------------------------ particles
  const particles = [];
  const particleLayer = L.particles;
  function spawnParticle(x, y, kind = "steam") {
    if (REDUCED || particles.length > 60) return;
    const node = el("circle", { class: `k-particle k-${kind}`, r: 5 }, particleLayer);
    // Leaking gas spreads down into the kitchen; running fans then pull it back up.
    const gas = kind === "gas";
    particles.push({
      node, x: x + rand(-10, 10), y: y + rand(-8, 8),
      vx: gas ? rand(-40, 40) : rand(-6, 6), vy: gas ? rand(20, 45) : rand(-18, -10),
      life: 0, max: gas ? rand(3, 4.5) : world.fan ? rand(1.2, 1.8) : rand(3.5, 5.5), r: rand(4, 7),
    });
  }
  function spawnMist() {
    if (REDUCED || particles.length > 70) return;
    for (const x of NOZZLES) {
      if (Math.random() < 0.45) continue;
      const node = el("circle", { class: "k-particle k-mist", r: 4 }, particleLayer);
      const a = rand(30, 150) * DEG;
      particles.push({ node, x, y: 106, vx: Math.cos(a) * rand(30, 70), vy: Math.sin(a) * rand(40, 90), life: 0, max: rand(1.4, 2.2), r: rand(3, 5), mist: true });
    }
  }

  function updateParticles(dt) {
    for (let i = particles.length - 1; i >= 0; i -= 1) {
      const p = particles[i];
      p.life += dt;
      if (p.mist) {
        p.vx *= 0.97;
        p.vy *= 0.97;
      } else if (world.fan) {
        const nearest = ROTORS.reduce((best, x) => (Math.abs(x - p.x) < Math.abs(best - p.x) ? x : best), ROTORS[0]);
        p.vx += (nearest - p.x) * dt * 1.8;
        p.vy += -140 * dt;
      } else {
        p.vx += rand(-14, 14) * dt;
        p.vy *= 0.995;
      }
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      p.r += dt * (p.mist ? 16 : world.fan ? 3 : 7);
      const fade = 1 - p.life / p.max;
      if (fade <= 0 || (p.y < 50 && !p.mist)) {
        p.node.remove();
        particles.splice(i, 1);
        continue;
      }
      p.node.setAttribute("cx", p.x.toFixed(1));
      p.node.setAttribute("cy", p.y.toFixed(1));
      p.node.setAttribute("r", p.r.toFixed(1));
      p.node.style.opacity = (Math.min(1, p.life * 3) * fade * 0.55).toFixed(2);
    }
  }

  // ------------------------------------------------------------------ scene logic
  const BURNER_POS = {
    wok1: [485, 146], wok2: [555, 146], wok3: [625, 146], r0: [705, 126], r2: [837, 126], r3: [705, 174],
    r5: [837, 174], griddle: [941, 146], fryer: [1071, 136], stock: [1196, 148],
  };
  let spawnClock = 0;

  function stationWorker(stationId) {
    return people.find((p) => p.mode === "work" && p.stationId === stationId && !p.task?.visit && p.act !== "wait");
  }

  function updateBurners(dt) {
    const open = !world.valveClosed && !world.suppression;
    const lit = new Map();
    for (const [id, station] of Object.entries(STATIONS)) {
      if (!station.burners) continue;
      const worker = open ? stationWorker(id) : null;
      for (const burnerId of station.burners) lit.set(burnerId, worker ? (worker.act === "wok" ? "high" : "on") : null);
      if (station.wok) {
        const toss = worker && worker.act === "wok" ? Math.max(0, Math.sin(worker.t * 7)) : 0;
        WOKS[station.wok].setAttribute("transform", `translate(${STATIONS[id].x} ${146 + toss * 7}) rotate(${toss * 4})`);
        if (worker && toss > 0.85 && Math.random() < 0.3) spawnParticle(station.x, 140, "smoke");
      }
    }
    // Simmering burners stay on low while gas is available.
    if (open) {
      lit.set("r1", "low");
      if (!lit.get("stock")) lit.set("stock", "low");
    }
    for (const [id, burnerState] of Object.entries(BURNERS)) {
      const state = lit.get(id) || null;
      if (burnerState.lit !== state) {
        burnerState.lit = state;
        burnerState.group.classList.toggle("lit", Boolean(state));
        burnerState.group.classList.toggle("high", state === "high");
        burnerState.group.classList.toggle("low", state === "low");
      }
    }
    fryBubbles.classList.toggle("on", Boolean(lit.get("fryer")));
    const frying = stationWorker("fryer");
    const lift = frying && frying.act === "fry" ? 10 * Math.max(0, Math.sin(frying.t * 1.6)) : 0;
    baskets.setAttribute("transform", `translate(0 ${(-lift).toFixed(1)})`);
    griddleFood.classList.toggle("on", Boolean(lit.get("griddle")));
    combiLed.classList.toggle("on", true);
    dishLed.classList.toggle("on", Boolean(stationWorker("dish")));

    spawnClock += dt;
    if (spawnClock > 0.18) {
      spawnClock = 0;
      for (const [id, state] of lit) {
        if (!state || !BURNER_POS[id]) continue;
        if (state === "low" && Math.random() > 0.25) continue;
        const [x, y] = BURNER_POS[id];
        spawnParticle(x, y - 6, id === "fryer" || id === "griddle" ? "smoke" : "steam");
      }
      if (stationWorker("dish")) spawnParticle(1490, 330, "steam");
      if (world.suppression) spawnMist();
      if (world.leak === "leaking") {
        spawnParticle(LEAK.x, LEAK.y + 8, "gas");
        spawnParticle(LEAK.x, LEAK.y + 8, "gas");
      }
    }
  }

  function updateDoors() {
    const nearExit = people.some((p) => !p.hidden && Math.hypot(p.x - 1560, p.y - 860) < 70);
    exitDoor.setAttribute("transform", nearExit || world.evacuating ? "rotate(-68 1560 822)" : "");
    const atCooler = people.some((p) => p.mode === "work" && p.stationId === "cooler");
    coolerDoor.setAttribute("transform", atCooler ? "rotate(70 250 202)" : "");
    const nearDining = people.some((p) => Math.abs(p.x - 820) < 50 && p.y > 900);
    diningDoors[0].setAttribute("transform", nearDining ? "rotate(60 762 960)" : "");
    diningDoors[1].setAttribute("transform", nearDining ? "rotate(-60 878 960)" : "");
  }

  function avoid(dt) {
    for (const a of people) {
      let target = 0;
      if (a.mode === "walk" && !a.hidden) {
        const hx = Math.cos(a.angle);
        const hy = Math.sin(a.angle);
        for (const b of people) {
          if (b === a || b.hidden || b.mode === "safe") continue;
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const ahead = dx * hx + dy * hy;
          const lateral = -dx * hy + dy * hx;
          if (ahead > -12 && ahead < 48 && Math.abs(lateral) < 30) {
            target = lateral > 0 ? -14 : 14;
            break;
          }
        }
      }
      a.side += (target - a.side) * Math.min(1, dt * 6);
    }
  }

  function updateEvacuation(now) {
    // Gas danger or a suppression discharge both clear the kitchen.
    const danger = world.level === "danger" || world.suppression;
    if (danger && !world.evacuating) {
      world.evacuating = true;
      people.forEach((person, index) => person.evacuate(MUSTER_SLOTS[index % MUSTER_SLOTS.length]));
    }
    if (world.evacuating) {
      if (danger) world.allClearAt = now + 6000;
      else if (now >= world.allClearAt) {
        world.evacuating = false;
        people.forEach((person) => person.resume());
      }
    }
  }

  let lastLevel = "waiting";
  function onLevelChange(level) {
    if (level === "warning" && lastLevel !== "warning" && lastLevel !== "danger") {
      const chef = people.find((p) => p.id === "minh");
      if (chef && !chef.evacuating) {
        chef.queue = [
          { at: ["valve"], act: "inspect", dur: [4, 6], label: "Kiểm tra van gas" },
          { at: ["hood"], act: "inspect", dur: [4, 6], label: "Kiểm tra chụp hút" },
        ];
        if (chef.mode === "work" || chef.mode === "idle") chef.timer = Math.min(chef.timer, 0.6);
      }
    }
    lastLevel = level;
  }

  function applyVisuals() {
    scene.classList.toggle("fan-on", world.fan);
    scene.classList.toggle("exhaust-on", world.exhaust);
    scene.classList.toggle("valve-closed", world.valveClosed);
    scene.classList.toggle("siren-on", world.siren);
    scene.classList.toggle("siren-muted", Boolean(world.muted) && !world.siren);
    scene.classList.toggle("suppression-on", world.suppression);
    scene.classList.toggle("purge", world.purge);
    scene.classList.toggle("leaking", world.leak === "leaking");
    scene.classList.toggle("leak-isolated", world.leak === "isolated");
    scene.classList.toggle("evacuating", world.evacuating);
    for (const level of ["normal", "warning", "danger", "waiting"]) scene.classList.toggle(level, world.level === level);
    const ventilation = (world.fan ? 0.75 : 1) * (world.exhaust ? 0.6 : 1);
    const base = clamp(world.co / 320, 0.03, 0.62) * ventilation;
    haze.setAttribute("fill", world.level === "danger" ? "url(#kHazeDanger)" : world.level === "warning" ? "url(#kHazeWarning)" : "url(#kHazeNormal)");
    haze.setAttribute("opacity", (world.level === "waiting" ? 0.02 : base * 0.55).toFixed(3));
    leakHaze.setAttribute("opacity", world.level === "danger" ? (0.28 * ventilation * (world.purge ? 0.25 : 1)).toFixed(3) : "0");
    if (world.purge) haze.setAttribute("opacity", (base * 0.12).toFixed(3));
    foam.style.opacity = (world.foam || 0).toFixed(2);
  }

  // ------------------------------------------------------------------ roster
  const roster = document.getElementById("crewRoster");
  const crewCount = document.getElementById("crewCount");
  const rosterItems = new Map();
  if (roster) {
    for (const person of people) {
      const item = document.createElement("div");
      item.className = "crew-chip";
      item.innerHTML = `<i style="--c:${person.apron || person.jacket}"></i><div><b>${person.name}</b><small>${person.role}</small></div><span></span>`;
      roster.appendChild(item);
      rosterItems.set(person.id, item);
    }
  }
  function renderRoster() {
    let working = 0;
    let moving = 0;
    let safe = 0;
    for (const person of people) {
      const item = rosterItems.get(person.id);
      const state = person.mode === "muster" || person.mode === "safe" ? "safe" : person.mode === "walk" ? "moving" : person.act === "wait" ? "blocked" : "working";
      if (state === "working") working += 1;
      if (state === "moving") moving += 1;
      if (state === "safe") safe += 1;
      if (item) {
        item.dataset.state = state;
        const text = person.activity.replace(/^→ /, "→ ");
        const span = item.querySelector("span");
        if (span.textContent !== text) span.textContent = text;
      }
    }
    if (crewCount) {
      crewCount.textContent = world.evacuating
        ? `${world.suppression ? "Phun dập lửa" : "Sơ tán"} · ${safe}/${people.length} đã an toàn`
        : `${people.length} nhân sự · ${working} đang thao tác · ${moving} di chuyển`;
      crewCount.classList.toggle("evac", world.evacuating);
    }
  }

  // ------------------------------------------------------------------ loop
  let last = performance.now();
  let rosterClock = 0;
  let renderClock = 0;
  const RENDER_INTERVAL = 1 / 30;  // SVG writes per second; the simulation still runs per frame
  const sceneErrors = [];
  function simulate(dt) {
    updateEvacuation(Date.now());
    for (const person of people) person.update(dt);
    avoid(dt);
    updateBurners(dt);
    updateParticles(dt);
    // Foam builds up while the suppression system discharges and slowly drains afterwards.
    world.foam = clamp((world.foam || 0) + (world.suppression ? dt * 0.35 : -dt * 0.04), 0, 1);
  }

  function frame(now) {
    // Always schedule the next frame first: one bad frame must never freeze the scene.
    requestAnimationFrame(frame);
    // Advance in real time with small sub-steps, so people keep their walking speed even
    // when the browser renders at a low frame rate.
    let elapsed = Math.min(1, Math.max(0, (now - last) / 1000));
    last = now;
    try {
      while (elapsed > 0) {
        const dt = Math.min(0.05, elapsed);
        simulate(dt);
        elapsed -= dt;
        rosterClock += dt;
        renderClock += dt;
      }
      // Visual updates are capped at ~30/s while the simulation keeps full time resolution:
      // this roughly halves the per-frame SVG-attribute writes without slowing anyone down.
      if (renderClock >= RENDER_INTERVAL) {
        renderClock = 0;
        updateDoors();
        renderPlates();
        foam.style.opacity = (world.foam || 0).toFixed(2);
        for (const person of people) person.render();
      }
      if (rosterClock > 0.4) {
        rosterClock = 0;
        renderRoster();
        applyVisuals();
      }
    } catch (error) {
      if (sceneErrors.length < 20) sceneErrors.push(String(error?.stack || error));
      console.error("Kitchen scene frame failed", error);
    }
  }
  requestAnimationFrame(frame);

  window.KitchenScene = {
    update(next) {
      if (next.level && next.level !== world.level) onLevelChange(next.level);
      Object.assign(world, next);
      applyVisuals();
    },
    setLabels(visible) {
      world.labels = visible;
    },
    errors() {
      return [...sceneErrors];
    },
    snapshot() {
      return people.map((p) => ({ id: p.id, name: p.name, mode: p.mode, activity: p.activity, x: Math.round(p.x), y: Math.round(p.y) }));
    },
  };
})();

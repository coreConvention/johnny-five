/**
 * 3D memory graph view.
 *
 * Loaded as an ES module and exposed to the page's classic script through
 * `window.MemoryGraph`. Renders every memory as a point placed by semantic
 * meaning, with one switchable mode of derived relationships drawn as edges.
 *
 * Two rules this file follows deliberately:
 *
 *  - Filtering never re-fetches a layout. Positions come from the server once
 *    per projection and are then fixed; a filter only changes what is visible.
 *    Re-projecting on every filter would make points jump and destroy the
 *    spatial familiarity that makes the view useful.
 *  - The render loop is stopped whenever the view is hidden. The dashboard is a
 *    long-lived localhost tab; an idle WebGL loop would spin a GPU all day for
 *    a canvas nobody is looking at.
 */

import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";

const API = "/api/v1";

// Per-mode copy shown under the controls. The user picks a mode by reading
// what it means, rather than having to know the data model first.
const EDGE_HELP = {
  semantic: "Lines join memories that say similar things — computed from meaning, so it catches relatives that share no words.",
  tags: "Lines join memories you labelled alike. Common tags are ignored; agreeing on a rare tag counts for much more.",
  project: "Colours by project, and links each memory to its closest relatives from that same project.",
  time: "Threads memories together in the order they were learned, linking those captured in the same burst of work.",
  none: "Positions only — nearness on screen still means similar meaning.",
};

const TYPE_COLORS = {
  lesson: 0x35b79a,
  project: 0x4a8fe0,
  feedback: 0xe0913b,
  reference: 0xa070d8,
  user: 0xe06a8c,
};
const TYPE_ORDER = ["lesson", "project", "feedback", "reference", "user"];

// Tier drives opacity: the corpus's "temperature" should read at a glance
// without another colour dimension competing with type.
const TIER_ALPHA = { hot: 1.0, warm: 0.62, cold: 0.4, archived: 0.18 };

let renderer, scene, camera, controls, raycaster;
let points, pointGeom, lines, lineGeom;
let container, tooltipEl, panelEl, legendEl, metaEl;
let running = false;
let frameHandle = null;

let data = { nodes: [], edges: [], meta: {} };
let visible = [];          // per-node boolean, recomputed by applyFilters()
let selected = -1;
let hovered = -1;

const opts = { projection: "tsne", edges: "semantic", threshold: 0.6, k: 8 };
const filters = { type: "", tier: "", project: "", query: "" };

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

/** Read a CSS custom property so the canvas tracks the page's light/dark mode. */
function cssVar(name, fallback) {
  const v = getComputedStyle(document.body).getPropertyValue(name).trim();
  return v || fallback;
}

function themeColors() {
  return {
    bg: new THREE.Color(cssVar("--bg", "#14171a")),
    ink: new THREE.Color(cssVar("--ink", "#e6e9ec")),
    // `--muted` is mid-tone in BOTH themes, which `--ink` is not: ink is
    // near-white on dark, so tinting weak edges toward it made the weakest
    // links the brightest thing on screen — exactly backwards.
    muted: new THREE.Color(cssVar("--muted", "#9aa4ae")),
    accent: new THREE.Color(cssVar("--accent", "#35b79a")),
  };
}

// ---------------------------------------------------------------------------
// Scene construction
// ---------------------------------------------------------------------------

const VERTEX_SHADER = `
  attribute float size;
  attribute float alpha;
  attribute vec3 color;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vColor = color;
    vAlpha = alpha;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    // Perspective-correct sizing: points shrink with distance like real geometry.
    gl_PointSize = size * (300.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
  }
`;

const FRAGMENT_SHADER = `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    // Square sprites read as noise at this density; mask to a soft disc.
    vec2 d = gl_PointCoord - vec2(0.5);
    float r = length(d);
    if (r > 0.5) discard;
    float edge = smoothstep(0.5, 0.35, r);
    if (vAlpha <= 0.001) discard;
    gl_FragColor = vec4(vColor, vAlpha * edge);
  }
`;

function buildScene() {
  const theme = themeColors();
  const rect = container.getBoundingClientRect();
  const width = Math.max(rect.width, 1);
  const height = Math.max(rect.height, 1);

  scene = new THREE.Scene();
  scene.background = theme.bg;
  // Fog gives depth cues that a flat point cloud otherwise lacks entirely.
  scene.fog = new THREE.Fog(theme.bg.getHex(), 260, 620);

  camera = new THREE.PerspectiveCamera(55, width / height, 0.5, 3000);
  camera.position.set(0, 0, 260);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(width, height);
  container.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.55;
  controls.minDistance = 20;
  controls.maxDistance = 900;

  raycaster = new THREE.Raycaster();
  // Points are infinitely thin to a ray; this is the pick radius in world units.
  raycaster.params.Points.threshold = 2.2;

  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("click", onClick);
  window.addEventListener("resize", onResize);
}

function disposeScene() {
  if (!renderer) return;
  window.removeEventListener("resize", onResize);
  renderer.domElement.removeEventListener("pointermove", onPointerMove);
  renderer.domElement.removeEventListener("click", onClick);
  clearGeometry();
  controls.dispose();
  renderer.dispose();
  if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
  renderer = scene = camera = controls = raycaster = null;
}

function clearGeometry() {
  if (points) { scene.remove(points); pointGeom.dispose(); points.material.dispose(); points = null; }
  if (lines) { scene.remove(lines); lineGeom.dispose(); lines.material.dispose(); lines = null; }
}

// ---------------------------------------------------------------------------
// Geometry from data
// ---------------------------------------------------------------------------

function buildPoints() {
  const n = data.nodes.length;
  const positions = new Float32Array(n * 3);
  const colors = new Float32Array(n * 3);
  const sizes = new Float32Array(n);
  const alphas = new Float32Array(n);

  const c = new THREE.Color();
  for (let i = 0; i < n; i++) {
    const node = data.nodes[i];
    positions[i * 3] = node.x;
    positions[i * 3 + 1] = node.y;
    positions[i * 3 + 2] = node.z;
    c.setHex(TYPE_COLORS[node.type] ?? 0x8899aa);
    colors[i * 3] = c.r; colors[i * 3 + 1] = c.g; colors[i * 3 + 2] = c.b;
    // Importance 0-10 -> a readable size band. sqrt keeps the biggest points
    // from swamping their neighbours the way a linear map does.
    sizes[i] = 1.6 + Math.sqrt(Math.max(node.importance, 0)) * 1.5;
    alphas[i] = TIER_ALPHA[node.tier] ?? 0.6;
  }

  pointGeom = new THREE.BufferGeometry();
  pointGeom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  pointGeom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  pointGeom.setAttribute("size", new THREE.BufferAttribute(sizes, 1));
  pointGeom.setAttribute("alpha", new THREE.BufferAttribute(alphas, 1));

  points = new THREE.Points(
    pointGeom,
    new THREE.ShaderMaterial({
      vertexShader: VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
      transparent: true,
      depthWrite: false,
    })
  );
  scene.add(points);
}

function buildLines() {
  if (lines) { scene.remove(lines); lineGeom.dispose(); lines.material.dispose(); lines = null; }
  const kept = data.edges.filter(([a, b]) => visible[a] && visible[b]);
  if (!kept.length) return;

  const positions = new Float32Array(kept.length * 6);
  const colors = new Float32Array(kept.length * 6);
  const alphas = new Float32Array(kept.length * 2);
  const theme = themeColors();
  const c = new THREE.Color();

  // Ink budget, measured in total line LENGTH rather than edge count.
  //
  // Count is a poor proxy: on this corpus shared-tag mode has 1.9x the edges of
  // semantic mode but 4.9x the total length, because tag partners sit anywhere
  // in the cloud while semantic partners are neighbours by construction. Every
  // pixel a line crosses adds another layer of alpha, so length is what
  // actually determines whether the view reads as structure or as fog.
  //
  // Damped by a square root: matching the budget exactly would leave the
  // sparse, short-edged modes almost invisible.
  let totalLength = 0;
  for (const [a, b] of kept) {
    const na = data.nodes[a], nb = data.nodes[b];
    totalLength += Math.hypot(na.x - nb.x, na.y - nb.y, na.z - nb.z);
  }
  // K=100 was tuned by eye against the two extremes on a real corpus: semantic
  // (many short edges) must show structure, shared-tag (fewer but far longer
  // edges) must not become fog.
  const base = Math.min(0.32, Math.max(0.03, 100 / Math.sqrt(Math.max(totalLength, 1))));

  for (let e = 0; e < kept.length; e++) {
    const [a, b, w] = kept[e];
    const na = data.nodes[a], nb = data.nodes[b];
    positions[e * 6] = na.x; positions[e * 6 + 1] = na.y; positions[e * 6 + 2] = na.z;
    positions[e * 6 + 3] = nb.x; positions[e * 6 + 4] = nb.y; positions[e * 6 + 5] = nb.z;
    const weight = Math.min(Math.max(w, 0), 1);
    // Strong relationships take the accent and more opacity; weak ones sink
    // toward the muted mid-tone and fade.
    c.copy(theme.muted).lerp(theme.accent, weight);
    const alpha = base * (0.3 + 0.7 * weight);
    for (const off of [0, 3]) {
      colors[e * 6 + off] = c.r; colors[e * 6 + off + 1] = c.g; colors[e * 6 + off + 2] = c.b;
    }
    alphas[e * 2] = alpha;
    alphas[e * 2 + 1] = alpha;
  }

  lineGeom = new THREE.BufferGeometry();
  lineGeom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  lineGeom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  lineGeom.setAttribute("alpha", new THREE.BufferAttribute(alphas, 1));
  lines = new THREE.LineSegments(
    lineGeom,
    // A ShaderMaterial rather than LineBasicMaterial: the latter has only one
    // global opacity, so link strength could not modulate transparency.
    new THREE.ShaderMaterial({
      vertexShader: `
        attribute float alpha;
        varying vec3 vColor;
        varying float vAlpha;
        void main() {
          vColor = color;
          vAlpha = alpha;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        varying vec3 vColor;
        varying float vAlpha;
        void main() { gl_FragColor = vec4(vColor, vAlpha); }
      `,
      vertexColors: true,
      transparent: true,
      depthWrite: false,
    })
  );
  scene.add(lines);
}

// ---------------------------------------------------------------------------
// Filtering — visibility only, never a re-layout
// ---------------------------------------------------------------------------

function applyFilters() {
  const q = filters.query.trim().toLowerCase();
  const alpha = pointGeom.getAttribute("alpha");
  const size = pointGeom.getAttribute("size");
  let shown = 0;

  for (let i = 0; i < data.nodes.length; i++) {
    const node = data.nodes[i];
    let ok = true;
    if (filters.type && node.type !== filters.type) ok = false;
    if (filters.tier && node.tier !== filters.tier) ok = false;
    if (filters.project && (node.project || "(none)") !== filters.project) ok = false;

    let hit = false;
    if (ok && q) {
      hit = (node.preview || "").toLowerCase().includes(q)
        || (node.tags || []).some(t => t.toLowerCase().includes(q))
        || node.id.toLowerCase().includes(q);
      if (!hit) ok = false;
    }

    visible[i] = ok;
    if (ok) shown++;

    const base = TIER_ALPHA[node.tier] ?? 0.6;
    alpha.array[i] = ok ? base : 0;
    const importanceSize = 1.6 + Math.sqrt(Math.max(node.importance, 0)) * 1.5;
    // A search hit or the selection is inflated so it can be found in a crowd.
    const emphasis = (i === selected) ? 2.6 : (q && hit ? 1.9 : 1.0);
    size.array[i] = importanceSize * emphasis;
  }

  alpha.needsUpdate = true;
  size.needsUpdate = true;
  buildLines();
  updateMeta(shown);
}

function updateMeta(shown) {
  if (!metaEl) return;
  const m = data.meta || {};
  const edgeCount = lineGeom ? lineGeom.getAttribute("position").count / 2 : 0;
  const bits = [
    `${shown.toLocaleString()} / ${(m.node_count || 0).toLocaleString()} memories`,
    `${edgeCount.toLocaleString()} links`,
    `${m.projection || ""}${m.cached ? " (cached)" : ""}`,
  ];
  if (m.truncated) bits.push(`⚠ showing strongest ${m.edge_count.toLocaleString()} of ${m.total_edges.toLocaleString()}`);
  metaEl.textContent = bits.filter(Boolean).join(" · ");
}

// ---------------------------------------------------------------------------
// Interaction
// ---------------------------------------------------------------------------

const pointer = new THREE.Vector2();
let pointerClient = { x: 0, y: 0 };

function pickIndex(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObject(points);
  // Hits come back nearest-first, but filtered-out points are still in the
  // buffer (alpha 0), so skip them rather than picking an invisible memory.
  for (const hit of hits) {
    if (visible[hit.index]) return hit.index;
  }
  return -1;
}

function onPointerMove(event) {
  pointerClient = { x: event.clientX, y: event.clientY };
  const idx = pickIndex(event);
  if (idx === hovered) { positionTooltip(); return; }
  hovered = idx;
  if (idx < 0) { tooltipEl.hidden = true; renderer.domElement.style.cursor = "grab"; return; }

  const node = data.nodes[idx];
  tooltipEl.replaceChildren();
  const head = document.createElement("div");
  head.className = "gt-head";
  head.textContent = `${node.type} · ${node.tier} · importance ${node.importance}`;
  const body = document.createElement("div");
  body.textContent = node.preview || "(no preview)";
  tooltipEl.append(head, body);
  tooltipEl.hidden = false;
  renderer.domElement.style.cursor = "pointer";
  positionTooltip();
}

function positionTooltip() {
  if (tooltipEl.hidden) return;
  const pad = 14;
  const rect = tooltipEl.getBoundingClientRect();
  let left = pointerClient.x + pad;
  let top = pointerClient.y + pad;
  if (left + rect.width > window.innerWidth - 8) left = pointerClient.x - rect.width - pad;
  if (top + rect.height > window.innerHeight - 8) top = pointerClient.y - rect.height - pad;
  tooltipEl.style.left = `${Math.max(8, left)}px`;
  tooltipEl.style.top = `${Math.max(8, top)}px`;
}

async function onClick(event) {
  const idx = pickIndex(event);
  if (idx < 0) return;
  selected = idx;
  applyFilters();
  await showDetail(data.nodes[idx].id);
}

async function showDetail(id) {
  panelEl.hidden = false;
  panelEl.replaceChildren(elem("div", "gp-loading", "Loading…"));
  try {
    const res = await fetch(`${API}/memories/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const m = await res.json();

    const close = elem("button", "gp-close", "×");
    close.title = "Close";
    close.addEventListener("click", () => { panelEl.hidden = true; selected = -1; applyFilters(); });

    const meta = elem("div", "gp-meta");
    meta.append(
      pill(m.type, TYPE_COLORS[m.type]),
      pill(m.tier),
      // Importance decays continuously, so the stored value is a long float.
      pill(`importance ${Number(m.importance).toFixed(2)}`),
      pill(`${m.access_count} retrievals`)
    );

    const tags = elem("div", "gp-tags");
    for (const t of (m.tags || [])) tags.append(elem("span", "tag", t));

    const content = elem("pre", "gp-content", m.content || "");

    const foot = elem("div", "gp-foot");
    foot.textContent = [
      m.project_dir || "no project scope",
      `created ${(m.created_at || "").slice(0, 10)}`,
      m.id,
    ].join(" · ");

    panelEl.replaceChildren(close, meta, tags, content, foot);
  } catch (e) {
    panelEl.replaceChildren(elem("div", "gp-loading", `Could not load memory: ${e.message}`));
  }
}

function elem(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

function pill(text, hex) {
  const el = elem("span", "pill", text);
  if (hex !== undefined) {
    el.style.borderColor = `#${hex.toString(16).padStart(6, "0")}`;
    el.style.color = `#${hex.toString(16).padStart(6, "0")}`;
  }
  return el;
}

/**
 * Frame the camera on the data rather than on a fixed distance.
 *
 * Uses a 95th-percentile radius, not the bounding sphere: a few far-flung
 * outliers would otherwise dictate the zoom and leave the bulk of the corpus a
 * small knot in the centre. Overshooting the outliers slightly is the right
 * trade — they stay reachable by zooming out.
 */
function fitCamera() {
  if (!data.nodes.length) return;
  let cx = 0, cy = 0, cz = 0;
  for (const n of data.nodes) { cx += n.x; cy += n.y; cz += n.z; }
  const count = data.nodes.length;
  cx /= count; cy /= count; cz /= count;

  const radii = data.nodes
    .map(n => Math.hypot(n.x - cx, n.y - cy, n.z - cz))
    .sort((a, b) => a - b);
  const radius = radii[Math.floor(radii.length * 0.95)] || radii[radii.length - 1] || 100;

  const fov = (camera.fov * Math.PI) / 180;
  // Widen the fit when the viewport is letterboxed, or the sides get clipped.
  const effective = camera.aspect < 1 ? fov * camera.aspect : fov;
  const dist = (radius / Math.sin(Math.max(effective, 0.2) / 2)) * 1.12;

  controls.target.set(cx, cy, cz);
  camera.position.set(cx, cy, cz + dist);
  camera.near = Math.max(0.5, dist / 800);
  camera.far = dist * 8;
  camera.updateProjectionMatrix();
  controls.update();

  scene.fog.near = dist * 0.6;
  scene.fog.far = dist * 2.6;
}

/**
 * Size the canvas to whatever vertical space is actually left.
 *
 * A CSS `calc(100vh - <constant>)` cannot work here: the control bar wraps to a
 * second row at narrow widths, so the amount of chrome above the stage is not a
 * constant. Getting this wrong is worse than it looks — the canvas swallows
 * wheel events for zoom, so a stage that overflows the viewport leaves the user
 * unable to scroll down to the legend at all.
 */
function sizeStage() {
  const stage = container.parentElement;
  if (!stage) return;
  const top = stage.getBoundingClientRect().top + window.scrollY;
  const reserveBelow = 96; // legend + meta + page padding
  const height = Math.max(300, window.innerHeight - top - reserveBelow);
  stage.style.height = `${height}px`;
}

function onResize() {
  if (!renderer) return;
  sizeStage();
  const rect = container.getBoundingClientRect();
  camera.aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1);
  camera.updateProjectionMatrix();
  renderer.setSize(Math.max(rect.width, 1), Math.max(rect.height, 1));
}

function animate() {
  if (!running) return;
  frameHandle = requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

// ---------------------------------------------------------------------------
// Data + controls
// ---------------------------------------------------------------------------

async function load(setStatus) {
  const params = new URLSearchParams({
    projection: opts.projection,
    edges: opts.edges,
    threshold: String(opts.threshold),
    k: String(opts.k),
  });
  setStatus(`Building ${opts.projection} layout…`);
  const res = await fetch(`${API}/graph?${params}`);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* keep status */ }
    throw new Error(detail);
  }
  data = await res.json();
  visible = new Array(data.nodes.length).fill(true);
  selected = -1;

  clearGeometry();
  buildPoints();
  populateProjectFilter();
  applyFilters();
  fitCamera();
  setStatus(
    `${data.meta.node_count.toLocaleString()} memories · ` +
    `${data.meta.edge_count.toLocaleString()} ${data.meta.edge_mode} links · ` +
    `${data.meta.cached ? "cached layout" : `computed in ${(data.meta.elapsed_ms / 1000).toFixed(1)}s`}`
  );
}

function populateProjectFilter() {
  const sel = document.getElementById("gProject");
  if (!sel) return;
  const counts = new Map();
  for (const n of data.nodes) {
    const key = n.project || "(none)";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const current = sel.value;
  const ordered = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  sel.replaceChildren(new Option("any project", ""));
  for (const [name, count] of ordered) sel.append(new Option(`${name} (${count})`, name));
  sel.value = current;
}

function buildLegend() {
  if (!legendEl) return;
  legendEl.replaceChildren();
  for (const type of TYPE_ORDER) {
    const item = elem("span", "g-legend-item");
    const dot = elem("span", "g-dot");
    dot.style.background = `#${TYPE_COLORS[type].toString(16).padStart(6, "0")}`;
    item.append(dot, elem("span", null, type));
    legendEl.append(item);
  }
  const note = elem("span", "g-legend-note", "size = importance · fade = tier");
  legendEl.append(note);
}

// ---------------------------------------------------------------------------
// Public entry points
// ---------------------------------------------------------------------------

async function open(setStatus) {
  container = document.getElementById("graphCanvas");
  tooltipEl = document.getElementById("graphTooltip");
  panelEl = document.getElementById("graphPanel");
  legendEl = document.getElementById("graphLegend");
  metaEl = document.getElementById("graphMeta");

  if (!renderer) {
    buildScene();
    buildLegend();
    wireControls(setStatus);
  }
  onResize();
  running = true;
  animate();

  if (!data.nodes.length) {
    try {
      await load(setStatus);
    } catch (e) {
      setStatus(`Graph failed: ${e.message}`, true);
    }
  }
}

function close() {
  running = false;
  if (frameHandle) cancelAnimationFrame(frameHandle);
  frameHandle = null;
  if (tooltipEl) tooltipEl.hidden = true;
}

function wireControls(setStatus) {
  const reload = async () => {
    try { await load(setStatus); }
    catch (e) { setStatus(`Graph failed: ${e.message}`, true); }
  };

  document.getElementById("gProjection").addEventListener("change", e => {
    opts.projection = e.target.value;
    reload();
  });

  const help = document.getElementById("graphHelp");
  const setHelp = () => { help.textContent = EDGE_HELP[opts.edges] || ""; };
  setHelp();

  document.getElementById("gEdges").addEventListener("change", e => {
    opts.edges = e.target.value;
    setHelp();
    reload();
  });

  const thr = document.getElementById("gThreshold");
  const thrOut = document.getElementById("gThresholdOut");
  thrOut.textContent = opts.threshold.toFixed(2);
  thr.addEventListener("input", e => { thrOut.textContent = Number(e.target.value).toFixed(2); });
  // Only refetch on release: dragging would otherwise fire a request per pixel.
  thr.addEventListener("change", e => { opts.threshold = Number(e.target.value); reload(); });

  document.getElementById("gType").addEventListener("change", e => { filters.type = e.target.value; applyFilters(); });
  document.getElementById("gTier").addEventListener("change", e => { filters.tier = e.target.value; applyFilters(); });
  document.getElementById("gProject").addEventListener("change", e => { filters.project = e.target.value; applyFilters(); });

  let searchTimer = null;
  document.getElementById("gSearch").addEventListener("input", e => {
    clearTimeout(searchTimer);
    const value = e.target.value;
    searchTimer = setTimeout(() => { filters.query = value; applyFilters(); }, 150);
  });

  document.getElementById("gReset").addEventListener("click", fitCamera);

  // Follow the OS theme if it flips while the tab is open.
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (!renderer) return;
      const theme = themeColors();
      scene.background = theme.bg;
      scene.fog.color = theme.bg;
      buildLines();
    });
  }
}

window.MemoryGraph = { open, close, dispose: disposeScene };

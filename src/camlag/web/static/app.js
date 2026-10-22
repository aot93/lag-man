// cam-lag control UI. Plain ES module, no build step. All server text goes in via
// textContent, never as HTML.

const $ = (id) => document.getElementById(id);
const TC_RATES = { 50: [25, 50, 100], 60: [30, 60, 120] };
const FIELD_LABELS = {
  test_name: "Test", camera_name: "Camera", camera_fps: "Camera fps", run_number: "Run",
  date_time: "Date/time", operator: "Operator", location: "Location", notes: "Notes",
};
const CHAR_W = 0.6; // IBM Plex Mono advance, matches core/layout.py
const ASCENT = 1.025; // IBM Plex Mono ascent in em

let config = null;
let status = null;
let online = false;

// ---- API -------------------------------------------------------------------------------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api/${path}`, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

async function act(fn) {
  try {
    const out = await fn();
    showError(null);
    return out;
  } catch (e) {
    showError(e.message);
    return undefined;
  }
}

function showError(msg) {
  $("error").hidden = !msg;
  $("error-text").textContent = msg || "";
}

// ---- Theme -----------------------------------------------------------------------------

const THEME_KEY = "cam-lag.theme";
const THEMES = ["system", "light", "dark"];

function loadTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return THEMES.includes(v) ? v : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  $("theme").textContent = `Theme: ${theme === "system" ? "auto" : theme}`;
  try {
    if (theme === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Storage may be unavailable; the theme still applies for this visit.
  }
}

// ---- Config form -----------------------------------------------------------------------

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

function patchFor(path, value) {
  const keys = path.split(".");
  const out = {};
  let o = out;
  keys.slice(0, -1).forEach((k) => { o = o[k] = {}; });
  o[keys.at(-1)] = value;
  return out;
}

function fmtFraction(v) {
  return `${Math.round(v * 1000) / 10}%`;
}

function syncForm() {
  if (!config) return;
  for (const el of document.querySelectorAll("[data-path]")) {
    const v = getPath(config, el.dataset.path);
    if (el.classList.contains("seg")) {
      for (const b of el.querySelectorAll("button")) b.classList.toggle("on", b.value === String(v));
      continue;
    }
    if (el === document.activeElement && el.type !== "range") continue;
    if (el.type === "checkbox") el.checked = Boolean(v);
    else el.value = v;
  }
  for (const out of document.querySelectorAll("output[data-for]")) {
    out.textContent = fmtFraction(getPath(config, out.dataset.for));
  }
  for (const b of $("rate-set").querySelectorAll("button")) {
    b.classList.toggle("on", b.value === config.rate_set);
  }
  renderTcRates();
  renderSlateFields();
  if (document.activeElement !== $("start-value")) $("start-value").value = config.start_value;
}

function renderTcRates() {
  const box = $("tc-rate");
  const rates = TC_RATES[config.rate_set];
  if (box.dataset.set !== config.rate_set) {
    box.replaceChildren(...rates.map((r, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.value = String(r);
      b.textContent = i === rates.length - 1 ? `${r} fps (fast)` : `${r} fps`;
      b.addEventListener("click", () => applyPatch({ tc_rate: r }));
      return b;
    }));
    box.dataset.set = config.rate_set;
  }
  for (const b of box.querySelectorAll("button")) b.classList.toggle("on", Number(b.value) === config.tc_rate);
}

function renderSlateFields() {
  const body = $("slate-fields");
  if (!body.children.length) {
    for (const [name, label] of Object.entries(FIELD_LABELS)) {
      const tr = document.createElement("tr");
      const show = document.createElement("input");
      show.type = "checkbox";
      show.dataset.path = `slate.fields.${name}.enabled`;
      show.setAttribute("aria-label", `Show ${label}`);
      const value = document.createElement("input");
      value.type = "text";
      value.maxLength = 60;
      value.dataset.path = `slate.fields.${name}.value`;
      value.setAttribute("aria-label", label);
      if (name === "run_number" || name === "date_time") value.placeholder = "auto";
      const cells = [show, label, value].map((c) => {
        const td = document.createElement("td");
        if (typeof c === "string") td.textContent = c; else td.append(c);
        return td;
      });
      tr.append(...cells);
      body.append(tr);
      bindInput(show);
      bindInput(value);
    }
    syncForm();
  }
}

let pending = {};
let pendingTimer = null;

function deepMerge(a, b) {
  for (const [k, v] of Object.entries(b)) {
    if (v && typeof v === "object" && !Array.isArray(v)) a[k] = deepMerge(a[k] || {}, v);
    else a[k] = v;
  }
  return a;
}

function queuePatch(patch, delay = 250) {
  pending = deepMerge(pending, patch);
  clearTimeout(pendingTimer);
  pendingTimer = setTimeout(flushPatch, delay);
}

async function flushPatch() {
  const patch = pending;
  pending = {};
  await applyPatch(patch);
}

async function applyPatch(patch) {
  const out = await act(() => api("PUT", "config", patch));
  if (out) config = out;
  syncForm();
  refreshPreview();
}

function readInput(el) {
  if (el.type === "checkbox") return el.checked;
  if (el.type === "range" || el.type === "number") return Number(el.value);
  return el.value;
}

function bindInput(el) {
  const path = el.dataset.path;
  const handler = () => {
    const v = readInput(el);
    if (el.type === "number" && !Number.isFinite(v)) return;
    const out = document.querySelector(`output[data-for="${path}"]`);
    if (out) out.textContent = fmtFraction(v);
    queuePatch(patchFor(path, v), el.type === "text" ? 500 : 150);
  };
  el.addEventListener(el.type === "text" ? "input" : "change", handler);
  if (el.type === "range") el.addEventListener("input", handler);
}

function bindSeg(seg) {
  for (const b of seg.querySelectorAll("button")) {
    b.addEventListener("click", () => applyPatch(patchFor(seg.dataset.path, b.value)));
  }
}

// ---- Status ----------------------------------------------------------------------------

const STATE_LABELS = { idle: "Idle", pre_roll: "Slate", running: "Running" };

function renderStatus() {
  const s = status;
  $("kf-tc").textContent = s.timecode;
  $("kf-count").textContent = s.count.toLocaleString("en-GB");
  const stateLabel = s.state === "pre_roll" ? `Slate ${s.pre_roll_remaining.toFixed(1)} s` : STATE_LABELS[s.state];
  $("kf-state").textContent = stateLabel;
  $("kf-state").parentElement.className = `kf ${s.state === "running" ? "ok" : ""}`;
  $("kf-run").textContent = s.state === "idle" ? `Next run ${s.run_number}` : `Run ${s.run_number} · ${s.run_id}`;

  const hz = s.measured_hz;
  $("kf-hz").textContent = hz ? `${hz.toFixed(2)} Hz` : "-- Hz";
  $("kf-hz-l").textContent = s.simulated_timing
    ? "Headless: simulated timing, no display"
    : `Measured refresh · expected ${s.refresh_hz} Hz`;
  $("kf-hz").parentElement.className = `kf ${s.rate_mismatch ? "bad" : hz ? "ok" : ""}`;

  $("kf-frames").textContent = String(s.repeated_refreshes);
  $("kf-frames-l").textContent = `Repeated refreshes · ${s.early_presents} early presents`;
  $("kf-frames").parentElement.className = `kf ${s.unacked_frame_warnings ? "warn" : ""}`;

  const n = s.unacked_frame_warnings;
  $("frame-alert").hidden = n === 0;
  if (n) {
    const last = s.recent_events.at(-1);
    $("frame-alert-title").textContent = `${n} frame warning${n === 1 ? "" : "s"}.`;
    $("frame-alert-detail").textContent = last
      ? `Last at ${last.timecode}: ${last.kind === "repeat" ? `previous frame held for ${last.refreshes} extra refresh${last.refreshes === 1 ? "" : "es"}` : "present did not wait for vsync"}.`
      : "";
  }
  const lw = s.layout_warnings;
  $("layout-alert").hidden = !lw.length;
  $("layout-alert-text").textContent = lw.join(" ");

  $("start").disabled = s.state !== "idle";
  $("stop").disabled = s.state === "idle";
  for (const b of $("rate-set").querySelectorAll("button")) b.disabled = s.state !== "idle";

  renderEvents(s.recent_events);
  $("foot-display").textContent = `Output ${s.display.width} × ${s.display.height}`;
}

let lastEventsKey = "";

function renderEvents(events) {
  const key = events.map((e) => `${e.frame}`).join(",");
  if (key === lastEventsKey) return;
  lastEventsKey = key;
  $("events-empty").hidden = events.length > 0;
  $("events").replaceChildren(...events.slice().reverse().map((e) => {
    const tr = document.createElement("tr");
    const cells = [
      [e.timecode, "mono"],
      [e.kind === "repeat" ? "Repeat" : "Early present", ""],
      [String(e.refreshes), "r"],
      [e.gap_ms.toFixed(2), "r mono"],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      td.className = cls;
      tr.append(td);
    }
    return tr;
  }));
}

function setOnline(ok) {
  if (ok === online) return;
  online = ok;
  $("conn").textContent = ok ? "Connected" : "Offline";
  $("conn").className = `pill ${ok ? "ok" : "bad"}`;
}

let configRev = null;

async function pollStatus() {
  try {
    status = await api("GET", "status");
    setOnline(true);
    renderStatus();
    // Another device changed the configuration: pick it up unless an edit is in flight.
    if (configRev !== null && status.config_rev !== configRev && !Object.keys(pending).length) {
      config = await api("GET", "config");
      syncForm();
    }
    configRev = status.config_rev;
  } catch {
    setOnline(false);
  }
  setTimeout(pollStatus, 250);
}

// ---- Preview ---------------------------------------------------------------------------

let previewBusy = false;

async function refreshPreview() {
  if (previewBusy || document.hidden) return;
  previewBusy = true;
  try {
    drawPreview(await api("GET", "preview"));
  } catch {
    // status polling reports connection problems
  } finally {
    previewBusy = false;
  }
}

function drawPreview(pv) {
  const canvas = $("preview");
  const cssW = canvas.clientWidth || 960;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(cssW * dpr);
  const h = Math.round((w * pv.height) / pv.width);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  const k = w / pv.width;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = pv.bg;
  ctx.fillRect(0, 0, w, h);
  ctx.textBaseline = "alphabetic";
  for (const op of pv.ops) {
    ctx.fillStyle = op.colour;
    if (op.type === "fill") {
      ctx.fillRect(op.x * k, op.y * k, Math.max(1, op.w * k), Math.max(1, op.h * k));
    } else {
      ctx.font = `600 ${op.px * k}px "IBM Plex Mono", monospace`;
      [...op.text].forEach((ch, i) => {
        ctx.fillText(ch, (op.x + Math.round(i * CHAR_W * op.px)) * k, (op.y + ASCENT * op.px) * k);
      });
    }
  }
}

// ---- Presets and runs ------------------------------------------------------------------

function button(label, onClick, cls = "btn small") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

function link(label, href) {
  const a = document.createElement("a");
  a.className = "btn small";
  a.href = href;
  a.textContent = label;
  return a;
}

async function loadPresets() {
  const out = await act(() => api("GET", "presets"));
  if (!out) return;
  $("presets-empty").hidden = out.presets.length > 0;
  $("presets").replaceChildren(...out.presets.map((name) => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.className = "name";
    span.textContent = name;
    const enc = encodeURIComponent(name);
    li.append(
      span,
      button("Load", async () => {
        const cfg = await act(() => api("POST", `presets/${enc}/load`));
        if (cfg) { config = cfg; syncForm(); refreshPreview(); }
      }),
      link("Export", `/api/presets/${enc}?download=1`),
      button("Delete", async () => {
        if (!confirm(`Delete preset "${name}"?`)) return;
        await act(() => api("DELETE", `presets/${enc}`));
        loadPresets();
      }, "btn small danger"),
    );
    return li;
  }));
}

async function savePreset() {
  const name = $("preset-name").value.trim();
  if (!name) { showError("Enter a preset name."); return; }
  if (await act(() => api("PUT", `presets/${encodeURIComponent(name)}`))) {
    $("preset-name").value = "";
    loadPresets();
  }
}

async function importPreset(file) {
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch {
    showError(`${file.name} is not valid JSON.`);
    return;
  }
  const name = file.name.replace(/\.json$/i, "").replace(/[^A-Za-z0-9 _.-]/g, "_").slice(0, 64) || "imported";
  if (await act(() => api("PUT", `presets/${encodeURIComponent(name)}`, data))) loadPresets();
}

async function loadRuns() {
  const out = await act(() => api("GET", "runs"));
  if (!out) return;
  $("runs-empty").hidden = out.runs.length > 0;
  $("runs").replaceChildren(...out.runs.map((r) => {
    const tr = document.createElement("tr");
    const camera = (r.slate || []).find((l) => l.startsWith("CAMERA")) || "";
    const started = r.started ? new Date(r.started).toLocaleString("en-GB") : "";
    const cells = [
      r.run_number != null ? String(r.run_number) : "",
      started,
      r.tc_rate ? `${r.tc_rate} fps / ${r.refresh_hz} Hz` : "",
      camera.replace(/^CAMERA\s+/, ""),
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    const td = document.createElement("td");
    td.className = "r";
    td.append(link("Log", `/api/runs/${encodeURIComponent(r.id)}`));
    tr.append(td);
    return tr;
  }));
}

// ---- Wiring ----------------------------------------------------------------------------

async function init() {
  let theme = loadTheme();
  applyTheme(theme);
  $("theme").addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    applyTheme(theme);
  });

  for (const el of document.querySelectorAll("input[data-path], select[data-path]")) bindInput(el);
  for (const seg of document.querySelectorAll(".seg[data-path]")) bindSeg(seg);
  for (const b of $("rate-set").querySelectorAll("button")) {
    b.addEventListener("click", () => applyPatch({ rate_set: b.value, tc_rate: TC_RATES[b.value][0] }));
  }

  const command = (name) => async () => {
    if (await act(() => api("POST", name))) {
      if (name === "stop" || name === "start") loadRuns();
      refreshPreview();
    }
  };
  $("start").addEventListener("click", command("start"));
  $("stop").addEventListener("click", command("stop"));
  $("reset").addEventListener("click", command("reset"));
  $("ack").addEventListener("click", command("warnings/ack"));
  $("set-start").addEventListener("click", async () => {
    if (await act(() => api("POST", "start-value", { value: $("start-value").value.trim() }))) {
      config = await api("GET", "config");
      syncForm();
      refreshPreview();
    }
  });
  $("preset-save").addEventListener("click", savePreset);
  $("preset-name").addEventListener("keydown", (e) => { if (e.key === "Enter") savePreset(); });
  $("preset-import").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) importPreset(file);
  });

  config = await act(() => api("GET", "config"));
  syncForm();
  await document.fonts.load('600 20px "IBM Plex Mono"').catch(() => {});
  pollStatus();
  refreshPreview();
  setInterval(refreshPreview, 400);
  loadPresets();
  loadRuns();
}

init();

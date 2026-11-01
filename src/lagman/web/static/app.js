// lag-man control UI. Plain ES module, no build step. All server text goes in via
// textContent, never as HTML.

import { themeControl } from "./theme.js";

const $ = (id) => document.getElementById(id);
const FIRST_TC_RATE = { 50: 25, 60: 30 }; // the server sends the full list in status.tc_rates
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
  if (!config || !status) return;
  const box = $("tc-rate");
  const rates = status.tc_rates;
  const key = rates.join(",");
  if (box.dataset.rates !== key) {
    box.replaceChildren(...rates.map((r, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.value = String(r);
      b.textContent = i === rates.length - 1 ? `${r} fps (fast)` : `${r} fps`;
      b.addEventListener("click", () => applyPatch({ tc_rate: r }));
      return b;
    }));
    box.dataset.rates = key;
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
  renderTcRates();
  renderDisplayAlert(s);
  const modeKey = `${s.display.mode}|${s.state}|${s.rate_set}`;
  if (modeKey !== displayKey) {
    displayKey = modeKey;
    loadDisplay();
  }
  $("foot-display").textContent = `Output ${s.display.width} × ${s.display.height} · counting at ${s.refresh_hz} Hz`;
}

function renderDisplayAlert(s) {
  let title = "";
  let detail = "";
  if (s.display_warning) {
    title = "Display rate.";
    detail = s.display_warning;
  } else if (s.rate_mismatch && !s.simulated_timing) {
    title = `Measured refresh ${s.measured_hz.toFixed(2)} Hz, expected ${s.refresh_hz} Hz.`;
    detail = "The display is not running at the rate it reports. Frame warnings are paused until the rates match.";
  }
  $("display-alert").hidden = !title;
  $("display-alert-title").textContent = title;
  $("display-alert-detail").textContent = detail;
}

// ---- Display ---------------------------------------------------------------------------

let displayKey = "";

function fmtMode(m) {
  return `${m.width} × ${m.height} · ${m.hz} Hz`;
}

async function loadDisplay() {
  const info = await act(() => api("GET", "display"));
  if (!info || !status) return;
  const current = info.modes.find((m) => m.mode === info.current);
  $("display-mode").textContent = current ? fmtMode(current) : (info.current || "Unknown");
  const fits = !status.display_warning;
  $("display-pill").textContent = fits ? `${status.rate_set}Hz set` : "Wrong rate";
  $("display-pill").className = `pill ${fits ? "ok" : "bad"}`;
  const tick = 1000 / status.refresh_hz;
  $("display-tick").textContent = `Counting at ${status.refresh_hz} Hz: ${Number.isInteger(tick) ? tick : tick.toFixed(3)} ms per tick.`;
  $("display-note").textContent = info.note;
  $("display-empty").hidden = info.modes.length > 0;
  const idle = status.state === "idle";
  $("display-modes").replaceChildren(...info.modes.map((m) => {
    const li = document.createElement("li");
    const isCurrent = m.mode === info.current;
    li.className = isCurrent ? "current" : "";
    const name = document.createElement("span");
    name.className = "name mono";
    name.textContent = fmtMode(m);
    const rates = document.createElement("span");
    rates.className = "rates";
    rates.textContent = m.rate_sets.map((k) => `${k}Hz set${m.hz < 100 ? " (fallback)" : ""}`).join(", ");
    li.append(name, rates);
    if (isCurrent) {
      const pill = document.createElement("span");
      pill.className = "pill";
      pill.textContent = "Current";
      li.append(pill);
    } else {
      const b = button("Use", () => setDisplayMode(m));
      b.disabled = !info.can_set || !idle;
      if (!idle) b.title = "Stop the run first";
      li.append(b);
    }
    return li;
  }));
}

async function setDisplayMode(m) {
  $("display-mode").textContent = `Switching to ${fmtMode(m)}…`;
  const info = await act(() => api("PUT", "display", { mode: m.mode }));
  if (info) {
    config = await api("GET", "config");
    syncForm();
    refreshPreview();
  }
  displayKey = "";
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

// ---- Network ---------------------------------------------------------------------------

let network = null;
let netFilled = false;
let netTimer = null;
let netFormError = ""; // a rejected Apply, kept until the next one succeeds

function fmtCountdown(s) {
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function bareAddress(cidr) {
  return (cidr || "").split("/")[0];
}

function readIpv4(pre) {
  return {
    method: $(`${pre}-method`).value,
    address: $(`${pre}-address`).value.trim(),
    gateway: $(`${pre}-gateway`).value.trim(),
    dns: $(`${pre}-dns`).value.trim(),
  };
}

function fillIpv4(pre, ip, current) {
  $(`${pre}-method`).value = ip.method;
  $(`${pre}-address`).value = ip.address || (current && current.addresses[0]) || "";
  $(`${pre}-gateway`).value = ip.gateway || (current && current.gateway) || "";
  $(`${pre}-dns`).value = (ip.dns && ip.dns.length ? ip.dns : (current && current.dns) || []).join(", ");
  showStatic(pre);
}

function showStatic(pre) {
  const manual = $(`${pre}-method`).value === "manual";
  for (const el of document.querySelectorAll(`.${pre}-static`)) el.hidden = !manual;
}

function setWifiMode(mode) {
  for (const b of $("net-wifi-mode").querySelectorAll("button")) b.classList.toggle("on", b.value === mode);
  $("net-client").hidden = mode !== "client";
  $("net-hotspot").hidden = mode !== "hotspot";
}

function fillNetworkForms(n) {
  const eth = n.devices.ethernet;
  const wifi = n.devices.wifi;
  fillIpv4("net-w", n.profiles.wired || { method: "auto" }, eth);
  const client = n.profiles.client || { ssid: wifi && wifi.mode === "client" ? wifi.ssid : "", ipv4: { method: "auto" } };
  $("net-c-ssid").value = client.ssid;
  $("net-c-password").value = "";
  $("net-c-password").placeholder = n.profiles.client ? "Saved; blank keeps it" : "";
  fillIpv4("net-c", client.ipv4, wifi && wifi.mode === "client" ? wifi : null);
  const hotspot = n.profiles.hotspot || { ssid: "lag-man", band: "2.4", address: "192.168.4.1/24" };
  $("net-h-ssid").value = hotspot.ssid;
  $("net-h-password").value = "";
  $("net-h-password").placeholder = n.profiles.hotspot ? "Saved; blank keeps it" : "8 to 63 characters";
  $("net-h-band").value = hotspot.band;
  $("net-h-address").value = hotspot.address;
  setWifiMode(wifi && wifi.mode === "hotspot" ? "hotspot" : "client");
}

function renderNetwork(n) {
  const rows = [];
  for (const [kind, d] of Object.entries(n.devices)) {
    let state = d.state.charAt(0).toUpperCase() + d.state.slice(1);
    if (kind === "wifi" && d.mode === "hotspot") state = `Hotspot “${d.ssid}”`;
    else if (kind === "wifi" && d.ssid) state = `Joined “${d.ssid}”`;
    rows.push([`${kind === "wifi" ? "Wi-Fi" : "Wired"} · ${d.device}`, state, d.addresses.join(", ") || "None"]);
  }
  $("net-ports").replaceChildren(...rows.map((cells) => {
    const tr = document.createElement("tr");
    cells.forEach((text, i) => {
      const td = document.createElement("td");
      if (i === 2) td.className = "mono";
      td.textContent = text;
      tr.append(td);
    });
    return tr;
  }));
  $("net-reach").textContent = n.hostname ? `Also reachable as http://${n.hostname}.local:${location.port || 80}/ where the network supports it.` : "";
  const err = netFormError || n.error || n.last_error;
  $("net-error").hidden = !err;
  $("net-error-text").textContent = err || "";
  $("net-note").textContent = n.note;
  $("net-forms").disabled = !n.available || !!n.pending;
  if (n.available && !n.pending && !netFilled) {
    netFilled = true;
    fillNetworkForms(n);
  }

  const p = n.pending;
  $("net-alert").hidden = !p;
  $("net-alert-actions").hidden = !p || p.state !== "waiting";
  if (p) {
    $("net-alert-title").textContent = {
      applying: `Applying the ${p.label} settings.`,
      waiting: `Keep the new ${p.label} settings?`,
      reverting: `Restoring the previous ${p.label} settings.`,
    }[p.state];
    $("net-alert-detail").textContent = p.state === "waiting"
      ? `They are reverted in ${fmtCountdown(p.remaining_s)} unless you keep them.`
      : p.state === "applying" ? "If this page stops responding, reconnect at the new address and press Keep." : "";
  }
}

async function loadNetwork() {
  clearTimeout(netTimer);
  try {
    network = await api("GET", "network");
    renderNetwork(network);
  } catch (e) {
    // The connection may be changing under us; try again shortly.
  }
  netTimer = setTimeout(loadNetwork, network && network.pending ? 1000 : 15000);
}

function newAddressHint(kind, body) {
  if (kind === "wired" && body.method === "manual") return `http://${bareAddress(body.address)}:${location.port || 80}/`;
  if (kind === "hotspot") return `Wi-Fi “${body.ssid}”, then http://${bareAddress(body.address) || "192.168.4.1"}:${location.port || 80}/`;
  if (kind === "client" && body.ipv4.method === "manual") return `Wi-Fi “${body.ssid}”, then http://${bareAddress(body.ipv4.address)}:${location.port || 80}/`;
  return `the address the network gives it${network && network.hostname ? ` (try http://${network.hostname}.local:${location.port || 80}/)` : ""}`;
}

async function applyNetwork(kind, body) {
  const ok = window.confirm(`Apply these ${kind === "client" ? "Wi-Fi" : kind} settings?\n\n`
    + `This page may lose its connection. Reconnect at ${newAddressHint(kind, body)} and press Keep within two minutes, or the previous settings come back.`);
  if (!ok) return;
  let out;
  try {
    out = await api("PUT", `network/${kind}`, body);
  } catch (e) {
    // Shown in the card, beside the form, not in the page alert out of view.
    netFormError = e.message;
    if (network) renderNetwork(network);
    $("net-error").scrollIntoView({ block: "nearest" });
    return;
  }
  netFormError = "";
  if (out) {
    network = out;
    netFilled = false; // refill from the saved settings once the change settles
    renderNetwork(out);
    loadNetwork();
  }
}

async function netDecision(path) {
  const out = await act(() => api("POST", `network/${path}`));
  if (out) {
    netFilled = false;
    renderNetwork(out);
    loadNetwork();
  }
}

async function scanWifi() {
  $("net-c-scan-hint").textContent = "Scanning…";
  const out = await act(() => api("GET", "network/scan"));
  const nets = out ? out.networks : [];
  $("net-c-scan").replaceChildren(...nets.map((n) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = n.ssid;
    const meta = document.createElement("span");
    meta.className = "rates";
    meta.textContent = `${n.signal}% · ${n.security || "open"}`;
    li.append(name, meta, button("Use", () => {
      $("net-c-ssid").value = n.ssid;
      $("net-c-password").focus();
    }));
    return li;
  }));
  $("net-c-scan-hint").textContent = out ? `${nets.length} network${nets.length === 1 ? "" : "s"} found.` : "";
}

function bindNetwork() {
  for (const pre of ["net-w", "net-c"]) {
    $(`${pre}-method`).addEventListener("change", () => {
      showStatic(pre);
    });
  }
  for (const b of $("net-wifi-mode").querySelectorAll("button")) b.addEventListener("click", () => setWifiMode(b.value));
  $("net-w-apply").addEventListener("click", () => applyNetwork("wired", readIpv4("net-w")));
  $("net-c-apply").addEventListener("click", () => applyNetwork("client", {
    ssid: $("net-c-ssid").value.trim(), password: $("net-c-password").value, ipv4: readIpv4("net-c"),
  }));
  $("net-h-apply").addEventListener("click", () => applyNetwork("hotspot", {
    ssid: $("net-h-ssid").value.trim(), password: $("net-h-password").value,
    band: $("net-h-band").value, address: $("net-h-address").value.trim(),
  }));
  $("net-c-scan-btn").addEventListener("click", scanWifi);
  $("net-keep").addEventListener("click", () => netDecision("confirm"));
  $("net-revert").addEventListener("click", () => netDecision("revert"));
}

// ---- Wiring ----------------------------------------------------------------------------

async function init() {
  themeControl($("theme"));

  for (const el of document.querySelectorAll("input[data-path], select[data-path]")) bindInput(el);
  for (const seg of document.querySelectorAll(".seg[data-path]")) bindSeg(seg);
  for (const b of $("rate-set").querySelectorAll("button")) {
    b.addEventListener("click", () => applyPatch({ rate_set: b.value, tc_rate: FIRST_TC_RATE[b.value] }));
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
  bindNetwork();
  loadNetwork();
}

init();

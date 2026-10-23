// lag-man analysis app. Plain ES module, no build step. Server text goes in via textContent;
// the only HTML inserted is the chart SVG the server builds with escaped values.

import { themeControl } from "/static/theme.js";

const TOKEN = document.querySelector('meta[name="lagman-token"]').content;
const $ = (id) => document.getElementById(id);
const COLOURS = () => {
  const cs = getComputedStyle(document.documentElement);
  return { source: cs.getPropertyValue("--s2").trim() || "#2e7de0", wall: cs.getPropertyValue("--s1").trim() || "#ee2737" };
};
const TABS = [["details", "Recording"], ["regions", "Regions"], ["analyse", "Analyse"], ["results", "Results"]];

let app = null; // last /api/state
let route = { name: "home" };
let cleanup = []; // timers and listeners owned by the current view

// ---- helpers ---------------------------------------------------------------------------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Lagman-Token": TOKEN } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api/${path}`, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

// For <img> and links, which cannot send the token header.
function link(path, params = {}) {
  const q = new URLSearchParams({ ...params, t: TOKEN });
  return `/api/${path}?${q}`;
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

const enc = encodeURIComponent;
const ms = (v, d = 1) => (v == null || v === "" ? "–" : `${Number(v).toFixed(d)} ms`);
const bytes = (n) => (n > 1e9 ? `${(n / 1e9).toFixed(2)} GB` : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`);
const when = (iso) => (iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "");
const ago = (s) => (s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} days`);

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function card(title, ...children) {
  return h("section", { class: "card" }, h("h2", { class: "section", text: title }), ...children);
}

function kf(value, label, cls = "") {
  return h("div", { class: `kf ${cls}` }, h("div", { class: "n", text: value }), h("div", { class: "l", text: label }));
}

// ---- state polling ---------------------------------------------------------------------

async function refreshState() {
  try {
    const prev = app;
    app = await api("GET", "state");
    $("conn").textContent = "Connected";
    $("conn").className = "pill ok";
    $("ws-path").textContent = app.workspace;
    onState(prev);
  } catch {
    $("conn").textContent = "Offline";
    $("conn").className = "pill bad";
  }
}

function activeJobs() {
  return (app?.jobs || []).filter((j) => j.state === "queued" || j.state === "running");
}

function onState(prev) {
  const jobs = activeJobs();
  const pill = $("jobs-pill");
  pill.hidden = !jobs.length;
  if (jobs.length) {
    const j = jobs.find((x) => x.state === "running") || jobs[0];
    pill.textContent = j.total ? `${j.label} · ${Math.round((100 * j.done) / j.total)}%` : j.label;
    pill.className = "pill run";
  }
  // Tell views about finished jobs.
  const before = new Map((prev?.jobs || []).map((j) => [j.id, j.state]));
  for (const j of app.jobs) {
    const was = before.get(j.id);
    if (was && was !== j.state && !["queued", "running"].includes(j.state)) onJobFinished(j);
  }
  if (route.name === "home") renderHomeTable();
  if (route.name === "recording" && route.tab === "analyse") updateProgress();
}

function onJobFinished(job) {
  if (job.state === "failed") showError(`${job.label} failed: ${job.message}`);
  if (job.kind === "demo" && job.state === "done" && route.name === "home") renderHome();
  if (job.kind === "analyse" && route.name === "recording" && route.id === job.recording) {
    if (job.state === "done") go(`#/recording/${enc(job.recording)}/results`);
    else render();
  }
}

// ---- routing ---------------------------------------------------------------------------

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] === "recording" && parts[1]) return { name: "recording", id: parts[1], tab: TABS.some(([t]) => t === parts[2]) ? parts[2] : "details" };
  return { name: "home" };
}

async function render() {
  for (const fn of cleanup) fn();
  cleanup = [];
  route = parseRoute();
  showError(null);
  if (!app) await refreshState();
  if (route.name === "recording") await renderRecording();
  else renderHome();
  window.scrollTo(0, 0);
}

// ---- home ------------------------------------------------------------------------------

function renderHome() {
  const view = $("view");
  const tableBox = h("div", { id: "rec-table" });
  const combinedBox = h("div", { id: "combined" });
  view.replaceChildren(
    h("div", { class: "hero" },
      h("p", { class: "eyebrow", text: "Glass-to-glass latency" }),
      h("h2", { text: "Recordings" }),
      h("p", { class: "lede", text: "Add a control camera recording, attach its run log, mark the two code strips once per camera position, then analyse. Results and reports are kept in the workspace." })),
    h("div", { class: "toolbar" },
      h("button", { class: "btn primary", type: "button", onclick: () => pickFile("video", "Add a recording", addRecording) }, "Add recording"),
      h("button", { class: "btn", type: "button", onclick: () => generatorDialog() }, "Fetch run logs from generator"),
      h("button", { class: "btn", type: "button", onclick: makeDemo }, "Make a demo recording")),
    tableBox,
    combinedBox,
  );
  renderHomeTable();
  renderCombined();
}

function statusOf(r) {
  if (!r.exists) return ["Video missing", "bad"];
  if (r.job) return [r.job.total ? `Analysing ${Math.round((100 * r.job.done) / r.job.total)}%` : "Queued", "run"];
  if (r.summary) return ["Analysed", "ok"];
  if (!r.run_log && !r.rate_set) return ["Needs run log", "warn"];
  if (!r.setup) return ["Needs regions", "warn"];
  return ["Ready to analyse", ""];
}

let lastTableKey = "";

function renderHomeTable() {
  const box = $("rec-table");
  if (!box || !app) return;
  const key = JSON.stringify(app.recordings.map((r) => [r.id, r.setup, r.run_log, r.rate_set, r.summary?.analysed, r.job?.done, r.job?.state, r.exists]));
  if (key === lastTableKey && box.children.length) return;
  lastTableKey = key;
  if (!app.recordings.length) {
    box.replaceChildren(h("section", { class: "card empty-state" },
      h("h2", { class: "section", text: "No recordings yet" }),
      h("p", { text: "Add a video from the control camera, or make a demo recording to try the workflow." }),
      h("button", { class: "btn primary", type: "button", onclick: () => pickFile("video", "Add a recording", addRecording) }, "Add recording")));
    return;
  }
  const rows = app.recordings.map((r) => {
    const [label, cls] = statusOf(r);
    const s = r.summary;
    const open = () => go(`#/recording/${enc(r.id)}/${s ? "results" : "details"}`);
    return h("tr", { onclick: open },
      h("td", {}, h("a", { href: `#/recording/${enc(r.id)}/${s ? "results" : "details"}`, text: r.name, onclick: (e) => e.stopPropagation() }), h("span", { class: "sub-line", text: r.video })),
      h("td", { text: [s?.camera, s?.camera_fps && `${s.camera_fps} fps`].filter(Boolean).join(" · ") || "–" }),
      h("td", { text: r.setup || "–" }),
      h("td", {}, h("span", { class: `pill ${cls}`, text: label })),
      h("td", { class: "r", text: s ? ms(s.mean_ms) : "–" }),
      h("td", { class: "r", text: s ? `${s.n} / ${s.frames}` : "–" }));
  });
  box.replaceChildren(h("div", { class: "tablewrap tall" },
    h("table", { class: "data clickable" },
      h("thead", {}, h("tr", {}, ...["Recording", "Camera", "Setup", "Status", "Mean", "Samples"].map((t, i) => h("th", { class: i >= 4 ? "r" : "", text: t })))),
      h("tbody", {}, rows))));
}

async function renderCombined() {
  const box = $("combined");
  const out = await act(() => api("GET", "combined"));
  if (!box || !out || !out.rows.length) return;
  box.replaceChildren(h("div", { class: "mt" }, card("Summary per camera and frame rate",
    h("div", { class: "tablewrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ...["Camera", "Fps", "Runs", "Samples", "Min", "Max", "Mean"].map((t, i) => h("th", { class: i >= 2 ? "r" : "", text: t })))),
      h("tbody", {}, out.rows.map((r) => h("tr", {},
        h("td", { text: r.camera }), h("td", { text: r.camera_fps }),
        h("td", { class: "r", text: r.runs }), h("td", { class: "r", text: r.samples }),
        h("td", { class: "r", text: ms(r.min_ms) }), h("td", { class: "r", text: ms(r.max_ms) }), h("td", { class: "r", text: ms(r.mean_ms) })))))),
    h("div", { class: "row" }, h("a", { class: "btn", href: link("combined.csv"), download: "latency-summary.csv" }, "Download CSV")))));
}

async function addRecording(path) {
  const rec = await act(() => api("POST", "recordings", { video: path }));
  if (!rec) return;
  closeDialog();
  await refreshState();
  go(`#/recording/${enc(rec.id)}/details`);
}

async function makeDemo() {
  if (await act(() => api("POST", "demo"))) refreshState();
}

// ---- dialogs ---------------------------------------------------------------------------

function openDialog(title, ...children) {
  $("dlg-title").textContent = title;
  $("dlg-body").replaceChildren(...children);
  if (!$("dlg").open) $("dlg").showModal();
}

function closeDialog() {
  if ($("dlg").open) $("dlg").close();
}

// File browser on this PC. kind: "video" or "runlog".
function pickFile(kind, title, onPick) {
  const input = h("input", { type: "text", spellcheck: "false", "aria-label": "Folder" });
  const list = h("ul", { class: "fb", role: "listbox" });
  const hint = h("p", { class: "hint", text: kind === "video" ? "Showing folders and video files (.mp4, .mov, .mts, .mxf …)." : "Showing folders and run logs (.jsonl) from the generator's runs folder." });
  let parent = null;
  async function load(path) {
    const out = await act(() => api("GET", `fs?${new URLSearchParams(path ? { path } : {})}`));
    if (!out) return;
    input.value = out.path;
    parent = out.parent;
    const items = [];
    if (out.parent !== null && out.parent !== undefined) items.push(entry("dir", "..", "Up a folder", () => load(out.parent)));
    for (const d of out.dirs) items.push(entry("dir", d, "", () => load(out.path ? joinPath(out.path, d) : d)));
    for (const f of out.files.filter((f) => f.kind === kind)) items.push(entry("file", f.name, `${bytes(f.bytes)} · ${when(f.modified)}`, () => onPick(f.path)));
    if (!items.length) items.push(h("li", { class: "dir" }, h("span", { class: "nm", text: "Nothing here to pick." })));
    list.replaceChildren(...items);
  }
  function entry(type, name, meta, fn) {
    return h("li", { class: type, tabindex: "0", onclick: fn, onkeydown: (e) => { if (e.key === "Enter") fn(); } },
      h("span", { class: "ico", text: type === "dir" ? "▸" : "•" }), h("span", { class: "nm", text: name }), h("span", { class: "meta", text: meta }));
  }
  openDialog(title,
    h("div", { class: "fb-bar" }, input,
      h("button", { class: "btn small", type: "button", onclick: () => load(input.value) }, "Go"),
      h("button", { class: "btn small", type: "button", onclick: () => parent != null && load(parent) }, "Up"),
      h("button", { class: "btn small", type: "button", onclick: () => load(app?.home) }, "Home"),
      h("button", { class: "btn small", type: "button", onclick: () => load("") }, "Drives")),
    list, hint);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") load(input.value); });
  load(kind === "runlog" ? null : app?.last_dir);
}

function joinPath(dir, name) {
  const sep = dir.includes("\\") ? "\\" : "/";
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}

function generatorDialog(recId) {
  const url = h("input", { type: "text", value: app?.generator_url || "", placeholder: "192.168.1.50:8080", spellcheck: "false" });
  const pw = h("input", { type: "password", placeholder: "only if one is set", autocomplete: "off" });
  const list = h("div", {});
  async function listRuns() {
    const out = await act(() => api("POST", "generator/runs", { url: url.value, password: pw.value || null }));
    if (!out) return;
    if (!out.runs.length) { list.replaceChildren(h("p", { class: "empty", text: "The generator has no runs yet." })); return; }
    list.replaceChildren(h("div", { class: "tablewrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ...["Run", "Started", "Clock", "Camera", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, out.runs.map((r) => {
        const camera = (r.slate || []).find((l) => l.startsWith("CAMERA"))?.replace(/^CAMERA\s+/, "") || "";
        const btn = h("button", { class: `btn small ${r.fetched ? "" : "primary"}`, type: "button" }, recId ? "Use for this recording" : r.fetched ? "Fetch again" : "Fetch");
        btn.addEventListener("click", async () => {
          const got = await act(() => api("POST", "generator/fetch", { url: url.value, password: pw.value || null, run_id: r.id }));
          if (!got) return;
          if (recId) {
            if (await act(() => api("PATCH", `recordings/${enc(recId)}`, { run_log: got.path }))) { closeDialog(); render(); }
          } else {
            btn.textContent = "Fetched";
            btn.className = "btn small";
          }
        });
        return h("tr", {}, h("td", { text: r.run_number ?? "" }), h("td", { text: when(r.started) }),
          h("td", { text: r.tc_rate ? `${r.tc_rate} fps / ${r.refresh_hz} Hz` : "" }), h("td", { text: camera }), h("td", { class: "actions" }, btn));
      })))));
  }
  openDialog("Run logs from the generator",
    h("p", { class: "steps-line", text: "The generator serves its run logs on the network. Fetched logs are saved in the workspace." }),
    h("div", { class: "form" }, h("label", {}, "Generator address", url), h("label", {}, "Password", pw)),
    h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", onclick: listRuns }, "List runs")),
    h("div", { class: "mt-s" }, list));
  if (url.value) listRuns();
}

// ---- recording -------------------------------------------------------------------------

async function renderRecording() {
  const view = $("view");
  const info = await act(() => api("GET", `recordings/${enc(route.id)}/info`));
  if (!info) {
    view.replaceChildren(h("p", {}, h("a", { href: "#/", text: "← All recordings" })));
    return;
  }
  const rec = info.recording;
  const run = info.run;
  const done = { details: !!(rec.run_log || rec.rate_set) && !run.error, regions: !!info.setup, analyse: !!info.summary, results: !!info.summary };
  const tabs = h("nav", { class: "tabs", "aria-label": "Steps" }, TABS.map(([t, label], i) =>
    h("a", { href: `#/recording/${enc(rec.id)}/${t}`, class: done[t] ? "done" : "", "aria-current": route.tab === t ? "page" : null },
      h("span", { class: "n", text: done[t] ? "✓" : i + 1 }), label)));
  const camera = run.slate?.CAMERA;
  view.replaceChildren(
    h("div", { class: "crumbs" }, h("a", { href: "#/", text: "← All recordings" })),
    h("div", { class: "hero" },
      h("p", { class: "eyebrow", text: [camera, run.slate?.FPS && `${run.slate.FPS} fps`, run.run_number && `run ${run.run_number}`].filter(Boolean).join(" · ") || "Recording" }),
      h("h2", { text: info.video.name }),
      h("p", { class: "path", text: rec.video })),
    tabs,
    h("div", { id: "tab" }));
  const box = $("tab");
  if (route.tab === "details") detailsTab(box, info);
  else if (route.tab === "regions") regionsTab(box, info);
  else if (route.tab === "analyse") analyseTab(box, info);
  else resultsTab(box, info);
}

// -- details --

async function detailsTab(box, info) {
  const rec = info.recording;
  const run = info.run;
  const v = info.video;
  const patch = async (changes) => { if (await act(() => api("PATCH", `recordings/${enc(rec.id)}`, changes))) { await refreshState(); render(); } };

  const current = rec.run_log
    ? (run.error
      ? h("div", { class: "alert" }, h("p", { text: `This run log cannot be read: ${run.error}` }))
      : h("dl", { class: "vals" },
          h("dt", { text: "Run log" }), h("dd", { text: rec.run_log }),
          h("dt", { text: "Run" }), h("dd", { text: [run.run_number, run.run_id].filter(Boolean).join(" · ") }),
          h("dt", { text: "Slate" }), h("dd", { text: Object.entries(run.slate || {}).map(([k, val]) => `${k} ${val}`).join(" · ") || "–" }),
          h("dt", { text: "Clock" }), h("dd", { text: `${run.refresh_hz} Hz source, ${run.tick_ms.toFixed(3)} ms per tick` })))
    : h("p", { text: "No run log attached. Pick one below so the results carry the slate and the run's settings, or choose the rate set by hand." });

  const choices = h("div", { class: "choice", role: "radiogroup" });
  const useBtn = h("button", { class: "btn primary", type: "button", disabled: true }, "Use selected run log");
  const logs = await act(() => api("GET", `runlogs?${new URLSearchParams({ recording: rec.id })}`));
  const list = logs?.runlogs || [];
  for (const [i, l] of list.entries()) {
    const radio = h("input", { type: "radio", name: "runlog", value: l.path });
    radio.addEventListener("change", () => { useBtn.disabled = false; });
    const near = l.delta_s != null ? ` · ${ago(l.delta_s)} from this video's time` : "";
    choices.append(h("label", {}, radio, h("span", {},
      h("strong", { text: `Run ${l.run_number ?? "?"}${l.camera ? ` · ${l.camera}` : ""}${l.fps ? ` · ${l.fps} fps` : ""}` }),
      i === 0 && l.delta_s != null ? h("span", { class: "pill ok", text: "closest" }) : null,
      h("small", { text: `${when(l.started)} · ${l.tc_rate} fps / ${l.refresh_hz} Hz${near}` }))));
  }
  useBtn.addEventListener("click", () => {
    const sel = choices.querySelector("input:checked");
    if (sel) patch({ run_log: sel.value });
  });

  const rateSel = h("select", { "aria-label": "Rate set" },
    h("option", { value: "", text: "From the run log" }),
    h("option", { value: "50", text: "50Hz regions · 100Hz source, 100fps capture" }),
    h("option", { value: "60", text: "60Hz regions · 120Hz source, 120fps capture" }));
  rateSel.value = rec.rate_set || "";
  rateSel.addEventListener("change", () => patch({ rate_set: rateSel.value || null }));
  const fpsIn = h("input", { type: "number", min: "1", max: "1000", step: "1", placeholder: "from rate set", value: rec.capture_fps ?? "" });
  fpsIn.addEventListener("change", () => patch({ capture_fps: fpsIn.value ? Number(fpsIn.value) : null }));

  const setupSel = h("select", { "aria-label": "Regions setup" }, h("option", { value: "", text: "None yet: mark them" }), ...app.setups.map((s) => h("option", { value: s, text: s })));
  setupSel.value = rec.setup || "";
  setupSel.addEventListener("change", () => patch({ setup: setupSel.value || null }));

  box.replaceChildren(h("div", { class: "split" },
    h("div", { class: "stack" },
      card("Run log", current,
        list.length ? h("div", {}, h("div", { class: "sub", text: "Run logs found (workspace, and next to the video)" }), choices, useBtn) : h("p", { class: "empty", text: "No run logs found in the workspace or next to the video. Fetch them from the generator or pick a file." }),
        h("div", { class: "row" },
          h("button", { class: "btn", type: "button", onclick: () => generatorDialog(rec.id) }, "Fetch from generator…"),
          h("button", { class: "btn", type: "button", onclick: () => pickFile("runlog", "Pick a run log", async (p) => { closeDialog(); await patch({ run_log: p }); }) }, "Pick a file…"),
          rec.run_log ? h("button", { class: "btn", type: "button", onclick: () => patch({ run_log: null }) }, "Detach") : null)),
      card("Without a run log",
        h("div", { class: "form" }, h("label", {}, "Rate set", rateSel), h("label", {}, "Capture rate (fps)", fpsIn)),
        h("p", { class: "hint", text: "The capture rate is the real rate the S5 recorded at (100 or 120fps), not the file's playback rate. Leave blank to use the rate set's." }))),
    h("div", { class: "stack" },
      card("Video",
        h("dl", { class: "vals" },
          h("dt", { text: "Frames" }), h("dd", { text: String(v.frames) }),
          h("dt", { text: "Size" }), h("dd", { text: `${v.width} × ${v.height}` }),
          h("dt", { text: "File rate" }), h("dd", { text: `${v.file_fps} fps (playback)` }),
          h("dt", { text: "File" }), h("dd", { text: `${bytes(v.bytes)} · ${when(v.modified)}` }))),
      card("Regions",
        h("div", { class: "form" }, h("label", {}, "Setup", setupSel)),
        h("p", { class: "hint", text: "A setup holds the two strip regions for one camera position. Reuse it for every recording made from that position." }),
        h("div", { class: "row" }, h("a", { class: "btn primary", href: `#/recording/${enc(rec.id)}/regions` }, rec.setup ? "Check or adjust regions" : "Mark regions"))),
      card("Remove",
        h("p", { class: "hint", text: "Removes the recording and its results from the workspace. The video file is not touched." }),
        h("div", { class: "row" }, h("button", { class: "btn danger", type: "button", onclick: async () => {
          if (!confirm(`Remove ${v.name} from the workspace?`)) return;
          if (await act(() => api("DELETE", `recordings/${enc(rec.id)}`))) { await refreshState(); go("#/"); }
        } }, "Remove recording"))))));
}

// -- regions --

function regionsTab(box, info) {
  const rec = info.recording;
  const v = info.video;
  const view = h("canvas", { class: "view", "aria-label": "Video frame. Click the corners of a strip." });
  const loupe = h("canvas", { class: "loupe", width: "200", height: "200", hidden: true });
  const slider = h("input", { type: "range", min: "0", max: String(Math.max(0, v.frames - 1)), value: "0", "aria-label": "Frame" });
  const num = h("input", { type: "number", min: "0", max: String(Math.max(0, v.frames - 1)), value: "0", "aria-label": "Frame number" });
  const modeSeg = h("div", { class: "seg", role: "group", "aria-label": "Region being marked" },
    h("button", { class: "btn on", type: "button", value: "source" }, "Source monitor"),
    h("button", { class: "btn", type: "button", value: "wall" }, "LED wall"));
  const status = { source: h("p", { text: "Not marked." }), wall: h("p", { text: "Not marked." }) };
  const detail = { source: h("p", { class: "hint" }), wall: h("p", { class: "hint" }) };
  const latency = h("p", { class: "hint mono" });
  const defaultName = rec.setup || info.run.slate?.CAMERA || `setup-${app.setups.length + 1}`;
  const nameIn = h("input", { type: "text", value: defaultName.replace(/[^A-Za-z0-9 _.-]/g, "-").slice(0, 64), maxlength: "64" });
  const saveBtn = h("button", { class: "btn primary", type: "button", disabled: true }, "Save regions");
  const saved = h("div", { class: "alert ok", hidden: true }, h("p", {}));

  const pts = { source: info.setup?.source || [], wall: info.setup?.wall || [] };
  let mode = "source";
  let frame = info.setup?.marked_on_frame ?? 0;
  let img = new Image();
  let drag = null;
  let timer = null;
  const ZOOM = 6;

  function setMode(m) {
    mode = m;
    for (const b of modeSeg.children) b.classList.toggle("on", b.value === m);
  }
  function loadFrame(n) {
    frame = Math.max(0, Math.min(n, v.frames - 1));
    slider.value = num.value = frame;
    const next = new Image();
    next.onload = () => { img = next; draw(); schedule(); };
    next.src = link(`recordings/${enc(rec.id)}/frame`, { n: frame });
  }
  function draw() {
    if (view.width !== v.width) { view.width = v.width; view.height = v.height; }
    const ctx = view.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const lw = Math.max(2, v.width / 640);
    const col = COLOURS();
    for (const name of ["source", "wall"]) {
      const p = pts[name];
      if (!p.length) continue;
      ctx.strokeStyle = ctx.fillStyle = col[name];
      ctx.lineWidth = lw;
      ctx.beginPath();
      p.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      if (p.length === 4) ctx.closePath();
      ctx.stroke();
      for (const [x, y] of p) { ctx.beginPath(); ctx.arc(x, y, lw * 2.5, 0, Math.PI * 2); ctx.fill(); }
    }
  }
  function toImage(e) {
    const r = view.getBoundingClientRect();
    return [(e.clientX - r.left) * (view.width / r.width), (e.clientY - r.top) * (view.height / r.height)];
  }
  function loopOrder(p) {
    const cx = p.reduce((s, q) => s + q[0], 0) / 4;
    const cy = p.reduce((s, q) => s + q[1], 0) / 4;
    return [...p].sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
  }
  function nearest(x, y) {
    // While placing corners every click adds one: a thin strip's corners sit close together.
    if (pts[mode].length > 0 && pts[mode].length < 4) return null;
    const reach = 10 * (view.width / view.getBoundingClientRect().width);
    for (const name of ["source", "wall"]) {
      if (pts[name].length !== 4) continue;
      const i = pts[name].findIndex(([px, py]) => Math.hypot(px - x, py - y) < reach);
      if (i >= 0) return { name, i };
    }
    return null;
  }
  function showLoupe(x, y, e) {
    loupe.hidden = false;
    const r = view.getBoundingClientRect();
    loupe.classList.toggle("left", e.clientX - r.left > r.width * 0.6);
    const ctx = loupe.getContext("2d");
    const s = loupe.width / ZOOM;
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, loupe.width, loupe.height);
    ctx.drawImage(view, x - s / 2, y - s / 2, s, s, 0, 0, loupe.width, loupe.height);
    ctx.strokeStyle = "#ff4453";
    ctx.beginPath();
    ctx.moveTo(loupe.width / 2, 0); ctx.lineTo(loupe.width / 2, loupe.height);
    ctx.moveTo(0, loupe.height / 2); ctx.lineTo(loupe.width, loupe.height / 2);
    ctx.stroke();
  }
  view.addEventListener("pointerdown", (e) => {
    const [x, y] = toImage(e);
    const hit = nearest(x, y);
    if (hit) { drag = hit; view.setPointerCapture(e.pointerId); return; }
    if (pts[mode].length >= 4) pts[mode] = [];
    pts[mode].push([x, y]);
    if (pts[mode].length === 4) {
      pts[mode] = loopOrder(pts[mode]);
      if (mode === "source" && pts.wall.length < 4) setMode("wall");
    }
    draw();
    showLoupe(x, y, e);
    schedule();
  });
  view.addEventListener("pointermove", (e) => {
    const [x, y] = toImage(e);
    if (drag) { pts[drag.name][drag.i] = [x, y]; draw(); schedule(); }
    showLoupe(x, y, e);
  });
  view.addEventListener("pointerup", () => { drag = null; });
  view.addEventListener("pointerleave", () => { if (!drag) loupe.hidden = true; });

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(check, 200);
  }
  function describe(name, r) {
    if (!r) {
      status[name].textContent = pts[name].length ? `${pts[name].length} of 4 corners.` : "Not marked.";
      detail[name].textContent = "";
      return false;
    }
    if (r.error) { status[name].textContent = r.error; detail[name].textContent = ""; return false; }
    const word = { ok: "Reads", transition: "Caught mid-change", unreadable: "Cannot read" }[r.status];
    status[name].textContent = r.status === "ok" ? `${word}: count ${r.count}${r.reversed ? " (reversed)" : ""}` : `${word}${r.reason ? `: ${r.reason}` : ""}`;
    detail[name].textContent = `${r.ok_frames} of ${r.checked} frames from here read cleanly.`;
    return r.ok_frames >= Math.ceil(r.checked / 2);
  }
  async function check() {
    const out = await act(() => api("POST", `recordings/${enc(rec.id)}/check`, {
      frame, source: pts.source.length === 4 ? pts.source : null, wall: pts.wall.length === 4 ? pts.wall : null,
    }));
    if (!out) return;
    const a = describe("source", out.source);
    const b = describe("wall", out.wall);
    const lat = out.latency_ticks || [];
    latency.textContent = lat.length ? `Latency over the next frames, in ticks: ${lat.join(" ")}` : "";
    saveBtn.disabled = !(a && b);
  }
  saveBtn.addEventListener("click", async () => {
    const out = await act(() => api("POST", `recordings/${enc(rec.id)}/regions`, { frame, source: pts.source, wall: pts.wall, name: nameIn.value }));
    if (!out) return;
    pts.source = out.setup.source;
    pts.wall = out.setup.wall;
    draw();
    check();
    const tab = document.querySelector('.tabs a[href$="/regions"]');
    if (tab) { tab.classList.add("done"); tab.querySelector(".n").textContent = "✓"; }
    saved.hidden = false;
    saved.replaceChildren(h("p", { text: `Saved as setup "${out.name}". After snapping to the cells the source reads cleanly in ${out.clean.source} of ${out.checked} frames and the wall in ${out.clean.wall} (frames caught mid-change do not count).` }),
      h("div", { class: "row" }, h("a", { class: "btn primary", href: `#/recording/${enc(rec.id)}/analyse` }, "Next: analyse")));
    refreshState();
  });
  for (const b of modeSeg.children) b.addEventListener("click", () => setMode(b.value));
  slider.addEventListener("input", () => loadFrame(Number(slider.value)));
  num.addEventListener("change", () => loadFrame(Number(num.value)));
  cleanup.push(() => clearTimeout(timer));

  const clear = (name) => h("button", { class: "btn small", type: "button", onclick: () => { pts[name] = []; setMode(name); draw(); schedule(); } }, "Clear");
  box.replaceChildren(h("div", { class: "split" },
    card("Frame",
      h("div", { class: "stage" }, view, loupe),
      h("div", { class: "frame-row" },
        h("button", { class: "btn small", type: "button", "aria-label": "Previous frame", onclick: () => loadFrame(frame - 1) }, "◀"),
        slider,
        h("button", { class: "btn small", type: "button", "aria-label": "Next frame", onclick: () => loadFrame(frame + 1) }, "▶"),
        num)),
    h("div", { class: "stack" },
      card("How to mark", h("ol", { class: "how" },
        h("li", { text: "Pick a frame where both strips are clear." }),
        h("li", { text: "Choose a region, then click the four corners of that strip, in any order. The white cells at each end show where it starts and stops." }),
        h("li", { text: "Drag a corner to adjust it; the magnifier helps place it on the edge." }),
        h("li", { text: "When both regions read, name the setup and save. Saving snaps the ends onto the cells." }))),
      card("Regions", modeSeg,
        h("div", { class: "region source" }, h("h3", {}, "Source monitor", clear("source")), status.source, detail.source),
        h("div", { class: "region wall" }, h("h3", {}, "LED wall", clear("wall")), status.wall, detail.wall),
        latency,
        h("div", { class: "form mt-s" }, h("label", {}, "Setup name", nameIn)),
        h("div", { class: "toolbar mt-s" }, saveBtn),
        saved))));
  loadFrame(frame);
}

// -- analyse --

function analyseTab(box, info) {
  const rec = info.recording;
  const ready = info.setup && !info.run.error && (rec.run_log || rec.rate_set || true);
  const startIn = h("input", { type: "number", min: "0", step: "0.1", placeholder: "start" });
  const endIn = h("input", { type: "number", min: "0", step: "0.1", placeholder: "end" });
  const btn = h("button", { class: "btn primary", type: "button", disabled: !ready }, info.summary ? "Analyse again" : "Analyse");
  btn.addEventListener("click", async () => {
    const body = {};
    if (startIn.value) body.start_s = Number(startIn.value);
    if (endIn.value) body.end_s = Number(endIn.value);
    if (await act(() => api("POST", `recordings/${enc(rec.id)}/analyse`, body))) { await refreshState(); updateProgress(); }
  });
  const missing = [];
  if (!info.setup) missing.push(h("li", {}, "Regions are not marked yet. ", h("a", { href: `#/recording/${enc(rec.id)}/regions`, text: "Mark them" })));
  if (info.run.error) missing.push(h("li", { text: `The run log cannot be read: ${info.run.error}` }));
  if (!rec.run_log) missing.push(h("li", {}, "No run log: the rate set is used without the slate or run checks. ", h("a", { href: `#/recording/${enc(rec.id)}/details`, text: "Attach one" })));
  box.replaceChildren(h("div", { class: "split" },
    card("Analyse",
      h("dl", { class: "vals" },
        h("dt", { text: "Clock" }), h("dd", { text: info.run.error ? "–" : `${info.run.refresh_hz} Hz source (${info.run.tick_ms.toFixed(3)} ms per tick)` }),
        h("dt", { text: "Capture" }), h("dd", { text: info.run.error ? "–" : `${info.run.capture_fps} fps (${(1000 / info.run.capture_fps).toFixed(3)} ms per frame)` }),
        h("dt", { text: "Regions" }), h("dd", { text: rec.setup || "–" }),
        h("dt", { text: "Frames" }), h("dd", { text: String(info.video.frames) })),
      missing.length ? h("ul", { class: "checks mt-s" }, missing.map((m) => { m.className = "warn"; return m; })) : null,
      h("div", { class: "form mt-s" }, h("label", {}, "From (s, real time)", startIn), h("label", {}, "To (s, real time)", endIn)),
      h("p", { class: "hint", text: "Leave blank to analyse the whole recording." }),
      h("div", { class: "toolbar mt-s" }, btn)),
    card("Progress", h("div", { id: "progress-box" }))));
  updateProgress();
}

function updateProgress() {
  const box = $("progress-box");
  if (!box || !app) return;
  const jobs = app.jobs.filter((j) => j.recording === route.id && j.kind === "analyse");
  const job = jobs.at(-1);
  if (!job) { box.replaceChildren(h("p", { class: "empty", text: "Not started." })); return; }
  const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
  const active = job.state === "running" || job.state === "queued";
  box.replaceChildren(
    h("p", {}, h("strong", { text: { queued: "Queued", running: "Analysing", done: "Finished", failed: "Failed", cancelled: "Cancelled" }[job.state] }),
      job.total ? ` · frame ${job.done} of ${job.total}` : ""),
    h("div", { class: "progress", role: "progressbar", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" }, h("div", { style: `width:${job.state === "done" ? 100 : pct}%` })),
    job.message ? h("p", { class: "hint", text: job.message }) : null,
    active ? h("button", { class: "btn danger", type: "button", onclick: () => act(() => api("POST", `jobs/${job.id}/cancel`)) }, "Cancel")
      : job.state === "done" ? h("a", { class: "btn primary", href: `#/recording/${enc(route.id)}/results` }, "See results") : null);
}

// -- results --

async function resultsTab(box, info) {
  const rec = info.recording;
  if (!info.summary) {
    box.replaceChildren(card("Results", h("p", { class: "empty", text: "Not analysed yet." }), h("div", { class: "row" }, h("a", { class: "btn primary", href: `#/recording/${enc(rec.id)}/analyse` }, "Analyse"))));
    return;
  }
  const res = await act(() => api("GET", `recordings/${enc(rec.id)}/results`));
  if (!res) return;
  const s = res.summary;
  const c = s.counts;
  const rows = res.rows;
  const reviewImg = h("img", { alt: "Selected frame with both regions marked" });
  const vals = h("dl", { class: "vals" });
  const reviewTitle = h("p", { class: "hint", text: "Click a dot, a flag tick or a table row to look at that frame." });
  let current = null;

  async function review(n) {
    n = Math.max(0, Math.min(n, info.video.frames - 1));
    current = n;
    reviewImg.src = link(`recordings/${enc(rec.id)}/frame`, { n, overlay: 1 });
    reviewTitle.textContent = `Frame ${n}`;
    for (const tr of table.querySelectorAll("tr.sel")) tr.classList.remove("sel");
    table.querySelector(`tr[data-frame="${n}"]`)?.classList.add("sel");
    const d = await act(() => api("GET", `recordings/${enc(rec.id)}/sample?${new URLSearchParams({ n })}`));
    if (!d || current !== n) return;
    vals.replaceChildren(
      h("dt", { text: "Status" }), h("dd", { text: d.status + (d.note ? ` (${d.note})` : "") }),
      h("dt", { text: "Latency" }), h("dd", { text: d.latency_ms !== null && d.latency_ms !== "" && d.latency_ms != null ? `${Number(d.latency_ms).toFixed(1)} ms (${d.latency_ticks} ticks)` : "–" }),
      h("dt", { text: "Source" }), h("dd", { text: d.source_tc ? `${d.source_tc} · ${d.source_count}${d.source_reversed ? " · reversed" : ""}` : d.source_reason || "–" }),
      h("dt", { text: "Wall" }), h("dd", { text: d.wall_tc ? `${d.wall_tc} · ${d.wall_count}${d.wall_reversed ? " · reversed" : ""}` : d.wall_reason || "–" }),
      h("dt", { text: "Time" }), h("dd", { text: `${d.time_s} s` }));
  }
  function nextFlagged(dir) {
    const flagged = rows.filter((r) => r.status !== "ok").map((r) => Number(r.frame));
    if (!flagged.length) return;
    const from = current ?? -1;
    const next = dir > 0 ? flagged.find((f) => f > from) ?? flagged[0] : [...flagged].reverse().find((f) => f < from) ?? flagged.at(-1);
    review(next);
  }

  const filterSeg = h("div", { class: "seg", role: "group", "aria-label": "Show" },
    ...[["all", "All"], ["ok", "Good"], ["flagged", "Flagged"]].map(([v, t]) => h("button", { class: `btn small ${v === "all" ? "on" : ""}`, type: "button", value: v }, t)));
  const table = h("table", { class: "data clickable" });
  function fillTable(filter) {
    const shown = rows.filter((r) => filter === "all" || (filter === "ok" ? r.status === "ok" : r.status !== "ok"));
    table.replaceChildren(
      h("thead", {}, h("tr", {}, ...["Frame", "Time s", "Source", "Wall", "Latency", "Status"].map((t, i) => h("th", { class: i < 2 || i === 4 ? "r" : "", text: t })))),
      h("tbody", {}, shown.slice(0, 5000).map((r) => h("tr", { dataset: { frame: r.frame }, class: Number(r.frame) === current ? "sel" : "", onclick: () => review(Number(r.frame)) },
        h("td", { class: "r mono", text: r.frame }), h("td", { class: "r mono", text: r.time_s }),
        h("td", { class: "mono", text: r.source_tc }), h("td", { class: "mono", text: r.wall_tc }),
        h("td", { class: "r mono", text: r.latency_ms ? Number(r.latency_ms).toFixed(1) : "" }),
        h("td", { text: r.status + (r.note ? ` · ${r.note}` : "") })))));
  }
  for (const b of filterSeg.children) b.addEventListener("click", () => {
    for (const x of filterSeg.children) x.classList.toggle("on", x === b);
    fillTable(b.value);
  });
  fillTable("all");

  const chart = (svg) => {
    const d = h("div", { class: "card" });
    d.innerHTML = svg; // built server-side with escaped values; the CSP blocks scripts
    d.addEventListener("click", (e) => {
      const f = e.target.closest("[data-frame]");
      if (f) review(Number(f.dataset.frame));
    });
    return d;
  };
  const checks = [
    [c.ok, "good samples used for the figures", false],
    [c.transition, "frames with a strip caught mid-change (left out)", true],
    [c.unreadable, "frames where a strip could not be read", true],
    [c.suspect, "frames that decoded but look wrong (negative latency, or outside the run)", true],
    [s.source_breaks, "places where the source strip did not advance as expected", true],
    [s.generator_events, "dropped or repeated generator frames during this recording", true],
  ];
  const fileLink = (name, label, download) => h("a", { class: "btn", href: link(`recordings/${enc(rec.id)}/files/${name}`, download ? { download: 1 } : {}), target: download ? null : "_blank", rel: "noopener" }, label);

  box.replaceChildren(
    h("section", { class: "keyfacts" },
      kf(ms(s.mean_ms), "Mean latency"), kf(ms(s.median_ms), "Median"),
      kf(`${s.min_ms ?? "–"}–${ms(s.max_ms)}`, "Min to max"),
      kf(String(s.n), `Good samples of ${s.frames} frames${s.stdev_ms != null ? ` · σ ${ms(s.stdev_ms)}` : ""}`)),
    h("div", { class: "toolbar" },
      fileLink("report.html", "Open report", false), fileLink("results.csv", "Download CSV", true), fileLink("summary.json", "Download summary", true),
      h("a", { class: "btn", href: `#/recording/${enc(rec.id)}/analyse` }, "Analyse again")),
    h("div", { class: "split mt-s" },
      h("div", { class: "stack" },
        h("div", {}, h("h2", { class: "section", text: "Latency over time" }), chart(res.charts.scatter)),
        h("div", {}, h("h2", { class: "section", text: "Distribution" }), chart(res.charts.histogram)),
        card("Checks", h("ul", { class: "checks" }, checks.map(([n, t, warn]) => h("li", { class: warn && n ? "warn" : "" }, h("strong", { text: String(n) }), t)))),
        card("Samples", filterSeg, h("div", { class: "tablewrap tall mt-s" }, table))),
      h("div", { class: "stack review", style: "position:sticky;top:12px" },
        card("Review", reviewTitle, reviewImg, vals,
          h("div", { class: "row" },
            h("button", { class: "btn small", type: "button", onclick: () => review((current ?? 0) - 1) }, "◀ Frame"),
            h("button", { class: "btn small", type: "button", onclick: () => review((current ?? -1) + 1) }, "Frame ▶"),
            h("button", { class: "btn small", type: "button", onclick: () => nextFlagged(-1) }, "◀ Flagged"),
            h("button", { class: "btn small", type: "button", onclick: () => nextFlagged(1) }, "Flagged ▶"))))));
  const first = rows.find((r) => r.status === "ok");
  if (first) review(Number(first.frame));
}

// ---- start -----------------------------------------------------------------------------

themeControl($("theme"));
$("error-close").addEventListener("click", () => showError(null));
window.addEventListener("hashchange", render);
const poll = async () => {
  await refreshState();
  setTimeout(poll, activeJobs().length ? 700 : 3000);
};
poll().then(render);

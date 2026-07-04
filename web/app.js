/* =========================================================================
   osu!collector-gui — frontend controller
   Talks to the Python engine via window.pywebview.api.*, receives progress
   through window.ocOnEvent(). Vanilla JS, no build step.
   ========================================================================= */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  labels: { default_target: "", new_target: "", no_merge: "Don't merge" },
  collections: [],   // scanned lazer collections
  running: false,
  totalCollections: 0,
  lastFile: "",
};

let api = null;               // window.pywebview.api once ready
const pending = [];           // calls queued before the bridge is ready

function callApi(method, ...args) {
  if (api && typeof api[method] === "function") return api[method](...args);
  return new Promise((resolve) => pending.push({ method, args, resolve }));
}

/* ----------------------------------------------------------------- boot */
window.addEventListener("pywebviewready", onBridgeReady);
// Fallback: if the bridge is already there (fast load), wire up immediately.
document.addEventListener("DOMContentLoaded", () => {
  wireStaticUi();
  if (window.pywebview && window.pywebview.api) onBridgeReady();
});

function onBridgeReady() {
  if (api) return;
  api = window.pywebview.api;
  pending.splice(0).forEach((p) => p.resolve(api[p.method](...p.args)));
  init();
}

async function init() {
  let st;
  try {
    st = await api.get_state();
  } catch (e) {
    return;
  }
  applyState(st);
  // Auto-scan the osu! folder for existing collections (no button needed).
  scanCollections();
  // Quietly check GitHub for a newer release.
  checkUpdate();
}

async function checkUpdate() {
  let r;
  try { r = await api.check_update(); } catch (e) { return; }
  if (r && r.update) {
    state.update = r;
    const p = $("#update-pill");
    p.textContent = "⬆ Update to v" + r.latest;
    p.classList.remove("hidden");
  }
}

/* --------------------------------------------------------- apply state */
function applyState(st) {
  state.labels = st.labels || state.labels;
  $("#version").textContent = "v" + st.version + " · by " + st.author;
  setTheme(st.theme, false);

  $("#output").value = st.output_dir || "";
  const s = st.settings || {};
  setCheck("auto_import", s.auto_import);
  setCheck("skip_video", s.skip_video);
  setCheck("skip_already_imported", s.skip_already_imported);
  setCheck("restart_lazer_after", s.restart_lazer_after);
  setCheck("generate_osdb", s.generate_osdb);
  setCheck("consolidate_osdb", s.consolidate_osdb);
  setCheck("cleanup_after_import", s.cleanup_after_import);
  setVal("download_parallel", s.download_parallel);
  setVal("import_parallel", s.import_parallel);
  setVal("import_delay_ms", s.import_delay_ms);
  refreshTunePresetActive();
  setVal("osu_binary", s.osu_binary);
  setVal("lazer_realm_path", s.lazer_realm_path);
  setVal("cm_cli_command", s.cm_cli_command);
  setVal("custom_mirrors", s.custom_mirrors);

  renderDetected(st.detected || {});
  buildTargetOptions(st.target);
  refreshGo();
}

function setCheck(id, v) { const el = $("#" + id); if (el) el.checked = !!v; }
function setVal(id, v) { const el = $("#" + id); if (el && v != null) el.value = v; }

function renderDetected(d) {
  const row = (sel, ok, path) => {
    const el = $(sel);
    const pill = el.querySelector(".pill");
    pill.classList.toggle("ok", ok);
    pill.classList.toggle("bad", !ok);
    el.querySelector(".path").textContent = ok ? path : "not found";
  };
  row("#d-osu", d.osu_detected, d.osu_binary);
  row("#d-realm", d.realm_detected, d.realm_path);
  row("#d-cm", d.cm_detected, d.cm_cli_command);
  const n = [d.osu_detected, d.realm_detected, d.cm_detected].filter(Boolean).length;
  $("#foot-detect").textContent = n + "/3 detected";
}

/* -------------------------------------------------- target collections */
function buildTargetOptions(selected) {
  const sel = $("#target");
  sel.innerHTML = "";
  const add = (label, value) => {
    const o = document.createElement("option");
    o.textContent = label; o.value = value != null ? value : label;
    sel.appendChild(o);
  };
  add("One collection per osu!collector collection", state.labels.default_target);
  state.collections.forEach((c) =>
    add(`${c.name}  (${c.count} maps)`, c.name));
  add("➕ Create new collection…", state.labels.new_target);
  add("Don't add to any collection", state.labels.no_merge);
  if (selected) {
    const match = Array.from(sel.options).find((o) => o.value === selected);
    if (match) sel.value = selected;
  }
  onTargetChange();
}

async function scanCollections() {
  let res;
  try { res = await api.scan_collections(); } catch (e) { return; }
  if (res && res.ok && res.collections && res.collections.length) {
    state.collections = res.collections;
    buildTargetOptions($("#target").value);
    buildExportTargets();
    toast("Found " + res.collections.length + " osu!lazer collection(s)", "ok",
          "// scan complete");
  }
}

function onTargetChange() {
  const wrap = $("#new-name-wrap");
  wrap.classList.toggle("hidden", $("#target").value !== state.labels.new_target);
}

/* ---------------------------------------------------------- preview posters */
let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 450);
}

async function runPreview() {
  const text = $("#ids").value;
  const box = $("#posters");
  if (!text.trim()) { box.innerHTML = ""; return; }
  let res;
  try { res = await api.preview(text); } catch (e) { return; }
  box.innerHTML = "";
  (res.collections || []).forEach((c, i) => {
    const el = document.createElement("div");
    el.className = "poster";
    el.style.animationDelay = (i * 60) + "ms";
    if (c.error) {
      el.classList.add("err");
      el.textContent = "// collection " + c.id + " — " + c.error;
    } else {
      const kicker = c.count != null ? `// ${c.count} maps` : `// collection`;
      const by = c.uploader ? `by ${escapeHtml(c.uploader)} · #${c.id}` : `#${c.id}`;
      el.innerHTML =
        (c.cover ? `<div class="bg" style="background-image:url('${c.cover}')"></div><div class="duo"></div>` : "") +
        `<div class="p-kicker">${kicker}</div>` +
        `<div class="p-name">${escapeHtml(c.name)}</div>` +
        `<div class="p-by">${by}</div>`;
    }
    box.appendChild(el);
  });
}

/* ------------------------------------------------------------ collect + go */
function collectSettings() {
  return {
    auto_import: $("#auto_import").checked,
    skip_video: $("#skip_video").checked,
    skip_already_imported: $("#skip_already_imported").checked,
    restart_lazer_after: $("#restart_lazer_after").checked,
    generate_osdb: $("#generate_osdb").checked,
    consolidate_osdb: $("#consolidate_osdb").checked,
    cleanup_after_import: $("#cleanup_after_import").checked,
    download_parallel: parseInt($("#download_parallel").value || "16", 10),
    import_parallel: parseInt($("#import_parallel").value || "1", 10),
    import_delay_ms: parseInt($("#import_delay_ms").value || "0", 10),
    osu_binary: $("#osu_binary").value,
    lazer_realm_path: $("#lazer_realm_path").value,
    cm_cli_command: $("#cm_cli_command").value,
    custom_mirrors: $("#custom_mirrors").value,
  };
}

/* ---------------------------------------------------- tuning speed presets */
// Each preset sets the three tuning fields. "Max speed" stops at 32 because
// the download executor is hard-clamped to 32 workers (higher does nothing).
const TUNE_PRESETS = {
  gentle:   { download_parallel: 6,  import_parallel: 1, import_delay_ms: 100 },
  balanced: { download_parallel: 16, import_parallel: 1, import_delay_ms: 0 },
  maxspeed: { download_parallel: 32, import_parallel: 1, import_delay_ms: 0 },
};

// Highlight the chip whose values match the current fields (or none).
function refreshTunePresetActive() {
  const cur = {
    download_parallel: parseInt($("#download_parallel").value || "0", 10),
    import_parallel: parseInt($("#import_parallel").value || "0", 10),
    import_delay_ms: parseInt($("#import_delay_ms").value || "0", 10),
  };
  $$("#tune-presets .tune-preset").forEach((btn) => {
    const p = TUNE_PRESETS[btn.dataset.preset];
    const match = p && p.download_parallel === cur.download_parallel
      && p.import_parallel === cur.import_parallel
      && p.import_delay_ms === cur.import_delay_ms;
    btn.classList.toggle("active", !!match);
  });
}

// Apply a preset to the fields, reflect it, and persist immediately.
async function applyTunePreset(name) {
  const p = TUNE_PRESETS[name];
  if (!p) return;
  setVal("download_parallel", p.download_parallel);
  setVal("import_parallel", p.import_parallel);
  setVal("import_delay_ms", p.import_delay_ms);
  refreshTunePresetActive();
  await saveSettings();
}

// Save the settings panel (shared by the Save button + the speed presets).
async function saveSettings() {
  const r = await callApi("save_settings", {
    output_dir: $("#output").value,
    target: $("#target").value,
    new_collection_name: $("#new-name").value,
    settings: collectSettings(),
  });
  const st = $("#save-status");
  if (r && r.ok) { st.textContent = "saved ✓"; if (r.state) renderDetected(r.state.detected || {}); }
  else st.textContent = "save failed";
  setTimeout(() => (st.textContent = ""), 2500);
  return r;
}

function refreshGo() {
  $("#go").disabled = state.running || !$("#ids").value.trim();
}

async function onGo() {
  if (state.running) return;
  const payload = {
    ids_text: $("#ids").value,
    output_dir: $("#output").value,
    target: $("#target").value,
    new_collection_name: $("#new-name").value,
    settings: collectSettings(),
  };
  let res;
  try { res = await api.start(payload); } catch (e) {
    toast(String(e), "bad", "// error"); return;
  }
  if (!res || !res.ok) {
    toast((res && res.error) || "Couldn't start.", "bad", "// can't start");
    return;
  }
  if (res.warning) toast(res.warning, "bad", "// heads up");
  state.running = true;
  state.totalCollections = res.count;
  state.lastFile = "";
  $("#log").textContent = "";
  switchView("activity");
  showDock(true);
  $("#dock-title").textContent = "Starting…";
  $("#dock-count").textContent = "";
  $("#dock-file").textContent = "";
  setFill(2);
  $("#activity-badge").classList.remove("hidden");
  refreshGo();
}

/* ----------------------------------------------------------- events in */
function onCmSetup(d) {
  const status = $("#cm-install-status");
  const btn = $("#install-cm");
  if (d.line) {
    if (status) status.textContent = d.line;
    appendLog(d.line, "l-dim");
  }
  if (d.done) {
    if (btn) btn.disabled = false;
    if (d.ok) {
      if (status) status.textContent = "Installed ✓";
      toast("Collection Manager is ready", "ok", "// setup");
      (async () => {
        try { applyState(await api.get_state()); } catch (e) {}
      })();
    } else {
      if (status) status.textContent = d.error || "Install failed";
      toast(d.error || "Collection Manager install failed", "bad", "// setup");
    }
  }
}

window.ocOnEvent = function (msg) {
  const ev = msg && msg.event;
  const d = (msg && msg.data) || {};
  switch (ev) {
    case "log": appendLog(d.line); break;
    case "prep": onPrep(d); break;
    case "collection_started": onCollectionStarted(d); break;
    case "beatmap_progress": onBeatmapProgress(d); break;
    case "collection_finished": break;
    case "awaiting_import_confirmation": showMergeModal(d.n); break;
    case "batch_finished": onBatchFinished(d); break;
    case "cm_setup": onCmSetup(d); break;
    case "error":
      appendLog("ERROR: " + d.message, "l-err");
      toast(d.message, "bad", "// error");
      break;
  }
};

function onPrep(d) {
  // Pre-download phase feedback (fetching the list / probing the library) so
  // "Starting…" isn't an opaque wait on large collections.
  if (d.title) $("#dock-title").textContent = d.title;
  if (d.detail) $("#dock-file").textContent = d.detail;
}

function onCollectionStarted(d) {
  state.curIdx = d.idx; state.curTotal = d.total || state.totalCollections;
  state.curName = d.name; state.setTotal = d.n_sets || 1; state.setDone = 0;
  $("#dock-title").textContent =
    `Collection ${d.idx}/${state.curTotal} · ${d.name}`;
  updateAggregate();
}

function onBeatmapProgress(d) {
  state.setDone = d.current; state.setTotal = d.total || 1;
  $("#dock-count").textContent = `${d.current} / ${d.total}`;
  if (state.lastFile) $("#dock-file").textContent = state.lastFile;
  updateAggregate();
}

function updateAggregate() {
  const tc = state.curTotal || state.totalCollections || 1;
  const within = state.setTotal > 0 ? state.setDone / state.setTotal : 0;
  const frac = ((state.curIdx ? state.curIdx - 1 : 0) + within) / tc;
  setFill(Math.max(2, Math.min(100, frac * 100)));
}

function onBatchFinished(d) {
  state.running = false;
  setFill(100);
  $("#dock-title").textContent = `Done · ${d.ok}/${d.total} collections succeeded`;
  $("#dock-file").textContent = "";
  $("#activity-badge").classList.add("hidden");
  refreshGo();
  if (d.ok > 0) { confetti(); toast(`${d.ok}/${d.total} collections done`, "ok", "// complete"); }
  else toast("Finished — nothing succeeded", "bad", "// complete");
  setTimeout(() => { if (!state.running) showDock(false); }, 3500);
}

/* ----------------------------------------------------------------- log */
function appendLog(line, cls) {
  if (line == null) return;
  // Pull the full ".osz" filename (with spaces) out of a "[n/total] name" line.
  const m = line.match(/\]\s+(.+\.osz)\s*$/);
  if (m) state.lastFile = m[1].trim();
  const box = $("#log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const span = document.createElement("span");
  span.className = cls || classForLine(line);
  span.textContent = line + "\n";
  box.appendChild(span);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function classForLine(line) {
  const t = line.trimStart();
  if (t.startsWith("===")) return "l-head";
  if (t.startsWith("[retry-ok]")) return "l-ok";   // recovered on retry
  if (t.startsWith("[error") || t.startsWith("ERROR") || t.includes("WARNING")) return "l-err";
  // Rate-limit / retry chatter is expected, not an error — keep it calm.
  if (t.startsWith("[skip") || t.startsWith("[cleanup") || t.startsWith("[settings")
      || t.startsWith("[retry") || t.startsWith("[rate-limited")) return "l-skip";
  if (t.includes("[probe]")) return "l-probe";
  if (/^\[\d+\/\d+\]/.test(t) || t.startsWith("[lazer] done") || t.startsWith("[.osdb]")) return "l-ok";
  return "";
}

/* ------------------------------------------------------------- merge modal */
function showMergeModal(n) {
  const scrim = document.createElement("div");
  scrim.className = "modal-scrim";
  scrim.innerHTML = `
    <div class="modal">
      <span class="kicker">// confirm before merge</span>
      <h3 style="margin-top:8px;">Has osu!lazer finished importing?</h3>
      <p>${n} map(s) were sent to osu!lazer. It imports asynchronously, so it
         may still be extracting and hashing beatmaps in the background.</p>
      <p>Open osu!lazer, wait for the import notifications to finish, then continue.</p>
      <p class="warn">Continuing while imports are in flight terminates lazer
         mid-import and those maps won't make the merged collection.</p>
      <div class="modal-actions">
        <button class="btn btn-ghost" id="merge-cancel">Cancel batch</button>
        <button class="btn btn-primary" id="merge-go">Continue merge</button>
      </div>
    </div>`;
  document.body.appendChild(scrim);
  const close = () => scrim.remove();
  scrim.querySelector("#merge-go").onclick = () => { callApi("confirm_merge", true); close(); };
  scrim.querySelector("#merge-cancel").onclick = () => { callApi("confirm_merge", false); close(); };
}

/* --------------------------------------------------------------- dock */
function showDock(on) {
  $("#dock").classList.toggle("live", on);
  document.documentElement.style.setProperty("--dock-h", on ? "76px" : "0px");
}
function setFill(pct) { $("#dock-fill").style.width = pct + "%"; }

/* -------------------------------------------------------------- toasts */
function toast(text, kind, tag) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.innerHTML = (tag ? `<div class="tt">${tag}</div>` : "") + escapeHtml(text);
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 350); }, 4200);
}

/* ------------------------------------------------------------- confetti */
function confetti() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const cv = $("#confetti"); cv.classList.remove("hidden");
  const ctx = cv.getContext("2d");
  cv.width = innerWidth; cv.height = innerHeight;
  const colors = ["#ee5a6f", "#f0c84f", "#3fc56e", "#5aa6e6", "#c45fc9"];
  const parts = Array.from({ length: 140 }, () => ({
    x: innerWidth / 2 + (Math.random() - 0.5) * 200,
    y: innerHeight * 0.35,
    vx: (Math.random() - 0.5) * 11,
    vy: Math.random() * -12 - 4,
    s: Math.random() * 6 + 3,
    c: colors[(Math.random() * colors.length) | 0],
    r: Math.random() * 6,
  }));
  let frames = 0;
  (function tick() {
    frames++;
    ctx.clearRect(0, 0, cv.width, cv.height);
    parts.forEach((p) => {
      p.vy += 0.32; p.x += p.vx; p.y += p.vy; p.r += 0.1;
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r);
      ctx.fillStyle = p.c; ctx.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * 0.5);
      ctx.restore();
    });
    if (frames < 150) requestAnimationFrame(tick);
    else { ctx.clearRect(0, 0, cv.width, cv.height); cv.classList.add("hidden"); }
  })();
}

/* -------------------------------------------------------------- theme */
function setTheme(theme, persist) {
  const t = theme === "light" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("oc-theme", t); } catch (e) {}
  if (persist) callApi("save_theme", t);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  setTheme(cur === "light" ? "dark" : "light", true);
}

/* ----------------------------------------------------------- static wiring */
function wireStaticUi() {
  $("#theme-toggle").onclick = toggleTheme;
  $("#update-pill").onclick = async () => {
    toast("Downloading update…", "", "// update");
    const r = await callApi("apply_update",
      (state.update && state.update.download_url) || "");
    if (r && r.ok && r.opened === "page")
      toast(r.message || "Opened the releases page in your browser.", "ok", "// update");
    else if (r && r.ok)
      toast("Installer launched — close this app to finish updating.", "ok", "// update");
    else
      toast((r && r.error) || "Update failed.", "bad", "// update");
  };
  $$(".nav-item").forEach((b) => (b.onclick = () => switchView(b.dataset.view)));
  $("#ids").addEventListener("input", () => { refreshGo(); schedulePreview(); });
  $("#target").addEventListener("change", onTargetChange);
  $("#go").onclick = onGo;
  $("#dock-cancel").onclick = () => { callApi("cancel"); toast("Cancelling…", "", "// cancel"); };
  const cmBtn = $("#install-cm");
  if (cmBtn) cmBtn.onclick = async () => {
    cmBtn.disabled = true;
    const st = $("#cm-install-status");
    if (st) st.textContent = "Starting…";
    const r = await callApi("install_collection_manager");
    if (r && r.ok === false) {
      cmBtn.disabled = false;
      if (st) st.textContent = r.error || "Couldn't start";
      toast(r.error || "Couldn't start the install", "bad", "// setup");
    } else {
      toast("Installing Collection Manager… watch the Activity log", "", "// setup");
    }
  };
  $("#open-folder").onclick = () => callApi("open_folder", $("#output").value);

  $("#save-settings").onclick = () => saveSettings();

  $$("#tune-presets .tune-preset").forEach((btn) => {
    btn.onclick = () => applyTunePreset(btn.dataset.preset);
  });
  ["download_parallel", "import_parallel", "import_delay_ms"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("input", refreshTunePresetActive);
  });

  $("#browse-output").onclick = async () => {
    const p = await callApi("choose_folder", $("#output").value);
    if (p) $("#output").value = p;
  };
  $("#browse-osu").onclick = async () => {
    const p = await callApi("choose_file", $("#osu_binary").value);
    if (p) { $("#osu_binary").value = p; await saveSettings(); }
  };
  $("#browse-realm").onclick = async () => {
    const p = await callApi("choose_file", $("#lazer_realm_path").value);
    if (p) { $("#lazer_realm_path").value = p; await saveSettings(); }
  };

  window.addEventListener("resize", () => {
    const cv = $("#confetti");
    if (!cv.classList.contains("hidden")) { cv.width = innerWidth; cv.height = innerHeight; }
  });
}

function switchView(name) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "activity") $("#activity-badge").classList.add("hidden");
}

/* --------------------------------------------------------------- utils */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ----------------------------------------------------- day presets (R3D) */
// Hardcoded, in weekday order. These are Red's own osu!collector collections.
const PRESETS = [
  { day: "Monday",   name: "AimSlop",             count: "15,631", id: "21994", slug: "Monday-AimSlop" },
  { day: "Tuesday",  name: "Streams",             count: "12,127", id: "21995", slug: "Tuesday-Streams" },
  { day: "Thursday", name: "Finger Control Hell", count: "8,754",  id: "21996", slug: "Thursday-Finger-Control-Hell" },
  { day: "Friday",   name: "Techy",               count: "2,087",  id: "21997", slug: "Friday-Techy" },
];

function presetLink(p) {
  return "https://osucollector.com/collections/" + p.id + "/" + p.slug;
}

// Pull a collection id out of a pasted line ("…/collections/123/…" or a bare "123").
function lineCollectionId(line) {
  line = (line || "").trim();
  const m = line.match(/collections\/(\d+)/);
  if (m) return m[1];
  return /^\d+$/.test(line) ? line : null;
}

function presetActive(p) {
  return $("#ids").value.split("\n").some((l) => lineCollectionId(l) === p.id);
}

// Tap a preset → add its link to the box if absent, remove it if already there.
function togglePreset(p) {
  const lines = $("#ids").value.split("\n");
  let next;
  if (presetActive(p)) {
    next = lines.filter((l) => lineCollectionId(l) !== p.id);
  } else {
    next = lines.filter((l) => l.trim());
    next.push(presetLink(p));
  }
  $("#ids").value = next.join("\n").replace(/^\n+/, "");
  refreshGo();
  schedulePreview();
  refreshPresetStates();
}

function refreshPresetStates() {
  $$("#presets .preset").forEach((c) => {
    const p = PRESETS[+c.dataset.idx];
    if (!p) return;
    const on = presetActive(p);
    c.classList.toggle("added", on);
    const mark = c.querySelector(".padd");
    if (mark) mark.textContent = on ? "✓" : "＋";
  });
}

function renderPresets() {
  const box = $("#presets");
  if (!box) return;
  box.innerHTML = "";
  PRESETS.forEach((p, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "preset";
    b.dataset.idx = i;
    b.innerHTML =
      '<span class="padd">＋</span>' +
      '<span class="pday">' + escapeHtml(p.day) + "</span>" +
      '<span class="pname">' + escapeHtml(p.name) + "</span>" +
      '<span class="pcount">' + p.count + " maps · #" + p.id + "</span>";
    b.onclick = () => togglePreset(p);
    box.appendChild(b);
  });
  refreshPresetStates();
}

// One-click recommended: load all four, force the per-collection target so each lands
// in its own same-named lazer collection (no merging), then start the download now.
function importAllPresets() {
  if (state.running) return;
  $("#ids").value = PRESETS.map(presetLink).join("\n");
  const sel = $("#target");
  if (sel && state.labels.default_target) {
    sel.value = state.labels.default_target;
    onTargetChange();
  }
  refreshGo();
  refreshPresetStates();
  onGo();
}

document.addEventListener("DOMContentLoaded", () => {
  renderPresets();
  const all = $("#import-all");
  if (all) all.onclick = importAllPresets;
  const ids = $("#ids");
  if (ids) ids.addEventListener("input", refreshPresetStates);
});

/* --------------------------------------------------------- export tab */
function buildExportTargets() {
  const sel = $("#export-target");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = "";
  const add = (label, value) => {
    const o = document.createElement("option");
    o.textContent = label;
    o.value = value;
    sel.appendChild(o);
  };
  add("All collections", "");
  state.collections.forEach((c) => add(`${c.name}  (${c.count} maps)`, c.name));
  if (prev) sel.value = prev;
}

async function onExport() {
  const target = $("#export-target").value;          // "" = all collections
  const fmt = $("#export-format").value || ".db";     // ".db" | ".osdb"
  const status = $("#export-status");
  const base = (target || "collections").replace(/[\\/:*?"<>|]+/g, "_");
  status.textContent = "choose where to save…";
  const dest = await callApi("choose_save_path", base + fmt);
  if (!dest) { status.textContent = ""; return; }
  status.textContent = "exporting…";
  toast("Exporting…", "", "// export");
  const r = await callApi("export_to_file", { collection: target, dest });
  if (r && r.ok) {
    status.textContent = "saved ✓";
    toast("Exported to " + r.path, "ok", "// export");
  } else {
    status.textContent = "failed";
    toast((r && r.error) || "Export failed.", "bad", "// export");
  }
  setTimeout(() => { status.textContent = ""; }, 4500);
}

document.addEventListener("DOMContentLoaded", () => {
  buildExportTargets();
  const btn = $("#export-go");
  if (btn) btn.onclick = onExport;
});

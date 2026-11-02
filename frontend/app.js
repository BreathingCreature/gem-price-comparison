/* Last-Event-ID is used on reconnect; unknown event types are ignored on purpose. */
(function () {
"use strict";
var API = window.GEM_API_BASE || "";
var R = window.GemRender;
var $ = function (id) { return document.getElementById(id); };
var gel = $; // dynamic ids are built at run time, not in the page
var MAX_FEED_LINES = 600;
var THEME_KEY = "gem-price-theme";
var POLL_MS = 2000;
var SHOP_ROWS_SHOWN = 6;
var MONOGRAM_COLORS = ["#1f57c3", "#17784a", "#9a5a00", "#7a3ea1", "#0f6b78", "#a13d2d", "#40536b", "#6b5b1f"];
var state = {
  health: null, demo: false, gates: {}, providers: null,
  runs: [], view: "start", runId: null, source: null, lastSeq: 0,
  startedAt: null, finishedAt: null, elapsedSeconds: 0, timerId: null,
  pending: [], frame: null, replayMode: false, stats: R.newStats(),
  result: null, retrying: [], runtimeWarnings: [], feed: [], feedHidden: 0,
  filter: "all", query: "", filterShopsKey: "", activeTab: "matches",
  submitting: false, active: false, queued: false, finished: false, failed: false,
  renderedResult: null, lastClock: "", lastUrl: "",
  pipelineUserToggled: false, pipelineCollapsed: false,
  shopsExpanded: false, shopRowOrder: [], shopRowEls: {},
  specsExpanded: false, providerPoll: null, providerEls: {},
  iconProbes: {}, toastTimer: null
};

function h(tag, attrs) {
  var el = document.createElement(tag);
  attrs = attrs || {};
  Object.keys(attrs).forEach(function (key) {
    var value = attrs[key];
    if (value == null) return;
    if (key === "text") el.textContent = String(value);
    else if (key === "class") el.className = String(value);
    else if (key === "dataset") {
      Object.keys(value).forEach(function (name) { el.dataset[name] = String(value[name]); });
    } else el.setAttribute(key, String(value));
  });
  for (var i = 2; i < arguments.length; i += 1) append(el, arguments[i]);
  return el;
}

function append(el, child) {
  if (child == null || child === false) return;
  if (Array.isArray(child)) { child.forEach(function (item) { append(el, item); }); return; }
  if (child.nodeType) el.appendChild(child); else el.appendChild(document.createTextNode(String(child)));
}

function clear(el) { if (!el) return; while (el.firstChild) el.removeChild(el.firstChild); }
function setText(el, text) {
  if (!el) return;
  var value = text == null ? "" : String(text);
  if (el.textContent !== value) el.textContent = value;
}
function setState(el, value) { if (el && el.dataset.state !== value) el.dataset.state = value; }

function svg(path) {
  var ns = "http://www.w3.org/2000/svg";
  var el = document.createElementNS(ns, "svg");
  el.setAttribute("viewBox", "0 0 16 16");
  el.setAttribute("aria-hidden", "true");
  el.setAttribute("class", "icon");
  var p = document.createElementNS(ns, "path");
  p.setAttribute("d", path);
  el.appendChild(p);
  return el;
}

function linkOrText(url, label, attrs) {
  var href = R.safeHref(url);
  if (href) {
    return h("a", { href: href, target: "_blank", rel: "noopener noreferrer", class: (attrs && attrs.class) || "" }, label);
  }
  return h("span", { class: (attrs && attrs.class) || "" }, label);
}

function toast(message) {
  var el = $("toast");
  setText(el, message);
  el.hidden = false;
  if (state.toastTimer) clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(function () { el.hidden = true; }, 2400);
}
function announce(message) { setText($("page-alert"), message); }

function probeIcon(src, onReady) {
  var known = state.iconProbes[src];
  if (known === "no") return;
  if (known === "ok") { onReady(); return; }
  if (known) { known.push(onReady); return; }
  var waiting = [];
  state.iconProbes[src] = waiting;
  fetch(src, { method: "HEAD" }).then(function (response) {
    var ok = response.status === 200;
    state.iconProbes[src] = ok ? "ok" : "no";
    if (ok) waiting.forEach(function (callback) { callback(); });
  }).catch(function () { state.iconProbes[src] = "no"; });
}

function siteIcon(domain) {
  var name = String(domain || "").toLowerCase().replace(/^https?:\/\//, "").split("/")[0];
  var wrap = h("span", { class: "site-icon", title: name },
    h("span", { class: "site-icon-letter", text: (name.charAt(0) || "?").toUpperCase() }));
  if (!name) return wrap;
  probeIcon(API + "/api/icon/" + encodeURIComponent(name), function () {
    if (wrap.classList.contains("has-image")) return;
    var img = h("img", { src: API + "/api/icon/" + encodeURIComponent(name), alt: "", width: "16", height: "16", loading: "lazy" });
    img.addEventListener("load", function () { wrap.classList.add("has-image"); });
    img.addEventListener("error", function () { if (img.parentNode) img.parentNode.removeChild(img); });
    wrap.appendChild(img);
  });
  return wrap;
}


function monogram(name) {
  name = String(name || "endpoint");
  var letter = (name.charAt(0) || "?").toUpperCase();
  var hash = 0;
  for (var i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) % 997;
  var tile = h("span", { class: "monogram", text: letter });
  tile.style.backgroundColor = MONOGRAM_COLORS[hash % MONOGRAM_COLORS.length];
  return tile;
}

function providerIcon(name) {
  var label = R.providerLabel(name).toLowerCase();
  var key = label.split(/[./:]/)[0] || "endpoint";
  var wrap = h("span", { class: "provider-icon" }, monogram(key));
  probeIcon("icons/" + encodeURIComponent(key) + ".svg", function () {
    if (wrap.classList.contains("has-image")) return;
    var img = h("img", { src: "icons/" + encodeURIComponent(key) + ".svg", alt: "", width: "16", height: "16" });
    img.addEventListener("load", function () {
      var old = wrap.querySelector(".monogram");
      if (old) old.remove();
      wrap.classList.add("has-image");
    });
    img.addEventListener("error", function () { if (img.parentNode) img.parentNode.removeChild(img); });
    wrap.appendChild(img);
  });
  return wrap;
}

function savedTheme() {
  try { var value = window.localStorage.getItem(THEME_KEY); return value === "light" || value === "dark" ? value : null; }
  catch (e) { return null; }
}
function currentTheme() {
  var set = document.documentElement.getAttribute("data-theme");
  if (set === "light" || set === "dark") return set;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { window.localStorage.setItem(THEME_KEY, theme); } catch (e) { /* storage is optional */ }
  var button = $("theme-toggle");
  if (button) {
    button.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    button.setAttribute("title", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
  }
}
function initTheme() {
  var theme = savedTheme();
  if (theme) { applyTheme(theme); return; }
  var dark = currentTheme() === "dark";
  var button = $("theme-toggle");
  if (button) {
    button.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    button.setAttribute("title", dark ? "Switch to light theme" : "Switch to dark theme");
  }
}

function loadHealth() {
  fetch(API + "/api/health").then(function (response) { return response.ok ? response.json() : null; })
    .then(function (body) {
      if (!body) return;
      state.health = body;
      state.demo = !!body.demo;
      state.gates = body.gates || {};
      if (body.llm_ready != null) state.llmReady = body.llm_ready;
      $("demo-badge").hidden = !state.demo;
      if (state.demo) {
        setText($("llm-strategy"), "Demo mode uses a fake LLM. Endpoint status is not used.");
        state.providers = null;
        renderProviders();
        paintSystem();
      } else {
        loadProviders();
      }
      renderLlmBanner();
    }).catch(function () { /* the page still works without health details */ });
}

function loadProviders() {
  fetch(API + "/api/llm/status").then(function (response) { return response.json(); })
    .then(function (data) {
      state.providers = data;
      renderProviders();
      if (data && data.endpoints) {
        state.llmReady = data.endpoints.filter(function (row) { return row.state === "ready"; }).length;
      }
      renderLlmBanner();
      paintSystem();
    }).catch(function () {
      state.providers = null;
      setText($("llm-strategy"), "LLM endpoint status is unavailable.");
    });
}

function renderLlmBanner() {
  var banner = $("llm-banner");
  if (!banner) return;
  banner.hidden = state.demo || state.llmReady !== 0;
}

function loadRuns() {
  fetch(API + "/api/runs").then(function (response) { return response.json(); })
    .then(function (body) { state.runs = (body && body.runs) || []; paintRecent(); })
    .catch(function () { state.runs = []; paintRecent(); });
}

function recentLabel(status) {
  if (status === "running") return "Running";
  if (status === "failed") return "Failed";
  if (status === "cached") return "Cached";
  return "Done";
}
function recentTime(run, status) {
  if (status === "running") return "starting now";
  if (run.checked_at) return R.relativeTime(run.checked_at);
  if (run.created) return R.relativeTime(new Date(run.created * 1000).toISOString());
  return "saved run";
}
function recentClass(status) {
  if (status === "running") return "is-running";
  if (status === "failed") return "is-failed";
  if (status === "cached") return "is-cached";
  return "is-done";
}

function paintRecent() {
  var list = $("recent-grid");
  var empty = $("recent-empty");
  clear(list);
  if (!state.runs.length) {
    empty.hidden = false;
    setText($("recent-note"), "Saved on this machine, newest first.");
    return;
  }
  empty.hidden = true;
  var shown = state.runs.slice(0, 12);
  setText($("recent-note"), state.runs.length > shown.length
    ? "The last " + shown.length + " of " + state.runs.length + " runs on this machine."
    : state.runs.length + " saved or live run" + (state.runs.length === 1 ? "" : "s") + ".");
  var fragment = document.createDocumentFragment();
  shown.forEach(function (run) {
    var status = R.recentState(run);
    var title = run.title || "Untitled run";
    var row = h("button", { class: "recent-row", type: "button" },
      h("span", { class: "recent-title", text: title }),
      h("span", { class: "recent-price", text: run.cheapest_price != null ? R.money(run.cheapest_price) : "No confirmed price" }),
      h("span", { class: "recent-time", text: recentTime(run, status) }),
      h("span", { class: "recent-state " + recentClass(status), text: recentLabel(status) }));
    row.title = title;
    row.addEventListener("click", function () { openSavedRun(run.id); });
    fragment.appendChild(row);
  });
  list.appendChild(fragment);
}

function showScreen(name) {
  state.view = name;
  $("start-screen").hidden = name !== "start";
  $("run-screen").hidden = name !== "run";
  $("back-btn").hidden = name !== "run";
  $("run-clock").hidden = name !== "run";
  $("run-state-label").hidden = name !== "run";
  if (name === "start") {
    $("run-again").hidden = true;
    stopClock();
    closeSource();
    stopProviderPoll();
    updateTitle();
  }
}

function formError(message) {
  var el = $("url-error");
  setText(el, message || "");
  el.hidden = !message;
}

function openLinkDialog() {
  var dialog = $("link-dialog");
  if (!dialog) { formError("Enter a link to one GeM product page."); return; }
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    dialog.setAttribute("open", "open");
  }
  $("gem-url").focus();
}

function closeLinkDialog() {
  var dialog = $("link-dialog");
  if (dialog && typeof dialog.close === "function" && dialog.open) dialog.close();
  $("gem-url").focus();
}

function startRun(url, refresh) {
  if (state.submitting) return;
  state.submitting = true;
  var button = $("run-btn");
  button.disabled = true;
  setText(button, "Starting");
  formError("");
  fetch(API + "/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gem_url: url, refresh: !!refresh })
  }).then(function (response) {
    return response.json().then(function (body) { return { ok: response.ok, body: body }; });
  }).then(function (result) {
    state.submitting = false;
    button.disabled = false;
    setText(button, "Compare");
    if (!result.ok) {
      formError(typeof result.body.detail === "string" ? result.body.detail : "The server did not accept that link.");
      return;
    }
    beginRun(result.body.run_id, result.body.status, result.body.queue_position, url);
  }).catch(function () {
    state.submitting = false;
    button.disabled = false;
    setText(button, "Compare");
    formError("Could not reach the server. Is it running?");
  });
}

function beginRun(runId, status, queuePosition, url) {
  closeSource();
  resetRunView();
  state.runId = runId;
  state.lastUrl = url || $("gem-url").value.trim();
  try { window.history.replaceState(null, "", "?run=" + encodeURIComponent(runId)); } catch (e) { /* old browsers */ }
  showScreen("run");
  state.queued = status === "queued";
  setRunStatus(status === "queued" ? queuedLabel(queuePosition) : "Running", status === "queued" ? "queued" : "running");
  announce(status === "queued" ? queuedLabel(queuePosition) + "." : "Run started.");
  updateTitle();
  attach(runId);
}

function queuedLabel(position) {
  var ahead = Number(position);
  if (!isFinite(ahead) || ahead <= 0) return "Waiting to start";
  return "Waiting, " + ahead + " run" + (ahead === 1 ? "" : "s") + " ahead";
}

function openSavedRun(runId) {
  closeSource();
  resetRunView();
  state.runId = runId;
  state.pipelineCollapsed = true;
  state.pipelineUserToggled = true;
  setPipelineCollapsed(true);
  showScreen("run");
  setRunStatus("Opening the saved run", "queued");
  announce("Opening the saved run.");
  fetch(API + "/api/runs/" + encodeURIComponent(runId)).then(function (response) {
    return response.ok ? response.json() : null;
  }).then(function (run) {
    if (!run || !run.result) {
      showRunError("That saved run could not be opened.");
      setRunStatus("Not available", "failed");
      finishClockOnly();
      return;
    }
    state.result = run.result;
    state.lastUrl = (run.result.gem && run.result.gem.url) || run.gem_url || state.lastUrl;
    $("run-again").hidden = !state.lastUrl;
    state.finished = true;
    state.failed = run.status === "failed";
    state.active = false;
    state.renderedResult = null;
    if (state.failed) setRunStatus("Failed", "failed");
    else setRunStatus(run.result.from_cache ? "Cached result" : "Saved result", "done");
    paintAll();
    replayTrace(run.result.trace_id);
    applyPipelineCollapsed();
    if (run.live && run.status !== "done" && run.status !== "failed") {
      state.finished = false;
      setRunStatus("Running", "running");
      attach(runId);
    } else {
      finishClockOnly();
    }
    try { window.history.replaceState(null, "", "?run=" + encodeURIComponent(runId)); } catch (e) { /* old browsers */ }
  }).catch(function () {
    showRunError("Could not reach the server. Is it running?");
    setRunStatus("Not available", "failed");
    finishClockOnly();
  });
}

function replayTrace(traceId) {
  if (!traceId) {
    setText($("activity-empty"), "This saved run kept no event trace.");
    $("activity-empty").hidden = false;
    return;
  }
  fetch(API + "/api/traces/" + encodeURIComponent(traceId)).then(function (response) {
    return response.ok ? response.json() : null;
  }).then(function (trace) {
    var events = (trace && trace.events) || [];
    if (!events.length) {
      setText($("activity-empty"), "This saved run kept no event trace.");
      $("activity-empty").hidden = false;
      return;
    }
    state.replayMode = true;
    events.forEach(function (event) { queueEvent(event); });
  }).catch(function () {
    setText($("activity-empty"), "The event trace for this saved run could not be read.");
    $("activity-empty").hidden = false;
  });
}

function closeSource() {
  if (state.source) { state.source.close(); state.source = null; }
  if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  $("connection-banner").hidden = true;
}
function attach(runId) { closeSource(); state.runId = runId; connect(runId, state.lastSeq); }

function connect(runId, after) {
  var suffix = after ? "?after=" + encodeURIComponent(after) : "";
  var source = new EventSource(API + "/api/runs/" + runId + "/events" + suffix);
  state.source = source;
  source.onopen = function () {
    $("connection-banner").hidden = true;
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  };
  source.onmessage = function (msg) {
    $("connection-banner").hidden = true;
    var event;
    try { event = JSON.parse(msg.data); } catch (e) { return; }
    queueEvent(event);
  };
  source.addEventListener("done", function (msg) {
    var payload = {};
    try { payload = JSON.parse(msg.data); } catch (e) { payload = {}; }
    queueEvent({ type: "stream_done", status: payload.status || "done" });
  });
  source.onerror = function () {
    if (state.finished) { source.close(); return; }
    $("connection-banner").hidden = false;
    setText($("connection-text"), "Reconnecting to the run");
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(function () {
      if (state.finished) return;
      setText($("connection-text"), "Connection lost. The run may still be going on the server.");
      announce("Connection lost.");
    }, 15000);
  };
}

function queueEvent(event) {
  state.pending.push(event);
  if (!state.frame) state.frame = window.requestAnimationFrame(flushEvents);
}

function flushEvents() {
  state.frame = null;
  var batch = state.pending;
  state.pending = [];
  var replaying = state.replayMode;
  batch.forEach(function (event) {
    try { handle(event); } catch (e) { if (window.console && console.warn) console.warn("event handler failed", e); }
  });
  if (replaying) { state.replayMode = false; paintAll(); } else { paintLive(); }
}

function handle(event) {
  if (!event || !event.type) return;
  if (event.seq != null && !isNaN(Number(event.seq))) state.lastSeq = Math.max(state.lastSeq, Number(event.seq));
  if (state.replayMode) { R.applyEvent(state.stats, event); addFeedEntry(event); return; }
  R.applyEvent(state.stats, event);
  addFeedEntry(event);
  switch (event.type) {
    case "run_started":
      state.active = true; state.queued = false; state.startedAt = Date.now();
      state.finished = false; state.failed = false; startClock();
      setRunStatus("Running", "running"); announce("Run started.");
      break;
    case "cache_hit": setRunStatus("Cached result", "done"); break;
    case "gem_product": paintGemCard(); break;
    case "normalized": paintGemCard(); break;
    case "shop_started": state.renderedResult = null; break;
    case "shop_finished": state.renderedResult = null; break;
    case "shop_updated":
      state.retrying = state.retrying.filter(function (shop) { return shop !== event.shop; });
      state.renderedResult = null;
      break;
    case "retry_started":
      state.retrying = event.shops || [];
      setRunStatus("First result in, retry running", "running");
      announce("Retry started for " + (event.shops || []).join(", ") + ".");
      break;
    case "run_finished":
      if (event.result) state.result = event.result;
      state.retrying = []; state.renderedResult = null;
      setRunStatus("First result ready", "running");
      announce(firstResultAnnouncement(state.result));
      break;
    case "run_updated":
      if (event.result) state.result = event.result;
      state.retrying = []; state.renderedResult = null;
      break;
    case "similar_ready":
      if (state.result) { state.result.similar = event.similar || []; state.renderedResult = null; }
      break;
    case "llm_unavailable":
      addRuntimeWarning("No LLM is available right now, so listings cannot be checked." + (event.reason ? " " + event.reason : ""));
      break;
    case "lane_failed":
      addRuntimeWarning("The " + (event.lane || "shop") + " part crashed: " + (event.error || "unknown error"));
      break;
    case "run_failed":
      addRuntimeWarning("The run failed: " + (event.error || event.message || "unknown error"));
      showRunError(event.error || event.message || "The run failed.");
      finishRun("failed");
      break;
    case "done": finishRun("done"); break;
    case "stream_done": finishRun(event.status === "failed" ? "failed" : "done"); break;
    default: break;
  }
}

function addRuntimeWarning(message) {
  if (message && state.runtimeWarnings.indexOf(message) < 0) state.runtimeWarnings.push(message);
  paintWarnings();
}
function firstResultAnnouncement(result) {
  if (!result) return "First result ready.";
  if (result.cheapest) return "First result ready. Best price " + R.money(result.cheapest.price) + " on " + result.cheapest.shop + ".";
  if ((result.matches || []).length) return "First result ready. A match was confirmed.";
  if ((result.possible_matches || []).length) return "First result ready. Only possible matches so far.";
  return "First result ready. No confirmed match yet.";
}

function resetRunView() {
  state.stats = R.newStats();
  state.result = null;
  state.retrying = [];
  state.runtimeWarnings = [];
  state.feed = [];
  state.feedHidden = 0;
  state.filter = "all";
  state.query = "";
  state.filterShopsKey = "";
  state.activeTab = "matches";
  state.lastSeq = 0;
  state.renderedResult = null;
  state.active = false;
  state.queued = false;
  state.finished = false;
  state.failed = false;
  state.startedAt = null;
  state.finishedAt = null;
  state.elapsedSeconds = 0;
  state.lastClock = "";
  state.pipelineUserToggled = false;
  state.pipelineCollapsed = false;
  state.shopsExpanded = false;
  state.shopRowOrder = [];
  state.shopRowEls = {};
  state.specsExpanded = false;
  stopClock();
  setText($("run-clock"), "00:00");
  setText($("run-state-label"), "Starting");
  $("run-error").hidden = true;
  setText($("run-error-text"), "");
  $("connection-banner").hidden = true;
  setText($("page-alert"), "");
  $("run-again").hidden = true;
  selectTab("matches", false);
  $("gem-card").hidden = true;
  $("verdict").hidden = true;
  $("details-card").hidden = true;
  setText($("gem-title"), "Reading the GeM page");
  setText($("gem-price"), "");
  setText($("gem-price-kind"), "");
  $("gem-image").hidden = true;
  $("gem-image").removeAttribute("src");
  $("gem-image-fallback").hidden = false;
  clear($("gem-specs"));
  $("gem-specs-toggle").hidden = true;
  clear($("price-bars"));
  clear($("matches-body"));
  clear($("matches-cards"));
  clear($("possible-body"));
  clear($("possible-cards"));
  clear($("similar-list"));
  clear($("warnings-list"));
  clear($("activity-list"));
  clear($("activity-filters"));
  clear($("run-stats"));
  clear($("shops-list"));
  $("shops-details").open = false;
  setText($("shops-count"), "");
  setText($("answer-kicker"), "");
  setText($("verdict-title"), "");
  setText($("answer-price"), "");
  setText($("answer-shop"), "");
  clear($("answer-shop-icon"));
  setText($("answer-saving"), "");
  setText($("answer-detail"), "");
  $("answer-link").hidden = true;
  $("verdict").dataset.kind = "searching";
  $("activity-empty").hidden = false;
  setText($("activity-empty"), "Events appear here as the run moves.");
  setText($("activity-count"), "");
  setText($("llm-run-summary"), "");
  $("similar-section").hidden = true;
  $("warnings-section").hidden = true;
  $("matches-empty").hidden = false;
  $("matches-table-wrap").hidden = true;
  $("possible-empty").hidden = false;
  $("possible-table-wrap").hidden = true;
  $("details-card").hidden = false;
  setPipelineCollapsed(false);
  buildPipeline();
  paintPipeline();
  paintCounts();
  paintMatches();
  paintPossible();
  paintSummary();
}

function startClock() {
  stopClock();
  state.timerId = setInterval(tickClock, 500);
  tickClock();
}
function stopClock() { if (state.timerId) clearInterval(state.timerId); state.timerId = null; }
function finishClockOnly() { stopClock(); state.active = false; updateTitle(); }
function tickClock() {
  if (!state.active || !state.startedAt) return;
  state.elapsedSeconds = (Date.now() - state.startedAt) / 1000;
  var text = R.clock(state.elapsedSeconds);
  if (text !== state.lastClock) { state.lastClock = text; setText($("run-clock"), text); updateTitle(); }
}

function setRunStatus(label, kind) {
  var el = $("run-state-label");
  setText(el, label);
  el.dataset.state = kind;
}

function finishRun(status) {
  if (state.finished && status !== "failed") return;
  state.finished = true;
  state.active = false;
  state.failed = status === "failed";
  state.finishedAt = Date.now();
  if (state.startedAt) state.elapsedSeconds = (state.finishedAt - state.startedAt) / 1000;
  stopClock();
  closeSource();
  if (state.failed) {
    setRunStatus("Failed", "failed");
    announce("The run failed.");
  } else {
    setRunStatus(state.result && state.result.from_cache ? "Cached result" : "Done", "done");
    announce(doneAnnouncement());
    $("run-clock").hidden = false;
    setText($("run-clock"), R.clock(state.elapsedSeconds));
    if (R.shouldAutoCollapse(state.pipelineUserToggled, "done")) setPipelineCollapsed(true);
  }
  if (!state.result && state.runId) {
    fetch(API + "/api/runs/" + encodeURIComponent(state.runId)).then(function (response) { return response.json(); })
      .then(function (run) { if (run && run.result) { state.result = run.result; state.renderedResult = null; } paintAll(); loadRuns(); scrollToSummary(); })
      .catch(function () { loadRuns(); });
  } else {
    paintAll();
    loadRuns();
  }
  stopProviderPoll();
  if (state.activeTab === "system" && !state.demo) loadProviders();
  $("run-again").hidden = !state.lastUrl;
  scrollToSummary();
  updateTitle();
}

function doneAnnouncement() {
  if (!state.result) return "Run complete.";
  if (state.result.cheapest) return "Run complete. Best price " + R.money(state.result.cheapest.price) + " on " + state.result.cheapest.shop + ".";
  if ((state.result.matches || []).length) return "Run complete. A match was confirmed.";
  return "Run complete. No confirmed match.";
}

function scrollBehavior() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

function scrollToSummary() {
  var target = $("verdict");
  if (!target || target.hidden) return;
  try { target.scrollIntoView({ behavior: scrollBehavior(), block: "start" }); }
  catch (e) { target.scrollIntoView(); }
}

function showRunError(message) {
  setText($("run-error-text"), message || "The run failed.");
  $("run-error").hidden = false;
  state.renderedResult = null;
}

function paintLive() {
  paintPipeline();
  paintCounts();
  paintSummary();
  if (state.renderedResult !== state.result) {
    state.renderedResult = state.result;
    paintGemCard();
    paintMatches();
    paintPossible();
    paintSimilar();
    paintWarnings();
  }
  paintSystem();
  paintActivityState();
  updateTitle();
}

function paintAll() {
  paintGemCard();
  paintPipeline();
  paintCounts();
  paintSummary();
  state.renderedResult = state.result;
  paintMatches();
  paintPossible();
  paintSimilar();
  paintWarnings();
  paintSystem();
  paintActivityState();
  paintRecent();
  updateTitle();
}

function paintCounts() {
  var counts = R.tabCounts(state.result, state.stats);
  setText($("count-matches"), counts.matches);
  setText($("count-possible"), counts.possible);
  setText($("count-activity"), counts.activity);
}

function gemModel() {
  var gem = (state.result && state.result.gem) || {};
  var stats = state.stats.gem || {};
  var price = gem.price || (stats.price != null ? { low: stats.price, high: stats.price, kind: stats.priceType || "unknown" } : null);
  return {
    title: gem.canonical_name || gem.title || stats.title || "",
    pageTitle: gem.title || stats.title || "",
    brand: gem.brand || stats.brand || "",
    model: gem.model || stats.model || "",
    price: price,
    image: gem.image || stats.image || "",
    specs: gem.specs || stats.specs || {}
  };
}

function paintGemCard() {
  var gem = gemModel();
  var card = $("gem-card");
  var hasProduct = !!(gem.title || gem.price);
  card.hidden = !hasProduct;
  if (!hasProduct) return;
  setText($("gem-kicker"), gem.title ? "The GeM listing" : "Reading the GeM page");
  setText($("gem-title"), gem.title || gem.pageTitle || "Reading the GeM page");
  if (gem.price && gem.price.low != null) {
    setText($("gem-price"), R.moneyRange(gem.price));
    setText($("gem-price-kind"), "GeM price, " + R.priceKindLabel(gem.price.kind));
  } else {
    setText($("gem-price"), "Price not read yet");
    setText($("gem-price-kind"), "");
  }
  paintGemImage(gem.image);
  paintSpecs(gem);
}

function paintGemImage(src) {
  var img = $("gem-image");
  var fallback = $("gem-image-fallback");
  var usable = typeof src === "string" && (/^https:\/\/([a-z0-9-]+\.)*gem\.gov\.in\//i.test(src) || /^data:image\//i.test(src));
  if (!usable) {
    img.hidden = true;
    img.removeAttribute("src");
    fallback.hidden = false;
    return;
  }
  if (img.getAttribute("src") === src && !img.hidden) return;
  img.hidden = false;
  fallback.hidden = true;
  img.src = src;
}

function specsEntries(gem) {
  var specs = gem.specs && typeof gem.specs === "object" ? gem.specs : {};
  var rows = [];
  var seen = {};
  function add(key, value) {
    if (value == null || value === "") return;
    var lower = String(key).toLowerCase();
    if (seen[lower]) return;
    seen[lower] = true;
    rows.push({ key: String(key), value: String(value) });
  }
  add("Brand", gem.brand);
  add("Model", gem.model);
  Object.keys(specs).forEach(function (key) {
    if (String(key).toLowerCase() === "brand" || String(key).toLowerCase() === "model") return;
    var value = specs[key];
    if (value != null && typeof value === "object") value = JSON.stringify(value);
    add(key, value);
  });
  return rows;
}

function paintSpecs(gem) {
  var list = $("gem-specs");
  var toggle = $("gem-specs-toggle");
  clear(list);
  var rows = specsEntries(gem);
  var limit = state.specsExpanded ? rows.length : 12;
  rows.slice(0, limit).forEach(function (row) {
    var value = row.value;
    var dd = h("dd", { text: value.length > 120 ? value.slice(0, 117) + "..." : value });
    if (value.length > 120) dd.title = value;
    list.appendChild(h("div", { class: "spec-row" }, h("dt", { text: row.key }), dd));
  });
  if (rows.length > 12) {
    toggle.hidden = false;
    setText(toggle, state.specsExpanded ? "Show fewer" : "Show all " + rows.length);
  } else {
    toggle.hidden = true;
  }
}

function paintSummary() {
  var show = state.finished && !state.failed;
  var card = $("verdict");
  card.hidden = !show;
  // The Activity and System tabs are useful while the run is going; only the
  // answer and the result tables wait for the final event.
  $("details-card").hidden = false;
  if (!show) return;
  var result = state.result;
  var model = R.answerModel(result, { active: state.active, finished: state.finished, retrying: state.retrying });
  card.dataset.kind = model.kind;
  setText($("answer-kicker"), model.kicker);
  setText($("verdict-title"), model.title || model.kicker || "Result");
  setText($("answer-detail"), model.detail);
  setText($("answer-shop"), model.shop ? "on " + model.shop : "");
  var shopIcon = $("answer-shop-icon");
  clear(shopIcon);
  if (model.shop) shopIcon.appendChild(siteIcon(model.shop));
  setText($("answer-price"), model.price != null ? R.money(model.price) : (model.headline || ""));
  var saving = result ? R.savingInfo(result) : null;
  var savingEl = $("answer-saving");
  savingEl.className = "answer-saving" + (saving && saving.dearer ? " is-dearer" : "");
  if (saving) {
    setText(savingEl, saving.cheaper ? "saves " + R.money(saving.amount) + ", " + saving.percent.toFixed(1) + "%"
      : saving.dearer ? "costs " + R.money(Math.abs(saving.amount)) + " more, " + Math.abs(saving.percent).toFixed(1) + "%"
      : "the same price as GeM");
  } else {
    setText(savingEl, "");
  }
  var link = $("answer-link");
  var href = model.url ? R.safeHref(model.url) : null;
  if (href) { link.href = href; link.hidden = false; } else { link.removeAttribute("href"); link.hidden = true; }
  paintBars(result);
}

function paintBars(result) {
  var container = $("price-bars");
  clear(container);
  if (!result) return;
  var model = R.barRows(result);
  if (model.rows.length < 2) return;
  model.rows.forEach(function (row) {
    var fill = h("span", { class: "bar-fill" });
    fill.style.width = Math.max(0, Math.min(100, row.width)) + "%";
    var tags = [];
    if (row.isGem) tags.push("GeM");
    if (row.cheapest) tags.push("cheapest");
    if (row.bulk) tags.push("B2B");
    if (!row.inStock) tags.push("out of stock");
    container.appendChild(h("div", { class: "bar-row" + (row.isGem ? " is-gem" : "") + (row.cheapest ? " is-cheapest" : "") },
      h("span", { class: "bar-label", text: row.label }),
      h("span", { class: "bar-track" }, fill),
      h("span", { class: "bar-price" }, R.money(row.price), tags.length ? h("span", { class: "bar-tag", text: tags.join(", ") }) : "")));
  });
}

/* The pipeline is built once and only its values change, so a row never moves. */
var LANE_STEPS = [["search", "Search"], ["filter", "Filter"], ["verify", "Verify"], ["confirmed", "Confirmed"]];
var SHOP_STEPS = [["filter", "Filter"], ["verify", "Verify"], ["confirmed", "Confirmed"]];

function stepEl(id, label) {
  return h("div", { class: "step", id: id, dataset: { state: "waiting" } },
    h("span", { class: "step-label", text: label }),
    h("span", { class: "step-value", id: id + "-value", text: "waiting" }));
}

function laneIcon(lane) {
  if (lane && lane.key) {
    var name = String(lane.key).toLowerCase().replace(/^https?:\/\//, "").split("/")[0];
    return h("span", { class: "site-icon", title: name },
      h("span", { class: "site-icon-letter", text: (name.charAt(0) || "?").toUpperCase() }));
  }
  return h("span", { class: "lane-icon" }, svg("M7 2.2a4.8 4.8 0 104.1 7.3L14 12.6"));
}

function buildPipeline() {
  var top = $("pipeline-top");
  var lanes = $("map-lanes");
  var end = $("pipeline-end");
  clear(top);
  clear(lanes);
  clear(end);
  top.appendChild(h("div", { class: "pipeline-node", id: "node-read", dataset: { state: "waiting" } },
    h("span", { class: "node-label", text: "Read GeM page" }),
    h("span", { class: "node-value", id: "node-read-value", text: "waiting" })));
  var understand = h("div", { class: "pipeline-node", id: "node-understand", dataset: { state: "waiting" } },
    h("span", { class: "node-label", text: "Understand (LLM)" }),
    h("span", { class: "node-value", id: "node-understand-value", text: "waiting" }));
  understand.appendChild(h("details", { class: "node-detail", id: "understand-detail", hidden: "" },
    h("summary", { id: "understand-summary" }),
    h("ul", { id: "understand-queries", class: "plain-list" })));
  top.appendChild(understand);

  R.LANES.forEach(function (lane) {
    if (lane.id === "other") {
      var web = h("section", { class: "lane lane-web", id: "lane-other", dataset: { state: "waiting" } },
        h("div", { class: "lane-head" },
          laneIcon(lane),
          h("span", { class: "lane-name", text: "Web search" }),
          h("span", { class: "lane-status", id: "lane-other-status", text: "Waiting" })));
      var steps = h("div", { class: "lane-steps", id: "lane-other-steps" });
      steps.appendChild(stepEl("lane-other-search", "Discover"));
      steps.appendChild(stepEl("lane-other-filter", "Filter"));
      steps.appendChild(stepEl("lane-other-verify", "Verify"));
      steps.appendChild(stepEl("lane-other-confirmed", "Confirmed"));
      web.appendChild(steps);
      web.appendChild(h("div", { class: "shop-rows", id: "shop-rows" }));
      web.appendChild(h("button", { class: "text-button shop-more", id: "shop-more", type: "button", hidden: "" }));
      lanes.appendChild(web);
      return;
    }
    var laneEl = h("section", { class: "lane", id: "lane-" + lane.id, dataset: { state: "waiting" } },
      h("div", { class: "lane-head" },
        laneIcon(lane),
        h("span", { class: "lane-name", text: lane.label }),
        h("span", { class: "lane-status", id: "lane-" + lane.id + "-status", text: "Waiting" })));
    var laneSteps = h("div", { class: "lane-steps", id: "lane-" + lane.id + "-steps" });
    LANE_STEPS.forEach(function (pair) { laneSteps.appendChild(stepEl("lane-" + lane.id + "-" + pair[0], pair[1])); });
    laneEl.appendChild(laneSteps);
    lanes.appendChild(laneEl);
  });

  end.appendChild(h("div", { class: "end-markers" },
    h("span", { class: "end-marker", id: "node-checks", dataset: { state: "waiting" } }, "Checks and first result: ", h("span", { id: "node-checks-value", text: "waiting" })),
    h("span", { class: "end-marker", id: "node-retry", dataset: { state: "waiting" } }, "Retry gaps: ", h("span", { id: "node-retry-value", text: "not needed yet" }))));
}

function setPipelineCollapsed(collapsed) {
  state.pipelineCollapsed = !!collapsed;
  var body = $("pipeline-body");
  var button = $("pipeline-toggle");
  if (body) body.hidden = state.pipelineCollapsed;
  if (button) {
    button.setAttribute("aria-expanded", state.pipelineCollapsed ? "false" : "true");
    setText(button, state.pipelineCollapsed ? "Show details" : "Hide details");
  }
  paintPipelineSummary();
}

function applyPipelineCollapsed() { setPipelineCollapsed(state.pipelineCollapsed); }

function paintPipelineSummary() {
  var elapsed = state.elapsedSeconds;
  var summary = R.pipelineSummary(state.stats, elapsed);
  var text;
  if (state.finished && !state.failed) {
    text = "Pipeline finished: " + summary.lanes + " lanes, " + summary.shops + " shops, " + summary.steps + " steps, " + elapsed.toFixed(1) + " s.";
  } else if (state.failed) {
    text = "Pipeline stopped: " + summary.lanes + " lanes, " + summary.shops + " shops, " + summary.steps + " steps.";
  } else {
    text = summary.lanes + " lanes, " + summary.shops + " shops, " + summary.steps + " steps.";
  }
  setText($("pipeline-summary-text"), text);
}

function paintPipeline() {
  var stats = state.stats;
  var lanes = R.pipelineModel(stats);
  updateMapNode("node-read", "node-read-value", lanes.read);
  updateMapNode("node-understand", "node-understand-value", lanes.understand);
  var product = stats.product || {};
  var detail = gel("understand-detail");
  if (detail) {
    var queries = product.queries || [];
    if (queries.length) {
      detail.hidden = false;
      setText(gel("understand-summary"), queries.length + " search phrase" + (queries.length === 1 ? "" : "s"));
      var listEl = gel("understand-queries");
      clear(listEl);
      queries.slice(0, 12).forEach(function (query) { listEl.appendChild(h("li", { text: query })); });
    } else {
      detail.hidden = true;
    }
  }
  lanes.lanes.forEach(function (lane) {
    var laneEl = $("lane-" + lane.id);
    if (!laneEl) return;
    var entry = laneStats(lane.id);
    var model = R.laneRowModel(entry);
    var values = {
      search: model.search,
      filter: model.filter,
      verify: model.verify,
      confirmed: String(model.confirmed)
    };
    setState(laneEl, lane.state);
    setText($("lane-" + lane.id + "-status"), lane.statusLabel);
    laneEl.title = laneTitle(lane.id, lane.help);
    lane.nodes.forEach(function (node) {
      var id = "lane-" + lane.id + "-" + node.id;
      setState($(id), node.state);
      var value = values[node.id] == null ? node.value : values[node.id];
      setNodeValue($(id + "-value"), value, node.state);
    });
    var confirmedEl = $("lane-" + lane.id + "-confirmed");
    if (confirmedEl) {
      var confirmedDone = model.confirmed > 0 || !!entry.status;
      setState(confirmedEl, confirmedDone ? "done" : "waiting");
      setNodeValue($("lane-" + lane.id + "-confirmed-value"), String(model.confirmed), confirmedDone ? "done" : "waiting");
    }
  });
  paintWebLane(stats);
  updateMapNode("node-checks", "node-checks-value", lanes.checks);
  updateMapNode("node-retry", "node-retry-value", lanes.retry);
  paintShopRows(stats);
  paintPipelineSummary();
}

function laneStats(id) {
  if (id === "flipkart") return state.stats.shops["flipkart.com"] || {};
  if (id === "amazon") return state.stats.shops["amazon.in"] || {};
  return state.stats.shops["other"] || {};
}

function setNodeValue(el, value, nodeState) {
  if (!el) return;
  var text = String(value == null ? "" : value);
  clear(el);
  if (text === "0") {
    el.textContent = text;
    el.dataset.kind = "muted";
    return;
  }
  var parts = text.split(" ");
  if (parts.length === 2 && /^[+-]?\d+$/.test(parts[0]) && /^-?\d+$/.test(parts[1])) {
    el.appendChild(h("span", { class: "count-good", text: parts[0] }));
    el.appendChild(document.createTextNode(" "));
    el.appendChild(h("span", { class: "count-bad", text: parts[1] }));
    el.dataset.kind = "counts";
    return;
  }
  el.textContent = text;
  el.dataset.kind = nodeState === "blocked" || nodeState === "warning" ? "bad"
    : nodeState === "done" ? "good" : nodeState === "active" ? "accent" : "muted";
}

function paintWebLane(stats) {
  var rows = R.laneRows(stats);
  var total = { kept: 0, dropped: 0, matches: 0, rejected: 0, seen: 0 };
  rows.forEach(function (row) {
    total.kept += Number(row.kept) || 0;
    total.dropped += Number(row.dropped) || 0;
    total.matches += Number(row.matches) || 0;
    total.rejected += Number(row.rejected) || 0;
    total.seen += Number(row.listings) || 0;
  });
  var discover = R.laneRowModel({ queries: 0, queriesDone: 0, seen: (stats.shops.other && stats.shops.other.listings) || total.seen, kept: 0, dropped: 0, rejected: 0, matches: 0 });
  setState(gel("lane-other-search"), total.seen || (stats.shops.other && stats.shops.other.listings) ? "done" : (stats.step === "search" ? "active" : "waiting"));
  setNodeValue(gel("lane-other-search-value"), discover.search, total.seen ? "done" : "waiting");
  var filterState = total.kept || total.dropped ? "done" : (rows.length ? "active" : "waiting");
  setState(gel("lane-other-filter"), filterState);
  setNodeValue(gel("lane-other-filter-value"), total.kept || total.dropped ? R.pairCount(total.kept, total.dropped) : "waiting", filterState);
  var verifyState = total.matches || total.rejected ? "done" : (total.kept ? "active" : "waiting");
  setState(gel("lane-other-verify"), verifyState);
  setNodeValue(gel("lane-other-verify-value"), total.matches || total.rejected ? R.pairCount(total.matches, total.rejected) : "waiting", verifyState);
  var confirmedState = total.matches ? "done" : (state.finished ? "done" : "waiting");
  setState(gel("lane-other-confirmed"), confirmedState);
  setNodeValue(gel("lane-other-confirmed-value"), String(total.matches || 0), confirmedState);
  var status = (stats.shops.other && stats.shops.other.status) || webStatus(stats);
  setText(gel("lane-other-status"), status ? R.shopStatusLabel(status) : (rows.length ? "Searching" : "Waiting"));
  gel("lane-other").title = rows.map(function (row) { return row.shop + ": " + pipelineRowTitle(row); }).join("\n");
}

function webStatus(stats) {
  var rows = R.laneRows(stats);
  if (!rows.length) return "";
  if (rows.some(function (row) { return row.status === "found"; })) return "found";
  if (rows.some(function (row) { return row.status === "possible"; })) return "possible";
  if (rows.some(function (row) { return row.status === "not_searched"; })) return "not_searched";
  if (rows.every(function (row) { return row.status === "not_found"; })) return "not_found";
  return "";
}

function pipelineRowTitle(row) {
  var facts = (state.result && state.result.shops && state.result.shops[row.shop]) || {};
  var parts = [];
  var status = facts.status || row.status;
  if (status) parts.push(R.shopStatusLabel(status));
  if (facts.seen != null || row.listings) parts.push((facts.seen != null ? facts.seen : row.listings) + " seen");
  if (facts.dropped != null || row.dropped) parts.push((facts.dropped != null ? facts.dropped : row.dropped) + " dropped");
  if (facts.checked != null || row.judged) parts.push((facts.checked != null ? facts.checked : row.judged) + " checked");
  if (facts.matches != null || row.matches) parts.push((facts.matches != null ? facts.matches : row.matches) + " match" + ((facts.matches != null ? facts.matches : row.matches) === 1 ? "" : "es"));
  if (facts.seconds != null) parts.push(Number(facts.seconds).toFixed(1) + " s");
  if (facts.retried) parts.push("retried");
  if (facts.reason) parts.push(facts.reason);
  return parts.join(" \u00b7 ");
}

function laneTitle(id, fallback) {
  var key = id === "flipkart" ? "flipkart.com" : id === "amazon" ? "amazon.in" : "";
  if (!key) return fallback || "";
  var s = state.stats.shops[key] || {};
  var text = pipelineRowTitle({
    shop: key, status: s.status || "", listings: s.seen || s.listings || 0,
    kept: s.kept || 0, dropped: s.dropped || 0, judged: s.judged || 0,
    matches: s.matches || 0, possible: s.possible || 0, rejected: s.rejected || 0
  });
  return text || fallback || "";
}


function shopSafeId(domain) {
  return "shop-" + String(domain).toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

function paintShopRows(stats) {
  var rows = R.laneRows(stats);
  var container = gel("shop-rows");
  rows.forEach(function (row) {
    if (state.shopRowEls[row.shop]) return;
    var id = shopSafeId(row.shop);
    var el = h("div", { class: "shop-row", id: id, dataset: { state: "waiting" } },
      h("div", { class: "shop-head" },
        siteIcon(row.shop),
        h("span", { class: "shop-name", text: row.shop }),
        h("span", { class: "shop-status", text: row.status ? R.shopStatusLabel(row.status) : "Searching" })));
    var steps = h("div", { class: "shop-steps" });
    SHOP_STEPS.forEach(function (pair) { steps.appendChild(stepEl(id + "-" + pair[0], pair[1])); });
    el.appendChild(steps);
    state.shopRowEls[row.shop] = { el: el, steps: steps };
    state.shopRowOrder.push(row.shop);
    container.appendChild(el);
  });
  rows.forEach(function (row) {
    var record = state.shopRowEls[row.shop];
    if (!record) return;
    var model = R.laneRowModel(row);
    setState(record.el, row.status === "found" ? "done" : row.status === "possible" ? "warning" : row.status === "not_searched" || row.status === "incomplete" || row.status === "timed_out" ? "warning" : "active");
    record.el.title = pipelineRowTitle(row);
    var nameEl = record.el.querySelector(".shop-status");
    if (nameEl) setText(nameEl, row.status ? R.shopStatusLabel(row.status) : "Searching");
    updateStep(record.el, "-filter", model.filter, row.kept || row.dropped ? "done" : "active");
    updateStep(record.el, "-verify", model.verify, row.judged || row.rejected ? "done" : "active");
    updateStep(record.el, "-confirmed", String(row.matches || 0), row.matches ? "done" : (row.status ? "done" : "waiting"));
  });
  var more = gel("shop-more");
  if (!more) return;
  state.shopRowOrder.forEach(function (shop, index) {
    var record = state.shopRowEls[shop];
    if (!record) return;
    record.el.hidden = index >= SHOP_ROWS_SHOWN && !state.shopsExpanded;
  });
  var hiddenCount = Math.max(0, state.shopRowOrder.length - SHOP_ROWS_SHOWN);
  if (hiddenCount && !state.shopsExpanded) {
    more.hidden = false;
    setText(more, "Show " + hiddenCount + " more");
  } else if (hiddenCount) {
    more.hidden = false;
    setText(more, "Show fewer");
  } else {
    more.hidden = true;
  }
}

function updateStep(root, suffix, value, kind) {
  var valueEl = root.querySelector(".step[id$='" + suffix + "-value']");
  var step = root.querySelector(".step[id$='" + suffix + "']");
  if (!valueEl || !step) return;
  setState(step, kind);
  setNodeValue(valueEl, value, kind);
}

function updateMapNode(nodeId, valueId, node) {
  setState($(nodeId), node.state);
  setNodeValue($(valueId), node.value, node.state);
}

function paintMatches() {
  var result = state.result;
  var ready = state.finished;
  var rows = ready ? R.matchRows(result, state.gates) : [];
  var empty = $("matches-empty");
  var wrap = $("matches-table-wrap");
  var body = $("matches-body");
  var cards = $("matches-cards");
  clear(body);
  clear(cards);
  setText(empty, ready ? "No confirmed match yet. This tab fills in when a shop returns the same product." : "Results appear when the run finishes.");
  empty.hidden = rows.length > 0;
  wrap.hidden = rows.length === 0;
  if (!rows.length) return;
  var gemPrice = result && result.gem && result.gem.price ? result.gem.price.low : null;
  var scaleRows = rows.map(function (row) { return { delta: row.delta, price: row.price, isGem: false }; });
  if (gemPrice != null) scaleRows.push({ delta: 0, price: gemPrice, isGem: true });
  var scale = R.scaleFor(scaleRows);
  setText($("scale-gem"), gemPrice != null ? "GeM " + R.money(gemPrice) : "GeM price unknown");
  var cheapest = result && result.cheapest;
  var fragment = document.createDocumentFragment();
  rows.forEach(function (row) {
    var best = !!(cheapest && row.price != null && Number(row.price) === Number(cheapest.price) && R.shopKey(row.shop) === R.shopKey(cheapest.shop || ""));
    var pair = matchRow(row, scale, gemPrice, best);
    fragment.appendChild(pair[0]);
    fragment.appendChild(pair[1]);
    cards.appendChild(matchCard(row, scale, gemPrice, best));
  });
  body.appendChild(fragment);
}

function matchRow(row, scale, gemPrice, best) {
  var tr = h("tr", { class: best ? "is-best" : "" });
  tr.appendChild(shopCell(row, best));
  tr.appendChild(priceCell(row, scale, gemPrice));
  var confidence = h("td", { class: "cell-confidence" });
  confidence.appendChild(confidenceView(row));
  tr.appendChild(confidence);
  var why = h("td", { class: "cell-why" });
  var button = h("button", { class: "disclosure", type: "button", "aria-expanded": "false", "aria-label": "Why this was accepted" });
  button.appendChild(svg("M6 3l5 5-5 5"));
  why.appendChild(button);
  tr.appendChild(why);

  var detail = h("tr", { class: "why-row", hidden: "" });
  var cell = h("td", { colspan: "4" });
  cell.appendChild(whyDetails(row.reason, row.checks));
  detail.appendChild(cell);
  button.addEventListener("click", function () {
    var open = detail.hidden;
    detail.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.classList.toggle("is-open", open);
  });
  return [tr, detail];
}

function shopCell(row, best) {
  var cell = h("td", { class: "cell-shop" + (best ? " is-best" : "") });
  var line = h("div", { class: "shop-line" });
  line.appendChild(siteIcon(row.shop));
  line.appendChild(linkOrText(row.url, row.shop || "shop", { class: "shop-link" }));
  if (row.bulk) line.appendChild(h("span", { class: "note-tag", text: "B2B" }));
  if (!row.inStock) line.appendChild(h("span", { class: "note-tag is-bad", text: "out of stock" }));
  cell.appendChild(line);
  var title = h("span", { class: "listing-title", text: row.title || "Untitled listing" });
  if (row.title) title.title = row.title;
  cell.appendChild(title);
  return cell;
}

function priceCell(row, scale, gemPrice) {
  var cell = h("td", { class: "cell-price" });
  cell.appendChild(h("span", { class: "price-strong", text: row.priceText }));
  cell.appendChild(priceDelta(row, scale, gemPrice));
  return cell;
}

function priceDelta(row, scale, gemPrice) {
  if (row.delta == null || gemPrice == null) {
    return h("span", { class: "delta-note", text: "GeM price unknown" });
  }
  var track = h("span", { class: "delta-track" });
  var denom = scale.half * gemPrice;
  var fraction = denom > 0 ? Math.min(1, Math.abs(row.delta) / denom) : 0;
  var aria;
  if (row.delta === 0) {
    aria = "The same price as GeM";
  } else {
    var fill = h("span", { class: "delta-fill " + (row.delta < 0 ? "is-less" : "is-more") });
    fill.style.width = (fraction * 50) + "%";
    fill.style.left = row.delta < 0 ? (50 - fraction * 50) + "%" : "50%";
    track.appendChild(fill);
    var amount = h("span", { class: "delta-amount " + (row.delta < 0 ? "is-less" : "is-more") });
    amount.textContent = (row.delta < 0 ? "-" : "+") + R.money(Math.abs(row.delta));
    amount.style.left = row.delta < 0 ? (50 - fraction * 50) + "%" : (50 + fraction * 50) + "%";
    amount.style.transform = row.delta < 0 ? "translateX(-100%)" : "translateX(0)";
    track.appendChild(amount);
    aria = R.money(Math.abs(row.delta)) + (row.delta < 0 ? " cheaper than GeM" : " dearer than GeM");
  }
  track.appendChild(h("span", { class: "delta-tick", "aria-hidden": "true" }));
  track.setAttribute("role", "img");
  track.setAttribute("aria-label", aria);
  return track;
}

function confidenceView(row) {
  var value = row.confidence;
  if (value == null) return h("span", { class: "confidence muted", text: "-" });
  var min = Number(state.gates.min_confidence != null ? state.gates.min_confidence : 0.75);
  var low = Number(state.gates.low_confidence != null ? state.gates.low_confidence : 0.85);
  return h("span", { class: "confidence" + (value < low ? " is-warn" : ""), text: String(Math.round(value * 100)) });
}

function whyDetails(reason, checks) {
  var body = h("div", { class: "why-body" });
  if (reason) body.appendChild(h("p", { text: reason }));
  if (checks && checks.length) {
    var list = h("ul");
    checks.forEach(function (check) { list.appendChild(h("li", { text: check })); });
    body.appendChild(list);
  }
  return body;
}

function matchCard(row, scale, gemPrice, best) {
  var card = h("article", { class: "match-card" + (best ? " is-best" : "") });
  var head = h("div", { class: "match-card-head" });
  head.appendChild(siteIcon(row.shop));
  head.appendChild(linkOrText(row.url, row.shop || "shop", { class: "shop-link" }));
  if (row.bulk) head.appendChild(h("span", { class: "note-tag", text: "B2B" }));
  if (!row.inStock) head.appendChild(h("span", { class: "note-tag is-bad", text: "out of stock" }));
  card.appendChild(head);
  var title = h("span", { class: "listing-title", text: row.title || "Untitled listing" });
  if (row.title) title.title = row.title;
  card.appendChild(title);
  var price = h("div", { class: "match-card-price" });
  price.appendChild(h("span", { class: "price-strong", text: row.priceText }));
  price.appendChild(priceDelta(row, scale, gemPrice));
  card.appendChild(price);
  card.appendChild(h("div", { class: "match-card-meta" }, confidenceView(row)));
  card.appendChild(whyDetails(row.reason, row.checks));
  return card;
}

function paintPossible() {
  var result = state.result;
  var ready = state.finished;
  var rows = ready ? R.possibleRows(result) : [];
  var empty = $("possible-empty");
  var wrap = $("possible-table-wrap");
  var body = $("possible-body");
  var cards = $("possible-cards");
  clear(body);
  clear(cards);
  setText(empty, ready ? "No possible matches. A listing with an unclear pack or an unknown price would appear here." : "Possible matches appear when the run finishes.");
  empty.hidden = rows.length > 0;
  wrap.hidden = rows.length === 0;
  if (!rows.length) return;
  var gemPrice = result && result.gem && result.gem.price ? result.gem.price.low : null;
  var scaleRows = rows.map(function (row) { return { delta: row.delta, price: row.price, isGem: false }; });
  if (gemPrice != null) scaleRows.push({ delta: 0, price: gemPrice, isGem: true });
  var scale = R.scaleFor(scaleRows);
  setText($("possible-scale-gem"), gemPrice != null ? "GeM " + R.money(gemPrice) : "GeM price unknown");
  var fragment = document.createDocumentFragment();
  rows.forEach(function (row) {
    fragment.appendChild(possibleRow(row, scale, gemPrice));
    cards.appendChild(possibleCard(row, scale, gemPrice));
  });
  body.appendChild(fragment);
}

function possibleRow(row, scale, gemPrice) {
  var tr = h("tr");
  tr.appendChild(shopCell(row, false));
  var price = h("td", { class: "cell-price" });
  price.appendChild(h("span", { class: "price-strong", text: row.priceText }));
  price.appendChild(priceDelta(row, scale, gemPrice));
  tr.appendChild(price);
  tr.appendChild(h("td", { class: "cell-confidence" }, confidenceView(row)));
  var why = h("td", { class: "possible-why", text: row.why });
  why.title = row.why;
  tr.appendChild(why);
  return tr;
}

function possibleCard(row, scale, gemPrice) {
  var card = h("article", { class: "match-card" });
  var head = h("div", { class: "match-card-head" });
  head.appendChild(siteIcon(row.shop));
  head.appendChild(linkOrText(row.url, row.shop || "shop", { class: "shop-link" }));
  if (row.bulk) head.appendChild(h("span", { class: "note-tag", text: "B2B" }));
  if (!row.inStock) head.appendChild(h("span", { class: "note-tag is-bad", text: "out of stock" }));
  card.appendChild(head);
  var title = h("span", { class: "listing-title", text: row.title || "Untitled listing" });
  if (row.title) title.title = row.title;
  card.appendChild(title);
  var price = h("div", { class: "match-card-price" });
  price.appendChild(h("span", { class: "price-strong", text: row.priceText }));
  price.appendChild(priceDelta(row, scale, gemPrice));
  card.appendChild(price);
  card.appendChild(h("div", { class: "match-card-meta" }, confidenceView(row)));
  card.appendChild(h("p", { class: "why", text: row.why }));
  return card;
}

function paintSimilar() {
  var show = state.finished && R.showsSimilar(state.result);
  $("similar-section").hidden = !show;
  var list = $("similar-list");
  clear(list);
  if (!show) return;
  R.similarRows(state.result).forEach(function (row) {
    list.appendChild(h("article", { class: "match-card" },
      h("div", { class: "match-card-head" }, h("span", { class: "shop-link", text: row.shop || "shop" }), h("span", { class: "price-strong", text: row.priceText })),
      h("span", { class: "listing-title", text: row.title || "Untitled listing" }),
      h("span", { class: "hint", text: "Similar, not the same product." })));
  });
}

function paintWarnings() {
  var warnings = [];
  if (state.result && state.result.warnings) warnings = warnings.concat(state.result.warnings);
  state.runtimeWarnings.forEach(function (warning) { if (warnings.indexOf(warning) < 0) warnings.push(warning); });
  var section = $("warnings-section");
  var list = $("warnings-list");
  clear(list);
  section.hidden = warnings.length === 0;
  warnings.forEach(function (warning) {
    list.appendChild(h("li", { class: state.runtimeWarnings.indexOf(warning) >= 0 ? "is-run" : "", text: warning }));
  });
}

function paintSystem() {
  var llm = state.stats.llm || {};
  var cached = state.result && state.result.from_cache;
  setText($("llm-run-summary"), llm.calls
    ? "This run: " + llm.calls + " LLM call" + (llm.calls === 1 ? "" : "s") + ", " + llm.failed + " failed, " + llm.cached + " reused, " + llm.seconds.toFixed(1) + " s waiting in total."
    : "This run has no LLM calls yet.");
  var stats = $("run-stats");
  clear(stats);
  [["Run time", R.clock(state.elapsedSeconds)], ["Events seen", String(state.stats.events)], ["Saved cache", cached ? "yes, served from cache" : state.result ? "no" : "not yet"]].forEach(function (pair) {
    stats.appendChild(h("div", null, h("dt", { text: pair[0] }), h("dd", { text: pair[1] })));
  });
  paintShopsDetails();
  paintThisRun();
}

function paintShopsDetails() {
  var shops = state.result && state.result.shops ? state.result.shops : {};
  var list = $("shops-list");
  var count = $("shops-count");
  clear(list);
  var keys = Object.keys(shops).filter(function (key) { return key !== "other"; });
  setText(count, keys.length ? "(" + keys.length + ")" : "");
  if (!keys.length) {
    list.appendChild(h("p", { class: "empty", text: "No shop facts for this run." }));
    return;
  }
  keys.forEach(function (key) {
    var entry = shops[key] || {};
    var row = h("div", { class: "shop-fact" });
    row.appendChild(h("span", { class: "shop-fact-name", text: key }));
    row.appendChild(h("span", { class: "shop-fact-state", text: R.shopStatusLabel(entry.status) }));
    var parts = [];
    if (entry.seen != null) parts.push(entry.seen + " seen");
    if (entry.dropped != null) parts.push(entry.dropped + " dropped");
    if (entry.checked != null) parts.push(entry.checked + " checked");
    if (entry.matches) parts.push(entry.matches + " match" + (entry.matches === 1 ? "" : "es"));
    if (entry.seconds != null) parts.push(Number(entry.seconds).toFixed(1) + " s");
    if (entry.retried) parts.push("retried");
    row.appendChild(h("span", { class: "shop-fact-line", text: parts.join(" · ") }));
    if (entry.reason) row.appendChild(h("span", { class: "shop-fact-note", text: entry.reason }));
    list.appendChild(row);
  });
}

function paintThisRun() {
  var byEndpoint = (state.stats.llm && state.stats.llm.byEndpoint) || {};
  Object.keys(state.providerEls).forEach(function (endpoint) {
    var record = state.providerEls[endpoint];
    var entry = byEndpoint[endpoint];
    if (!record || !record.thisRun) return;
    record.thisRun.textContent = thisRunText(entry);
  });
}

function thisRunText(entry) {
  if (!entry) return "0";
  return entry.calls + " call" + (entry.calls === 1 ? "" : "s") + (entry.failed ? ", " + entry.failed + " failed" : "");
}

function renderProviders() {
  var body = $("llm-table-body");
  if (!state.providers) { clear(body); state.providerEls = {}; return; }
  if (state.providers.error) {
    clear(body);
    state.providerEls = {};
    setText($("llm-strategy"), "Config problem: " + state.providers.error);
    return;
  }
  setText($("llm-strategy"), "Strategy: " + (state.providers.strategy || "unknown") + ". One row per endpoint.");
  var rows = state.providers.endpoints || [];
  var seen = {};
  rows.forEach(function (row) {
    var endpoint = row.endpoint || row.provider || row.model || "endpoint";
    seen[endpoint] = true;
    var record = state.providerEls[endpoint];
    if (!record) {
      var tr = h("tr", { dataset: { endpoint: endpoint } });
      var providerCell = h("td", { class: "provider-cell" }, providerIcon(row.provider || endpoint));
      providerCell.appendChild(h("span", { class: "provider-name", text: endpoint }));
      tr.appendChild(providerCell);
      tr.title = [row.provider ? "Provider: " + row.provider : "", row.model ? "Model: " + row.model : ""].filter(Boolean).join(" \u00b7 ");
      var stateCell = h("td", { class: "provider-state" });
      tr.appendChild(stateCell);
      var calls = h("td", { class: "num" });
      var errors = h("td", { class: "num" });
      var thisRun = h("td", { class: "num" });
      var note = h("td", { class: "note-cell" });
      tr.appendChild(calls); tr.appendChild(errors); tr.appendChild(thisRun); tr.appendChild(note);
      body.appendChild(tr);
      record = { tr: tr, stateCell: stateCell, calls: calls, errors: errors, thisRun: thisRun, note: note };
      state.providerEls[endpoint] = record;
    }
    var view = R.providerState(row.state);
    clear(record.stateCell);
    record.stateCell.appendChild(h("span", { class: "status-dot is-" + view.kind }));
    record.stateCell.appendChild(h("span", { class: "state-word is-" + view.kind, text: view.word }));
    record.calls.textContent = String(row.calls == null ? 0 : row.calls);
    record.errors.textContent = String(row.errors == null ? 0 : row.errors);
    record.note.textContent = row.reason || row.last_error || "";
    record.note.title = record.note.textContent;
  });
  Object.keys(state.providerEls).forEach(function (endpoint) {
    if (seen[endpoint]) return;
    var record = state.providerEls[endpoint];
    if (record.tr.parentNode) record.tr.parentNode.removeChild(record.tr);
    delete state.providerEls[endpoint];
  });
  paintThisRun();
}

function startProviderPoll() {
  if (state.providerPoll || document.hidden || state.demo || state.finished) return;
  state.providerPoll = setInterval(loadProviders, POLL_MS);
}
function stopProviderPoll() {
  if (state.providerPoll) clearInterval(state.providerPoll);
  state.providerPoll = null;
}
function syncProviderPoll() {
  if (state.demo) { stopProviderPoll(); return; }
  if (state.activeTab === "system" && !document.hidden && state.view === "run") {
    if (state.finished || state.failed) {
      stopProviderPoll();
      loadProviders();
      return;
    }
    startProviderPoll();
    loadProviders();
  } else {
    stopProviderPoll();
  }
}

var TAB_ORDER = ["matches", "possible", "activity", "system"];
function selectTab(name, focus) {
  if (TAB_ORDER.indexOf(name) < 0) name = "matches";
  state.activeTab = name;
  TAB_ORDER.forEach(function (tab) {
    var button = $("tab-" + tab);
    var selected = tab === name;
    button.setAttribute("aria-selected", selected ? "true" : "false");
    button.tabIndex = selected ? 0 : -1;
    $("panel-" + tab).hidden = !selected;
  });
  if (focus) $("tab-" + name).focus();
  syncProviderPoll();
}

function onTabKey(event) {
  var index = TAB_ORDER.indexOf(state.activeTab);
  if (event.key === "ArrowRight") index = (index + 1) % TAB_ORDER.length;
  else if (event.key === "ArrowLeft") index = (index + TAB_ORDER.length - 1) % TAB_ORDER.length;
  else if (event.key === "Home") index = 0;
  else if (event.key === "End") index = TAB_ORDER.length - 1;
  else return;
  event.preventDefault();
  selectTab(TAB_ORDER[index], true);
}

function copyText(text, message) {
  function fallback() {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(area);
    toast(ok ? message : "Copy did not work in this browser.");
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { toast(message); }).catch(fallback);
  } else {
    fallback();
  }
}

function downloadCsv() {
  if (!state.result) return;
  var blob = new Blob([R.csvText(state.result)], { type: "text/csv;charset=utf-8" });
  var url = URL.createObjectURL(blob);
  var link = document.createElement("a");
  link.href = url;
  link.download = "gem-price-comparison.csv";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
  toast("CSV downloaded.");
}

function updateTitle() {
  var title = "GeM Price Comparison";
  if (state.view === "run") {
    if (state.failed) title = "Failed, GeM Price Comparison";
    else if (state.finished && state.result) {
      var saving = R.savingInfo(state.result);
      if (saving && saving.cheaper) title = "Saves " + R.money(saving.amount) + " (" + saving.percent.toFixed(1) + "%), GeM Price Comparison";
      else if (saving && saving.dearer) title = R.money(Math.abs(saving.amount)) + " more (" + Math.abs(saving.percent).toFixed(1) + "%), GeM Price Comparison";
      else if (state.result.cheapest) title = "Best " + R.money(state.result.cheapest.price) + ", GeM Price Comparison";
      else title = "No confirmed match, GeM Price Comparison";
    } else if (state.active && state.startedAt) {
      title = "(" + R.clock(state.elapsedSeconds) + ") Running, GeM Price Comparison";
    } else if (state.queued) {
      title = "Waiting, GeM Price Comparison";
    }
  }
  if (document.title !== title) document.title = title;
}

/* Activity feed */
function addFeedEntry(event) {
  var line = R.describeEvent(event);
  if (!line) return;
  var li = h("li", { class: "feed-line is-" + line.kind + (line.level ? " is-" + line.level : "") + (line.key ? " is-key" : "") });
  li.appendChild(h("span", { class: "feed-text", text: line.text }));
  var details = Array.isArray(line.detail) ? line.detail.filter(Boolean) : (line.detail ? [line.detail] : []);
  if (details.length) {
    var detail = h("details", { class: "feed-detail" });
    detail.open = $("open-details").checked;
    detail.appendChild(h("summary", { text: "Details" }));
    var list = h("ul");
    details.forEach(function (item) { list.appendChild(h("li", { text: item })); });
    detail.appendChild(list);
    li.appendChild(detail);
  }
  var shop = R.eventShopKey(event);
  li.dataset.shop = shop || "";
  li.dataset.key = line.key ? "1" : "0";
  li.dataset.level = line.level || "";
  li.dataset.search = (line.text + " " + details.join(" ")).toLowerCase();
  $("activity-list").appendChild(li);
  var entry = { event: event, line: line, el: li };
  state.feed.push(entry);
  if (state.feed.length > MAX_FEED_LINES) {
    var old = state.feed.shift();
    if (old.el && old.el.parentNode) old.el.parentNode.removeChild(old.el);
    state.feedHidden += 1;
  }
  applyFeedFilter(entry);
  if ($("follow-latest").checked) feedScrollToEnd();
}
function feedScrollToEnd() { var feed = $("activity-list"); feed.scrollTop = feed.scrollHeight; }
function feedMatches(entry) {
  if (state.query && entry.el.dataset.search.indexOf(state.query) < 0) return false;
  if (state.filter === "all") return true;
  if (state.filter === "key") return entry.el.dataset.key === "1";
  if (state.filter === "warn") return entry.el.dataset.level === "warn" || entry.el.dataset.level === "bad";
  if (state.filter.indexOf("shop:") === 0) return entry.el.dataset.shop === state.filter.slice(5);
  return true;
}
function applyFeedFilter(entry) { entry.el.hidden = !feedMatches(entry); }
function applyFeedFilters() { state.feed.forEach(applyFeedFilter); paintActivityState(); }
function paintActivityState() {
  var visible = 0;
  state.feed.forEach(function (entry) { if (!entry.el.hidden) visible += 1; });
  $("activity-empty").hidden = visible > 0;
  if (!state.feed.length) setText($("activity-empty"), "Events appear here as the run moves.");
  else if (!visible) setText($("activity-empty"), "No activity lines match this filter.");
  setText($("activity-count"), state.feed.length ? visible + " of " + state.feed.length + " lines" + (state.feedHidden ? ", " + state.feedHidden + " older hidden" : "") : "");
  buildFeedFilters();
}
function buildFeedFilters() {
  var shops = Object.keys(state.stats.shops || {}).filter(function (key) { return key !== "other"; }).sort();
  var key = shops.join("|");
  if (key === state.filterShopsKey) return;
  state.filterShopsKey = key;
  var row = $("activity-filters");
  clear(row);
  var options = [["all", "All"], ["key", "Key steps"], ["warn", "Warnings"]];
  shops.forEach(function (shop) { options.push(["shop:" + shop, R.shopKey(shop)]); });
  options.forEach(function (option) {
    var button = h("button", { class: "filter-button", type: "button", "aria-pressed": state.filter === option[0] ? "true" : "false", text: option[1] });
    button.dataset.filter = option[0];
    button.addEventListener("click", function () {
      state.filter = option[0];
      Array.prototype.forEach.call(row.children, function (child) {
        child.setAttribute("aria-pressed", child.dataset.filter === state.filter ? "true" : "false");
      });
      applyFeedFilters();
    });
    row.appendChild(button);
  });
}

function bindEvents() {
  $("theme-toggle").addEventListener("click", function () {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });
  $("run-form").addEventListener("submit", function (event) {
    event.preventDefault();
    if (state.submitting) return;
    var url = $("gem-url").value.trim();
    if (!url) { openLinkDialog(); return; }
    if (!state.demo && !R.isProductUrl(url)) { openLinkDialog(); return; }
    formError("");
    startRun(url, false);
  });
  $("gem-url").addEventListener("input", function () { formError(""); });
  $("dialog-close").addEventListener("click", function (event) { event.preventDefault(); closeLinkDialog(); });
  $("link-dialog").addEventListener("close", function () { $("gem-url").focus(); });
  $("back-btn").addEventListener("click", function () {
    closeSource();
    stopProviderPoll();
    try { window.history.replaceState(null, "", window.location.pathname); } catch (e) { /* old browsers */ }
    showScreen("start");
    loadRuns();
  });
  $("run-again").addEventListener("click", function () {
    var url = state.lastUrl || $("gem-url").value.trim();
    if (url) startRun(url, true);
  });
  $("retry-connection").addEventListener("click", function () {
    if (!state.runId) return;
    closeSource();
    connect(state.runId, state.lastSeq);
    setText($("connection-text"), "Reconnecting to the run");
  });
  $("copy-summary").addEventListener("click", function () {
    if (!state.result) return;
    copyText(R.summaryText(state.result), "Summary copied.");
  });
  $("download-csv").addEventListener("click", downloadCsv);
  $("pipeline-toggle").addEventListener("click", function () {
    state.pipelineUserToggled = true;
    setPipelineCollapsed(!state.pipelineCollapsed);
  });
  $("map-lanes").addEventListener("click", function (event) {
    if (event.target && event.target.id === "shop-more") {
      state.shopsExpanded = !state.shopsExpanded;
      paintShopRows(state.stats);
    }
  });
  $("gem-specs-toggle").addEventListener("click", function () {
    state.specsExpanded = !state.specsExpanded;
    paintSpecs(gemModel());
  });
  $("gem-image").addEventListener("error", function () {
    $("gem-image").hidden = true;
    $("gem-image-fallback").hidden = false;
  });
  $("tablist").addEventListener("keydown", onTabKey);
  TAB_ORDER.forEach(function (tab) {
    $("tab-" + tab).addEventListener("click", function () { selectTab(tab, false); });
  });
  $("activity-search").addEventListener("input", function () {
    state.query = $("activity-search").value.trim().toLowerCase();
    applyFeedFilters();
  });
  $("follow-latest").addEventListener("change", function () { if ($("follow-latest").checked) feedScrollToEnd(); });
  $("open-details").addEventListener("change", function () {
    var open = $("open-details").checked;
    Array.prototype.forEach.call($("activity-list").querySelectorAll("details"), function (detail) { detail.open = open; });
  });
  document.addEventListener("visibilitychange", syncProviderPoll);
}

function init() {
  initTheme();
  bindEvents();
  selectTab("matches", false);
  loadHealth();
  loadRuns();
  updateTitle();
  paintRecent();
  setPipelineCollapsed(false);
  var params = new URLSearchParams(window.location.search);
  var runId = params.get("run");
  if (runId) openSavedRun(runId);
}
init();
})();

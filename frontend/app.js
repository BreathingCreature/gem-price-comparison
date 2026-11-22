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
var PROVIDER_RETRY_MS = 10000;
var PROVIDER_MAX_FAILURES = 3;
var FACTS_SHOWN = 8;
var GLOBE_PATH = "M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18";
var GEM_PATH = "M4 9l1.5-5h13L20 9M4 9v10h16V9M4 9c0 2 3 2 4 0 1 2 3 2 4 0 1 2 3 2 4 0 1 2 4 2 4 0M10 19v-5h4v5";
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
  highlights: [], shopRowOrder: [], shopRowEls: {},
  specsExpanded: false, providerPoll: null, providerEls: {},
  providerFailures: 0, providerRetryTimer: null,
  iconProbes: {}, shopManifest: null, shopManifestPromise: null,
  toastTimer: null
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

function svgEl(paths, className) {
  var ns = "http://www.w3.org/2000/svg";
  var el = document.createElementNS(ns, "svg");
  el.setAttribute("viewBox", "0 0 24 24");
  el.setAttribute("aria-hidden", "true");
  el.setAttribute("class", className || "svg");
  (Array.isArray(paths) ? paths : [paths]).forEach(function (d) {
    var p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    el.appendChild(p);
  });
  return el;
}

function globeGlyph() {
  return h("span", { class: "ico glyph" }, svgEl(GLOBE_PATH));
}
function gemGlyph() {
  return h("span", { class: "ico glyph" }, svgEl(GEM_PATH));
}
function chipGlyph() {
  return h("span", { class: "ico glyph" }, svgEl([
    "M9 3v2M15 3v2M9 19v2M15 19v2M3 9h2M3 15h2M19 9h2M19 15h2",
    "M7 7h10v10H7z"
  ]));
}
function externalArrow() {
  return svgEl(["M14 4h6v6M20 4l-9 9", "M18 14v5H5V6h5"]);
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

/* Icons: the bundled shop manifest first, then the site icon route, then a globe. */
function bareHost(value) {
  var text = String(value == null ? "" : value).trim().toLowerCase();
  text = text.replace(/^[a-z]+:\/\//, "").split("/")[0].split("?")[0].split("#")[0];
  text = text.replace(/:\d+$/, "");
  return text;
}

function hostCandidates(host) {
  var list = [host];
  var noWww = host.replace(/^www\./, "");
  if (noWww !== host) list.push(noWww);
  var parts = noWww.split(".");
  while (parts.length > 3) {
    parts.shift();
    list.push(parts.join("."));
  }
  return list;
}

function loadShopIcons() {
  if (state.shopManifestPromise) return state.shopManifestPromise;
  state.shopManifestPromise = fetch(API + "/shops/manifest.json")
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (body) {
      state.shopManifest = body && body.icons ? body.icons : {};
      return state.shopManifest;
    })
    .catch(function () { state.shopManifest = {}; return state.shopManifest; });
  return state.shopManifestPromise;
}

function manifestIcon(host) {
  var icons = state.shopManifest || {};
  var names = hostCandidates(host);
  for (var i = 0; i < names.length; i += 1) {
    if (icons[names[i]]) return icons[names[i]];
  }
  return "";
}

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
  var host = bareHost(domain);
  var wrap = h("span", { class: "site-icon", title: host });
  if (!host) { wrap.hidden = true; return wrap; }
  function showGlobe() {
    clear(wrap);
    wrap.classList.add("is-glyph");
    wrap.appendChild(svgEl(GLOBE_PATH));
  }
  function showImage(src) {
    clear(wrap);
    wrap.classList.remove("is-glyph");
    var img = h("img", { src: src, alt: "", width: "20", height: "20", loading: "lazy" });
    img.addEventListener("error", showGlobe);
    wrap.appendChild(img);
  }
  function choose() {
    var file = manifestIcon(host);
    if (file) { showImage(API + "/shops/" + encodeURIComponent(file)); return; }
    var api = API + "/api/icon/" + encodeURIComponent(host);
    probeIcon(api, function () { if (wrap.isConnected) showImage(api); });
  }
  if (state.shopManifest !== null) choose(); else showGlobe();
  loadShopIcons().then(function () { if (wrap.isConnected) choose(); });
  return wrap;
}

function modelIconEl(endpoint) {
  var info = R.modelIcon(endpoint);
  if (!info.key) return chipGlyph();
  var img = h("img", { class: "ico" + (info.mono ? " mono" : ""), src: API + "/icons/" + encodeURIComponent(info.key) + ".svg", alt: "" });
  img.addEventListener("error", function () {
    if (img.parentNode) img.parentNode.replaceChild(chipGlyph(), img);
  });
  return img;
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
      state.providerFailures = 0;
      state.providers = data;
      renderProviders();
      if (data && data.endpoints) {
        state.llmReady = data.endpoints.filter(function (row) { return row.state === "ready"; }).length;
      }
      renderLlmBanner();
      paintSystem();
    }).catch(function () {
      state.providers = null;
      state.providerFailures += 1;
      if (state.providerFailures >= PROVIDER_MAX_FAILURES) {
        stopProviderPoll();
        setText($("llm-strategy"), "LLM endpoint status is unavailable. Open the System tab again to retry.");
        return;
      }
      setText($("llm-strategy"), "LLM endpoint status is unavailable. Retrying in 10 seconds.");
      if (state.providerRetryTimer) clearTimeout(state.providerRetryTimer);
      state.providerRetryTimer = setTimeout(function () {
        state.providerRetryTimer = null;
        if (state.activeTab === "system" && !document.hidden && state.view === "run" && !state.finished) {
          startProviderPoll();
        }
      }, PROVIDER_RETRY_MS);
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
    var row = h("button", { class: "recent", type: "button" },
      h("span", { class: "t", text: title }),
      h("span", { class: "p", text: run.cheapest_price != null ? R.money(run.cheapest_price) : "No confirmed price" }),
      h("span", { class: "w", text: recentTime(run, status) + " \u00b7 " + recentLabel(status) }));
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
    state.highlights = (run.result.gem && run.result.gem.highlights) || [];
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
    case "gem_highlights":
      state.highlights = event.facts || [];
      if (state.result && state.result.gem) state.result.gem.highlights = state.highlights;
      state.renderedResult = null;
      paintGemCard();
      break;
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
  state.shopRowOrder = [];
  state.shopRowEls = {};
  state.highlights = [];
  state.specsExpanded = false;
  stopClock();
  setText($("run-clock"), "00:00");
  setText($("run-state-text"), "Starting");
  $("run-state-label").dataset.state = "queued";
  $("run-error").hidden = true;
  setText($("run-error-text"), "");
  $("connection-banner").hidden = true;
  setText($("page-alert"), "");
  $("run-again").hidden = true;
  selectTab("matches", false);
  $("gem-card").hidden = true;
  $("verdict").hidden = true;
  $("details-card").hidden = true;
  $("gem-url-link").hidden = true;
  setText($("gem-url-host"), "");
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
  clear($("possible-body"));
  clear($("similar-list"));
  clear($("warnings-list"));
  clear($("activity-list"));
  clear($("activity-filters"));
  clear($("run-stats"));
  clear($("shops-list"));
  $("shops-details").open = false;
  setText($("shops-count"), "");
  setText($("answer-kicker"), "Searching shops");
  setText($("verdict-title"), "Best price found");
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
  setText($("run-state-text"), label);
  $("run-state-label").dataset.state = kind;
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
      .then(function (run) {
        if (run && run.result) {
          state.result = run.result;
          state.highlights = (run.result.gem && run.result.gem.highlights) || [];
          state.renderedResult = null;
        }
        paintAll(); loadRuns(); scrollToSummary();
      })
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
    url: gem.url || state.lastUrl || "",
    title: gem.canonical_name || gem.title || stats.title || "",
    pageTitle: gem.title || stats.title || "",
    brand: gem.brand || stats.brand || "",
    model: gem.model || stats.model || "",
    price: price,
    image: gem.image || stats.image || "",
    specs: gem.specs || stats.specs || {},
    highlights: gem.highlights || []
  };
}

function paintGemCard() {
  var gem = gemModel();
  var card = $("gem-card");
  var hasProduct = !!(gem.title || gem.price);
  card.hidden = !hasProduct;
  if (!hasProduct) return;
  setText($("gem-title"), gem.title || gem.pageTitle || "Reading the GeM page");
  var href = R.safeHref(gem.url);
  var link = $("gem-url-link");
  if (href) {
    link.href = href;
    link.hidden = false;
    setText($("gem-url-host"), bareHost(href));
  } else {
    link.removeAttribute("href");
    link.hidden = true;
    setText($("gem-url-host"), "");
  }
  if (gem.price && gem.price.low != null) {
    setText($("gem-price"), R.moneyRange(gem.price));
    setText($("gem-price-kind"), R.priceKindLabel(gem.price.kind));
  } else {
    setText($("gem-price"), "Price not read");
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

function gemFacts(gem) {
  var rows = [];
  var fromResult = gem.highlights;
  var highlights = (Array.isArray(fromResult) && fromResult.length) ? fromResult : (state.highlights || []);
  if (highlights.length) {
    highlights.forEach(function (fact) {
      if (!fact || fact.label == null || fact.value == null) return;
      rows.push({ key: String(fact.label), value: String(fact.value) });
    });
  }
  if (rows.length) return rows;
  return specsEntries(gem);
}

function paintSpecs(gem) {
  var list = $("gem-specs");
  var toggle = $("gem-specs-toggle");
  clear(list);
  var rows = gemFacts(gem);
  if (!rows.length) {
    rows = [{ key: "Details", value: "No details were read from the page." }];
  }
  var limit = state.specsExpanded ? rows.length : FACTS_SHOWN;
  rows.slice(0, limit).forEach(function (row) {
    var value = row.value;
    var dd = h("dd", { text: value.length > 120 ? value.slice(0, 117) + "..." : value });
    if (value.length > 120) dd.title = value;
    list.appendChild(h("div", { class: "spec-row" }, h("dt", { text: row.key }), dd));
  });
  if (rows.length > FACTS_SHOWN) {
    toggle.hidden = false;
    setText(toggle, state.specsExpanded ? "Show fewer" : "Show all " + rows.length + " details");
  } else {
    toggle.hidden = true;
    state.specsExpanded = false;
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
  setText($("answer-shop"), model.shop || "");
  var shopIcon = $("answer-shop-icon");
  clear(shopIcon);
  if (model.shop) shopIcon.appendChild(siteIcon(model.shop));
  setText($("answer-price"), model.price != null ? R.money(model.price) : (model.headline || ""));
  var saving = result ? R.savingInfo(result) : null;
  var savingEl = $("answer-saving");
  savingEl.className = "save" + (saving && saving.dearer ? " is-dearer" : "");
  if (saving) {
    setText(savingEl, saving.cheaper ? "Saves " + R.money(saving.amount) + " (" + saving.percent.toFixed(1) + "%) against GeM"
      : saving.dearer ? "Costs " + R.money(Math.abs(saving.amount)) + " (" + Math.abs(saving.percent).toFixed(1) + "%) more than GeM"
      : "The same price as GeM");
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
  var gemPrice = result && result.gem && result.gem.price ? result.gem.price.low : null;
  model.rows.forEach(function (row) {
    var fill = h("i", { class: row.isGem ? "gem" : "" });
    fill.style.width = Math.max(0, Math.min(100, row.width)) + "%";
    var note = "";
    if (!row.isGem && gemPrice != null && row.price != null && Number(row.price) !== Number(gemPrice)) {
      note = (row.price < gemPrice ? "-" : "+") + R.money(Math.abs(Number(row.price) - Number(gemPrice)));
    }
    var value = h("span", { class: "val" }, R.money(row.price), note ? h("small", { text: note }) : "");
    container.appendChild(h("div", { class: "bar-row" },
      h("span", { class: "name" }, row.isGem ? gemGlyph() : siteIcon(row.label), h("span", { text: row.label })),
      h("span", { class: "bar-track" }, fill),
      value));
  });
}

/* The pipeline is built once and only its values change, so a row never moves. */
var LANE_STEPS = [["search", "Search"], ["filter", "Filter"], ["verify", "Verify"], ["confirmed", "Confirmed"]];

function laneCell(id, label, withSmall) {
  return h("div", { class: "cell", id: id, dataset: { label: label } },
    h("b", { id: id + "-value", text: "waiting" }),
    withSmall ? h("small", { id: id + "-small" }) : null);
}

function laneStateEl(id) {
  return h("span", { class: "state", id: id },
    h("span", { class: "dot", "aria-hidden": "true" }),
    h("span", { id: id + "-text", text: "Waiting" }));
}

function laneWho(icon, name) {
  return h("div", { class: "who" }, icon, h("span", null, name));
}

function buildPipeline() {
  var lanes = $("map-lanes");
  clear(lanes);
  state.shopRowEls = {};
  state.shopRowOrder = [];
  R.LANES.forEach(function (lane) {
    if (lane.id === "other") {
      lanes.appendChild(h("div", { class: "lane", id: "lane-other", dataset: { state: "waiting" } },
        laneWho(globeGlyph(), "Web search"),
        laneCell("lane-other-search", "Search", true),
        laneCell("lane-other-filter", "Filter"),
        laneCell("lane-other-verify", "Verify"),
        laneCell("lane-other-confirmed", "Confirmed"),
        laneStateEl("lane-other-status")));
      return;
    }
    lanes.appendChild(h("div", { class: "lane", id: "lane-" + lane.id, dataset: { state: "waiting" } },
      laneWho(siteIcon(lane.key), lane.label),
      laneCell("lane-" + lane.id + "-search", "Search", true),
      laneCell("lane-" + lane.id + "-filter", "Filter"),
      laneCell("lane-" + lane.id + "-verify", "Verify"),
      laneCell("lane-" + lane.id + "-confirmed", "Confirmed"),
      laneStateEl("lane-" + lane.id + "-status")));
  });
  lanes.appendChild(h("div", { class: "shop-rows", id: "shop-rows" }));
}

function setPipelineCollapsed(collapsed) {
  state.pipelineCollapsed = !!collapsed;
  var card = $("pipeline-card");
  var body = $("pipeline-body");
  var button = $("pipeline-toggle");
  if (card) card.dataset.open = state.pipelineCollapsed ? "false" : "true";
  if (body) body.hidden = state.pipelineCollapsed;
  if (button) button.setAttribute("aria-expanded", state.pipelineCollapsed ? "false" : "true");
}

function applyPipelineCollapsed() { setPipelineCollapsed(state.pipelineCollapsed); }

var STATE_WORDS = { found: "found", possible: "possible", not_found: "not found", not_searched: "blocked", incomplete: "incomplete", timed_out: "timed out" };
function stateWord(status) { return STATE_WORDS[status] || "waiting"; }

function stateView(entry) {
  var status = (entry && entry.status) || "";
  if (status === "found") return { name: "done", label: stateWord(status) };
  if (status === "possible") return { name: "warning", label: stateWord(status) };
  if (status === "not_found") return { name: "none", label: stateWord(status) };
  if (status === "not_searched") return { name: "blocked", label: stateWord(status) };
  if (status === "incomplete" || status === "timed_out") return { name: "warning", label: stateWord(status) };
  if (entry && entry.lastBlocked && entry.blocked) return { name: "blocked", label: "blocked" };
  if (entry && (entry.queries || entry.listings || entry.judged || entry.kept)) return { name: "active", label: "working" };
  return { name: "waiting", label: "waiting" };
}

function applyLaneView(laneEl, stateEl, textEl, view, title) {
  setState(laneEl, view.name);
  if (stateEl) stateEl.className = "state " + view.name;
  setText(textEl, view.label);
  laneEl.title = title || "";
}

function paintPipeline() {
  var stats = state.stats;
  var model = R.pipelineModel(stats);
  setStepLine("node-read", model.read);
  setStepLine("node-understand", model.understand);
  var laneModels = {};
  model.lanes.forEach(function (lane) { laneModels[lane.id] = lane; });
  ["flipkart", "amazon"].forEach(function (id) {
    var laneEl = $("lane-" + id);
    if (!laneEl) return;
    var entry = laneStats(id);
    var row = R.laneRowModel(entry);
    var view = stateView(entry);
    applyLaneView(laneEl, $("lane-" + id + "-status"), gel("lane-" + id + "-status-text"), view, laneTitle(id, laneModels[id] && laneModels[id].help));
    setNodeValue(gel("lane-" + id + "-search-value"), row.search, view.name);
    setText(gel("lane-" + id + "-search-small"), listingText(entry.seen || entry.listings || 0));
    setNodeValue(gel("lane-" + id + "-filter-value"), row.filter, entry.filtered ? "done" : view.name);
    setNodeValue(gel("lane-" + id + "-verify-value"), row.verify, entry.judged || row.confirmed ? "done" : view.name);
    setNodeValue(gel("lane-" + id + "-confirmed-value"), String(row.confirmed), row.confirmed ? "done" : view.name);
  });
  paintWebLane(stats);
  paintShopRows(stats);
}

function listingText(count) {
  var n = Number(count) || 0;
  if (!n) return "";
  return n + " listing" + (n === 1 ? "" : "s");
}

function setStepLine(id, node) {
  var line = $(id);
  if (line) setState(line, node.state);
  setNodeValue($(id + "-value"), node.value, node.state);
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
    el.className = "muted";
    return;
  }
  if (text === "waiting" || text === "-") {
    el.textContent = text;
    el.className = "muted";
    return;
  }
  var parts = text.split(" ");
  if (parts.length === 2 && /^[+-]?\d+$/.test(parts[0]) && /^-?\d+$/.test(parts[1])) {
    el.appendChild(h("span", { class: "pos", text: parts[0] }));
    el.appendChild(document.createTextNode(" "));
    el.appendChild(h("span", { class: parts[1] === "-0" ? "zero" : "neg", text: parts[1] }));
    return;
  }
  el.textContent = text;
  el.className = nodeState === "blocked" || nodeState === "warning" ? "warn"
    : nodeState === "done" || nodeState === "none" ? "pos"
    : nodeState === "active" ? "accent" : "muted";
}

function paintWebLane(stats) {
  var rows = R.laneRows(stats);
  var total = { listings: 0 };
  rows.forEach(function (row) { total.listings += Number(row.listings) || 0; });
  var pages = (stats.shops.other && stats.shops.other.listings) || total.listings || 0;
  var view = webStateView(stats, rows);
  applyLaneView(gel("lane-other"), gel("lane-other-status"), gel("lane-other-status-text"), view,
    rows.map(function (row) { return row.shop + ": " + pipelineRowTitle(row); }).join("\n"));
  setNodeValue(gel("lane-other-search-value"), pages ? pages + " page" + (pages === 1 ? "" : "s") : "waiting", view.name);
  setText(gel("lane-other-search-small"), rows.length ? rows.length + " shop" + (rows.length === 1 ? "" : "s") : "");
  ["filter", "verify", "confirmed"].forEach(function (step) {
    var el = gel("lane-other-" + step + "-value");
    if (!el) return;
    el.textContent = "-";
    el.className = "muted";
  });
}

function webStateView(stats, rows) {
  var status = webStatus(stats);
  if (status) {
    if (status === "found") return { name: "done", label: stateWord(status) };
    if (status === "possible") return { name: "warning", label: stateWord(status) };
    if (status === "not_found") return { name: "none", label: stateWord(status) };
    if (status === "not_searched") return { name: "blocked", label: stateWord(status) };
    return { name: "warning", label: stateWord(status) };
  }
  if (rows.length) return { name: "active", label: "searching" };
  if (stats.shops.other && stats.shops.other.listings) return { name: "active", label: "reading" };
  return { name: "waiting", label: "waiting" };
}

function webStatus(stats) {
  var rows = R.laneRows(stats);
  if (!rows.length) return "";
  if (rows.some(function (row) { return row.status === "found"; })) return "found";
  if (rows.some(function (row) { return row.status === "possible"; })) return "possible";
  if (rows.some(function (row) { return row.status === "not_searched"; })) return "not_searched";
  if (rows.some(function (row) { return row.status === "incomplete" || row.status === "timed_out"; })) {
    return rows.some(function (row) { return row.status === "timed_out"; }) ? "timed_out" : "incomplete";
  }
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
  if (!container) return;
  rows.forEach(function (row) {
    var record = state.shopRowEls[row.shop];
    if (!record) {
      var id = shopSafeId(row.shop);
      var el = h("div", { class: "lane sub shop-row", id: id, dataset: { state: "waiting" } },
        laneWho(siteIcon(row.shop), row.shop),
        laneCell(id + "-search", "Search"),
        laneCell(id + "-filter", "Filter"),
        laneCell(id + "-verify", "Verify"),
        laneCell(id + "-confirmed", "Confirmed"),
        laneStateEl(id + "-status"));
      container.appendChild(el);
      record = { id: id, el: el };
      state.shopRowEls[row.shop] = record;
      state.shopRowOrder.push(row.shop);
    }
    var model = R.laneRowModel(row);
    var view = stateView(row);
    applyLaneView(record.el, gel(record.id + "-status"), gel(record.id + "-status-text"), view, pipelineRowTitle(row));
    setNodeValue(gel(record.id + "-search-value"), "-", "waiting");
    setNodeValue(gel(record.id + "-filter-value"), model.filter, row.kept || row.dropped ? "done" : view.name);
    setNodeValue(gel(record.id + "-verify-value"), model.verify, row.judged || row.rejected ? "done" : view.name);
    setNodeValue(gel(record.id + "-confirmed-value"), String(model.confirmed), model.confirmed ? "done" : view.name);
  });
}

function paintMatches() {
  var result = state.result;
  var ready = state.finished;
  var rows = ready ? R.matchRows(result, state.gates) : [];
  var empty = $("matches-empty");
  var wrap = $("matches-table-wrap");
  var body = $("matches-body");
  clear(body);
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
    fragment.appendChild(matchRow(row, scale, gemPrice, best));
  });
  body.appendChild(fragment);
}

function matchRow(row, scale, gemPrice, best) {
  var el = h("div", { class: "m-row" + (best ? " best" : "") });
  el.appendChild(shopCell(row, best));
  el.appendChild(priceCell(row, scale, gemPrice));
  el.appendChild(confidenceCell(row));
  var button = h("button", { class: "m-open", type: "button", "aria-label": "Why this was accepted", "aria-expanded": "false" },
    svgEl("M6 9l6 6 6-6"));
  el.appendChild(button);
  var why = h("div", { class: "why" });
  fillWhy(why, row.reason, row.checks);
  el.appendChild(why);
  button.addEventListener("click", function () {
    var open = !el.classList.contains("open");
    el.classList.toggle("open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
  });
  return el;
}

function shopCell(row, best) {
  var cell = h("div", { class: "m-shop" });
  cell.appendChild(siteIcon(row.shop));
  var names = h("div", { class: "names" });
  var label = [h("span", { text: row.shop || "shop" }), externalArrow()];
  names.appendChild(linkOrText(row.url, label, { class: "name" }));
  if (best) names.appendChild(h("span", { class: "tag", text: "Best price" }));
  if (row.bulk) names.appendChild(h("span", { class: "tag", text: "B2B" }));
  if (!row.inStock) names.appendChild(h("span", { class: "tag is-bad", text: "out of stock" }));
  var title = h("div", { class: "title", text: row.title || "Untitled listing" });
  if (row.title) title.title = row.title;
  names.appendChild(title);
  cell.appendChild(names);
  return cell;
}

function priceCell(row, scale, gemPrice) {
  var cell = h("div", { class: "m-price" });
  cell.appendChild(h("strong", { text: row.priceText }));
  cell.appendChild(scaleEl(row, scale, gemPrice));
  return cell;
}

function scaleEl(row, scale, gemPrice) {
  var track = h("div", { class: "scale" }, h("span", { class: "mid" }));
  if (row.delta == null || gemPrice == null) {
    track.appendChild(h("span", { class: "amt", text: "GeM price unknown" }));
    return track;
  }
  var denom = scale.half * gemPrice;
  var fraction = denom > 0 ? Math.min(1, Math.abs(row.delta) / denom) : 0;
  var width = (fraction * 50).toFixed(1) + "%";
  if (row.delta !== 0) {
    var side = row.delta < 0 ? "cheap" : "dear";
    var fill = h("span", { class: "fill " + side });
    fill.style.width = width;
    var amount = h("span", { class: "amt " + side, text: (row.delta < 0 ? "-" : "+") + R.money(Math.abs(row.delta)) });
    amount.style.setProperty("--w", width);
    track.appendChild(fill);
    track.appendChild(amount);
  }
  track.setAttribute("role", "img");
  track.setAttribute("aria-label", row.delta === 0
    ? "The same price as GeM"
    : R.money(Math.abs(row.delta)) + (row.delta < 0 ? " cheaper than GeM" : " dearer than GeM"));
  return track;
}

function confidenceCell(row) {
  if (row.confidence == null) return h("div", { class: "m-conf muted", text: "-" });
  var low = Number(state.gates.low_confidence != null ? state.gates.low_confidence : 0.85);
  return h("div", { class: "m-conf" + (row.confidence < low ? " low" : ""), text: String(Math.round(row.confidence * 100)) });
}

function fillWhy(body, reason, checks) {
  if (reason) body.appendChild(h("p", { text: reason }));
  if (checks && checks.length) {
    var list = h("ul");
    checks.forEach(function (check) { list.appendChild(h("li", { text: check })); });
    body.appendChild(list);
  }
}

function paintPossible() {
  var result = state.result;
  var ready = state.finished;
  var rows = ready ? R.possibleRows(result) : [];
  var empty = $("possible-empty");
  var wrap = $("possible-table-wrap");
  var body = $("possible-body");
  clear(body);
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
  rows.forEach(function (row) { fragment.appendChild(possibleRow(row, scale, gemPrice)); });
  body.appendChild(fragment);
}

function possibleRow(row, scale, gemPrice) {
  var el = h("div", { class: "m-row possible" });
  el.appendChild(shopCell(row, false));
  el.appendChild(priceCell(row, scale, gemPrice));
  el.appendChild(confidenceCell(row));
  el.appendChild(h("span"));
  var why = h("div", { class: "why" });
  why.appendChild(h("p", { text: row.why }));
  el.appendChild(why);
  return el;
}

function paintSimilar() {
  var show = state.finished && R.showsSimilar(state.result);
  $("similar-section").hidden = !show;
  var list = $("similar-list");
  clear(list);
  if (!show) return;
  R.similarRows(state.result).forEach(function (row) {
    var head = h("div", { class: "similar-head" },
      siteIcon(row.shop),
      linkOrText(row.url, row.shop || "shop", { class: "name" }),
      h("span", { class: "price", text: row.priceText }));
    list.appendChild(h("article", { class: "similar-item" },
      head,
      h("span", { class: "title", text: row.title || "Untitled listing" }),
      h("p", { class: "note", text: "Similar, not the same product." })));
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
    row.appendChild(h("span", { class: "shop-fact-line", text: parts.join(" \u00b7 ") }));
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
      var el = h("div", { class: "p-row", dataset: { endpoint: endpoint } });
      var modelCell = h("div", { class: "p-model" },
        modelIconEl({ provider: row.provider, model: row.model || endpoint, endpoint: endpoint }),
        h("div", null,
          h("b", { text: row.model || endpoint }),
          h("small", { text: row.provider || R.providerLabel(endpoint) })));
      el.appendChild(modelCell);
      var stateCell = h("span", { class: "state muted" });
      el.appendChild(stateCell);
      var calls = h("span", { class: "num p-calls" });
      var errors = h("span", { class: "num p-errors" });
      var thisRun = h("span", { class: "num p-run muted" });
      var note = h("span", { class: "p-note" });
      el.appendChild(calls); el.appendChild(errors); el.appendChild(thisRun); el.appendChild(note);
      body.appendChild(el);
      record = { el: el, stateCell: stateCell, calls: calls, errors: errors, thisRun: thisRun, note: note };
      state.providerEls[endpoint] = record;
    }
    record.el.title = [row.provider ? "Provider: " + row.provider : "", row.model ? "Model: " + row.model : ""].filter(Boolean).join(" \u00b7 ");
    var view = R.providerState(row.state);
    clear(record.stateCell);
    record.stateCell.className = "state " + view.kind;
    record.stateCell.appendChild(h("span", { class: "dot", "aria-hidden": "true" }));
    record.stateCell.appendChild(h("span", { text: view.word }));
    record.calls.textContent = String(row.calls == null ? 0 : row.calls);
    record.errors.textContent = String(row.errors == null ? 0 : row.errors);
    record.note.textContent = row.reason || row.last_error || "";
    record.note.title = record.note.textContent;
  });
  Object.keys(state.providerEls).forEach(function (endpoint) {
    if (seen[endpoint]) return;
    var record = state.providerEls[endpoint];
    if (record.el.parentNode) record.el.parentNode.removeChild(record.el);
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
  if (state.providerRetryTimer) clearTimeout(state.providerRetryTimer);
  state.providerRetryTimer = null;
}
function syncProviderPoll() {
  if (state.demo) { stopProviderPoll(); return; }
  if (state.activeTab === "system" && !document.hidden && state.view === "run") {
    if (state.finished || state.failed) {
      stopProviderPoll();
      loadProviders();
      return;
    }
    state.providerFailures = 0;
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
  $("scroll-hint").addEventListener("click", function (event) {
    var target = $("recent-title");
    if (!target) return;
    event.preventDefault();
    var top = target.getBoundingClientRect().top + (window.pageYOffset || 0) - 60;
    try { window.scrollTo({ top: top, behavior: "auto" }); }
    catch (e) { window.scrollTo(0, top); }
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
  loadShopIcons();
  var params = new URLSearchParams(window.location.search);
  var runId = params.get("run");
  if (runId) openSavedRun(runId);
}
init();
})();

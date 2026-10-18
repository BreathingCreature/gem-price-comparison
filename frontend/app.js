// GeM Price Comparison frontend. Plain JavaScript, no build step.
// It talks to the backend only through the routes in docs/api.md.
(function () {
  "use strict";

  var API = window.GEM_API_BASE || "";
  var $ = function (id) { return document.getElementById(id); };

  // Steps we expect, in order. Unknown steps from the backend get appended.
  var STEP_LABELS = {
    extract: "Reading the GeM product page",
    normalize: "Working out what the product really is",
    search_flipkart: "Searching Flipkart",
    search_amazon: "Searching Amazon",
    search_both: "Searching Flipkart, Amazon and other shops at the same time",
    discovery: "Looking for other shops",
    match: "Checking which listings are the same product",
    match_flipkart: "Checking the Flipkart listings",
    match_amazon: "Checking the Amazon listings",
    match_other_shops: "Checking other shops' listings",
    retry: "Nothing matched yet, asking the LLM for better search words",
    retry_flipkart: "Searching Flipkart again",
    retry_amazon: "Searching Amazon again",
    match_retry: "Checking the new listings"
  };
  var EXPECTED = ["extract", "normalize", "search_flipkart", "search_amazon", "discovery", "match"];

  // B1/B2 (GEM_PARALLEL=1): the first pass runs three lanes at the same
  // time and their stage events arrive in any order. These steps must not
  // finish each other on arrival — that was the flicker. They stay active
  // together until a normal step (or the run itself) closes them all.
  var PHASE_STEPS = {
    search_both: true,
    discovery: true,
    match_flipkart: true,
    match_amazon: true,
    match_other_shops: true
  };

  var state;

  function freshState() {
    return {
      runId: null, startedAt: null, finished: false, source: null, events: [],
      steps: [], parallel: false, activeSteps: [], lastEventAt: null, lastText: "Starting...",
      llmCalls: [], checks: {}, searchRows: {}, timerId: null, finalElapsed: null, traceId: null
    };
  }
  state = freshState();

  // ---- tiny DOM helper (text only, so scraped titles can never inject HTML) ----
  function h(tag, attrs) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      if (k === "class") el.className = attrs[k];
      else if (k === "text") el.textContent = attrs[k];
      else el.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null) continue;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function money(v) {
    if (v == null || v === "" || isNaN(Number(v))) return "-";
    return "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }
  function mmss(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var m = Math.floor(seconds / 60), s = seconds % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }
  function shortHost(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch (e) { return url || ""; }
  }
  function safeHref(url) {
    // Scraped URLs are untrusted: only real web schemes become links,
    // everything else renders as plain text (never javascript:, data:, ...)
    if (typeof url !== "string") return null;
    var u = url.trim();
    return /^https?:\/\//i.test(u) ? u : null;
  }

  // ---- clock and the "right now" line ----
  function tick() {
    if (state.startedAt == null) return;
    var secs = state.finalElapsed != null ? state.finalElapsed : (Date.now() - state.startedAt) / 1000;
    $("elapsed").textContent = mmss(secs);
    if (!state.finished && state.lastEventAt) {
      var idle = Math.floor((Date.now() - state.lastEventAt) / 1000);
      var extra = idle >= 8 ? " (still working, last news " + idle + "s ago)" : "";
      $("now").textContent = state.lastText + extra;
    }
  }
  function startClock() {
    stopClock();
    state.timerId = setInterval(tick, 250);
  }
  function stopClock() {
    if (state.timerId) clearInterval(state.timerId);
    state.timerId = null;
  }
  function setPill(text, kind) {
    var pill = $("status-pill");
    pill.textContent = text;
    pill.className = "pill" + (kind ? " " + kind : "");
  }

  // ---- timeline ----
  function ensureStep(name) {
    var found = state.steps.filter(function (s) { return s.name === name; })[0];
    if (found) return found;
    var step = { name: name, label: STEP_LABELS[name] || name, status: "pending", start: null, secs: null };
    state.steps.push(step);
    return step;
  }
  function enterStep(name, label, t) {
    var opensPhase = name === "search_both";
    if (opensPhase) {
      // The parallel first pass replaces the one-shop steps entirely.
      state.parallel = true;
      dropPending(["search_flipkart", "search_amazon"]);
    }
    var soft = !opensPhase && state.parallel && PHASE_STEPS[name];
    if (soft && name.indexOf("match_") === 0) dropPending(["match"]);
    if (!soft) finishActiveSteps(t);
    var step = ensureStep(name);
    if (label) step.label = label;
    step.status = "active";
    step.start = t;
    step.secs = null;
    state.activeSteps.push(step);
    renderTimeline();
  }
  function dropPending(names) {
    state.steps = state.steps.filter(function (s) {
      return !(s.status === "pending" && names.indexOf(s.name) >= 0);
    });
  }
  function finishActiveSteps(t, failed) {
    state.activeSteps.forEach(function (s) {
      s.status = failed ? "failed" : "done";
      s.secs = Math.max(0, t - (s.start || 0));
    });
    state.activeSteps = [];
  }
  function renderTimeline() {
    var ol = $("timeline");
    clear(ol);
    state.steps.forEach(function (s) {
      var li = h("li", { class: s.status },
        h("span", { class: "dot" }),
        h("span", { text: s.label }),
        h("span", { class: "secs", text: s.secs != null ? s.secs.toFixed(1) + "s" : "" }));
      ol.appendChild(li);
    });
    renderStageTimes();
  }
  function renderStageTimes() {
    var body = $("stage-times").querySelector("tbody");
    clear(body);
    state.steps.forEach(function (s) {
      if (s.secs == null) return;
      body.appendChild(h("tr", null, h("td", { text: s.label }), h("td", { text: s.secs.toFixed(1) })));
    });
  }

  // ---- cards ----
  function showGemProduct(e) {
    var card = $("gem-card");
    clear(card);
    card.hidden = false;
    var dl = h("dl", { class: "kv" });
    function row(k, v) { if (v) { dl.appendChild(h("dt", { text: k })); dl.appendChild(h("dd", { text: String(v) })); } }
    row("Product", e.title);
    row("GeM price", e.price != null ? money(e.price) + " (" + (e.price_type || "unknown") + ")" : "not found on the page");
    row("Brand", e.brand);
    row("Model", e.model);
    row("Category", e.category);
    card.appendChild(h("h2", { text: "Found the GeM product" }));
    card.appendChild(dl);
  }
  function showNormalized(e) {
    var card = $("gem-card");
    var dl = h("dl", { class: "kv" });
    dl.appendChild(h("dt", { text: "Understood as" }));
    dl.appendChild(h("dd", { text: e.canonical_name || "" }));
    dl.appendChild(h("dt", { text: "Searching for" }));
    dl.appendChild(h("dd", { text: (e.queries || []).join("  |  ") }));
    card.appendChild(dl);
  }
  function searchCard(key, title) {
    var id = "search-" + key;
    var card = $(id);
    if (!card) {
      card = h("section", { class: "card", id: id }, h("h2", { text: title }));
      $("searches").appendChild(card);
    }
    return card;
  }
  function onSearchStarted(e) {
    var card = searchCard(e.marketplace, e.marketplace ? e.marketplace.charAt(0).toUpperCase() + e.marketplace.slice(1) : "Search");
    var status = h("span", { class: "muted", text: "searching..." });
    var list = h("ul", { class: "list" });
    card.appendChild(h("div", { class: "q" }, "Query: " + e.query + " ", status));
    card.appendChild(list);
    state.searchRows[e.marketplace + "|" + e.query] = { status: status, list: list };
  }
  function onSearchFinished(e) {
    var row = state.searchRows[e.marketplace + "|" + e.query];
    if (!row) { onSearchStarted(e); row = state.searchRows[e.marketplace + "|" + e.query]; }
    var text = e.blocked ? "blocked by the site (CAPTCHA)" : e.error ? "error: " + e.error : "found " + e.count + " product" + (e.count === 1 ? "" : "s");
    row.status.textContent = text;
    row.status.className = e.blocked || e.error ? "bad" : "ok";
    clear(row.list);
    (e.candidates || []).slice(0, 8).forEach(function (c) {
      row.list.appendChild(h("li", null, h("span", { text: c.title || c.url || "(no title)" }), h("span", { class: "price", text: money(c.price) })));
    });
    if ((e.candidates || []).length > 8) row.list.appendChild(h("li", { class: "muted" }, "+ " + (e.candidates.length - 8) + " more"));
  }
  function onDiscovery(e) {
    var card = searchCard("other", "Other shops");
    card.appendChild(h("div", { class: "q", text: (e.urls || []).length + " page(s) worth opening" }));
    var list = h("ul", { class: "list" });
    (e.urls || []).forEach(function (u) { list.appendChild(h("li", null, h("span", { text: u }))); });
    card.appendChild(list);
  }
  function onScrapeFinished(e) {
    var card = searchCard("other", "Other shops");
    var line = e.error ? "could not read: " + e.error : (e.title || "(no title)");
    card.appendChild(h("div", { class: "q", text: shortHost(e.url) + ": " + line + (e.price != null ? "  " + money(e.price) : "") }));
  }

  // ---- candidate checks ----
  function checkItem(url) {
    var item = state.checks[url];
    if (!item) {
      var li = h("li", { class: "checking" });
      $("check-list").appendChild(li);
      item = state.checks[url] = { li: li };
      $("checks").hidden = false;
    }
    return item;
  }
  function paintCheck(item, cls, head, verdict, why) {
    item.li.className = cls;
    clear(item.li);
    item.li.appendChild(h("div", null, h("span", { class: "verdict", text: verdict ? verdict + "  " : "" }), head));
    if (why) item.li.appendChild(h("div", { class: "why", text: why }));
  }
  function onChecking(e) {
    var item = checkItem(e.url);
    item.head = (e.title || e.url) + (e.price != null ? "  " + money(e.price) : "") + "  on " + (e.domain || shortHost(e.url));
    paintCheck(item, "checking", item.head + "  (" + e.index + " of " + e.total + ")", "Checking...", "");
  }
  function onVerdict(e) {
    var item = checkItem(e.url);
    var pct = Math.round((e.confidence || 0) * 100) + "%";
    item.head = item.head || (e.url);
    paintCheck(item, e.is_match ? "match" : "reject", item.head, e.is_match ? "Same product (" + pct + ")" : "Not the same (" + pct + ")", e.reason);
  }
  function onGateRejected(e) {
    var item = checkItem(e.url);
    paintCheck(item, "reject", item.head || e.url, "Rejected by a safety check", e.reason);
  }
  function onSkipped(e) {
    var item = checkItem(e.url);
    paintCheck(item, "skip", item.head || e.url, "Skipped", e.reason);
  }

  // ---- final result ----
  function showFinal(result, run) {
    var card = $("final");
    clear(card);
    card.hidden = false;
    var gem = result.gem_product || {};
    var cmp = result.comparison || {};
    var matches = result.matches || [];
    card.appendChild(h("h2", { text: "Result" }));
    if (result.from_cache) {
      var again = h("button", { type: "button", text: "Run it fresh" });
      again.addEventListener("click", function () { startRun(gem.url || $("gem-url").value, true); });
      card.appendChild(h("p", { class: "warn" }, "This is a saved result from an earlier run. ", again));
    }
    if (!matches.length) {
      card.appendChild(h("p", { text: "No confirmed matches on any marketplace." }));
    } else if (cmp.gem_price != null && cmp.cheapest_match_price != null) {
      var saving = cmp.savings_vs_gem;
      var msg = saving >= 0 ? "Cheaper elsewhere by " + money(saving) : "GeM is cheaper by " + money(-saving);
      card.appendChild(h("div", { class: "big " + (saving >= 0 ? "ok" : "warn"), text: msg }));
      card.appendChild(h("p", { class: "muted", text: "GeM " + money(cmp.gem_price) + "  vs  cheapest match " + money(cmp.cheapest_match_price) }));
    }
    if (matches.length) {
      var table = h("table", { class: "grid" }, h("thead", null, h("tr", null,
        h("th", { text: "Shop" }), h("th", { text: "Price" }), h("th", { text: "Vs GeM" }), h("th", { text: "Match" }), h("th", { text: "Link" }))));
      var body = h("tbody");
      matches.forEach(function (m) {
        var href = safeHref(m.candidate_url);
        var link = href
          ? h("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: "open" })
          : h("span", { class: "muted", text: m.candidate_url || "-" });
        var delta = m.delta_vs_gem == null ? "-" : (m.delta_vs_gem <= 0 ? "-" : "+") + money(Math.abs(m.delta_vs_gem));
        body.appendChild(h("tr", null, h("td", { text: m.source_domain }), h("td", { text: money(m.price_used) }),
          h("td", { class: m.cheaper_than_gem ? "ok" : "", text: delta }),
          h("td", { text: Math.round((m.confidence || 0) * 100) + "%" }), h("td", null, link)));
      });
      table.appendChild(body);
      card.appendChild(table);
    }
    if ((result.not_found_on || []).length) card.appendChild(h("p", { class: "muted", text: "Searched, nothing matching: " + result.not_found_on.join(", ") }));
    (result.search_issues || []).forEach(function (t) { card.appendChild(h("p", { class: "warn", text: "Search problem: " + t })); });
    (result.warnings || []).forEach(function (t) { card.appendChild(h("p", { class: "warn", text: "Warning: " + t })); });
  }

  // ---- advanced view ----
  function renderLlmCalls() {
    var body = $("llm-calls").querySelector("tbody");
    clear(body);
    var failed = 0, total = 0, secs = 0, perEndpoint = {};
    state.llmCalls.forEach(function (c) {
      total++; secs += c.seconds || 0;
      if (!c.ok) failed++;
      perEndpoint[c.endpoint] = (perEndpoint[c.endpoint] || 0) + 1;
      body.appendChild(h("tr", null, h("td", { text: String(c.t) }), h("td", { text: c.endpoint }), h("td", { text: c.purpose || "" }),
        h("td", { text: String(c.seconds) }), h("td", { class: c.ok ? "ok" : "bad", text: c.ok ? "ok" : (c.error || "failed") })));
    });
    if (total) {
      var parts = Object.keys(perEndpoint).map(function (k) { return k + ": " + perEndpoint[k]; });
      $("llm-summary").textContent = total + " call(s), " + failed + " failed, " + secs.toFixed(1) + "s waiting in total.  " + parts.join("   ");
    }
  }
  function addLogRow(e) {
    var filter = $("event-filter").value;
    var body = $("event-log").querySelector("tbody");
    if (filter && e.type.indexOf(filter) !== 0) return;
    var copy = {};
    Object.keys(e).forEach(function (k) { if (["seq", "t", "type"].indexOf(k) < 0) copy[k] = e[k]; });
    var details = JSON.stringify(copy);
    if (details.length > 300) details = details.slice(0, 300) + "...";
    body.appendChild(h("tr", null, h("td", { text: String(e.t) }), h("td", { text: String(e.seq) }), h("td", { text: e.type }), h("td", { text: details })));
  }
  function rebuildLog() {
    clear($("event-log").querySelector("tbody"));
    state.events.forEach(addLogRow);
  }
  function refreshDownload() {
    var blob = new Blob([JSON.stringify(state.events, null, 2)], { type: "application/json" });
    var a = $("download-events");
    if (a.dataset.url) URL.revokeObjectURL(a.dataset.url);
    a.dataset.url = URL.createObjectURL(blob);
    a.href = a.dataset.url;
  }
  function loadProviders() {
    fetch(API + "/api/llm/status").then(function (r) { return r.json(); }).then(function (data) {
      var body = $("providers").querySelector("tbody");
      clear(body);
      $("llm-strategy").textContent = data.error ? "Config problem: " + data.error : "Strategy: " + data.strategy;
      (data.endpoints || []).forEach(function (e) {
        var note = e.state === "disabled" ? e.reason : e.state === "cooling" ? "resting " + e.cooldown_remaining + "s. " + (e.last_error || "") : (e.last_error || "");
        var cls = e.state === "ready" ? "ok" : e.state === "cooling" ? "warn" : "bad";
        body.appendChild(h("tr", null, h("td", { text: e.endpoint }), h("td", { class: cls, text: e.state }), h("td", { text: String(e.calls) }),
          h("td", { text: String(e.errors) }), h("td", { text: e.avg_seconds == null ? "-" : String(e.avg_seconds) }), h("td", { text: note })));
      });
    }).catch(function () { /* the panel just stays as it was */ });
  }

  // ---- event handling ----
  var DESCRIBE = {
    run_started: function () { return "Starting the run..."; },
    cache_hit: function () { return "Found a saved result for this product."; },
    stage: function (e) { return (e.label || e.name) + "..."; },
    gem_product: function (e) { return "Found the GeM product: " + (e.title || ""); },
    normalized: function (e) { return "Understood the product as " + (e.canonical_name || "?"); },
    search_started: function (e) { return "Searching " + e.marketplace + " for \"" + e.query + "\"..."; },
    search_finished: function (e) { return e.marketplace + " returned " + e.count + " product(s)."; },
    discovery_finished: function (e) { return "Found " + (e.urls || []).length + " other page(s) to look at."; },
    scrape_started: function (e) { return "Opening " + shortHost(e.url) + "..."; },
    scrape_finished: function (e) { return "Read " + shortHost(e.url) + "."; },
    candidates_total: function (e) { return "Comparing " + e.count + " listing(s) with the GeM product..."; },
    candidate_checking: function (e) { return "Checking listing " + e.index + " of " + e.total + ": " + (e.title || e.url); },
    candidate_verdict: function (e) { return (e.is_match ? "Match: " : "Not a match: ") + (e.reason || ""); },
    retry_plan: function (e) { return "Trying again with: " + (e.new_queries || []).join(" | "); },
    llm_call: function (e) { return e.ok ? "LLM answered (" + e.endpoint + ")" : "LLM endpoint " + e.endpoint + " failed, switching..."; },
    llm_cooldown: function (e) { return "Resting " + e.endpoint + " for " + e.seconds + "s."; }
  };

  function handle(e) {
    state.events.push(e);
    state.lastEventAt = Date.now();
    var describe = DESCRIBE[e.type];
    if (describe) state.lastText = describe(e);

    switch (e.type) {
      case "stage": enterStep(e.name, e.label, e.t); break;
      case "gem_product": showGemProduct(e); break;
      case "normalized": showNormalized(e); break;
      case "search_started": onSearchStarted(e); break;
      case "search_finished": onSearchFinished(e); break;
      case "discovery_finished": onDiscovery(e); break;
      case "scrape_finished": onScrapeFinished(e); break;
      case "candidate_checking": onChecking(e); break;
      case "candidate_verdict": onVerdict(e); break;
      case "candidate_gate_rejected": onGateRejected(e); break;
      case "candidate_skipped": onSkipped(e); break;
      case "llm_call": state.llmCalls.push(e); renderLlmCalls(); break;
      case "run_finished":
        finishActiveSteps(e.t);
        state.traceId = e.trace_id || null;
        state.finalElapsed = e.t;
        state.lastText = "Done in " + mmss(e.t) + ".";
        renderTimeline();
        break;
      case "run_failed":
        finishActiveSteps(e.t, true);
        state.finalElapsed = e.t;
        state.lastText = "The run failed: " + e.error;
        renderTimeline();
        break;
    }
    addLogRow(e);
    tick();
  }

  function finishRun(status) {
    state.finished = true;
    stopClock();
    tick();
    $("now").textContent = state.lastText;
    setPill(status, status === "done" ? "done" : "failed");
    $("run-btn").disabled = false;
    refreshDownload();
    loadProviders();
    if (state.traceId && state.traceId.indexOf("demo") < 0) {
      var link = $("trace-link");
      link.href = API + "/api/traces/" + state.traceId;
      link.hidden = false;
    }
    if (state.source) { state.source.close(); state.source = null; }
    fetch(API + "/api/runs/" + state.runId).then(function (r) { return r.json(); }).then(function (run) {
      if (run.result) showFinal(run.result, run);
      if (run.error) {
        $("form-error").textContent = run.error;
        $("form-error").hidden = false;
      }
    });
  }

  function resetScreen() {
    if (state.source) state.source.close();
    stopClock();
    state = freshState();
    ["timeline", "searches", "check-list", "gem-card", "final"].forEach(function (id) { clear($(id)); });
    $("gem-card").hidden = true; $("final").hidden = true; $("checks").hidden = true;
    $("trace-link").hidden = true;
    clear($("event-log").querySelector("tbody"));
    clear($("llm-calls").querySelector("tbody"));
    clear($("stage-times").querySelector("tbody"));
    $("llm-summary").textContent = "No calls yet.";
    $("form-error").hidden = true;
    EXPECTED.forEach(ensureStep);
    renderTimeline();
  }

  function attach(runId, startedAtMs) {
    state.runId = runId;
    state.startedAt = startedAtMs || Date.now();
    state.lastEventAt = Date.now();
    state.lastText = "Waiting for the first update...";
    setPill("running", "running");
    $("run-btn").disabled = true;
    startClock();
    tick();
    try { history.replaceState(null, "", "?run=" + runId); } catch (e) { /* not important */ }
    var source = new EventSource(API + "/api/runs/" + runId + "/events");
    state.source = source;
    source.onmessage = function (msg) { handle(JSON.parse(msg.data)); };
    source.addEventListener("done", function (msg) { finishRun(JSON.parse(msg.data).status); });
    source.onerror = function () { if (state.finished) source.close(); };
    loadProviders();
  }

  function startRun(url, refresh) {
    resetScreen();
    $("gem-url").value = url;
    fetch(API + "/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gem_url: url, refresh: !!refresh })
    }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      if (!res.ok) {
        $("form-error").textContent = typeof res.body.detail === "string" ? res.body.detail : "The server did not accept that link.";
        $("form-error").hidden = false;
        return;
      }
      attach(res.body.run_id, Date.now());
    }).catch(function () {
      $("form-error").textContent = "Could not reach the server. Is it running?";
      $("form-error").hidden = false;
    });
  }

  // ---- page setup ----
  document.querySelectorAll(".tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (b) { b.classList.toggle("active", b === btn); });
      $("view-progress").hidden = btn.dataset.tab !== "progress";
      $("view-advanced").hidden = btn.dataset.tab !== "advanced";
      if (btn.dataset.tab === "advanced") loadProviders();
    });
  });
  $("event-filter").addEventListener("change", rebuildLog);
  $("run-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    startRun($("gem-url").value.trim(), $("refresh").checked);
  });
  setInterval(function () {
    if (!$("view-advanced").hidden && !state.finished) loadProviders();
  }, 3000);

  resetScreen();
  fetch(API + "/api/health").then(function (r) { return r.json(); }).then(function (h) { $("demo-badge").hidden = !h.demo; }).catch(function () {});

  var existing = new URLSearchParams(location.search).get("run");
  if (existing) {
    fetch(API + "/api/runs/" + existing).then(function (r) { return r.ok ? r.json() : null; }).then(function (run) {
      if (run) {
        $("gem-url").value = run.gem_url;
        attach(run.id, run.started ? run.started * 1000 : Date.now());
      }
    });
  }
})();

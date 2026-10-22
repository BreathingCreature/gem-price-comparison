// GeM Price Comparison frontend. Plain JavaScript, no build step.
// It talks to the backend only through the routes in docs/api.md and uses
// frontend/render.js for every formatted string and every sentence of the
// live activity feed. All text goes in through h() / textContent; scraped
// pages never get to inject markup.
(function () {
  "use strict";

  var API = window.GEM_API_BASE || "";
  var R = window.GemRender;
  var $ = function (id) { return document.getElementById(id); };

  var MAX_FEED_LINES = 600;  // the browser stays fast on very long runs

  var state;

  function freshState() {
    return {
      runId: null, source: null, active: false, finished: false,
      result: null, retrying: [], cards: {}, stats: R.newStats(),
      feedLines: 0, hiddenLines: 0, timerId: null, startedAt: null
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
      else if (k === "hidden") el.hidden = !!attrs[k];
      else el.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null) continue;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }
  function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); }
  function linkOrText(url) {
    var href = R.safeHref(url);
    if (href) return h("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: "Open" });
    return h("span", { class: "muted", text: url || "-" });
  }
  function setPill(text, kind) {
    var pill = $("status-pill");
    pill.textContent = text;
    pill.className = "pill" + (kind ? " " + kind : "");
  }
  function money(value) { return R.money(value); }

  // ---- start screen ----
  function loadRecent() {
    fetch(API + "/api/runs").then(function (r) { return r.json(); }).then(function (body) {
      var list = $("recent-runs");
      clear(list);
      var runs = (body && body.runs) || [];
      $("recent-empty").hidden = runs.length > 0;
      runs.forEach(function (run) {
        var price = run.cheapest_price != null ? "cheapest " + money(run.cheapest_price) : "not found";
        var when = run.checked_at ? String(run.checked_at).replace("T", " ").slice(0, 16) : (run.live ? "now" : "");
        var button = h("button", { type: "button", class: "recent-item" },
          h("span", { class: "recent-title", text: run.title || run.gem_url || "(untitled run)" }),
          h("span", { class: "muted", text: price + (when ? "  ·  " + when : "") }));
        button.addEventListener("click", function () { openSavedRun(run.id); });
        list.appendChild(h("li", null, button));
      });
    }).catch(function () { /* the list simply stays empty */ });
  }

  function showScreen(which) {
    $("start-screen").hidden = which !== "start";
    $("result-screen").hidden = which !== "result";
  }

  // ---- the product box: what GeM says, what the LLM understood ----
  function renderHeader(result) {
    var gem = (result && result.gem) || {};
    var seen = state.stats.gem || {};
    var title = gem.title || seen.title;
    if (title) $("result-title").textContent = title;
    var price = gem.price || null;
    var low = price ? price.low : seen.price;
    var high = price ? price.high : null;
    var kind = price ? (price.kind || "unknown") : (seen.priceType || "price");
    if (low != null) {
      $("result-price").textContent = "GeM price " + money(low) + (high != null && high !== low ? " to " + money(high) : "") + " (" + kind + ")";
    } else if (result || state.stats.gem) {
      $("result-price").textContent = "GeM price not found on the page";
    }
    if (result) {
      $("result-state").textContent = R.stateLabel(result, state.finished ? "done" : "running");
      $("result-state").className = "pill " + (result.from_cache ? "cached" : (state.finished ? "done" : "running"));
      $("cheapest-line").textContent = result.matches && result.matches.length || result.cheapest ? R.cheapestLine(result) : "";
    }
    renderChips(gem);
  }

  function renderChips(gem) {
    var box = $("product-chips");
    clear(box);
    var p = state.stats.product;
    var brand = (p && p.brand) || gem.brand;
    var model = (p && p.model) || gem.model;
    if (p && p.name) box.appendChild(h("span", { class: "chip", text: "understood as: " + p.name }));
    if (brand) box.appendChild(h("span", { class: "chip", text: "brand: " + brand }));
    if (model) box.appendChild(h("span", { class: "chip", text: "model: " + model }));
    if (p) p.queries.forEach(function (q) { box.appendChild(h("span", { class: "chip query", text: "search: " + q })); });
  }

  // ---- progress strip ----
  function renderSteps() {
    var list = $("steps");
    clear(list);
    R.STEPS.forEach(function (step) {
      var cls = state.stats.doneSteps[step.id] ? "done" : (state.stats.step === step.id && !state.finished ? "active" : "");
      list.appendChild(h("li", { class: cls, text: step.label }));
    });
  }

  // ---- shop cards ----
  function cardOrder(key) {
    if (key === "flipkart.com") return 0;
    if (key === "amazon.in") return 1;
    if (key === "other") return 9;
    return 5;
  }

  function cardFor(shop) {
    if (state.cards[shop]) return state.cards[shop];
    var badge = h("span", { class: "badge", text: "waiting" });
    var big = h("p", { class: "big", text: "" });
    var live = h("p", { class: "line", text: "" });
    var doing = h("p", { class: "doing", text: "" });
    var reason = h("p", { class: "reason muted" });
    var fill = h("span");
    var bar = h("div", { class: "bar" }, fill);
    var el = h("article", { class: "shop-card shop-running", "data-order": String(cardOrder(shop)) },
      h("div", { class: "row" }, h("h3", { text: shop === "other" ? "other shops" : shop }), badge),
      big, live, doing, reason, bar);
    var container = $("shop-cards");
    var before = null;
    for (var i = 0; i < container.children.length; i++) {
      if (Number(container.children[i].getAttribute("data-order")) > cardOrder(shop)) { before = container.children[i]; break; }
    }
    container.insertBefore(el, before);
    state.cards[shop] = { el: el, badge: badge, big: big, live: live, doing: doing, reason: reason, fill: fill, final: false };
    return state.cards[shop];
  }

  // Counters and "what it is doing now", from the event counters.
  function paintLive(shop) {
    var card = cardFor(shop);
    var s = state.stats.shops[shop];
    if (!s) return;
    card.live.textContent = R.liveShopLine(s);
    if (!card.final) {
      card.doing.textContent = s.activity || "";
      var total = Math.max(s.checking, s.judged, 1);
      var pct = s.judged ? Math.round(100 * s.judged / total) : (s.queries ? Math.round(100 * s.queriesDone / s.queries / 2) : 0);
      card.fill.style.width = Math.min(pct, 100) + "%";
    }
  }

  function fillCard(shop, entry) {
    var card = cardFor(shop);
    card.final = true;
    card.badge.textContent = (entry.status || "unknown").replace(/_/g, " ");
    var retry = R.retryLabel(shop, state.retrying);
    card.big.textContent = entry.matches > 0 && entry.price != null ? money(entry.price) : "";
    card.live.textContent = R.shopSummary(entry);
    card.doing.textContent = retry;
    card.reason.textContent = R.shopNeedsReason(entry) ? (entry.reason || "") : "";
    card.fill.style.width = "100%";
    card.el.className = "shop-card " + R.statusClass(entry.status) + (retry ? " shop-retrying" : "");
    card.el.dataset.shop = shop;
  }

  function renderMatches(result) {
    result = result || {};
    var rows = R.matchRows(result);
    clear($("matches-body"));
    rows.forEach(function (row) {
      $("matches-body").appendChild(h("tr", { class: row.inStock ? "" : "out-of-stock" },
        h("td", { text: row.bulk ? row.shop + " (B2B)" : row.shop }),
        h("td", { text: row.priceText }),
        h("td", { class: row.cheaper ? "ok" : "", text: row.vsGem }),
        h("td", { text: row.confidence }),
        h("td", { text: row.reason }),
        h("td", null, linkOrText(row.url))));
    });
    $("matches-section").hidden = rows.length === 0 && !state.active;
    $("matches-empty").hidden = rows.length > 0;

    var possible = R.possibleRows(result);
    clear($("possible-body"));
    possible.forEach(function (row) {
      $("possible-body").appendChild(h("tr", null,
        h("td", { text: row.shop }),
        h("td", { text: row.title }),
        h("td", { text: row.priceText }),
        h("td", { text: row.why }),
        h("td", null, linkOrText(row.url))));
    });
    $("possible-section").hidden = possible.length === 0;

    var similar = R.similarRows(result);
    clear($("similar-list"));
    similar.forEach(function (row) {
      $("similar-list").appendChild(h("li", null,
        h("span", { text: row.title + "  (" + row.shop + ")" }),
        h("span", { class: "price", text: row.priceText }),
        linkOrText(row.url)));
    });
    // The similar list is only for a run with nothing at all; a match makes
    // "similar" noise.
    $("similar-section").hidden = !(R.showsSimilar(result) && !state.active);

    var warnings = result.warnings || [];
    clear($("warnings-list"));
    warnings.forEach(function (text) { $("warnings-list").appendChild(h("li", { text: text })); });
    $("warnings-section").hidden = warnings.length === 0;
  }

  function renderResult(result) {
    if (!result) return;
    state.result = result;
    Object.keys(result.shops || {}).forEach(function (shop) { fillCard(shop, result.shops[shop]); });
    renderHeader(result);
    renderMatches(result);
  }

  function renderRetrying() {
    state.retrying.forEach(function (shop) {
      var card = state.cards[shop];
      if (card) {
        card.doing.textContent = "↻ retrying…";
        card.el.className = card.el.className + " shop-retrying";
        card.final = false;
        card.fill.style.width = "10%";
      }
    });
  }

  // ---- live activity feed ----
  var ICONS = { step: "▶", search: "⌕", filter: "≡", llm: "✶", verdict: "✔", shop: "■", retry: "↻", warn: "!", info: "i" };

  function formatTime(seconds) {
    return R.clock(seconds);
  }

  function addFeedLine(event) {
    var line = R.describeEvent(event);
    if (!line) return;
    var feed = $("feed");
    var main = h("span", { class: "main", text: line.text });
    var body;
    if (line.detail && line.detail.length) {
      var sub = h("div", { class: "sub-lines" });
      line.detail.forEach(function (text) { sub.appendChild(h("div", { text: text })); });
      var details = h("details", null, h("summary", null, main), sub);
      if ($("open-details").checked) details.open = true;
      body = details;
    } else {
      body = main;
    }
    var cls = (line.level || "") + (line.key ? "" : " detail-only");
    var item = h("li", { class: cls.trim() },
      h("span", { class: "t", text: formatTime(event.t) }),
      h("span", { class: "icon", text: ICONS[line.kind] || "•" }),
      h("span", { class: "msg" }, body));
    var follow = $("auto-scroll").checked;
    feed.appendChild(item);
    state.feedLines += 1;
    while (feed.children.length > MAX_FEED_LINES) {
      feed.removeChild(feed.firstChild);
      state.hiddenLines += 1;
    }
    $("feed-empty").hidden = true;
    $("activity-count").textContent = state.feedLines + " steps" + (state.hiddenLines ? " (older " + state.hiddenLines + " hidden)" : "");
    if (follow) feed.scrollTop = feed.scrollHeight;
  }

  function renderLlmLine() {
    var llm = state.stats.llm;
    var text = "";
    if (llm.calls || llm.cached) {
      text = llm.calls + " LLM call" + (llm.calls === 1 ? "" : "s") + (llm.failed ? " (" + llm.failed + " failed)" : "") +
        (llm.cached ? ", " + llm.cached + " reused" : "") + (llm.seconds ? ", " + llm.seconds.toFixed(1) + " s of waiting in total" : "");
    }
    $("llm-line").textContent = text;
  }

  function applyFeedMode() {
    $("feed").className = "feed" + ($("show-all").checked ? "" : " compact");
  }

  // ---- saved runs: replay the events that the trace kept ----
  function replayTrace(traceId) {
    if (!traceId) return;
    fetch(API + "/api/traces/" + traceId).then(function (r) { return r.ok ? r.json() : null; }).then(function (trace) {
      if (!trace || !trace.events || !trace.events.length) {
        $("feed-empty").hidden = false;
        return;
      }
      trace.events.forEach(function (event) {
        try {
          R.applyEvent(state.stats, event);
          addFeedLine(event);
        } catch (e) { /* one odd event must not stop the replay */ }
      });
      Object.keys(state.stats.shops).forEach(function (shop) { if (!state.cards[shop]) cardFor(shop); });
      if (state.result) renderResult(state.result);
      renderSteps();
      renderLlmLine();
      renderHeader(state.result);
    }).catch(function () { $("feed-empty").hidden = false; });
  }

  // ---- LLM status ----
  function loadProviders() {
    fetch(API + "/api/llm/status").then(function (r) { return r.json(); }).then(function (data) {
      clear($("providers-body"));
      $("llm-strategy").textContent = data.error ? "Config problem: " + data.error : "Strategy: " + data.strategy;
      (data.endpoints || []).forEach(function (endpoint) {
        $("providers-body").appendChild(h("tr", null,
          h("td", { text: endpoint.endpoint }),
          h("td", { class: endpoint.state === "ready" ? "ok" : endpoint.state === "limit" ? "bad" : "warn", text: endpoint.state }),
          h("td", { text: String(endpoint.calls) }),
          h("td", { text: String(endpoint.errors) }),
          h("td", { text: endpoint.reason || endpoint.last_error || "" })));
      });
    }).catch(function () { /* the panel just stays as it was */ });
  }

  // ---- clock ----
  function tick() {
    if (!state.active || !state.startedAt) return;
    $("clock").textContent = R.clock((Date.now() - state.startedAt) / 1000);
  }
  function startClock() { stopClock(); $("clock").hidden = false; state.timerId = setInterval(tick, 500); }
  function stopClock() { if (state.timerId) clearInterval(state.timerId); state.timerId = null; }

  // ---- events ----
  function handle(event) {
    // Every event first updates the counters and adds a feed line; the cases
    // below only do the extra work that some events need.
    R.applyEvent(state.stats, event);
    addFeedLine(event);
    Object.keys(state.stats.shops).forEach(function (shop) { paintLive(shop); });
    renderSteps();
    renderLlmLine();
    if (event.type === "gem_product" || event.type === "normalized") renderHeader(state.result);

    switch (event.type) {
      case "run_started":
        state.startedAt = Date.now();
        state.active = true;
        $("run-btn").disabled = true;
        setPill("running", "running");
        break;
      case "shop_started":
        cardFor(R.shopKey(event.shop));
        break;
      case "shop_finished":
        fillCard(R.shopKey(event.shop), event);
        break;
      case "shop_updated":
        state.retrying = state.retrying.filter(function (s) { return s !== event.shop; });
        fillCard(R.shopKey(event.shop), event);
        break;
      case "retry_started":
        state.retrying = event.shops || [];
        renderRetrying();
        $("result-state").textContent = "updating…";
        break;
      case "run_finished":
        state.result = event.result || state.result;
        if (state.result) renderResult(state.result);
        break;
      case "run_updated":
        state.result = event.result || state.result;
        state.retrying = [];
        if (state.result) renderResult(state.result);
        break;
      case "similar_ready":
        if (state.result) {
          state.result.similar = event.similar || [];
          renderMatches(state.result);
        }
        break;
      case "done":
        finishRun("done");
        break;
      case "run_failed":
        finishRun("failed");
        showFailure(event.error || event.message || "The run failed.");
        break;
      default:
        break; // unknown event types are ignored on purpose
    }
  }

  function showFailure(message) {
    $("form-error").textContent = message;
    $("form-error").hidden = false;
  }

  function finishRun(kind) {
    state.finished = true;
    state.active = false;
    stopClock();
    $("run-btn").disabled = false;
    setPill(kind === "failed" ? "failed" : "done", kind);
    renderSteps();
    if (state.result) {
      renderHeader(state.result);
      renderMatches(state.result);
    }
    if (state.source) { state.source.close(); state.source = null; }
    loadRecent();
  }

  function attach(runId) {
    state.runId = runId;
    if (state.source) state.source.close();
    var source = new EventSource(API + "/api/runs/" + runId + "/events");
    state.source = source;
    source.onmessage = function (msg) {
      var event;
      try { event = JSON.parse(msg.data); } catch (e) { return; }
      // One malformed or unexpected event must never blank the page.
      try { handle(event); } catch (e) {
        if (window.console && console.warn) console.warn("event handler failed", e);
      }
    };
    source.addEventListener("done", function (msg) {
      var payload = {};
      try { payload = JSON.parse(msg.data); } catch (e) { payload = {}; }
      finishRun(payload.status === "failed" ? "failed" : "done");
    });
    source.onerror = function () {
      // EventSource reconnects on its own and sends Last-Event-ID, so the
      // stream continues exactly after the last event this page saw.
      if (state.finished) source.close();
    };
    startClock();
    tick();
  }

  function resetRun() {
    if (state.source) state.source.close();
    stopClock();
    state = freshState();
    clear($("shop-cards"));
    clear($("matches-body"));
    clear($("possible-body"));
    clear($("similar-list"));
    clear($("warnings-list"));
    clear($("feed"));
    clear($("product-chips"));
    $("feed-empty").hidden = false;
    $("activity-count").textContent = "";
    $("llm-line").textContent = "";
    $("clock").hidden = true;
    ["matches-section", "possible-section", "similar-section", "warnings-section"].forEach(function (id) { $(id).hidden = true; });
    $("form-error").hidden = true;
    setPill("running", "running");
    renderSteps();
  }

  function startRun(url, refresh) {
    resetRun();
    showScreen("result");
    $("gem-url").value = url;
    $("result-title").textContent = "Starting…";
    $("result-price").textContent = "";
    $("result-state").textContent = "running";
    $("cheapest-line").textContent = "";
    $("result-state").className = "pill running";
    $("clock").textContent = "00:00";
    fetch(API + "/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gem_url: url, refresh: !!refresh })
    }).then(function (r) {
      return r.json().then(function (body) { return { ok: r.ok, body: body }; });
    }).then(function (res) {
      if (!res.ok) {
        showFailure(typeof res.body.detail === "string" ? res.body.detail : "The server did not accept that link.");
        $("run-btn").disabled = false;
        return;
      }
      try { history.replaceState(null, "", "?run=" + res.body.run_id); } catch (e) { /* not important */ }
      attach(res.body.run_id);
    }).catch(function () {
      showFailure("Could not reach the server. Is it running?");
      $("run-btn").disabled = false;
    });
  }

  function openSavedRun(runId) {
    resetRun();
    showScreen("result");
    setPill("saved", "cached");
    fetch(API + "/api/runs/" + runId).then(function (r) { return r.ok ? r.json() : null; }).then(function (run) {
      if (!run || !run.result) {
        showFailure("That saved run could not be opened.");
        return;
      }
      state.runId = run.id;
      state.result = run.result;
      state.finished = true;
      if (run.result.from_cache === false) run.result.from_cache = true;
      $("gem-url").value = run.gem_url || "";
      renderResult(run.result);
      renderHeader(run.result);
      setPill("cached", "cached");
      replayTrace(run.result.trace_id);
    }).catch(function () { showFailure("Could not reach the server. Is it running?"); });
  }

  // ---- page setup ----
  $("run-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var url = $("gem-url").value.trim();
    if (!/^https?:\/\/[^/]*gem\.gov\.in\//i.test(url)) {
      showFailure("That does not look like a GeM product link (gem.gov.in).");
      return;
    }
    startRun(url, $("refresh").checked);
  });
  $("llm-toggle").addEventListener("click", function () {
    var body = $("llm-body");
    body.hidden = !body.hidden;
    $("llm-toggle").textContent = body.hidden ? "Show LLM endpoints" : "Hide LLM endpoints";
    if (!body.hidden) loadProviders();
  });
  $("show-all").addEventListener("change", applyFeedMode);
  $("open-details").addEventListener("change", function () {
    var open = $("open-details").checked;
    var all = $("feed").querySelectorAll("details");
    for (var i = 0; i < all.length; i++) all[i].open = open;
  });

  renderSteps();
  applyFeedMode();
  loadRecent();
  fetch(API + "/api/health").then(function (r) { return r.json(); }).then(function (body) {
    $("demo-badge").hidden = !body.demo;
  }).catch(function () {});

  var existing = new URLSearchParams(location.search).get("run");
  if (existing) {
    openSavedRun(existing);
  } else {
    showScreen("start");
  }
})();

// GeM Price Comparison frontend. Plain JavaScript, no build step.
// It talks to the backend only through the routes in docs/api.md and uses
// frontend/render.js for every formatted string. All text goes in through
// h() / textContent; scraped pages never get to inject markup.
(function () {
  "use strict";

  var API = window.GEM_API_BASE || "";
  var R = window.GemRender;
  var $ = function (id) { return document.getElementById(id); };

  var state;

  function freshState() {
    return {
      runId: null, source: null, active: false, finished: false,
      result: null, retrying: [], cards: {}, lastText: "Waiting for the first update...",
      traceLoaded: null, timerId: null, startedAt: null, elapsed: 0
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
          h("span", { class: "muted", text: price + (when ? "  \u00b7  " + when : "") }));
        button.addEventListener("click", function () { openSavedRun(run.id); });
        list.appendChild(h("li", null, button));
      });
    }).catch(function () { /* the list simply stays empty */ });
  }

  // ---- result screen ----
  function showScreen(which) {
    $("start-screen").hidden = which !== "start";
    $("result-screen").hidden = which !== "result";
  }

  function renderHeader(result) {
    if (!result) return;
    var gem = result.gem || {};
    $("result-title").textContent = gem.title || "Product";
    var price = gem.price || {};
    var kind = price.kind || "unknown";
    $("result-price").textContent = price.low != null
      ? "GeM price " + money(price.low) + (price.high != null && price.high !== price.low ? " to " + money(price.high) : "") + " (" + kind + ")"
      : "GeM price not found on the page";
    $("result-state").textContent = R.stateLabel(result, state.finished ? "done" : "running");
    $("result-state").className = "pill " + (result.from_cache ? "cached" : "running");
    $("cheapest-line").textContent = R.cheapestLine(result);
  }

  function cardFor(shop) {
    if (state.cards[shop]) return state.cards[shop];
    var title = h("h3", { text: shop });
    var line = h("p", { class: "muted", text: "searching\u2026" });
    var reason = h("p", { class: "reason muted" });
    var extra = h("div", { class: "drop" });
    var card = h("article", { class: "shop-card shop-running" }, title, line, reason, extra);
    card.addEventListener("click", function () { loadTrace(shop); });
    $("shop-cards").appendChild(card);
    state.cards[shop] = { el: card, line: line, reason: reason };
    return state.cards[shop];
  }

  function fillCard(shop, entry) {
    var card = cardFor(shop);
    card.line.textContent = R.shopSummary(entry);
    card.reason.textContent = R.shopNeedsReason(entry) ? (entry.reason || "") : "";
    var retry = R.retryLabel(shop, state.retrying);
    if (retry) {
      card.reason.textContent = retry + " " + (entry.reason || "");
    }
    card.el.className = "shop-card " + R.statusClass(entry.status) + (retry ? " shop-retrying" : "");
    card.el.dataset.shop = shop;
  }

  function renderMatches(result) {
    result = result || {};
    var rows = R.matchRows(result);
    clear($("matches-body"));
    rows.forEach(function (row) {
      $("matches-body").appendChild(h("tr", { class: row.inStock ? "" : "out-of-stock" },
        h("td", { text: row.shop }),
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
    renderHeader(result);
    Object.keys(result.shops || {}).forEach(function (shop) { fillCard(shop, result.shops[shop]); });
    renderMatches(result);
  }

  function renderRetrying() {
    state.retrying.forEach(function (shop) {
      if (state.cards[shop]) {
        state.cards[shop].line.textContent = "\u21bb retrying\u2026";
      }
    });
  }

  // ---- trace drawer: whatever the drop log and the verdicts decided ----
  function loadTrace(shop) {
    var traceId = state.result && state.result.trace_id;
    if (!traceId || state.traceLoaded === traceId) return;
    state.traceLoaded = traceId;
    fetch(API + "/api/traces/" + traceId).then(function (r) { return r.ok ? r.json() : null; }).then(function (trace) {
      if (!trace) return;
      var panel = h("details", { class: "trace" }, h("summary", { text: "What happened (drop log and verdicts)" }));
      var stages = trace.stages || {};
      var matching = stages.matching || {};
      var decisions = matching.all_decisions || [];
      if (decisions.length) {
        var body = h("ul", { class: "plain" });
        decisions.forEach(function (d) {
          body.appendChild(h("li", null,
            h("span", { class: "muted", text: (d.source_domain || "?") + ": " }),
            h("span", { text: d.is_match ? "same product" : "not the same" }),
            h("span", { class: "muted", text: " \u2014 " + (d.reason || "") }),
            linkOrText(d.candidate_url)));
        });
        panel.appendChild(h("p", { class: "muted", text: "Verdicts" }));
        panel.appendChild(body);
      }
      var prefilter = stages.prefilter || {};
      if (prefilter.dropped && prefilter.dropped.length) {
        panel.appendChild(h("p", { class: "muted", text: "Dropped before the LLM (" + prefilter.dropped.length + ")" }));
        var dropped = h("ul", { class: "plain muted" });
        prefilter.dropped.slice(0, 20).forEach(function (item) {
          dropped.appendChild(h("li", { text: typeof item === "string" ? item : (item.title || item.url || "listing") }));
        });
        panel.appendChild(dropped);
      }
      var retry = stages.retry || {};
      if (retry.state || retry.skipped_reason) {
        panel.appendChild(h("p", { class: "muted", text: "Retry: " + (retry.state || retry.skipped_reason) }));
      }
      if (shop) {
        panel.appendChild(h("p", { class: "muted", text: "Showing the whole run, not only " + shop + "." }));
      }
      $("shop-cards").appendChild(panel);
    }).catch(function () { /* the drawer is optional */ });
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
    state.elapsed = (Date.now() - state.startedAt) / 1000;
  }
  function startClock() { stopClock(); state.timerId = setInterval(tick, 500); }
  function stopClock() { if (state.timerId) clearInterval(state.timerId); state.timerId = null; }

  // ---- events ----
  function handle(event) {
    switch (event.type) {
      case "run_started":
        state.startedAt = Date.now();
        state.active = true;
        $("run-btn").disabled = true;
        setPill("running", "running");
        break;
      case "gem_product":
        $("result-title").textContent = event.title || "Product";
        break;
      case "shop_started":
        cardFor(event.shop);
        break;
      case "shop_finished":
        fillCard(event.shop, event);
        break;
      case "shop_updated":
        state.retrying = state.retrying.filter(function (s) { return s !== event.shop; });
        fillCard(event.shop, event);
        break;
      case "retry_started":
        state.retrying = event.shops || [];
        renderRetrying();
        renderHeader((state.result || {}));
        $("result-state").textContent = "updating\u2026";
        break;
      case "run_finished":
        state.result = event.result || state.result;
        if (state.result) renderResult(state.result);
        state.finished = event.result && event.result.state !== "shown";
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
      case "cache_hit":
        state.lastText = "Using the saved result for this product.";
        break;
      case "done":
        finishRun(event.state === "done" ? "done" : "done");
        break;
      case "run_failed":
        finishRun("failed");
        showFailure(event.error || "The run failed.");
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
    ["matches-section", "possible-section", "similar-section", "warnings-section"].forEach(function (id) { $(id).hidden = true; });
    $("form-error").hidden = true;
    setPill("running", "running");
  }

  function startRun(url, refresh) {
    resetRun();
    showScreen("result");
    $("gem-url").value = url;
    $("result-title").textContent = "Starting\u2026";
    $("result-price").textContent = "";
    $("result-state").textContent = "running";
    $("cheapest-line").textContent = "";
    $("result-state").className = "pill running";
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

// Pure formatting for the result screen. No DOM, no fetch: the Node smoke
// test (tests/test_frontend_render.py) evaluates this file directly, and
// app.js uses the same functions for the page. Any change here changes both.
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.GemRender = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function money(value) {
    if (value == null || value === "" || isNaN(Number(value))) return "-";
    return "\u20b9" + Number(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function compareWord(saving) {
    if (saving == null || isNaN(Number(saving))) return "";
    return Number(saving) >= 0 ? "less" : "more";
  }

  function cheapSummary(result) {
    var cheapest = (result && result.cheapest) || null;
    if (!cheapest || cheapest.price == null) return null;
    return {
      shop: cheapest.shop || "?",
      price: cheapest.price,
      saving: cheapest.saving_vs_gem,
      url: cheapest.url || "",
      bulk: !!cheapest.bulk
    };
  }

  function cheapestLine(result) {
    var best = cheapSummary(result);
    if (!best) return "Cheapest: none found";
    var tail = "";
    if (best.saving != null && !isNaN(Number(best.saving))) {
      tail = " \u2014 " + money(Math.abs(Number(best.saving))) + " " + compareWord(best.saving) + " than GeM";
    }
    var bulk = best.bulk ? " (B2B/bulk, may be wholesale)" : "";
    return "Cheapest: " + money(best.price) + " on " + best.shop + bulk + tail;
  }

  function stateLabel(result, live) {
    result = result || {};
    if (result.from_cache) {
      return "cached result" + (result.checked_at ? " from " + String(result.checked_at).replace("T", " ").slice(0, 16) : "");
    }
    if (result.state === "shown") return "first result";
    if (result.state === "updating") return "updating\u2026";
    if (result.state === "done" || live === "done") return "complete";
    if (live === "failed") return "failed";
    return "running";
  }

  function shopSummary(entry) {
    entry = entry || {};
    var parts = [entry.status || "unknown"];
    if (entry.seen != null) parts.push(entry.seen + " seen");
    if (entry.dropped != null) parts.push(entry.dropped + " dropped");
    if (entry.checked != null) parts.push(entry.checked + " checked");
    if (entry.matches != null && entry.matches > 0) parts.push(entry.matches + " match" + (entry.matches === 1 ? "" : "es"));
    if (entry.seconds != null) parts.push(Number(entry.seconds).toFixed(1) + "s");
    return parts.join(" \u00b7 ");
  }

  function shopNeedsReason(entry) {
    entry = entry || {};
    return entry.status !== "found" && entry.status !== "possible";
  }

  function matchRows(result) {
    result = result || {};
    var gemPrice = result.gem && result.gem.price ? result.gem.price.low : null;
    return (result.matches || []).map(function (row) {
      var price = row.price != null ? row.price : null;
      var delta = null;
      if (price != null && gemPrice != null && !isNaN(Number(gemPrice))) {
        delta = Number(price) - Number(gemPrice);
      }
      return {
        shop: row.shop || "",
        bulk: !!row.bulk,
        title: row.title || "",
        price: price,
        priceText: money(price),
        confidence: row.confidence == null ? "" : Math.round(Number(row.confidence) * 100) + "%",
        reason: row.reason || "",
        url: row.url || "",
        inStock: row.in_stock !== false,
        vsGem: delta == null ? "-" : money(Math.abs(delta)) + (delta <= 0 ? " less" : " more"),
        cheaper: delta != null && delta < 0
      };
    });
  }

  function possibleRows(result) {
    result = result || {};
    return (result.possible_matches || []).map(function (row) {
      return {
        shop: row.shop || "",
        title: row.title || "",
        priceText: money(row.price),
        why: row.why || row.reason || "possible",
        url: row.url || ""
      };
    });
  }

  function similarRows(result) {
    result = result || {};
    return (result.similar || []).map(function (row) {
      return {
        shop: row.source_domain || row.shop || "",
        title: row.title || "",
        priceText: money(row.price),
        url: row.candidate_url || row.url || ""
      };
    });
  }

  function showsSimilar(result) {
    result = result || {};
    return (result.matches || []).length === 0 && (result.similar || []).length > 0;
  }

  function retryLabel(shop, retrying) {
    return retrying && retrying.indexOf(shop) >= 0 ? "\u21bb retrying\u2026" : "";
  }

  var STATUS_CLASS = {
    found: "shop-found",
    possible: "shop-possible",
    not_found: "shop-not-found",
    not_searched: "shop-unfinished",
    incomplete: "shop-unfinished",
    timed_out: "shop-unfinished"
  };

  function statusClass(status) {
    return STATUS_CLASS[status] || "shop-unfinished";
  }

  function safeHref(url) {
    // Scraped URLs are untrusted: only real web schemes become links,
    // everything else renders as plain text (never javascript:, data:, ...)
    if (typeof url !== "string") return null;
    var u = url.trim();
    return /^https?:\/\//i.test(u) ? u : null;
  }

  // ---------------------------------------------------------------------
  // Live activity: one plain sentence per pipeline event, and running
  // counters per shop. Pure functions, so the Node test can run them.
  // ---------------------------------------------------------------------

  // The big steps, in order. A "stage" event moves the strip forward.
  var STEPS = [
    { id: "read", label: "Read GeM page" },
    { id: "understand", label: "Understand product" },
    { id: "search", label: "Search shops" },
    { id: "filter", label: "Filter listings" },
    { id: "verify", label: "Check with LLM" },
    { id: "result", label: "First result" },
    { id: "retry", label: "Retry gaps" }
  ];

  function stepForStage(name) {
    name = String(name || "");
    if (name === "extract") return "read";
    if (name === "normalize") return "understand";
    if (name.indexOf("match") === 0) return "verify";
    if (name.indexOf("retry") === 0) return "retry";
    if (name.indexOf("search") === 0 || name === "discovery") return "search";
    return null;
  }

  function shopKey(value) {
    var v = String(value || "").toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];
    if (v === "flipkart") return "flipkart.com";
    if (v === "amazon") return "amazon.in";
    if (v === "other_shops") return "other";
    return v || "other";
  }

  function newStats() {
    return {
      step: null, doneSteps: {}, product: null, shops: {},
      llm: { calls: 0, failed: 0, cached: 0, seconds: 0 },
      events: 0, lastEventAt: 0
    };
  }

  function shopStats(stats, key) {
    if (!stats.shops[key]) {
      stats.shops[key] = { key: key, queries: 0, queriesDone: 0, listings: 0, kept: 0, dropped: 0,
        checking: 0, judged: 0, matches: 0, possible: 0, rejected: 0, notChecked: 0, blocked: 0, activity: "waiting" };
    }
    return stats.shops[key];
  }

  function advance(stats, id) {
    if (!id) return;
    var at = -1, now = -1, i;
    for (i = 0; i < STEPS.length; i++) {
      if (STEPS[i].id === stats.step) at = i;
      if (STEPS[i].id === id) now = i;
    }
    // search/filter/verify overlap between lanes, so the strip only moves forward
    if (now > at) {
      for (i = 0; i < now; i++) stats.doneSteps[STEPS[i].id] = true;
      stats.step = id;
    }
  }

  // Changes `stats` in place for one event. Unknown events change nothing.
  function applyEvent(stats, event) {
    if (!event || !event.type) return stats;
    stats.events += 1;
    if (event.t != null) stats.lastEventAt = event.t;
    var s;
    switch (event.type) {
      case "stage":
        advance(stats, stepForStage(event.name));
        break;
      case "gem_product":
        stats.gem = { title: event.title, price: event.price, priceType: event.price_type, brand: event.brand, model: event.model };
        break;
      case "normalized":
        stats.product = { name: event.canonical_name, brand: event.brand, model: event.model,
          queries: event.queries || [], identifiers: event.key_identifiers || [] };
        advance(stats, "search");
        break;
      case "shop_started":
        s = shopStats(stats, shopKey(event.shop));
        s.activity = "searching";
        break;
      case "search_started":
        s = shopStats(stats, shopKey(event.marketplace));
        s.queries += 1;
        s.activity = "searching: " + (event.query || "");
        break;
      case "search_finished":
        s = shopStats(stats, shopKey(event.marketplace));
        s.queriesDone += 1;
        s.listings += Number(event.count || 0);
        if (event.blocked) s.blocked += 1;
        s.activity = event.blocked ? "blocked by the shop" : "found " + (event.count || 0) + " listings";
        break;
      case "discovery_finished":
        s = shopStats(stats, "other");
        s.listings += (event.urls || []).length;
        s.activity = "found " + (event.urls || []).length + " pages to read";
        break;
      case "candidate_checking":
        s = shopStats(stats, shopKey(event.domain));
        s.checking += 1;
        s.activity = "checking " + (event.index || "?") + " of " + (event.total || "?");
        break;
      case "candidate_verdict":
        s = shopStats(stats, shopKey(event.domain));
        s.judged += 1;
        if (event.is_match) s.matches += 1; else s.rejected += 1;
        break;
      case "candidate_skipped":
        s = shopStats(stats, shopKey(event.url));
        s.notChecked += 1;
        break;
      case "candidate_gate_rejected":
        s = shopStats(stats, shopKey(event.url));
        s.rejected += 1;
        break;
      case "filter_finished":
        s = shopStats(stats, shopKey(event.shop));
        s.kept = Number(event.sent != null ? event.sent : event.kept || 0);
        s.dropped = Number(event.dropped || 0);
        break;
      case "llm_call":
        stats.llm.calls += 1;
        stats.llm.seconds += Number(event.seconds || 0);
        if (event.ok === false) stats.llm.failed += 1;
        break;
      case "llm_cache_hit":
        stats.llm.cached += 1;
        break;
      case "shop_finished":
      case "shop_updated":
        s = shopStats(stats, shopKey(event.shop));
        s.activity = event.status || "finished";
        break;
      case "run_finished":
        advance(stats, "result");
        stats.doneSteps.search = stats.doneSteps.filter = stats.doneSteps.verify = true;
        stats.doneSteps.result = true;
        break;
      case "retry_started":
        advance(stats, "retry");
        break;
      case "done":
        stats.doneSteps.retry = true;
        stats.finished = true;
        break;
      default:
        break;
    }
    return stats;
  }

  function short(text, n) {
    text = String(text == null ? "" : text);
    return text.length > n ? text.slice(0, n - 1) + "…" : text;
  }

  function shopLabel(value) {
    var k = shopKey(value);
    return k === "other" ? "other shops" : k;
  }

  function seconds(value) {
    return value == null || isNaN(Number(value)) ? "" : Number(value).toFixed(1) + "s";
  }

  function percent(value) {
    return value == null || isNaN(Number(value)) ? "" : Math.round(Number(value) * 100) + "%";
  }

  // One feed line for one event: { kind, level, key, text, detail }
  //   kind  - the icon group (step, search, filter, llm, verdict, shop, retry, warn, info)
  //   level - "ok" | "bad" | "warn" | ""   (colours the line)
  //   key   - true for the lines that tell the story; the rest are detail
  //   text  - one plain sentence;  detail - optional extra lines (array of strings)
  // Returns null for events that need no line.
  function describeEvent(e) {
    if (!e || !e.type) return null;
    var list, lines, i;
    switch (e.type) {
      case "run_started":
        return { kind: "step", level: "", key: true, text: "Run started." };
      case "cache_hit":
        return { kind: "info", level: "", key: true, text: "A saved result for this product is fresh enough, so nothing is searched again." };
      case "stage":
        return { kind: "step", level: "", key: true, text: e.label || e.name || "Next step" };
      case "gem_product":
        return { kind: "step", level: "ok", key: true,
          text: "Read the GeM page: " + short(e.title, 90) + (e.price != null ? " at " + money(e.price) + " (" + (e.price_type || "price") + ")" : ", no price found"),
          detail: [e.brand ? "Brand on the page: " + e.brand : "No brand on the page", e.model ? "Model on the page: " + e.model : "No model on the page"] };
      case "product_ready":
        return null;
      case "normalized":
        lines = [];
        list = e.queries || [];
        for (i = 0; i < list.length; i++) lines.push("Search words " + (i + 1) + ": " + list[i]);
        if ((e.key_identifiers || []).length) lines.push("Must match: " + e.key_identifiers.join(", "));
        return { kind: "step", level: "ok", key: true,
          text: "Understood the product as “" + short(e.canonical_name, 80) + "”" + (e.brand ? " (brand " + e.brand : " (no brand") + (e.model ? ", model " + e.model + ")" : ")"),
          detail: lines };
      case "llm_call":
        if (e.ok === false) {
          return { kind: "warn", level: "warn", key: true,
            text: "The LLM " + (e.endpoint || "endpoint") + " failed for " + (e.purpose || "a request") + (e.status ? " (HTTP " + e.status + ")" : "") + ". Trying another one.",
            detail: e.error ? [short(e.error, 200)] : [] };
        }
        return { kind: "llm", level: "", key: false,
          text: "LLM answered (" + (e.purpose || "request") + ") via " + (e.endpoint || "endpoint") + " in " + seconds(e.seconds) };
      case "llm_cache_hit":
        return { kind: "llm", level: "", key: false, text: "Reused an earlier LLM answer (" + (e.purpose || "request") + "), no new call." };
      case "llm_cooldown":
        return { kind: "warn", level: "warn", key: true, text: "LLM " + e.endpoint + " is cooling down for " + e.seconds + " s.", detail: e.reason ? [short(e.reason, 160)] : [] };
      case "llm_used_up":
        return { kind: "warn", level: "warn", key: true, text: "LLM " + e.endpoint + " has used up its allowance until " + (e.until || "later") + ".", detail: e.reason ? [short(e.reason, 160)] : [] };
      case "llm_disabled":
        return { kind: "warn", level: "bad", key: true, text: "LLM " + e.endpoint + " is switched off for this run.", detail: e.reason ? [short(e.reason, 160)] : [] };
      case "llm_unavailable":
        return { kind: "warn", level: "bad", key: true, text: "No LLM is available right now, so listings cannot be checked.", detail: e.reason ? [short(e.reason, 200)] : [] };
      case "shop_started":
        return { kind: "shop", level: "", key: true, text: "Started on " + shopLabel(e.shop) + "." };
      case "search_started":
        return { kind: "search", level: "", key: false, text: shopLabel(e.marketplace) + ": searching for “" + short(e.query, 80) + "”" };
      case "search_finished":
        list = e.candidates || [];
        lines = [];
        for (i = 0; i < list.length && i < 6; i++) {
          lines.push(short(list[i].title, 90) + (list[i].price != null ? "  —  " + money(list[i].price) : ""));
        }
        if (list.length > 6) lines.push("… and " + (list.length - 6) + " more");
        if (e.blocked) {
          return { kind: "search", level: "warn", key: true, text: shopLabel(e.marketplace) + ": the shop blocked this search (CAPTCHA or robot check).", detail: lines };
        }
        if (e.error) {
          return { kind: "search", level: "warn", key: true, text: shopLabel(e.marketplace) + ": the search failed.", detail: [short(e.error, 200)] };
        }
        return { kind: "search", level: Number(e.count) > 0 ? "ok" : "", key: true,
          text: shopLabel(e.marketplace) + ": “" + short(e.query, 60) + "” gave " + (e.count || 0) + " listing" + (Number(e.count) === 1 ? "" : "s"), detail: lines };
      case "discovery_finished":
        return { kind: "search", level: (e.urls || []).length ? "ok" : "", key: true,
          text: "Web search for other shops found " + (e.urls || []).length + " page" + ((e.urls || []).length === 1 ? "" : "s") + " to read.",
          detail: (e.urls || []).slice(0, 8).map(function (u) { return short(u, 100); }) };
      case "scrape_started":
        return { kind: "search", level: "", key: false, text: "Reading page " + short(e.url, 90) };
      case "scrape_finished":
        if (e.error) return { kind: "search", level: "warn", key: true, text: "Could not read " + short(e.url, 70) + ".", detail: [short(e.error, 160)] };
        return { kind: "search", level: "", key: false, text: "Read " + shopLabel(e.domain) + ": " + short(e.title, 80) + (e.price != null ? " at " + money(e.price) : ", no price") };
      case "prefilter_finished":
        return { kind: "filter", level: "", key: false,
          text: "Filter pass: kept " + e.kept + ", dropped " + (e.dropped || 0) + (e.top ? " (limit " + e.top + " per shop)" : "") + "." };
      case "filter_finished":
        return { kind: "filter", level: "", key: true,
          text: (e.shop ? shopLabel(e.shop) + ": " : "") + "Filter kept " + (e.sent != null ? e.sent : e.kept) + " listing" + ((e.sent != null ? e.sent : e.kept) === 1 ? "" : "s") + " for the LLM and dropped " + (e.dropped || 0) + " weak title" + ((e.dropped || 0) === 1 ? "" : "s") + ".",
          detail: e.top ? ["The filter keeps at most " + e.top + " listings per shop, plus every listing that has the model code."] : [] };
      case "candidates_total":
        return { kind: "filter", level: "", key: false, text: "Sending " + e.count + " listing" + (Number(e.count) === 1 ? "" : "s") + " to be checked." };
      case "candidate_checking":
        return { kind: "verdict", level: "", key: false,
          text: "Checking " + (e.index || "?") + "/" + (e.total || "?") + " on " + shopLabel(e.domain) + ": " + short(e.title, 80) + (e.price != null ? " (" + money(e.price) + ")" : "") };
      case "candidate_verdict":
        return { kind: "verdict", level: e.is_match ? "ok" : "", key: true,
          text: (e.is_match ? "Same product" : "Not the same") + " on " + shopLabel(e.domain) + (e.confidence != null ? " (" + percent(e.confidence) + " sure)" : "") + (e.price != null ? " at " + money(e.price) : ""),
          detail: e.reason ? [short(e.reason, 220)] : [] };
      case "candidate_reused":
        return { kind: "verdict", level: "", key: false, text: "A listing on " + shopLabel(e.domain) + " is the same as one already checked, so its answer was reused." };
      case "candidate_gate_rejected":
        return { kind: "verdict", level: "warn", key: true, text: "A price check rejected a listing on " + shopLabel(e.url) + ".", detail: e.reason ? [short(e.reason, 200)] : [] };
      case "candidate_skipped":
        return { kind: "warn", level: "warn", key: true, text: "A listing on " + shopLabel(e.url) + " was not checked.", detail: e.reason ? [short(e.reason, 200)] : [] };
      case "lane_failed":
        return { kind: "warn", level: "bad", key: true, text: "The " + shopLabel(e.lane) + " part crashed. The other shops carry on.", detail: e.error ? [short(e.error, 200)] : [] };
      case "shop_finished":
        return { kind: "shop", level: e.status === "found" ? "ok" : (e.status === "not_found" ? "" : "warn"), key: true,
          text: shopLabel(e.shop) + " finished: " + (e.status || "done").replace(/_/g, " ") + (e.matches ? ", " + e.matches + " match" + (e.matches === 1 ? "" : "es") : "") + (e.seconds != null ? " in " + seconds(e.seconds) : ""),
          detail: [(e.seen != null ? e.seen + " seen, " : "") + (e.dropped != null ? e.dropped + " dropped, " : "") + (e.checked != null ? e.checked + " checked" : ""), e.reason ? "Reason: " + e.reason : ""].filter(Boolean) };
      case "run_finished":
        return { kind: "step", level: "ok", key: true,
          text: "First result is ready: " + (e.matches != null ? e.matches + " match" + (e.matches === 1 ? "" : "es") : "see below") + (e.cheapest != null ? ", cheapest " + money(e.cheapest) : "") + "." };
      case "retry_started":
        return { kind: "retry", level: "", key: true, text: "Some shops did not finish cleanly. Trying again for: " + (e.shops || []).join(", ") + "." };
      case "retry_plan":
        return { kind: "retry", level: "", key: true, text: "The LLM suggested new search words.", detail: (e.new_queries || []).map(function (q) { return "New search words: " + q; }).concat(e.notes ? [short(e.notes, 200)] : []) };
      case "shop_updated":
        return { kind: "retry", level: e.status === "found" ? "ok" : "", key: true, text: shopLabel(e.shop) + " updated after the retry: " + (e.status || "").replace(/_/g, " ") + (e.matches ? ", " + e.matches + " match" + (e.matches === 1 ? "" : "es") : "") + "." };
      case "similar_ready":
        return { kind: "retry", level: "", key: true, text: (e.similar || []).length ? "Found " + e.similar.length + " similar (but not identical) products." : "No similar products found either." };
      case "run_updated":
        return { kind: "step", level: "", key: false, text: "The result was updated." };
      case "done":
        return { kind: "step", level: "ok", key: true, text: "Done." + (e.from_cache ? " (saved result)" : "") };
      case "run_failed":
        return { kind: "warn", level: "bad", key: true, text: "The run failed: " + short(e.error || e.message || "unknown error", 200) };
      default:
        return null;
    }
  }

  // The per-shop line on a live card, from the counters.
  function liveShopLine(s) {
    if (!s) return "";
    var parts = [];
    if (s.queries) parts.push(s.queriesDone + "/" + s.queries + " searches");
    if (s.listings) parts.push(s.listings + (s.listings === 1 ? " listing" : " listings"));
    if (s.dropped) parts.push(s.dropped + " dropped");
    if (s.judged || s.checking) parts.push(s.judged + "/" + Math.max(s.checking, s.judged) + " checked");
    if (s.matches) parts.push(s.matches + " same");
    return parts.join(" · ");
  }

  function clock(totalSeconds) {
    var t = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    var m = Math.floor(t / 60), sec = t % 60;
    return (m < 10 ? "0" : "") + m + ":" + (sec < 10 ? "0" : "") + sec;
  }

  return {
    STEPS: STEPS,
    stepForStage: stepForStage,
    shopKey: shopKey,
    newStats: newStats,
    applyEvent: applyEvent,
    describeEvent: describeEvent,
    liveShopLine: liveShopLine,
    clock: clock,
    money: money,
    cheapestLine: cheapestLine,
    cheapSummary: cheapSummary,
    stateLabel: stateLabel,
    shopSummary: shopSummary,
    shopNeedsReason: shopNeedsReason,
    matchRows: matchRows,
    possibleRows: possibleRows,
    similarRows: similarRows,
    showsSimilar: showsSimilar,
    retryLabel: retryLabel,
    statusClass: statusClass,
    safeHref: safeHref
  };
});

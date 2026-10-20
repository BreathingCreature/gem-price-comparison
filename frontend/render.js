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

  return {
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

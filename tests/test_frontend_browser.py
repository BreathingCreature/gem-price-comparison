"""Browser smoke test: the new page in Chromium, no console errors.

Skipped unless Playwright is installed. The suite must stay runnable without a
browser; this file is the extra check that the page really works after the unit
tests have read the pure models.
"""
import copy
import base64
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from api import demo  # noqa: E402
from api.server import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEMO_URL = "https://mkp.gem.gov.in/computer-mouse/demo-mouse/p-999-1000-cat.html"
SEARCH_URL = "https://mkp.gem.gov.in/search?q=mouse"
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(app):
    """A real uvicorn server, because EventSource needs a real HTTP stream."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True, name="frontend-smoke-server")
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    return server, thread, f"http://127.0.0.1:{port}"


def _stop(server, thread):
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def demo_server():
    previous_delay = demo.DEMO_DELAY
    demo.DEMO_DELAY = 0.12  # long enough to see the running screen
    server, thread, url = _serve(create_app(demo=True))
    try:
        yield url
    finally:
        _stop(server, thread)
        demo.DEMO_DELAY = previous_delay


@pytest.fixture(scope="module")
def plain_server():
    """A non-demo app, so the page checks a link before any request."""
    server, thread, url = _serve(create_app(runner=lambda *a: {}, demo=False))
    try:
        yield url
    finally:
        _stop(server, thread)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        last_error = None
        for launch in (
            lambda: playwright.chromium.launch(headless=True),
            lambda: playwright.chromium.launch(channel="chrome", headless=True),
        ):
            try:
                instance = launch()
                break
            except Exception as error:  # missing browser download, not a test failure
                last_error = error
        else:
            pytest.skip(f"A Chromium build is not available for Playwright: {last_error}")
        try:
            yield instance
        finally:
            instance.close()


def _js(cdp, expression):
    """Evaluate in the page without tripping the strict script-src CSP.

    Playwright's page.evaluate() is a normal script evaluation and the page's
    CSP correctly refuses it. CDP's Runtime.evaluate is an inspector command,
    not page script, so the test can read the page's own state.
    """
    result = cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    if result.get("exceptionDetails"):
        raise AssertionError(result["exceptionDetails"])
    return result["result"].get("value")


def _new_page(browser, base_url):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    console_errors, page_errors, requests, responses = [], [], [], []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.on("response", lambda response: responses.append((response.url, response.status)))
    page.add_init_script(
        """
        window.__eventSources = [];
        const NativeSource = window.EventSource;
        window.EventSource = function (url, options) {
          const source = new NativeSource(url, options);
          window.__eventSources.push(source);
          return source;
        };
        window.EventSource.prototype = NativeSource.prototype;
        """
    )
    page.goto(base_url, wait_until="networkidle")
    return context, page, cdp, console_errors, page_errors, requests, responses


def _assert_clean(console_errors, page_errors, requests, responses, base_url):
    assert page_errors == []
    assert console_errors == []
    external = [
        url for _, url in requests
        if url.startswith(("http://", "https://")) and not url.startswith(base_url)
    ]
    assert external == []
    failed = [
        (url, status) for url, status in responses
        if status >= 400 and "/api/icon/" not in url and "/icons/" not in url
    ]
    assert failed == []


def _overlaps(cdp):
    return _js(
        cdp,
        """(() => {
          const groups = [
            ['.topbar .left', '.topbar .right'],
            ['.search-control > *'],
            ['#pipeline-top .step-line'],
            ['#map-lanes > .lane', '#shop-rows > .lane'],
            ['.lane > *'],
            ['.tabs .tab'],
            ['.summary .sum-left', '.summary .sum-right']
          ];
          const bad = [];
          for (const selectors of groups) {
            const boxes = [];
            for (const sel of selectors) {
              document.querySelectorAll(sel).forEach(el => {
                if (el.offsetParent === null) return;
                const r = el.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) return;
                boxes.push({sel: sel, left: r.left, right: r.right, top: r.top, bottom: r.bottom});
              });
            }
            for (let i = 0; i < boxes.length; i += 1) {
              for (let j = i + 1; j < boxes.length; j += 1) {
                const a = boxes[i], b = boxes[j];
                const overlapX = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                const overlapY = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                if (overlapX > 1 && overlapY > 1) bad.push([a.sel, b.sel]);
              }
            }
          }
          return bad.slice(0, 5);
        })()""",
    )


def test_the_start_page_is_only_the_link_box_and_recent_runs(browser, demo_server):
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, demo_server)
    try:
        page.locator("#start-screen").wait_for(state="visible")
        assert page.locator("#run-screen").is_hidden()
        assert page.locator("#run-form").is_visible()
        assert page.locator("#recent-title").is_visible()
        assert page.locator("#demo-badge").inner_text() == "Demo data"
        assert page.locator("#llm-banner").is_hidden()
        assert page.locator("#scroll-hint").is_visible()
        hint = page.locator("#scroll-hint").bounding_box()
        assert hint is not None and hint["y"] > 700, "the recent-runs hint sits at the bottom edge"
        assert _js(cdp, "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        before = _js(cdp, "document.querySelector('#recent-title').getBoundingClientRect().top")
        page.click("#scroll-hint")
        page.wait_for_timeout(200)
        after = _js(cdp, "document.querySelector('#recent-title').getBoundingClientRect().top")
        assert after < before, f"the recent heading did not move from {before}"
        assert _js(cdp, "window.scrollY") > 0
        _assert_clean(console_errors, page_errors, requests, responses, demo_server)
    finally:
        context.close()


def test_a_search_link_shows_a_dialog_and_makes_no_request(browser, plain_server):
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, plain_server)
    try:
        page.fill("#gem-url", SEARCH_URL)
        page.click("#run-btn")
        page.locator("#link-dialog").wait_for(state="visible")
        assert "Open one product" in page.locator("#link-dialog").inner_text()
        page.wait_for_timeout(300)
        assert not [url for method, url in requests if method == "POST" and url.endswith("/api/runs")]
        page.click("#dialog-close")
        page.locator("#link-dialog").wait_for(state="hidden")
        assert page.locator("#start-screen").is_visible()
        _assert_clean(console_errors, page_errors, requests, responses, plain_server)
    finally:
        context.close()


def test_a_late_shop_icon_replaces_the_globe_and_no_slot_stays_blank(browser, demo_server):
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, demo_server)
    hits = {}

    def icon_route(route):
        host = route.request.url.rsplit("/", 1)[-1]
        hits[host] = hits.get(host, 0) + 1
        if hits[host] >= 2:
            route.fulfill(status=200, content_type="image/png", body=TINY_PNG)
        else:
            route.fulfill(status=204, headers={"Cache-Control": "no-store"})

    page.route("**/api/icon/**", icon_route)
    try:
        page.fill("#gem-url", DEMO_URL)
        page.click("#run-btn")
        page.locator("#run-state-label", has_text="Done").wait_for(timeout=45_000)
        page.locator('#shop-rows img[src*="/api/icon/example-shop.in"]').wait_for(state="attached", timeout=15_000)
        assert hits.get("example-shop.in", 0) >= 2
        blank = _js(
            cdp,
            """(() => Array.from(document.querySelectorAll('.site-icon')).filter(
                 (el) => !el.hidden && !el.querySelector('img') && !el.querySelector('svg')
               ).length)()""",
        )
        assert blank == 0
        assert page_errors == []
    finally:
        page.unroute("**/api/icon/**", icon_route)
        context.close()


def test_a_demo_run_hides_the_answer_until_done_then_collapses(browser, demo_server):
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, demo_server)
    try:
        page.fill("#gem-url", DEMO_URL)
        page.click("#run-btn")
        page.locator("#run-screen").wait_for(state="visible")
        page.locator("#gem-card").wait_for(state="visible", timeout=20_000)

        # The answer is not shown while the run is still going.
        assert page.locator("#verdict").is_hidden()
        assert page.locator("#details-card").is_visible()
        assert page.locator("#panel-matches").is_visible()
        assert "Results appear when the run finishes." in page.locator("#matches-empty").inner_text()
        assert page.locator("#pipeline-body").is_visible()

        page.locator("#run-state-label", has_text="Done").wait_for(timeout=45_000)
        page.locator("#verdict").wait_for(state="visible")
        assert page.locator("#answer-price").inner_text() == "\u20b91,149"
        assert page.locator("#answer-shop").inner_text() == "flipkart.com"
        assert "Saves" in page.locator("#answer-saving").inner_text()
        assert page.locator("#count-matches").inner_text() == "4"
        assert page.locator("#count-possible").inner_text() == "1"
        assert page.locator("#price-bars .chart-row:not(.chart-foot)").count() == 4
        assert page.locator("#price-bars [role='img']").get_attribute("aria-label").startswith("Price chart. GeM")
        assert page.locator("#price-bars .sr-only li").count() == 4
        chart_prices = _js(
            cdp,
            "Array.from(document.querySelectorAll('#price-bars .chart-row:not(.chart-foot)')).map(el => Number(el.dataset.price))",
        )
        assert chart_prices == sorted(chart_prices)
        assert page.locator("#price-bars .chart-axis .gem-line").count() == 4
        assert page.locator("#gem-title").inner_text() == "Logitech M650 Signature Wireless Mouse"
        assert page.locator("#gem-image").is_visible()
        assert page.locator("#gem-specs .spec-row").count() > 0
        assert page.locator("#lane-flipkart-status").inner_text() == "found"
        assert page.locator("#lane-amazon-status").inner_text() == "found"
        assert page.locator("#lane-other-status").inner_text() == "found"
        assert page.locator("#shop-rows .shop-row").count() >= 4

        # The pipeline folds itself away once the run is done, and the reader
        # can open it again with the chevron.
        assert page.locator("#pipeline-body").is_hidden()
        assert page.locator("#pipeline-toggle").get_attribute("aria-expanded") == "false"
        assert page.locator("#pipeline-card").get_attribute("data-open") == "false"
        assert page.locator("#pipeline-toggle").inner_text().strip() == ""
        page.click("#pipeline-toggle")
        assert page.locator("#pipeline-body").is_visible()
        assert page.locator("#pipeline-toggle").get_attribute("aria-expanded") == "true"

        # The event source is closed by the page, not left polling.
        assert _js(cdp, "window.__eventSources.map(s => s.readyState)") == [2]
        assert sum(1 for _, url in requests if "/events" in url) == 1

        page.locator("#tab-matches").focus()
        page.keyboard.press("ArrowRight")
        assert page.locator("#tab-possible").get_attribute("aria-selected") == "true"
        assert page.locator("#panel-possible").is_visible()
        assert "different pack" in page.locator("#possible-body").inner_text()

        page.click("#tab-system")
        assert page.locator("#panel-system").is_visible()
        assert "LLM call" in page.locator("#llm-run-summary").inner_text()
        assert page.locator("#run-stats").inner_text().startswith("Run time")

        page.click("#theme-toggle")
        assert _js(cdp, "document.documentElement.getAttribute('data-theme')") == "dark"
        assert _js(cdp, "getComputedStyle(document.body).backgroundColor") == "rgb(27, 30, 35)"
        page.click("#theme-toggle")
        assert _js(cdp, "document.documentElement.getAttribute('data-theme')") == "light"

        for width in (1440, 1024, 768, 360, 1280, 1920):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(120)
            assert _js(
                cdp,
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth",
            ), f"the page scrolls sideways at {width} px"
            assert _overlaps(cdp) == [], f"two controls overlap at {width} px"

        # A long title and a long shop name must wrap, not push the page out.
        _js(cdp, "document.querySelector('#gem-title').textContent = 'Wireless mouse with a very long product title '.repeat(5)")
        _js(cdp, "document.querySelector('#answer-shop').textContent = 'on a-shop-with-a-very-long-name.example'")
        for width in (360, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(120)
            assert _js(
                cdp,
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth",
            ), f"a long title scrolls sideways at {width} px"
            assert _overlaps(cdp) == [], f"a long title overlaps a control at {width} px"

        # 200 percent zoom halves the layout viewport, the same reflow a browser
        # zoom asks for. The page must still fit without sideways scrolling.
        page.set_viewport_size({"width": 1440, "height": 900})
        _js(cdp, "document.documentElement.style.zoom = '200%'")
        page.wait_for_timeout(150)
        assert _js(cdp, "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert _overlaps(cdp) == [], "two controls overlap at 200 percent zoom"
        _js(cdp, "document.documentElement.style.zoom = ''")

        _assert_clean(console_errors, page_errors, requests, responses, demo_server)
    finally:
        context.close()


def test_twelve_other_shops_fit_every_width(browser):
    """Every other-shop row is shown at once, at the narrow layouts too."""
    previous_delay = demo.DEMO_DELAY
    demo.DEMO_DELAY = 0
    try:
        base = demo.demo_runner(DEMO_URL, False, lambda event: None)
    finally:
        demo.DEMO_DELAY = previous_delay
    result = copy.deepcopy(base)
    for index in range(1, 9):
        result["shops"][f"extra-{index}.example"] = {
            "seen": 3,
            "dropped": 1,
            "checked": 2,
            "matches": 0,
            "possible": 0,
            "rejected": 2,
            "status": "not_found",
            "reason": "searched, nothing confirmed",
            "seconds": 1.2,
            "retried": False,
            "retryable": False,
        }
    other_shops = [
        key for key in result["shops"]
        if key not in ("flipkart.com", "amazon.in", "other")
    ]

    def runner(gem_url, refresh, on_event):
        sequence = 0

        def emit(event):
            nonlocal sequence
            sequence += 1
            event["seq"] = sequence
            event.setdefault("t", 0)
            on_event(event)

        emit({"type": "run_started"})
        emit({"type": "gem_product", "title": "Logitech M650", "price": 1299.0,
              "price_type": "fixed", "brand": "Logitech", "model": "M650"})
        emit({"type": "normalized", "canonical_name": "Logitech M650", "brand": "Logitech",
              "model": "M650", "queries": ["logitech m650"], "key_identifiers": ["M650"]})
        for shop in other_shops:
            emit({"type": "shop_started", "shop": shop})
            emit({"type": "filter_finished", "shop": shop, "sent": 2, "dropped": 1, "seen": 3})
            emit({"type": "shop_updated", "shop": shop, "status": "not_found"})
        return result

    server, thread, url = _serve(create_app(runner=runner, demo=True))
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, url)
    try:
        page.fill("#gem-url", DEMO_URL)
        page.click("#run-btn")
        page.locator("#run-state-label", has_text="Done").wait_for(timeout=30_000)
        page.click("#pipeline-toggle")
        assert page.locator("#shop-rows .shop-row").count() == len(other_shops)
        assert page.locator("#shop-extra-1-example-filter-value").inner_text() == "+2 -1"
        # The design has no cap and no Show more: every shop is on the map.
        assert page.locator("#shop-more").count() == 0
        for width in (360, 1024, 1280, 1920):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(120)
            assert _js(
                cdp,
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth",
            ), f"twelve shops scroll sideways at {width} px"
            assert _overlaps(cdp) == [], f"twelve shops overlap a control at {width} px"
        _assert_clean(console_errors, page_errors, requests, responses, url)
    finally:
        context.close()
        _stop(server, thread)


def test_the_system_tab_counts_this_run_for_each_provider(browser):
    """A non-demo app with a fake router: one row, one run call, no network."""
    from llm import router as router_module

    class FakeRouter:
        strategy = "test"

        def status(self):
            return [
                {
                    "endpoint": "fake/model",
                    "provider": "fake",
                    "model": "model",
                    "state": "ready",
                    "calls": 7,
                    "errors": 2,
                    "avg_seconds": 0.1,
                    "last_error": "",
                    "reason": "",
                }
            ]

    def runner(gem_url, refresh, on_event):
        on_event({"seq": 1, "t": 0, "type": "run_started"})
        on_event(
            {
                "seq": 2,
                "t": 0,
                "type": "llm_call",
                "endpoint": "fake/model",
                "provider": "fake",
                "model": "model",
                "purpose": "verify",
                "seconds": 0.1,
                "ok": True,
            }
        )
        time.sleep(0.2)
        return {
            "result_version": 2,
            "state": "done",
            "gem": {"title": "Fake product", "price": {"low": 100.0, "high": 100.0, "kind": "fixed"}},
            "gem_product": {"title": "Fake product", "gem_price": 100.0},
            "cheapest": None,
            "matches": [],
            "possible_matches": [],
            "similar": [],
            "shops": {},
            "warnings": [],
            "from_cache": False,
        }

    original = router_module.get_router
    router_module.get_router = lambda: FakeRouter()
    server, thread, url = _serve(create_app(runner=runner, demo=False))
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, url)
    try:
        page.fill("#gem-url", DEMO_URL)
        page.click("#run-btn")
        page.locator("#run-state-label", has_text="Done").wait_for(timeout=20_000)
        page.click("#tab-system")
        row = page.locator("div.p-row[data-endpoint='fake/model']")
        row.wait_for(state="visible")
        assert row.locator(".p-calls").inner_text() == "7"
        assert row.locator(".p-errors").inner_text() == "2"
        assert row.locator(".p-run").inner_text() == "1 call"
        _assert_clean(console_errors, page_errors, requests, responses, url)
    finally:
        context.close()
        _stop(server, thread)
        router_module.get_router = original


def test_fact_values_wrap_as_words_and_keep_an_eight_character_cell(browser):
    """The '6to10' bug: never let a spec value collapse into letter tiles."""
    long_value = (
        "Bluetooth 5.1 and Logi Bolt USB receiver with a stable wireless link "
        "to the computer and a long battery life of up to twenty four months "
        "on a single AA cell under normal office use and moderate clicking"
    )
    result = {
        "result_version": 2,
        "state": "done",
        "gem": {
            "title": "Mouse with long specs",
            "price": {"low": 1299.0, "high": 1299.0, "kind": "fixed"},
            "specs": {"Battery life": "6 to 10 hours", "Long note": long_value},
            "highlights": [],
        },
        "cheapest": None,
        "matches": [],
        "possible_matches": [],
        "similar": [],
        "shops": {},
        "warnings": [],
        "from_cache": False,
    }

    def runner(gem_url, refresh, on_event):
        on_event({
            "seq": 1, "t": 0, "type": "gem_product", "title": "Mouse with long specs",
            "price": 1299.0, "price_type": "fixed", "brand": "Brand", "model": "M1",
            "specs": {"Battery life": "6 to 10 hours", "Long note": long_value},
        })
        on_event({
            "seq": 2, "t": 0, "type": "normalized", "canonical_name": "Mouse with long specs",
            "brand": "Brand", "model": "M1", "queries": ["mouse"], "key_identifiers": ["M1"],
        })
        time.sleep(0.2)
        return result

    server, thread, url = _serve(create_app(runner=runner, demo=False))
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, url)
    try:
        page.fill("#gem-url", DEMO_URL)
        page.click("#run-btn")
        page.locator("#gem-card").wait_for(state="visible")
        values = _js(
            cdp,
            "Array.from(document.querySelectorAll('#gem-specs dd')).map(function (dd) {"
            " var probe = document.createElement('span');"
            " probe.textContent = '00000000';"
            " probe.style.font = getComputedStyle(dd).font;"
            " document.body.appendChild(probe);"
            " var eight = probe.getBoundingClientRect().width;"
            " probe.remove();"
            " return {width: dd.getBoundingClientRect().width, eight: eight,"
            " wrap: getComputedStyle(dd).overflowWrap, text: dd.textContent};"
            "})",
        )
        assert len(values) >= 2
        for value in values:
            assert value["wrap"] == "break-word", value
            assert value["width"] >= value["eight"] - 1, value
        assert any("6 to 10 hours" in value["text"] for value in values)
        for width in (360, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(120)
            assert _js(
                cdp,
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth",
            ), f"long fact values scroll sideways at {width} px"
        _assert_clean(console_errors, page_errors, requests, responses, url)
    finally:
        context.close()
        _stop(server, thread)

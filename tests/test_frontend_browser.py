"""Browser smoke test: the new page in Chromium, no console errors.

Skipped unless Playwright is installed. The suite must stay runnable without a
browser; this file is the extra check that the page really works after the unit
tests have read the pure models.
"""
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
            ['.topbar > *'],
            ['.search-control > *'],
            ['#pipeline-top .pipeline-node'],
            ['.lane'],
            ['.lane-steps .step'],
            ['.tablist .tab'],
            ['.summary-body > *']
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
        assert _js(cdp, "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
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
        assert page.locator("#answer-shop").inner_text() == "on flipkart.com"
        assert "saves" in page.locator("#answer-saving").inner_text()
        assert page.locator("#count-matches").inner_text() == "4"
        assert page.locator("#count-possible").inner_text() == "1"
        assert page.locator("#gem-title").inner_text() == "Logitech M650 Signature Wireless Mouse"
        assert page.locator("#gem-image").is_visible()
        assert page.locator("#gem-specs .spec-row").count() > 0
        assert page.locator("#lane-flipkart-status").inner_text() == "Found a match"
        assert page.locator("#lane-amazon-status").inner_text() == "Found a match"
        assert page.locator("#lane-other-status").inner_text() == "Found a match"
        assert page.locator("#shop-rows .shop-row").count() >= 4

        # The pipeline folds itself away once the run is done, and the reader
        # can open it again.
        assert page.locator("#pipeline-body").is_hidden()
        assert page.locator("#pipeline-toggle").inner_text() == "Show details"
        assert page.locator("#pipeline-toggle").get_attribute("aria-expanded") == "false"
        page.click("#pipeline-toggle")
        assert page.locator("#pipeline-body").is_visible()
        assert page.locator("#pipeline-toggle").inner_text() == "Hide details"

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
        assert _js(cdp, "getComputedStyle(document.body).backgroundColor") == "rgb(36, 39, 44)"
        page.click("#theme-toggle")
        assert _js(cdp, "document.documentElement.getAttribute('data-theme')") == "light"

        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(120)
            assert _js(
                cdp,
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth",
            ), f"the page scrolls sideways at {width} px"
            assert _overlaps(cdp) == [], f"two controls overlap at {width} px"

        _assert_clean(console_errors, page_errors, requests, responses, demo_server)
    finally:
        context.close()


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
        page.locator("tr[data-endpoint='fake/model']").wait_for(state="visible")
        cells = page.locator("tr[data-endpoint='fake/model'] td")
        assert cells.nth(2).inner_text() == "7"
        assert cells.nth(3).inner_text() == "2"
        assert cells.nth(4).inner_text() == "1 call"
        _assert_clean(console_errors, page_errors, requests, responses, url)
    finally:
        context.close()
        _stop(server, thread)
        router_module.get_router = original

"""Browser smoke test: a demo run in Chromium, no console errors.

Skipped unless Playwright and its Chromium build are installed. The suite must
stay runnable without a browser; this file is the extra check that the page
really works after the unit tests have read the pure models.
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def demo_server():
    """A real uvicorn server, because EventSource needs a real HTTP stream."""
    import uvicorn

    previous_delay = demo.DEMO_DELAY
    demo.DEMO_DELAY = 0
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(demo=True),
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True, name="frontend-smoke-server")
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the demo server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        demo.DEMO_DELAY = previous_delay


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except Exception as error:  # missing browser download, not a test failure
            pytest.skip(f"Chromium is not available for Playwright: {error}")
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
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    console_errors, page_errors, requests, responses = [], [], [], []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))
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


def test_a_demo_run_answers_without_errors_or_scroll(browser, demo_server):
    context, page, cdp, console_errors, page_errors, requests, responses = _new_page(browser, demo_server)
    try:
        page.locator("#demo-badge").wait_for(state="visible")
        assert page.locator("#demo-badge").inner_text() == "Demo data"
        assert page.locator("#llm-health").is_hidden()

        page.fill("#gem-url", DEMO_URL)
        page.click("#run-btn")
        page.locator("#run-state-label", has_text="Done").wait_for(timeout=20_000)

        assert page.locator("#answer-price").inner_text() == "\u20b91,149"
        assert page.locator("#answer-shop").inner_text() == "on flipkart.com"
        assert "saves" in page.locator("#answer-saving").inner_text()
        assert page.locator("#count-matches").inner_text() == "3"
        assert page.locator("#count-possible").inner_text() == "1"
        assert page.locator("#lane-flipkart-status").inner_text() == "Found a match"
        assert page.locator("#lane-amazon-status").inner_text() == "Found a match"
        assert "1 match, 1 possible" in page.locator("#lane-amazon-verify-value").inner_text()
        assert page.locator("#lane-other-status").inner_text() == "Found a match"

        # The source closes itself when the run ends; it is not left polling.
        assert _js(cdp, "window.__eventSources.map(s => s.readyState)") == [2]
        assert sum(1 for url in requests if "/events" in url) == 1

        page.keyboard.press("Tab")  # leave the search box
        page.locator("#tab-matches").focus()
        page.keyboard.press("ArrowRight")
        assert page.locator("#tab-possible").get_attribute("aria-selected") == "true"
        assert page.locator("#panel-possible").is_visible()
        assert page.locator("#panel-matches").is_hidden()

        page.click("#theme-toggle")
        assert _js(cdp, "document.documentElement.getAttribute('data-theme')") == "dark"
        assert _js(cdp, "getComputedStyle(document.body).backgroundColor") == "rgb(13, 16, 23)"
        page.click("#theme-toggle")
        assert _js(cdp, "document.documentElement.getAttribute('data-theme')") == "light"

        page.set_viewport_size({"width": 390, "height": 844})
        assert _js(
            cdp,
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth",
        ), "the page scrolls sideways at 390 px"

        assert page_errors == []
        assert console_errors == []
        external = [
            url for url in requests
            if url.startswith(("http://", "https://")) and not url.startswith(demo_server)
        ]
        assert external == []
        failed = [(url, status) for url, status in responses if status >= 400]
        assert failed == []
    finally:
        context.close()

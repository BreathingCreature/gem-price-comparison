"""Start the local web app with one command.

Runs the offline doctor checks, refuses to start when no LLM endpoint is
ready, picks port 8000 (or the next free one), starts uvicorn on 127.0.0.1
and opens the page in the default browser.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PORT_TRIES = 50
BROWSER_WAIT_SECONDS = 20.0


def doctor_passes() -> bool:
    """Run the offline doctor in a child process so its imports cannot
    change this process's module state."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "doctor.py")],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def ready_llm_count() -> int:
    try:
        from llm.router import get_router

        return sum(1 for row in get_router().status() if row.get("state") == "ready")
    except Exception:
        return 0


def pick_port(start: int = DEFAULT_PORT, tries: int = PORT_TRIES) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((HOST, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free port found in {start}-{start + tries - 1}.")


def open_browser_when_ready(server, port: int, timeout: float = BROWSER_WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            webbrowser.open(f"http://{HOST}:{port}")
            return
        time.sleep(0.1)


def main(argv: Optional[list[str]] = None) -> int:
    if not doctor_passes():
        print("The offline doctor checks failed. Fix the FAIL lines above, then run serve.py again.", file=sys.stderr)
        return 1
    ready = ready_llm_count()
    if ready == 0:
        print(
            "No LLM endpoint is ready. Put NVIDIA_API_KEY in .env, or copy "
            "llm_providers.example.json to llm_providers.json and add a provider key.",
            file=sys.stderr,
        )
        return 1

    port = pick_port()
    print(f"Starting on http://{HOST}:{port} ({ready} LLM endpoint(s) ready). Press Ctrl+C to stop.")

    import uvicorn

    config = uvicorn.Config("api.server:app", host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    threading.Thread(
        target=open_browser_when_ready,
        args=(server, port),
        name="open-browser",
        daemon=True,
    ).start()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

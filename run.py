#!/usr/bin/env python3
"""
Investment Fund — Phase 1 entry point.

Runs two things side-by-side in one container:
  • Trading loop       (background thread)
  • Control API server (main thread, uvicorn)

The control API owns the lifecycle of the app — when it exits, the container
exits, which makes `docker compose restart` and Ctrl+C behave predictably.
"""
import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "httpcore", "opentelemetry", "chromadb", "LiteLLM", "uvicorn.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

if not os.getenv("ANTHROPIC_API_KEY"):
    raise RuntimeError("ANTHROPIC_API_KEY is not set.")

log = logging.getLogger("run")

# Import after env + logging are set up
from fund.config      import settings
from fund.database    import init_db
from fund.crew        import run_loop
from fund.control_api import app as control_app


def _start_loop():
    try:
        run_loop()
    except Exception:
        log.exception("trading loop crashed")


if __name__ == "__main__":
    init_db()

    # Trading loop runs in a background thread; daemon=True so it dies with the process.
    t = threading.Thread(target=_start_loop, name="trading-loop", daemon=True)
    t.start()
    log.info("Trading loop thread started")

    # Control API is the foreground process
    import uvicorn
    uvicorn.run(
        control_app,
        host="0.0.0.0",
        port=settings.control_port,
        log_level=LOG_LEVEL.lower(),
    )

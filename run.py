#!/usr/bin/env python3
"""
Investment Fund — Phase 0 entry point.

Start order:
  Terminal 1:  cd market_sim && python main.py          (market simulator)
  Terminal 2:  python run.py                             (trading loop)

Or with Docker:
  docker compose up
"""
import logging
import os
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
# CrewAI is verbose; silence its internal chatter below WARNING to keep
# the trading log readable.  Set LOG_LEVEL=DEBUG to see everything.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("opentelemetry").setLevel(logging.WARNING)
logging.getLogger("chromadb").setLevel(logging.WARNING)

# ── Sanity check ──────────────────────────────────────────────────────────────
if not os.getenv("ANTHROPIC_API_KEY"):
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. "
        "Copy .env.example → .env and add your key."
    )

# ── Run ───────────────────────────────────────────────────────────────────────
from fund.crew import run_loop

if __name__ == "__main__":
    run_loop()

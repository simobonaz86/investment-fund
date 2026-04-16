"""
fund/crew.py — Trading loop orchestration

Phase 0 flow
─────────────
1. detect_signals()       Scan all assets; return those that moved >= threshold
2. run_research_phase()   Research Analyst analyses + Manager makes trade decision
3. run_execution_phase()  Execution Agent places order (only if Manager said YES)
4. log_decision()         Persist full audit trail to SQLite

The two-phase design is deliberate:
  • Phase 1 crew  = [Research Analyst, Investment Manager]  → lightweight, frequent
  • Phase 2 crew  = [Execution Agent]                        → only on approved trades
This mirrors the spec's "hiring" model — agents are spun up for a single task.

Output parsing
──────────────
Both agents return structured plain-text blocks.  We parse them with
_parse_research() and _parse_manager().  If the LLM drifts from the format,
the parsers return safe defaults (HOLD / no trade) to prevent bad orders.
"""
import logging
import os
import re
import time
from datetime import datetime

import httpx
from crewai import Crew, Task, Process

from fund.agents.execution import build_execution_agent
from fund.agents.manager   import build_investment_manager
from fund.agents.research  import build_research_analyst
from fund.config           import settings
from fund.database         import init_db, log_decision

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Signal detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_signals() -> list[dict]:
    """
    Query the market sim for each asset and return those whose
    price change (over the last 60 bars) meets the momentum threshold.
    """
    signals: list[dict] = []
    for symbol in settings.assets:
        try:
            r = httpx.get(
                f"{settings.market_sim_url}/v2/stocks/{symbol}/bars",
                params={"limit": 65},
                timeout=6.0,
            )
            r.raise_for_status()
            bars = r.json().get("bars", [])
            if len(bars) < 10:
                continue

            current  = bars[-1]["c"]
            baseline = bars[0]["c"]
            pct      = (current - baseline) / baseline

            if abs(pct) >= settings.momentum_threshold:
                sig = {
                    "symbol":        symbol,
                    "pct_change":    pct,
                    "current_price": current,
                    "direction":     "up" if pct > 0 else "down",
                    "detected_at":   datetime.utcnow().isoformat(),
                }
                log.info(
                    "Signal: %s moved %+.1f%% → $%.4f",
                    symbol, pct * 100, current,
                )
                signals.append(sig)

        except Exception as exc:
            log.warning("Signal scan failed for %s: %s", symbol, exc)

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# 2. Phase 1 — Research + Manager decision
# ══════════════════════════════════════════════════════════════════════════════

def run_research_phase(signal: dict) -> dict:
    """
    Spin up Research Analyst + Investment Manager.
    Returns a parsed manager decision dict.

    Manager decision keys:
        trade     bool    — True = approved
        direction str     — 'BUY' | 'SELL' | 'N/A'
        size_usd  float   — dollar amount approved
        reason    str     — one-sentence explanation
        _research dict    — raw research parse (for logging)
    """
    sym      = signal["symbol"]
    pct_str  = f"{signal['pct_change']:+.1%}"
    price    = signal["current_price"]
    conf_thr = settings.confidence_threshold
    max_usd  = settings.max_position_usd

    research_analyst = build_research_analyst()
    manager          = build_investment_manager()

    # ── Task 1: Research Analyst ──────────────────────────────────────────────
    research_task = Task(
        description=f"""
You have been hired to analyse {sym}.

Context:
  • Recent price move: {pct_str}
  • Current price: ${price:.4f}

Steps:
  1. Call get_price_bars("{sym}") to fetch recent OHLCV history.
  2. Call calculate_indicators("{sym}") to get RSI-14, SMA-5, SMA-20, and momentum.
  3. Assess whether the move is supported by technicals or is likely to reverse.

Return your verdict in EXACTLY this format — three lines, nothing else:

VERDICT: [BUY|HOLD|SELL]
CONFIDENCE: [0.00-1.00]
REASON: [one sentence, max 20 words]
""",
        expected_output=(
            "Exactly three lines: "
            "VERDICT: BUY|HOLD|SELL  |  CONFIDENCE: 0.00–1.00  |  REASON: one sentence."
        ),
        agent=research_analyst,
    )

    # ── Task 2: Investment Manager ────────────────────────────────────────────
    manager_task = Task(
        description=f"""
Review the Research Analyst's verdict for {sym} (above in context).

Your trade rules:
  • Only approve if verdict is BUY or SELL AND confidence >= {conf_thr:.2f}
  • HOLD verdict → TRADE: NO, always
  • Confidence < {conf_thr:.2f} → TRADE: NO, always
  • Max size: ${max_usd:.0f}
  • Call get_portfolio_state() to check existing exposure before deciding.

Return your decision in EXACTLY this format — four lines, nothing else:

TRADE: [YES|NO]
DIRECTION: [BUY|SELL|N/A]
SIZE_USD: [dollar amount, or 0]
REASON: [one sentence, max 20 words]
""",
        expected_output=(
            "Exactly four lines: "
            "TRADE: YES|NO  |  DIRECTION: BUY|SELL|N/A  |  SIZE_USD: number  |  REASON: one sentence."
        ),
        agent=manager,
        context=[research_task],
    )

    crew = Crew(
        agents=[research_analyst, manager],
        tasks=[research_task, manager_task],
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff()
    raw = result.raw if hasattr(result, "raw") else str(result)

    # Parse both outputs from the combined raw text
    research_parse = _parse_research(raw)
    manager_parse  = _parse_manager(raw)
    manager_parse["_research"] = research_parse

    log.info(
        "Research: %s (conf %.2f)  |  Manager: TRADE=%s %s $%.0f",
        research_parse.get("verdict",    "?"),
        research_parse.get("confidence", 0.0),
        manager_parse.get("trade"),
        manager_parse.get("direction", ""),
        manager_parse.get("size_usd",  0.0),
    )
    return manager_parse


# ══════════════════════════════════════════════════════════════════════════════
# 3. Phase 2 — Execution (only when Manager approves)
# ══════════════════════════════════════════════════════════════════════════════

def run_execution_phase(decision: dict, signal: dict) -> dict:
    """
    Spin up the Execution Agent to place the approved order.
    Returns a parsed fill dict.
    """
    sym       = signal["symbol"]
    direction = decision["direction"]
    size_usd  = decision["size_usd"]

    execution_agent = build_execution_agent()

    execute_task = Task(
        description=f"""
The Investment Manager has approved the following trade:
  • Symbol    : {sym}
  • Direction : {direction}
  • Size      : ${size_usd:.2f}

Steps:
  1. Call get_portfolio_state() to confirm current exposure.
  2. Call place_paper_order("{sym}", "{direction}", {size_usd:.2f}) to execute.
  3. Report the fill details.

Return your confirmation in EXACTLY this format — four lines, nothing else:

STATUS: [filled|failed]
FILL_PRICE: [price]
QUANTITY: [shares]
TOTAL_USD: [total dollar amount]
""",
        expected_output=(
            "Exactly four lines: "
            "STATUS: filled|failed  |  FILL_PRICE: number  |  QUANTITY: number  |  TOTAL_USD: number."
        ),
        agent=execution_agent,
    )

    crew = Crew(
        agents=[execution_agent],
        tasks=[execute_task],
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff()
    raw  = result.raw if hasattr(result, "raw") else str(result)
    fill = _parse_fill(raw)

    log.info(
        "Execution: %s %s @ $%.4f × %.4f = $%.2f  [%s]",
        sym, direction,
        fill.get("fill_price", 0.0),
        fill.get("quantity",   0.0),
        fill.get("total_usd",  0.0),
        fill.get("status",    "?"),
    )
    return fill


# ══════════════════════════════════════════════════════════════════════════════
# 4. Full trading cycle (one scan → research → decide → execute → log)
# ══════════════════════════════════════════════════════════════════════════════

def run_trading_cycle() -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds")
    log.info("─── Cycle %s ─── scanning %s", ts, settings.assets)

    signals = detect_signals()
    if not signals:
        log.info("No momentum signals.")
        return

    for sig in signals:
        sym = sig["symbol"]
        log.info("┌─ Session: %s %+.1f%%", sym, sig["pct_change"] * 100)

        # ── Phase 1: Research + Manager decision ──────────────────────────────
        decision = run_research_phase(sig)
        research = decision.get("_research", {})

        # ── Phase 2: Execute only if approved ─────────────────────────────────
        fill: dict = {}
        if decision["trade"]:
            log.info("│  Manager APPROVED → hiring Execution Agent")
            fill = run_execution_phase(decision, sig)
        else:
            log.info("│  Manager DECLINED — reason: %s", decision.get("reason", "N/A"))

        # ── Persist audit trail ───────────────────────────────────────────────
        log_decision(
            symbol           = sym,
            pct_change       = sig["pct_change"],
            research_verdict = research.get("verdict"),
            confidence       = research.get("confidence"),
            trade_taken      = decision["trade"],
            direction        = decision.get("direction"),
            size_usd         = decision.get("size_usd", 0.0),
            reason           = decision.get("reason", ""),
            fill_price       = fill.get("fill_price"),
        )

        log.info(
            "└─ %s done. trade=%s fill=$%.2f",
            sym,
            decision["trade"],
            fill.get("total_usd", 0.0),
        )


# ══════════════════════════════════════════════════════════════════════════════
# 5. Main loop
# ══════════════════════════════════════════════════════════════════════════════

def run_loop() -> None:
    """
    Blocking main loop.
    Calls run_trading_cycle() every CHECK_INTERVAL_SECONDS.
    Press Ctrl-C to stop.
    """
    init_db()
    log.info(
        "Investment Fund — Phase 0 started\n"
        "  Assets    : %s\n"
        "  Threshold : %.0f%%\n"
        "  Confidence: %.0f%%\n"
        "  Max trade : $%.0f\n"
        "  Interval  : %ds",
        settings.assets,
        settings.momentum_threshold  * 100,
        settings.confidence_threshold * 100,
        settings.max_position_usd,
        settings.check_interval_seconds,
    )

    try:
        while True:
            try:
                run_trading_cycle()
            except Exception as exc:
                log.error("Cycle error: %s", exc, exc_info=True)

            log.info("Sleeping %ds …\n", settings.check_interval_seconds)
            time.sleep(settings.check_interval_seconds)

    except KeyboardInterrupt:
        log.info("Shutdown requested. Exiting cleanly.")


# ══════════════════════════════════════════════════════════════════════════════
# Output parsers — safe defaults on any format deviation
# ══════════════════════════════════════════════════════════════════════════════

def _parse_research(text: str) -> dict:
    out = {"verdict": "HOLD", "confidence": 0.0, "reason": ""}
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("BUY", "SELL", "HOLD"):
                out["verdict"] = val
        elif line.upper().startswith("CONFIDENCE:"):
            try:
                out["confidence"] = float(re.search(r"[\d.]+", line.split(":", 1)[1])[0])
            except (TypeError, ValueError):
                pass
        elif line.upper().startswith("REASON:"):
            out["reason"] = line.split(":", 1)[1].strip()
    return out


def _parse_manager(text: str) -> dict:
    out = {"trade": False, "direction": "N/A", "size_usd": 0.0, "reason": ""}
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("TRADE:"):
            out["trade"] = "YES" in line.upper()
        elif line.upper().startswith("DIRECTION:"):
            val = line.split(":", 1)[1].strip().upper()
            out["direction"] = val if val in ("BUY", "SELL") else "N/A"
        elif line.upper().startswith("SIZE_USD:"):
            try:
                out["size_usd"] = float(re.search(r"[\d.]+", line.split(":", 1)[1])[0])
            except (TypeError, ValueError):
                pass
        elif line.upper().startswith("REASON:"):
            out["reason"] = line.split(":", 1)[1].strip()
    return out


def _parse_fill(text: str) -> dict:
    out = {"status": "unknown", "fill_price": 0.0, "quantity": 0.0, "total_usd": 0.0}
    mapping = {
        "STATUS:":     ("status",     str),
        "FILL_PRICE:": ("fill_price", float),
        "QUANTITY:":   ("quantity",   float),
        "TOTAL_USD:":  ("total_usd",  float),
    }
    for line in text.splitlines():
        line = line.strip()
        for prefix, (key, cast) in mapping.items():
            if line.upper().startswith(prefix):
                raw = line.split(":", 1)[1].strip()
                try:
                    out[key] = cast(re.search(r"[\d.]+", raw)[0]) if cast is float else raw.lower()
                except (TypeError, ValueError):
                    pass
    return out

"""Phase 2.2 trading loop.

Integrates Phase 2.1 two-phase flow (Research → Manager → Execution) with:
  * Every CEO decision broadcast to principals_chat via ceo.announce_*()
  * Kevin self-audit pre-execution (block_trade or flag on risk)
  * All agent spend accounted against 70/15/15 budget pools
  * Kevin debug_gate run on startup (once)

Signal detection is delegated to the market sim — scans ASSETS every
CHECK_INTERVAL_SECONDS for moves exceeding MOMENTUM_THRESHOLD.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from crewai import Agent, Crew, LLM, Task

from fund.agents import ceo as ceo_chat
from fund.agents import kevin as kevin_mod
from fund.assets import ASSETS as ASSET_META, threshold_for
from fund.config import settings
from fund.database import (conn, get_model, init_db, now_iso, record_spend,
                           upsert_agent)
from fund.market_hours import filter_open, is_open

log = logging.getLogger("fund.crew")


# ── Data types ──────────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol: str
    last_price: float
    change_pct: float


@dataclass
class ResearchVerdict:
    verdict: str       # BUY | HOLD | SELL
    confidence: float  # 0.0 - 1.0
    reason: str


@dataclass
class ManagerDecision:
    trade: bool
    direction: str     # BUY | SELL | HOLD
    size_usd: float
    reason: str


# ── LLM helpers ─────────────────────────────────────────────────────────────

def _ceo_llm() -> LLM:
    return LLM(model=get_model("ceo"), api_key=settings.ANTHROPIC_API_KEY)


def _specialist_llm() -> LLM:
    return LLM(model=get_model("specialist_default"),
               api_key=settings.ANTHROPIC_API_KEY)


# ── Signal detection ────────────────────────────────────────────────────────

def detect_signals() -> list[Signal]:
    """Scan open exchanges for assets that moved beyond their own threshold.

    Per-asset changes vs. Phase 2.2:
      * Each asset has its own momentum threshold (see fund/assets.py)
      * Exchanges closed right now are skipped entirely (no Yahoo call wasted)
      * Lookback uses the last 60 bars (≈1 hour of 1-min data) vs adjacent 2
        bars — real 1-min moves are noisy; hour-scale moves are the actual
        signal.
      * Stale data (market closed, weekend) is filtered out by the sim and by
        the scanner.
    """
    signals: list[Signal] = []
    universe = settings.assets_list
    open_tickers = filter_open(universe)
    if not open_tickers:
        log.debug("No exchanges open — skipping scan")
        return signals

    try:
        with httpx.Client(timeout=5.0) as client:
            for symbol in open_tickers:
                try:
                    r = client.get(
                        f"{settings.MARKET_SIM_URL}/v2/stocks/{symbol}/bars",
                        params={"limit": 65},
                    )
                    bars = r.json().get("bars", [])
                    if len(bars) < 10:
                        continue
                    baseline, current = bars[0]["c"], bars[-1]["c"]
                    change = (current - baseline) / baseline if baseline else 0
                    threshold = threshold_for(symbol,
                                              fallback=settings.MOMENTUM_THRESHOLD)
                    if abs(change) >= threshold:
                        log.info("Signal: %s moved %+.2f%% over lookback "
                                 "(threshold %.2f%%) → $%.4f",
                                 symbol, change * 100, threshold * 100, current)
                        signals.append(Signal(symbol, current, change))
                except Exception as e:
                    log.warning("signal fetch failed for %s: %s", symbol, e)
    except Exception as e:
        log.error("market sim unreachable: %s", e)
    return signals


# ── Agents (built fresh each cycle — cheap) ─────────────────────────────────

def build_research() -> Agent:
    return Agent(
        role="Research Analyst",
        goal="Produce a BUY/HOLD/SELL verdict with confidence 0-1.",
        backstory="You do quick technical + fundamental reads on momentum moves.",
        llm=_specialist_llm(),
        verbose=False, allow_delegation=False,
    )


def build_manager() -> Agent:
    return Agent(
        role="Investment Manager (CEO)",
        goal=("Decide whether to trade. Enforce position limits, "
              "confidence threshold, and mandate."),
        backstory=("You own the strategy. You can propose BUY/SELL/HOLD "
                   "with a USD size capped at MAX_POSITION_USD."),
        llm=_ceo_llm(),
        verbose=False, allow_delegation=False,
    )


def build_execution() -> Agent:
    return Agent(
        role="Execution Agent",
        goal="Place paper orders and report fills.",
        backstory="You are invoked only after the Manager approves a trade.",
        llm=_specialist_llm(),
        verbose=False, allow_delegation=False,
    )


# ── Parsers — forgiving to LLM drift ────────────────────────────────────────

def _parse_research(text: str) -> ResearchVerdict:
    verdict = "HOLD"
    conf = 0.5
    reason = text.strip()[:300]
    for line in text.splitlines():
        up = line.upper()
        if up.startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("BUY", "HOLD", "SELL"):
                verdict = v
        elif up.startswith("CONFIDENCE:"):
            try:
                conf = float(line.split(":", 1)[1].strip().replace("%", ""))
                if conf > 1:
                    conf /= 100.0
            except ValueError:
                pass
        elif up.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return ResearchVerdict(verdict, max(0.0, min(1.0, conf)), reason)


def _parse_manager(text: str) -> ManagerDecision:
    trade = False
    direction = "HOLD"
    size_usd = 0.0
    reason = text.strip()[:300]
    for line in text.splitlines():
        up = line.upper()
        if up.startswith("TRADE:"):
            trade = "YES" in up or "TRUE" in up
        elif up.startswith("DIRECTION:"):
            d = line.split(":", 1)[1].strip().upper()
            if d in ("BUY", "SELL", "HOLD"):
                direction = d
        elif up.startswith("SIZE_USD:"):
            try:
                size_usd = float(line.split(":", 1)[1].strip()
                                 .replace("$", "").replace(",", ""))
            except ValueError:
                pass
        elif up.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    size_usd = min(size_usd, settings.MAX_POSITION_USD)
    if direction == "HOLD":
        trade = False
    return ManagerDecision(trade, direction, size_usd, reason)


# ── Decision persistence ────────────────────────────────────────────────────

def _log_decision(signal: Signal, research: ResearchVerdict,
                  mgr: ManagerDecision, executed: bool) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO manager_decisions
               (symbol, research_verdict, confidence, trade_taken,
                direction, size_usd, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal.symbol, research.verdict, research.confidence,
             int(executed), mgr.direction, mgr.size_usd,
             mgr.reason, now_iso()),
        )
        return cur.lastrowid


def _place_order(symbol: str, direction: str, size_usd: float,
                 last_price: float) -> dict:
    """Execute against market sim. Returns fill details."""
    qty = round(size_usd / max(last_price, 0.01), 4)
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.post(
                f"{settings.MARKET_SIM_URL}/v2/orders",
                json={"symbol": symbol, "side": direction.lower(),
                      "qty": qty, "type": "market"},
            )
            return r.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Main loop ───────────────────────────────────────────────────────────────

def run_cycle() -> None:
    signals = detect_signals()
    if not signals:
        log.debug("No momentum signals.")
        return

    for sig in signals:
        log.info("Signal: %s moved %+.2f%% → $%.2f",
                 sig.symbol, sig.change_pct * 100, sig.last_price)

        # Phase 1: Research + Manager
        research_agent = build_research()
        manager_agent = build_manager()
        upsert_agent("research", "active", get_model("specialist_default"))

        t_research = Task(
            description=(
                f"Quick analysis of {sig.symbol}. Last price "
                f"${sig.last_price:.2f}, recent change "
                f"{sig.change_pct:+.2%}.\n\n"
                "Return exactly:\n"
                "VERDICT: BUY|HOLD|SELL\n"
                "CONFIDENCE: 0.0-1.0\n"
                "REASON: one sentence"
            ),
            expected_output="3 lines: VERDICT / CONFIDENCE / REASON.",
            agent=research_agent,
        )
        t_manager = Task(
            description=(
                f"Research verdict on {sig.symbol} is in context. Decide trade.\n"
                f"Rules: confidence must be >= {settings.CONFIDENCE_THRESHOLD}, "
                f"max position ${settings.MAX_POSITION_USD}.\n\n"
                "Return exactly:\n"
                "TRADE: YES|NO\n"
                "DIRECTION: BUY|SELL|HOLD\n"
                "SIZE_USD: float\n"
                "REASON: one sentence"
            ),
            expected_output="4 lines: TRADE / DIRECTION / SIZE_USD / REASON.",
            agent=manager_agent,
            context=[t_research],
        )

        try:
            crew1 = Crew(agents=[research_agent, manager_agent],
                         tasks=[t_research, t_manager], verbose=False)
            crew1_output = str(crew1.kickoff())
        except Exception as e:
            log.exception("Research+Manager crew failed")
            continue

        # Parse verdicts. CrewAI output is the concatenated last task;
        # use each task's .output where available.
        try:
            research_text = str(t_research.output) if t_research.output else crew1_output
            manager_text = str(t_manager.output) if t_manager.output else crew1_output
        except Exception:
            research_text = manager_text = crew1_output

        research = _parse_research(research_text)
        decision = _parse_manager(manager_text)

        upsert_agent("research", "idle")
        # Record spend (rough estimate — crewai doesn't expose token counts uniformly)
        record_spend("research", get_model("specialist_default"),
                     in_tok=1500, out_tok=200, cost_usd=0.01)
        record_spend("ceo", get_model("ceo"),
                     in_tok=1800, out_tok=250, cost_usd=0.03)

        # Decide whether to announce a proposed trade or a hold
        executed = False
        decision_id = _log_decision(sig, research, decision, executed=False)

        if (decision.trade and decision.direction in ("BUY", "SELL") and
                research.confidence >= settings.CONFIDENCE_THRESHOLD):

            ceo_chat.announce_decision(
                decision_id=decision_id, symbol=sig.symbol,
                direction=decision.direction, size_usd=decision.size_usd,
                reason=decision.reason,
            )

            # Kevin self-audit: simple concentration/confidence check
            if research.confidence < 0.80 and decision.size_usd > 600:
                kevin_mod.flag(
                    "yellow", "decision", str(decision_id),
                    f"Size ${decision.size_usd:.0f} at "
                    f"confidence {research.confidence:.2f} — "
                    "consider trimming.",
                )

            # Phase 2: Execution
            upsert_agent("execution", "active",
                         get_model("specialist_default"))
            fill = _place_order(sig.symbol, decision.direction,
                                decision.size_usd, sig.last_price)
            record_spend("execution", get_model("specialist_default"),
                         in_tok=500, out_tok=80, cost_usd=0.002)
            upsert_agent("execution", "idle")

            if fill.get("status") in ("filled", "accepted"):
                executed = True
                fill_px = fill.get("filled_avg_price") or sig.last_price
                ceo_chat.announce_hire.__self__ if False else None
                # Re-announce fill confirmation
                from fund.database import post_chat
                post_chat(
                    "ceo",
                    f"✅ Filled #{decision_id}: {decision.direction} "
                    f"{sig.symbol} @ ${fill_px:.2f}",
                    chat_room="principals",
                    thread=f"decision:{decision_id}",
                )
                # Update the manager_decisions row
                with conn() as c:
                    c.execute(
                        "UPDATE manager_decisions SET trade_taken=1 "
                        "WHERE id=?", (decision_id,),
                    )
            else:
                log.warning("fill rejected: %s", fill)
                kevin_mod.flag("red", "decision", str(decision_id),
                               f"Fill rejected: {fill.get('error', 'unknown')}")
        else:
            # No-trade branch
            ceo_chat.announce_hold(
                decision_id=decision_id, symbol=sig.symbol,
                reason=(decision.reason if not decision.trade
                        else f"confidence {research.confidence:.2f} "
                             f"below threshold {settings.CONFIDENCE_THRESHOLD}"),
            )


def main_loop() -> None:
    """Entrypoint — runs forever, one cycle per CHECK_INTERVAL_SECONDS."""
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Fund Phase 2.2 starting. Assets=%s, interval=%ds",
             settings.assets_list, settings.CHECK_INTERVAL_SECONDS)

    init_db()

    # Run Kevin debug gate once at boot
    gate = kevin_mod.debug_gate()
    if gate["ok"]:
        log.info("Kevin debug gate OK")
    else:
        log.error("Kevin debug gate FAILED: %s", gate["failures"])

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Shutting down.")
            return
        except Exception:
            log.exception("Cycle error — continuing.")
        time.sleep(settings.CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()

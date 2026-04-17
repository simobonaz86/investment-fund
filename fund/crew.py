"""
Trading loop orchestration (Phase 1).

Major changes vs Phase 0:
  • Manager HIRES specialists via a reasoning step (not a hardcoded switch)
  • Structured output via Pydantic (no regex)
  • Per-cycle cost tracking with budget enforcement
  • Per-asset cooldown prevents re-firing on persistent moves
  • Reflection step after each decision writes a lesson for next time
  • Runtime-mutable control state: thresholds, halt, assets all from DB

Cycle anatomy:
  1. Read control state      — halt? active assets? threshold?
  2. Check weekly budget caps  — overall + per-team
  3. detect_signals          — pure Python, zero tokens
  4. For each signal:
     a. Manager HIRING step  (schema: HiringPlan)
     b. Hire chosen specialists in parallel (schemas: ResearchVerdict, RiskReport)
     c. Manager DECIDING step (schema: ManagerDecision)
     d. If approved, hire Execution (schema: ExecutionResult)
     e. Reflection writes a lesson (schema: ReflectionNote)
     f. Set cooldown on the asset
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import httpx
from crewai import Crew, Task, Process

from fund.agents import (
    build_execution_agent,
    build_investment_manager,
    build_reflection_agent,
    build_research_analyst,
    build_risk_manager,
)
from fund.config   import settings
from fund.database import (
    add_lesson,
    init_db,
    is_on_cooldown,
    log_cost,
    log_decision,
    read_control,
    recent_lessons,
    set_cooldown,
    set_halted,
    spend_breakdown_last_week,
    weekly_spend,
)
from fund.schemas import (
    ExecutionResult,
    HiringPlan,
    ManagerDecision,
    ReflectionNote,
    ResearchVerdict,
    RiskReport,
)

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _run_crew_tracked(
    agent_name: str,
    agents: list,
    tasks: list[Task],
    task_ref: str = "",
):
    """
    Run a crew and track its token usage to the agent_costs table.
    Returns (result_obj, pydantic_output_or_None, cost_usd).
    """
    crew = Crew(agents=agents, tasks=tasks, process=Process.sequential, verbose=False)
    result = crew.kickoff()

    # Token accounting — CrewAI exposes usage on `crew.usage_metrics` and the LLM.
    # We fall back to estimated tokens if usage isn't available.
    tokens_in  = 0
    tokens_out = 0
    try:
        um = getattr(crew, "usage_metrics", None) or {}
        tokens_in  = int(getattr(um, "prompt_tokens",     getattr(um, "prompt_tokens", 0)) or 0)
        tokens_out = int(getattr(um, "completion_tokens", getattr(um, "completion_tokens", 0)) or 0)
        # Some versions expose it as a dict
        if hasattr(um, "get"):
            tokens_in  = tokens_in  or int(um.get("prompt_tokens", 0) or 0)
            tokens_out = tokens_out or int(um.get("completion_tokens", 0) or 0)
    except Exception:
        pass

    # Fallback: estimate from raw output length (very rough)
    if tokens_in == 0 and tokens_out == 0:
        raw = str(getattr(result, "raw", result))
        tokens_out = max(100, len(raw) // 4)
        tokens_in  = max(500, tokens_out * 3)   # rough prior

    model = agents[0].llm.model if agents else "unknown"
    cost  = log_cost(agent_name, model, tokens_in, tokens_out, task_ref)

    # Extract pydantic output from the last task
    pyd = None
    last = tasks[-1] if tasks else None
    if last and getattr(last, "output", None):
        pyd = getattr(last.output, "pydantic", None)

    return result, pyd, cost


def _team_for_agent(agent_name: str) -> str:
    """Map agent name → budget team bucket."""
    return {
        "manager":     "research",    # Manager sits with the Research team budget
        "research":    "research",
        "risk":        "risk",
        "sentiment":   "sentiment",
        "execution":   "execution",
        "accountant":  "accountant",
        "reflection":  "reflection",
    }.get(agent_name, agent_name)


def _budget_ok(agent_name: str) -> tuple[bool, str]:
    """Check overall and per-team weekly caps before spending anything."""
    overall = weekly_spend()
    if overall >= settings.weekly_budget_total_usd:
        return False, f"overall weekly cap reached (${overall:.2f} / ${settings.weekly_budget_total_usd:.2f})"

    team = _team_for_agent(agent_name)
    cap_attr = f"weekly_budget_{team}_usd"
    cap = getattr(settings, cap_attr, None)
    if cap is not None:
        spent = weekly_spend(agent_name)
        # Manager cost also counts against the research team
        if team == "research" and agent_name != "manager":
            spent += weekly_spend("manager")
        if spent >= cap:
            return False, f"{team} team weekly cap reached (${spent:.2f} / ${cap:.2f})"

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# Signal detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_signals(ctrl: dict) -> list[dict]:
    signals: list[dict] = []
    for symbol in ctrl["assets"]:
        if is_on_cooldown(symbol):
            log.debug("skip %s — on cooldown", symbol)
            continue
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
            pct = (current - baseline) / baseline

            if abs(pct) >= ctrl["momentum_threshold"]:
                log.info("signal  %s  %+.2f%% → $%.4f", symbol, pct * 100, current)
                signals.append({
                    "symbol":        symbol,
                    "pct_change":    pct,
                    "current_price": current,
                    "direction":     "up" if pct > 0 else "down",
                })
        except Exception as exc:
            log.warning("scan %s failed: %s", symbol, exc)

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# Phase A — Manager decides who to hire
# ══════════════════════════════════════════════════════════════════════════════

def run_hiring_step(signal: dict) -> HiringPlan | None:
    """Manager's first reasoning step: given this signal, who do we need?"""
    ok, why = _budget_ok("manager")
    if not ok:
        log.warning("hiring skipped — %s", why)
        return None

    sym  = signal["symbol"]
    pct  = signal["pct_change"]
    manager = build_investment_manager()
    lessons = recent_lessons(symbol=sym, limit=3)
    lesson_text = "\n".join(f"  • {l['outcome']}: {l['note']}" for l in lessons) or "  (none yet)"

    task = Task(
        description=f"""
You've received a price-momentum signal: {sym} moved {pct:+.1%}.

Recent lessons learned for this asset:
{lesson_text}

Decide which specialists to hire. Call get_portfolio_state to see current exposure.

Rules of thumb:
  • Research — almost always YES (evidence before action)
  • Risk — YES if the portfolio already holds {sym}, or if overall portfolio
    concentration is high
  • Sentiment — only if a news/earnings event seems relevant (default NO in sim)

Return a HiringPlan.
""",
        expected_output="A HiringPlan object.",
        agent=manager,
        output_pydantic=HiringPlan,
    )

    _, plan, cost = _run_crew_tracked("manager", [manager], [task], task_ref=f"{sym}:hire")
    log.info("  manager.hire  research=%s risk=%s sentiment=%s  ($%.4f)",
             plan.hire_research if plan else "?",
             plan.hire_risk if plan else "?",
             plan.hire_sentiment if plan else "?",
             cost)
    return plan


# ══════════════════════════════════════════════════════════════════════════════
# Phase B — Specialists report
# ══════════════════════════════════════════════════════════════════════════════

def run_research(signal: dict) -> ResearchVerdict | None:
    ok, why = _budget_ok("research")
    if not ok:
        log.warning("research skipped — %s", why); return None

    sym = signal["symbol"]
    analyst = build_research_analyst()
    task = Task(
        description=f"""
Analyse {sym}. Current move: {signal['pct_change']:+.1%}. Price: ${signal['current_price']:.4f}.

Steps:
  1. Call get_price_bars("{sym}").
  2. Call calculate_indicators("{sym}").
  3. Form a verdict.

Return a ResearchVerdict.
""",
        expected_output="A ResearchVerdict.",
        agent=analyst,
        output_pydantic=ResearchVerdict,
    )
    _, verdict, cost = _run_crew_tracked("research", [analyst], [task], task_ref=sym)
    log.info("  research      %s conf=%.2f  ($%.4f)",
             verdict.verdict if verdict else "?",
             verdict.confidence if verdict else 0.0,
             cost)
    return verdict


def run_risk(signal: dict, proposed_size: float) -> RiskReport | None:
    ok, why = _budget_ok("risk")
    if not ok:
        log.warning("risk skipped — %s", why); return None

    sym = signal["symbol"]
    rm = build_risk_manager()
    task = Task(
        description=f"""
Proposed trade: {sym}, size ${proposed_size:.2f}.

Steps:
  1. Call get_portfolio_state().
  2. Assess concentration and existing exposure.
  3. Return RiskReport with assessment and recommended_size_usd.
""",
        expected_output="A RiskReport.",
        agent=rm,
        output_pydantic=RiskReport,
    )
    _, report, cost = _run_crew_tracked("risk", [rm], [task], task_ref=sym)
    log.info("  risk          %s size=$%.2f  ($%.4f)",
             report.assessment if report else "?",
             report.recommended_size_usd if report else 0.0,
             cost)
    return report


# ══════════════════════════════════════════════════════════════════════════════
# Phase C — Manager decides
# ══════════════════════════════════════════════════════════════════════════════

def run_decision_step(
    signal: dict,
    ctrl: dict,
    research: ResearchVerdict | None,
    risk: RiskReport | None,
) -> ManagerDecision | None:
    ok, why = _budget_ok("manager")
    if not ok:
        log.warning("decision skipped — %s", why); return None

    sym = signal["symbol"]
    manager = build_investment_manager()

    research_txt = (
        f"  verdict={research.verdict} confidence={research.confidence:.2f} "
        f"reason={research.reason}" if research else "  (not hired)"
    )
    risk_txt = (
        f"  assessment={risk.assessment} recommended_size=${risk.recommended_size_usd:.2f} "
        f"reason={risk.reason}" if risk else "  (not hired)"
    )

    task = Task(
        description=f"""
You now have the specialist reports for {sym}:

Research:
{research_txt}

Risk:
{risk_txt}

Signal: {signal['pct_change']:+.1%} move.
Max position cap: ${ctrl['max_position_usd']:.0f}.
Confidence threshold: {ctrl['confidence_threshold']:.2f}.

Rules (non-negotiable):
  • Research verdict HOLD → TRADE: NO
  • Research confidence below threshold → TRADE: NO
  • Risk assessment 'block' → TRADE: NO
  • Size: min(max_position_usd, risk.recommended_size_usd if risk else max_position_usd)

Return a ManagerDecision.
""",
        expected_output="A ManagerDecision.",
        agent=manager,
        output_pydantic=ManagerDecision,
    )
    _, decision, cost = _run_crew_tracked("manager", [manager], [task], task_ref=f"{sym}:decide")
    log.info("  manager.decide %s %s $%.0f  ($%.4f)",
             "TRADE" if decision and decision.trade else "SKIP",
             decision.direction if decision else "?",
             decision.size_usd if decision else 0.0,
             cost)
    return decision


# ══════════════════════════════════════════════════════════════════════════════
# Phase D — Execution
# ══════════════════════════════════════════════════════════════════════════════

def run_execution(signal: dict, decision: ManagerDecision) -> ExecutionResult | None:
    ok, why = _budget_ok("execution")
    if not ok:
        log.warning("execution skipped — %s", why); return None

    sym = signal["symbol"]
    exec_agent = build_execution_agent()
    task = Task(
        description=f"""
Approved trade:
  Symbol:    {sym}
  Direction: {decision.direction}
  Size:      ${decision.size_usd:.2f}

Steps:
  1. Call get_portfolio_state().
  2. Call place_paper_order("{sym}", "{decision.direction}", {decision.size_usd:.2f}).
  3. Return an ExecutionResult reflecting the fill.
""",
        expected_output="An ExecutionResult.",
        agent=exec_agent,
        output_pydantic=ExecutionResult,
    )
    _, result, cost = _run_crew_tracked("execution", [exec_agent], [task], task_ref=sym)
    log.info("  execution     %s @ $%.4f × %.4f = $%.2f  ($%.4f)",
             result.status if result else "?",
             result.fill_price if result else 0.0,
             result.quantity if result else 0.0,
             result.total_usd if result else 0.0,
             cost)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Phase E — Reflection
# ══════════════════════════════════════════════════════════════════════════════

def run_reflection(
    signal: dict,
    decision: ManagerDecision | None,
    fill: ExecutionResult | None,
    decision_id: int,
) -> None:
    ok, _ = _budget_ok("reflection")
    if not ok:
        return

    sym  = signal["symbol"]
    agent = build_reflection_agent()

    summary = {
        "symbol":       sym,
        "signal_pct":   signal["pct_change"],
        "trade_taken":  bool(decision and decision.trade),
        "direction":    decision.direction if decision else "N/A",
        "size_usd":     decision.size_usd  if decision else 0.0,
        "filled":       fill.status == "filled" if fill else False,
        "fill_price":   fill.fill_price if fill else None,
    }
    task = Task(
        description=f"""
A decision just finished. Write ONE concise lesson for future cycles on {sym}.

Decision summary:
{json.dumps(summary, indent=2)}

If no trade was taken, label outcome as 'pending' and write why the fund waited.
If a trade was filled, label as 'pending' (we can't yet judge win/loss).
Fill the ReflectionNote schema.
""",
        expected_output="A ReflectionNote.",
        agent=agent,
        output_pydantic=ReflectionNote,
    )
    _, note, _cost = _run_crew_tracked("reflection", [agent], [task], task_ref=sym)
    if note:
        add_lesson(symbol=sym, decision_id=decision_id, outcome=note.outcome, note=note.note)


# ══════════════════════════════════════════════════════════════════════════════
# One full cycle
# ══════════════════════════════════════════════════════════════════════════════

def run_trading_cycle() -> None:
    ctrl = read_control()
    if ctrl["halted"]:
        log.info("cycle skipped — halted (%s)", ctrl.get("halt_reason") or "no reason")
        return

    overall = weekly_spend()
    log.info("─── cycle  weekly spend $%.2f / $%.2f  active=%s",
             overall, settings.weekly_budget_total_usd, ctrl["assets"])

    signals = detect_signals(ctrl)
    if not signals:
        log.info("  no signals")
        return

    for sig in signals:
        sym = sig["symbol"]
        log.info("┌─ %s  %+.2f%%", sym, sig["pct_change"] * 100)

        # A. Hiring plan
        plan = run_hiring_step(sig)
        if not plan:
            log.info("└─ skip (hiring step failed)")
            set_cooldown(sym, ctrl["cooldown_minutes"])
            continue

        # B. Specialists
        specialists_hired: list[str] = []
        research = risk = None
        if plan.hire_research:
            research = run_research(sig)
            specialists_hired.append("research")
        if plan.hire_risk:
            risk = run_risk(sig, proposed_size=ctrl["max_position_usd"])
            specialists_hired.append("risk")

        # C. Decision
        decision = run_decision_step(sig, ctrl, research, risk)
        if not decision:
            log.info("└─ skip (decision step failed)")
            set_cooldown(sym, ctrl["cooldown_minutes"])
            continue

        # Enforce research confidence rule defensively
        if research and research.confidence < ctrl["confidence_threshold"]:
            decision.trade = False
            decision.reason = f"confidence {research.confidence:.2f} below threshold"

        # D. Execution
        fill = None
        if decision.trade and decision.direction in ("BUY", "SELL"):
            fill = run_execution(sig, decision)

        # Audit trail
        did = log_decision(
            symbol           = sym,
            pct_change       = sig["pct_change"],
            specialists_hired= ",".join(specialists_hired),
            research_verdict = research.verdict if research else None,
            confidence       = research.confidence if research else None,
            trade_taken      = decision.trade,
            direction        = decision.direction,
            size_usd         = decision.size_usd,
            reason           = decision.reason,
            fill_price       = fill.fill_price if fill else None,
        )

        # E. Reflection + cooldown
        run_reflection(sig, decision, fill, did)
        set_cooldown(sym, ctrl["cooldown_minutes"])

        log.info("└─ done")


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════

def run_loop() -> None:
    init_db()
    ctrl = read_control()
    log.info(
        "Investment Fund — Phase 1\n"
        "  Manager model   : %s\n"
        "  Specialist model: %s\n"
        "  Weekly cap total: $%.2f\n"
        "  Assets          : %s\n"
        "  Threshold       : %.1f%%\n"
        "  Confidence      : %.0f%%",
        settings.manager_model,
        settings.research_model,
        settings.weekly_budget_total_usd,
        ctrl["assets"],
        ctrl["momentum_threshold"] * 100,
        ctrl["confidence_threshold"] * 100,
    )

    try:
        while True:
            try:
                run_trading_cycle()
            except Exception:
                log.exception("cycle error")

            ctrl = read_control()
            interval = ctrl["check_interval_sec"]
            log.info("sleeping %ds\n", interval)
            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("shutdown requested")
        set_halted(True, reason="keyboard interrupt")

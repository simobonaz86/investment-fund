"""
Trading loop orchestration (Phase 2.1).

Phase 2.1 additions:
  • Kevin the Auditor reviews every CEO decision before execution.
  • Kevin can pass / flag yellow / flag red / block.
  • Blocks create pending_approval rows — Board approves or rejects via dashboard.
  • Principals' room (CEO ↔ Kevin chat) captures Kevin's escalations.
  • Board alerts are raised on red flags and blocks.

Cycle anatomy (Phase 2.1):
  1. Read control state        — halt? active assets? threshold?
  2. Check weekly budget caps  — overall + per-team
  3. detect_signals            — pure Python, zero tokens
  4. For each signal:
     a. CEO HIRING step      (schema: HiringPlan)
     b. Hire specialists     (schemas: ResearchVerdict, RiskReport)
     c. CEO DECIDING step    (schema: ManagerDecision)
     d. *** KEVIN REVIEW ***  (schema: KevinReview)  ← new in 2.1
         - pass:     proceed
         - flag_*:   proceed, board alert
         - block:    halt, pending_approval created, skip execution
     e. If approved, hire Execution (schema: ExecutionResult)
     f. Reflection writes a lesson (schema: ReflectionNote)
     g. Set cooldown on the asset
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import httpx
from crewai import Crew, Task, Process

from fund.agents import (
    build_ceo,
    build_execution_agent,
    build_kevin,
    build_reflection_agent,
    build_research_analyst,
    build_risk_manager,
)
from fund.config   import settings
from fund.database import (
    add_board_alert,
    add_kevin_flag,
    add_lesson,
    add_pending_approval,
    add_principal_message,
    get_portfolio,
    init_db,
    is_on_cooldown,
    log_cost,
    log_decision,
    read_control,
    recent_lessons,
    set_cooldown,
    set_halted,
    spend_breakdown_last_week,
    touch_agent,
    weekly_spend,
)
from fund.market_data import pct_change
from fund.schemas import (
    ExecutionResult,
    HiringPlan,
    KevinReview,
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
            pct = pct_change(symbol, lookback_bars=60)
            if abs(pct) >= ctrl["momentum_threshold"]:
                # Get current price for context
                from fund.market_data import get_quote
                try:
                    q = get_quote(symbol)
                    current = (q["ap"] + q["bp"]) / 2.0
                except Exception:
                    current = 0.0

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
# Phase A — CEO decides who to hire
# ══════════════════════════════════════════════════════════════════════════════

def run_hiring_step(signal: dict) -> HiringPlan | None:
    """CEO's first reasoning step: given this signal, who do we need?"""
    ok, why = _budget_ok("manager")  # budget bucket still "manager" for backcompat
    if not ok:
        log.warning("hiring skipped — %s", why)
        return None

    sym  = signal["symbol"]
    pct  = signal["pct_change"]
    ceo = build_ceo()
    touch_agent("ceo")
    lessons = recent_lessons(symbol=sym, limit=3)
    lesson_text = "\n".join(f"  • {l['outcome']}: {l['note']}" for l in lessons) or "  (none yet)"

    task = Task(
        description=f"""
You are the CEO. You've received a price-momentum signal: {sym} moved {pct:+.1%}.

Recent lessons learned for this asset:
{lesson_text}

Decide which specialists to hire. Call get_portfolio_state to see current exposure.

Rules of thumb:
  • Research — almost always YES (evidence before action)
  • Risk — YES if the portfolio already holds {sym}, or if overall portfolio
    concentration is high
  • Sentiment — only if a news/earnings event seems relevant (default NO)

Remember: Kevin (your Auditor) will review your final decision. Write reasoning he can follow.

Keep `reason` under 3 sentences — dense and specific, not a memo.

Return a HiringPlan.
""",
        expected_output="A HiringPlan object.",
        agent=ceo,
        output_pydantic=HiringPlan,
    )

    _, plan, cost = _run_crew_tracked("manager", [ceo], [task], task_ref=f"{sym}:hire")
    log.info("  ceo.hire  research=%s risk=%s sentiment=%s  ($%.4f)",
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
    ceo = build_ceo()
    touch_agent("ceo")

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
You are the CEO. You now have the specialist reports for {sym}:

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

Kevin will audit your decision. Write a reason that explains trade-offs clearly.

Return a ManagerDecision.
""",
        expected_output="A ManagerDecision.",
        agent=ceo,
        output_pydantic=ManagerDecision,
    )
    _, decision, cost = _run_crew_tracked("manager", [ceo], [task], task_ref=f"{sym}:decide")
    log.info("  ceo.decide %s %s $%.0f  ($%.4f)",
             "TRADE" if decision and decision.trade else "SKIP",
             decision.direction if decision else "?",
             decision.size_usd if decision else 0.0,
             cost)
    return decision


# ══════════════════════════════════════════════════════════════════════════════
# Phase C2 — Kevin reviews the CEO's decision (new in 2.1)
# ══════════════════════════════════════════════════════════════════════════════

def run_kevin_review(signal: dict, decision: ManagerDecision,
                     research: ResearchVerdict | None,
                     risk: RiskReport | None,
                     ctrl: dict) -> KevinReview | None:
    """
    Kevin audits the CEO's decision before it reaches Execution.
    Returns a KevinReview. Calling code acts on .action:
      pass       → no-op
      flag_*     → log flag, board alert, proceed
      block      → create pending_approval, board alert, skip execution
    """
    ok, why = _budget_ok("risk")  # budget Kevin under the "risk" bucket
    if not ok:
        log.warning("kevin review skipped — %s; defaulting to pass", why)
        return KevinReview(action="pass", reason="audit skipped — budget cap")

    sym = signal["symbol"]
    kevin = build_kevin()
    touch_agent("kevin")

    # Build decision summary for Kevin
    research_txt = (
        f"Research: {research.verdict} conf {research.confidence:.2f} — {research.reason}"
        if research else "Research: not hired"
    )
    risk_txt = (
        f"Risk: {risk.assessment} rec ${risk.recommended_size_usd:.0f} — {risk.reason}"
        if risk else "Risk: not hired"
    )

    # Portfolio context
    pos = get_portfolio()
    pos_count = len(pos)

    task = Task(
        description=f"""
You are Kevin, the Auditor. Review the CEO's decision for {sym}.

CEO's inputs:
  • Signal: {signal['pct_change']:+.1%} move on {sym}
  • {research_txt}
  • {risk_txt}
  • Portfolio: {pos_count} existing positions
  • Confidence threshold in force: {ctrl['confidence_threshold']:.2f}
  • Max position cap: ${ctrl['max_position_usd']:.0f}

CEO's decision:
  • Trade: {decision.trade}
  • Direction: {decision.direction}
  • Size: ${decision.size_usd:.0f}
  • Reason: {decision.reason}

Review this decision. Pick ONE action:

  • pass — reasoning holds, portfolio impact fine, mandate respected
  • flag_yellow — minor concern worth noting (trade proceeds)
  • flag_red — serious concern (trade proceeds, Board is alerted)
  • block — this trade should not happen (Board approval required to proceed)

Be specific about the concern. Vague flags help no one.
If you see a recurring pattern (e.g. overtrading a given asset), set concern_pattern.

Return a KevinReview schema.
""",
        expected_output="A KevinReview schema.",
        agent=kevin,
        output_pydantic=KevinReview,
    )

    _, review, cost = _run_crew_tracked("risk", [kevin], [task], task_ref=f"{sym}:kevin")

    if review:
        log.info("  kevin.review %s  ($%.4f)", review.action.upper(), cost)
    return review


def _handle_kevin_review(signal: dict, decision: ManagerDecision,
                        review: KevinReview, decision_id_placeholder: int) -> bool:
    """
    Apply Kevin's review. Returns True if execution should proceed.
    `decision_id_placeholder` is filled later by log_decision; we reference
    it once known via flags/pending rows.
    """
    sym = signal["symbol"]

    if review.action == "pass":
        return True

    if review.action == "flag_yellow":
        add_kevin_flag(decision_id_placeholder, "yellow", review.reason, review.concern_pattern)
        add_principal_message(
            "kevin",
            f"Yellow flag on {sym}: {review.reason}",
            kind="flag",
            ref_id=decision_id_placeholder,
        )
        return True

    if review.action == "flag_red":
        add_kevin_flag(decision_id_placeholder, "red", review.reason, review.concern_pattern)
        add_principal_message(
            "kevin",
            f"RED FLAG on {sym}: {review.reason}",
            kind="flag",
            ref_id=decision_id_placeholder,
        )
        add_board_alert(
            priority="high",
            subject=f"Kevin red-flagged trade on {sym}",
            body=review.reason + (f"\nPattern: {review.concern_pattern}" if review.concern_pattern else ""),
            source="kevin",
            ref_id=decision_id_placeholder,
        )
        return True

    # block
    add_kevin_flag(decision_id_placeholder, "red", review.reason, review.concern_pattern)
    add_pending_approval(
        decision_id=decision_id_placeholder,
        symbol=sym,
        direction=decision.direction,
        size_usd=decision.size_usd,
        ceo_reason=decision.reason,
        kevin_reason=review.reason,
    )
    add_principal_message(
        "kevin",
        f"BLOCKED trade on {sym}. CEO wanted {decision.direction} ${decision.size_usd:.0f}. "
        f"Reason: {review.reason}",
        kind="flag",
        ref_id=decision_id_placeholder,
    )
    add_board_alert(
        priority="critical",
        subject=f"Kevin BLOCKED trade on {sym} — approval needed",
        body=(
            f"CEO proposed: {decision.direction} ${decision.size_usd:.0f}\n"
            f"CEO reason:   {decision.reason}\n"
            f"Kevin reason: {review.reason}"
        ),
        source="kevin",
        ref_id=decision_id_placeholder,
    )
    log.warning("  kevin BLOCKED %s — awaiting Board approval", sym)
    return False


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

        # A. CEO hiring plan
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

        # C. CEO decision
        decision = run_decision_step(sig, ctrl, research, risk)
        if not decision:
            log.info("└─ skip (decision step failed)")
            set_cooldown(sym, ctrl["cooldown_minutes"])
            continue

        # Enforce research confidence rule defensively
        if research and research.confidence < ctrl["confidence_threshold"]:
            decision.trade = False
            decision.reason = f"confidence {research.confidence:.2f} below threshold"

        # Log decision now so Kevin's flags have a valid decision_id to attach to
        did = log_decision(
            symbol           = sym,
            pct_change       = sig["pct_change"],
            specialists_hired= ",".join(specialists_hired),
            research_verdict = research.verdict if research else None,
            confidence       = research.confidence if research else None,
            trade_taken      = decision.trade,       # may be flipped by Kevin block below
            direction        = decision.direction,
            size_usd         = decision.size_usd,
            reason           = decision.reason,
            fill_price       = None,                  # updated after execution
        )

        # C2. Kevin's audit — only runs if CEO actually proposes a trade
        proceed_to_exec = True
        if decision.trade and decision.direction in ("BUY", "SELL"):
            review = run_kevin_review(sig, decision, research, risk, ctrl)
            if review:
                proceed_to_exec = _handle_kevin_review(sig, decision, review, did)
                if not proceed_to_exec:
                    # Kevin blocked — reflect in the log
                    log.info("  trade halted pending Board approval")

        # D. Execution (only if Kevin didn't block)
        fill = None
        if proceed_to_exec and decision.trade and decision.direction in ("BUY", "SELL"):
            fill = run_execution(sig, decision)
            # Update the decision row with fill info (lightweight patch)
            if fill and fill.fill_price:
                try:
                    from fund.database import get_connection
                    with get_connection() as conn:
                        conn.execute(
                            "UPDATE manager_decisions SET fill_price=? WHERE id=?",
                            (fill.fill_price, did),
                        )
                        conn.commit()
                except Exception:
                    log.exception("failed to patch fill_price")

        # If Kevin blocked, force trade_taken=0 in the decision record
        if not proceed_to_exec and decision.trade:
            try:
                from fund.database import get_connection
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE manager_decisions SET trade_taken=0 WHERE id=?", (did,),
                    )
                    conn.commit()
            except Exception:
                log.exception("failed to mark blocked decision")

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
        "Investment Fund — Phase 2.1\n"
        "  CEO model       : %s\n"
        "  Kevin model     : %s\n"
        "  Specialist model: %s\n"
        "  Weekly cap total: $%.2f\n"
        "  Assets          : %s\n"
        "  Threshold       : %.1f%%\n"
        "  Confidence      : %.0f%%",
        settings.ceo_model,
        settings.kevin_model,
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
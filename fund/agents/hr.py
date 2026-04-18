"""HR Agent — weekly cadence only.

Runs once per week (default Monday 09:00 UTC). Reads the past 7 days of:
  - CEO hiring decisions (from org_state transitions)
  - Specialist agent costs
  - Decision throughput (manager_decisions count)
  - Trade outcomes (fills vs. rejects)

Produces an org recommendation posted to principals_chat + Board chat.
HR is advisory — does NOT block, hire, or fire. Board acts on recs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from crewai import Agent, Task, Crew, LLM

from fund.config import settings
from fund.database import (conn, current_month, get_budget_status, get_model,
                           now_iso, post_chat, post_to_both, save_hr_review)


def _build_llm() -> LLM:
    return LLM(model=get_model("hr"), api_key=settings.ANTHROPIC_API_KEY)


def build_hr_agent() -> Agent:
    return Agent(
        role="Head of HR",
        goal=(
            "Review CEO hiring decisions and specialist cost efficiency over "
            "the past week. Recommend org structure adjustments to the Board. "
            "You do NOT hire, fire, or block — you advise only."
        ),
        backstory=(
            "You are the weekly auditor of org health. You watch for: "
            "(1) specialists hired but never used, "
            "(2) cost-per-decision creeping up, "
            "(3) repeated hires of the same role suggesting a permanent need, "
            "(4) under-utilisation of budget pools. "
            "Your output goes to the Board — be concise, evidence-driven, "
            "and recommend at most 3 actions."
        ),
        llm=_build_llm(),
        verbose=False,
        allow_delegation=False,
    )


def _gather_week_data() -> dict:
    """Pull the last 7 days of ops data for HR to analyse."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(
        timespec="seconds"
    )
    with conn() as c:
        decisions = c.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN trade_taken=1 THEN 1 ELSE 0 END) AS trades
               FROM manager_decisions WHERE created_at >= ?""",
            (week_ago,),
        ).fetchone()

        specialist_spend = c.execute(
            """SELECT agent_role, COUNT(*) AS invocations,
                      SUM(cost_usd) AS cost
               FROM agent_costs
               WHERE ts >= ?
                 AND agent_role NOT IN ('ceo','kevin','hr')
               GROUP BY agent_role""",
            (week_ago,),
        ).fetchall()

        principals_spend = c.execute(
            """SELECT agent_role, SUM(cost_usd) AS cost
               FROM agent_costs WHERE ts >= ?
                 AND agent_role IN ('ceo','kevin','hr')
               GROUP BY agent_role""",
            (week_ago,),
        ).fetchall()

    summary = {
        "decisions": decisions["n"] or 0,
        "trades_executed": decisions["trades"] or 0,
        "specialists": {r["agent_role"]: {"invocations": r["invocations"],
                                          "cost_usd": round(r["cost"], 4)}
                        for r in specialist_spend},
        "principals": {r["agent_role"]: round(r["cost"], 4)
                       for r in principals_spend},
        "budget_pools": get_budget_status(),
    }
    return summary


def run_hr_review() -> dict:
    """Execute one weekly HR review cycle and publish to chat + Board."""
    data = _gather_week_data()
    agent = build_hr_agent()

    task = Task(
        description=(
            "Review the past week of org activity and provide recommendations.\n\n"
            f"WEEK DATA (JSON):\n{json.dumps(data, indent=2)}\n\n"
            "Output exactly this format:\n"
            "EFFICIENCY: <cost_per_decision_usd>\n"
            "RECOMMENDATIONS:\n"
            "1. <action>\n"
            "2. <action>\n"
            "3. <action>\n"
            "RATIONALE: <1-2 sentences>"
        ),
        expected_output="Structured review with 1-3 recommendations.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=False)
    result = str(crew.kickoff())

    efficiency = _parse_efficiency(result, data)
    recs = _parse_recs(result)

    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    rev_id = save_hr_review(
        week_start=week_start,
        summary={"specialists": data["specialists"],
                 "principals": data["principals"],
                 "decisions": data["decisions"]},
        count=data["decisions"],
        efficiency=efficiency,
        recs=recs,
    )

    # Publish: principals see the review internally; Board sees it too
    msg = f"[HR weekly review #{rev_id}]\n{recs}"
    post_to_both("hr", msg, thread=f"week:{week_start}")

    return {
        "review_id": rev_id,
        "week_start": week_start,
        "efficiency_usd_per_decision": efficiency,
        "recommendations": recs,
        "raw": result,
    }


def _parse_efficiency(text: str, data: dict) -> float:
    """Extract EFFICIENCY line; fallback to computed ratio."""
    for line in text.splitlines():
        if line.strip().upper().startswith("EFFICIENCY:"):
            try:
                return float(line.split(":", 1)[1].strip().replace("$", ""))
            except ValueError:
                break
    # fallback: compute from totals
    total_cost = sum(v["cost_usd"] for v in data["specialists"].values())
    total_cost += sum(data["principals"].values())
    n = max(1, data["decisions"])
    return round(total_cost / n, 4)


def _parse_recs(text: str) -> str:
    """Extract the recommendations block."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("RECOMMENDATIONS"):
            return "\n".join(lines[i:]).strip()
    return text.strip()

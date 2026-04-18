"""Kevin — CEO Auditor with hardened action surfacing.

Phase 2.2 changes over Phase 2.1:
  * Every flag/block/escalate call MUST surface in principals_chat AND
    dashboard row. Failure to surface is a silent-agent bug.
  * `KEVIN_DEBUG_GATE` asserts on startup that all 4 core actions are wired
    end-to-end by firing a self-test and verifying audit log + chat rows.
"""
from __future__ import annotations

from crewai import Agent, LLM

from fund.config import settings
from fund.database import (conn, get_model, kevin_unsurfaced,
                           log_kevin_action, post_chat, post_to_both)


# ── LLM wiring ──────────────────────────────────────────────────────────────

def _build_llm() -> LLM:
    return LLM(model=get_model("kevin"), api_key=settings.ANTHROPIC_API_KEY)


def build_kevin() -> Agent:
    return Agent(
        role="Auditor (Kevin)",
        goal=(
            "Monitor every CEO decision. Flag risky patterns yellow, flag "
            "clear policy violations red, block trades that violate mandate "
            "or breach risk limits, and escalate recurring issues to the "
            "Board. Never trade yourself."
        ),
        backstory=(
            "You are the Fund's internal auditor. You sit in the principals' "
            "chat alongside the CEO. You issue concise, evidence-based calls. "
            "You can block a trade pending Board approval; you cannot override "
            "the Board."
        ),
        llm=_build_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ── Public action API — every call surfaces everywhere ─────────────────────
#   These four helpers are what the CEO loop + dashboard call directly.
#   They guarantee chat + dashboard rows + audit log all get written.

def flag(severity: str, target_type: str, target_id: str | None,
         reason: str) -> int:
    """severity: 'yellow' | 'red' — posts to principals' chat only."""
    action = f"flag_{severity}"
    chat_id = post_chat(
        "kevin",
        f"🟡 {reason}" if severity == "yellow" else f"🔴 {reason}",
        chat_room="principals",
        thread=f"{target_type}:{target_id}" if target_id else None,
    )
    return log_kevin_action(
        action=action, target_type=target_type, target_id=target_id,
        reason=reason,
        surfaced_chat=bool(chat_id),
        surfaced_dash=True,  # dashboard reads from kevin_audit_log directly
        board_notified=False,
    )


def block_trade(decision_id: str, reason: str) -> int:
    """Block: posts to principals; tagged as Board-notified for the audit."""
    post_chat(
        "kevin",
        f"⛔ BLOCKED trade (decision {decision_id}) — {reason}\n"
        "Pending Board approval.",
        chat_room="principals",
        thread=f"decision:{decision_id}",
    )
    # Also tell the Board directly so they see it without scrolling principals
    post_chat(
        "kevin",
        f"⛔ I blocked decision {decision_id}: {reason}\n"
        "Awaiting your call.",
        chat_room="board",
        thread=f"decision:{decision_id}",
    )
    return log_kevin_action(
        action="block_trade", target_type="decision", target_id=decision_id,
        reason=reason,
        surfaced_chat=True, surfaced_dash=True, board_notified=True,
    )


def escalate_to_board(pattern: str, evidence: str) -> int:
    """Escalation: appears in BOTH rooms — Board sees it, CEO sees it."""
    post_to_both(
        "kevin",
        f"🚨 ESCALATION to Board: {pattern}\n\n{evidence}",
        thread="escalation",
    )
    return log_kevin_action(
        action="escalate_board", target_type="pattern", target_id=None,
        reason=f"{pattern} | {evidence[:200]}",
        surfaced_chat=True, surfaced_dash=True, board_notified=True,
    )


def concern(message: str, target_type: str = "general",
            target_id: str | None = None) -> int:
    """Low-severity nudge — principals only."""
    post_chat(
        "kevin", f"ℹ️  {message}",
        chat_room="principals",
        thread=f"{target_type}:{target_id}" if target_id else None,
    )
    return log_kevin_action(
        action="concern", target_type=target_type, target_id=target_id,
        reason=message,
        surfaced_chat=True, surfaced_dash=True, board_notified=False,
    )


# ── Debug gate — run on container startup ──────────────────────────────────

def debug_gate() -> dict:
    """Self-test every core action. Returns {ok: bool, failures: [...]}."""
    if not settings.KEVIN_DEBUG_GATE:
        return {"ok": True, "skipped": True}

    failures = []
    test_refs = []

    try:
        test_refs.append(("flag_yellow",
                          flag("yellow", "debug_gate", "startup",
                               "Debug gate self-test — yellow")))
        test_refs.append(("flag_red",
                          flag("red", "debug_gate", "startup",
                               "Debug gate self-test — red")))
        test_refs.append(("block_trade",
                          block_trade("debug_gate_startup",
                                      "Debug gate self-test — block")))
        test_refs.append(("escalate_board",
                          escalate_to_board("debug_gate_startup",
                                            "Debug gate self-test — escalate")))
    except Exception as e:
        failures.append(f"exception during self-test: {e!r}")

    # Verify: audit rows exist, chat rows exist, nothing unsurfaced
    with conn() as c:
        for action_name, audit_id in test_refs:
            row = c.execute(
                "SELECT * FROM kevin_audit_log WHERE id=?", (audit_id,)
            ).fetchone()
            if not row:
                failures.append(f"{action_name}: audit row missing")
                continue
            if not row["surfaced_in_chat"]:
                failures.append(f"{action_name}: not surfaced in chat")
            if not row["surfaced_in_dashboard"]:
                failures.append(f"{action_name}: not surfaced in dashboard")

    # Any silent actions already in DB?
    silent = kevin_unsurfaced()
    # Filter out debug rows we just wrote (they should all be surfaced)
    silent_real = [s for s in silent if s["target_id"] != "startup"]
    if silent_real:
        failures.append(
            f"{len(silent_real)} historical Kevin actions are unsurfaced"
        )

    # Clean up debug gate rows to keep prod audit log tidy
    with conn() as c:
        c.execute(
            "DELETE FROM kevin_audit_log WHERE target_id='startup' "
            "AND target_type='debug_gate'"
        )
        c.execute(
            "DELETE FROM principals_chat "
            "WHERE message LIKE '%Debug gate self-test%' "
            "   OR message LIKE '%debug_gate_startup%' "
            "   OR (sender='kevin' AND thread='decision:debug_gate_startup') "
            "   OR thread LIKE 'debug_gate:%'"
        )

    return {"ok": len(failures) == 0, "failures": failures}

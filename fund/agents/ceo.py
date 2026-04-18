"""CEO chat integration.

Two responsibilities:

1. **Decision broadcasting** — every CEO decision (proposed trade, hire,
   acknowledgement of a Kevin flag) is posted to the principals' chat so
   Kevin and HR see it in real time and Board sees an audit trail.

2. **Board inbox handler** — runs on a schedule (every 30 s by default).
   Reads unread Board messages, drafts a single CEO response per batch
   using the CEO LLM, posts it to the Board room, and marks them read.
"""
from __future__ import annotations

import logging

from crewai import Agent, Crew, LLM, Task

from fund.config import settings
from fund.database import (get_model, mark_board_read, post_chat,
                           recent_chat, unread_board_for_ceo)

log = logging.getLogger(__name__)


# ── LLM + agent ─────────────────────────────────────────────────────────────

def _build_llm() -> LLM:
    return LLM(model=get_model("ceo"), api_key=settings.ANTHROPIC_API_KEY)


def build_ceo_chat_agent() -> Agent:
    """A lightweight CEO persona used ONLY for chat replies.

    The trading-loop CEO (in crew.py) is a separate Agent instance with the
    full toolset. This one is purpose-built for terse, professional replies
    to the Board — no tools, no reasoning chain, just a response.
    """
    return Agent(
        role="CEO (Chat)",
        goal=(
            "Respond to Board questions and directives concisely. "
            "Acknowledge what you'll change. Push back only with evidence."
        ),
        backstory=(
            "You report to the Board. You also coordinate with Kevin "
            "(your auditor) and the HR liaison. Your replies are short — "
            "1-3 sentences — and reference concrete state when relevant."
        ),
        llm=_build_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ── 1. Broadcasting CEO decisions to principals ────────────────────────────

def announce_decision(decision_id: int, symbol: str,
                      direction: str, size_usd: float, reason: str) -> int:
    """CEO announces a proposed trade in the principals' chat."""
    msg = (f"📈 Proposing **{direction} {symbol}** ${size_usd:,.0f} "
           f"(decision #{decision_id})\n→ {reason}")
    return post_chat("ceo", msg, chat_room="principals",
                     thread=f"decision:{decision_id}")


def announce_hold(decision_id: int, symbol: str, reason: str) -> int:
    return post_chat(
        "ceo",
        f"🟦 Holding {symbol} (decision #{decision_id}) — {reason}",
        chat_room="principals", thread=f"decision:{decision_id}",
    )


def announce_hire(role: str, model: str, reason: str) -> int:
    return post_chat(
        "ceo",
        f"🧑‍💼 Hired **{role}** ({model}) — {reason}",
        chat_room="principals", thread=f"hire:{role}",
    )


def announce_dismiss(role: str, reason: str) -> int:
    return post_chat(
        "ceo",
        f"👋 Dismissed **{role}** — {reason}",
        chat_room="principals", thread=f"hire:{role}",
    )


def acknowledge_kevin(target_thread: str, action_taken: str) -> int:
    """CEO acknowledges Kevin's flag/block/concern with what they'll do."""
    return post_chat(
        "ceo", f"@kevin acknowledged — {action_taken}",
        chat_room="principals", thread=target_thread,
    )


# ── 2. Board inbox handler — scheduled job ─────────────────────────────────

def process_board_inbox(force: bool = False) -> dict:
    """Read unread Board messages, draft a CEO reply, post + mark read.

    Returns: {"processed": n, "reply_id": int|None, "raw_reply": str|None}.
    Safe to call every 30 s; no-op when inbox is empty unless force=True.
    """
    unread = unread_board_for_ceo(limit=20)
    if not unread and not force:
        return {"processed": 0, "reply_id": None, "raw_reply": None}

    if not unread:
        return {"processed": 0, "reply_id": None, "raw_reply": None}

    # Build context: last 8 board messages (read + unread for thread continuity)
    context_msgs = list(reversed(recent_chat(limit=8, chat_room="board")))
    convo = "\n".join(
        f"[{m['sender']}] {m['message']}" for m in context_msgs
    )

    unread_block = "\n".join(
        f"- {m['message']}" for m in unread
    )

    agent = build_ceo_chat_agent()
    task = Task(
        description=(
            "The Board has sent you the following NEW messages:\n\n"
            f"{unread_block}\n\n"
            "Recent conversation context (oldest → newest):\n"
            f"{convo}\n\n"
            "Reply in 1-3 sentences. If they asked a question, answer it. "
            "If they gave a directive, acknowledge and state what you'll do. "
            "If they want a status update, summarise concisely. "
            "Do NOT prefix with 'CEO:' — just the message body."
        ),
        expected_output="A short, professional reply to the Board.",
        agent=agent,
    )

    try:
        result = str(Crew(agents=[agent], tasks=[task],
                          verbose=False).kickoff()).strip()
    except Exception as e:
        log.exception("CEO Board reply failed")
        # Post a fallback so the Board never sees their message ignored
        result = (
            "Acknowledged — I hit a runtime error generating a full reply. "
            "I've marked your message as read and will follow up shortly."
        )

    # Strip any accidental "CEO:" prefix
    for prefix in ("CEO:", "ceo:", "CEO -", "CEO —"):
        if result.startswith(prefix):
            result = result[len(prefix):].strip()

    reply_id = post_chat("ceo", result, chat_room="board",
                         thread="board-reply")
    mark_board_read([m["id"] for m in unread])

    return {
        "processed": len(unread),
        "reply_id": reply_id,
        "raw_reply": result,
    }

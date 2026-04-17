"""
Phase 2 Board dashboard — FastAPI backend.
Serves single-page HTML + JSON APIs.
Reads from the shared SQLite DB; talks to the fund's control API to halt/resume.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Shared DB layer from the fund package
from fund.database import (
    active_directives,
    add_directive,
    add_message,
    decide_pending,
    flags_for_decision,
    get_cash,
    get_connection,
    get_pending,
    get_portfolio,
    get_roster,
    mark_alert_read,
    operator_threads,
    pending_approvals,
    read_control,
    recent_flags,
    recent_messages,
    recent_principal_messages,
    recent_snapshots,
    set_agent_model,
    spend_breakdown_last_week,
    unread_board_alerts,
    update_control,
    weekly_spend,
)

log = logging.getLogger("dashboard")
app = FastAPI(title="Investment Fund — Board Dashboard")

STATIC_DIR = Path(__file__).parent / "static"
FUND_CONTROL_URL = os.getenv("FUND_CONTROL_URL", "http://fund:8002")


# ── UI ───────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Overview ──────────────────────────────────────────────────────────────────

@app.get("/api/overview")
def overview():
    positions = get_portfolio()
    cash      = get_cash()
    ctrl      = read_control()
    spend     = weekly_spend()

    # Compute simple total using avg_cost (real market value requires live quotes)
    pos_cost = sum(p["quantity"] * p["avg_cost"] for p in positions)

    # Try last snapshot for latest total_equity; fall back to book value
    snaps = recent_snapshots(limit=1)
    total_equity = snaps[0]["total_equity"] if snaps else (cash + pos_cost)

    return {
        "cash":          round(cash, 2),
        "positions_cost": round(pos_cost, 2),
        "total_equity":  round(total_equity, 2),
        "n_positions":   len(positions),
        "top_positions": sorted(
            [{"symbol": p["symbol"],
              "value":  round(p["quantity"] * p["avg_cost"], 2)}
             for p in positions],
            key=lambda x: -x["value"],
        )[:8],
        "halted":        bool(ctrl["halted"]),
        "halt_reason":   ctrl.get("halt_reason") or "",
        "weekly_spend":  round(spend, 4),
        "weekly_cap":    1.0,
    }


# ── Operators chat (projection of agent decisions) ───────────────────────────

@app.get("/api/threads")
def threads(limit: int = 20):
    return {"threads": operator_threads(limit=limit)}


# ── Manager chat (Board ↔ Manager) ───────────────────────────────────────────

class ChatIn(BaseModel):
    body: str


@app.get("/api/messages")
def messages(limit: int = 50):
    msgs = recent_messages(limit=limit)
    directives = active_directives()
    return {"messages": msgs, "directives": directives}


@app.post("/api/messages")
def post_message(msg: ChatIn):
    if not msg.body.strip():
        raise HTTPException(400, "empty")
    # Store as a Board message. The Manager's reply is generated in the
    # trading loop (Phase 2.1) or by a separate reply worker. For now we
    # acknowledge synchronously so the UI feels responsive.
    mid = add_message("board", msg.body.strip(), kind="chat")

    # Heuristic: if the message looks like a directive ("always", "don't",
    # "always", "never", "keep", "trim", "focus on"), also save it as a directive.
    lower = msg.body.lower()
    directive_cues = ("always ", "never ", "don't ", "do not ", "keep ",
                      "trim ", "focus on ", "prioritise ", "prioritize ",
                      "deprioritise ", "deprioritize ", "reduce ", "increase ")
    if any(cue in lower for cue in directive_cues):
        add_directive(msg.body.strip())

    # System ack
    add_message("system",
                "Received. Manager will see this on the next cycle.",
                kind="chat")
    return {"id": mid}


# ── Controls ─────────────────────────────────────────────────────────────────

class ControlPatch(BaseModel):
    assets_str:           str | None   = None
    momentum_threshold:   float | None = None
    confidence_threshold: float | None = None
    max_position_usd:     float | None = None
    check_interval_sec:   int | None   = None
    cooldown_minutes:     int | None   = None


@app.patch("/api/control")
def patch_control(patch: ControlPatch):
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields")
    update_control(**fields)
    return {"current": read_control()}


@app.get("/api/control-state")
def control_state():
    return read_control()


@app.post("/api/stop")
def stop(reason: str = "dashboard"):
    try:
        r = httpx.post(f"{FUND_CONTROL_URL}/stop",
                       params={"reason": reason}, timeout=4.0)
        return r.json()
    except Exception as e:
        raise HTTPException(502, f"control API unreachable: {e}")


@app.post("/api/resume")
def resume():
    try:
        r = httpx.post(f"{FUND_CONTROL_URL}/resume", timeout=4.0)
        return r.json()
    except Exception as e:
        raise HTTPException(502, f"control API unreachable: {e}")


# ── Spend ────────────────────────────────────────────────────────────────────

@app.get("/api/spend")
def spend():
    try:
        r = httpx.get(f"{FUND_CONTROL_URL}/spend", timeout=4.0)
        return r.json()
    except Exception:
        # Fallback to local DB read if control API unreachable
        breakdown = spend_breakdown_last_week()
        total = sum(breakdown.values())
        return {"total": round(total, 4), "by_agent": breakdown,
                "cap_total": 1.0, "caps": {}}


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2.1: Governance endpoints
# ═══════════════════════════════════════════════════════════════════════════

# ── Agent roster (live/idle view) ────────────────────────────────────────────

@app.get("/api/roster")
def roster():
    from datetime import datetime
    rows = get_roster(active_only=True)
    now = datetime.utcnow()
    for r in rows:
        la = r.get("last_active")
        if la:
            try:
                delta = (now - datetime.fromisoformat(la)).total_seconds()
                r["seconds_since_active"] = int(delta)
                r["live"] = delta < 120    # active within last 2 min
            except Exception:
                r["seconds_since_active"] = None
                r["live"] = False
        else:
            r["seconds_since_active"] = None
            r["live"] = False
    return {"roster": rows}


class AgentModelUpdate(BaseModel):
    agent_name: str
    model:      str


@app.patch("/api/roster/model")
def update_agent_model(update: AgentModelUpdate):
    set_agent_model(update.agent_name, update.model)
    return {"ok": True, "agent_name": update.agent_name, "model": update.model}


# ── Principals' chat room (CEO ↔ Kevin) ──────────────────────────────────────

@app.get("/api/principals-chat")
def principals_chat(limit: int = 50):
    return {"messages": recent_principal_messages(limit=limit)}


# ── Kevin flags ──────────────────────────────────────────────────────────────

@app.get("/api/kevin-flags")
def kevin_flags(limit: int = 20):
    return {"flags": recent_flags(limit=limit)}


# ── Pending approvals (Kevin blocks → Board decides) ─────────────────────────

@app.get("/api/pending-approvals")
def list_pending_approvals():
    return {"pending": pending_approvals(status="pending")}


class ApprovalDecision(BaseModel):
    decision: str  # "approved" or "rejected"


@app.post("/api/pending-approvals/{approval_id}")
def decide_approval(approval_id: int, body: ApprovalDecision):
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")
    row = get_pending(approval_id)
    if not row:
        raise HTTPException(404, "pending approval not found")
    if row["status"] != "pending":
        raise HTTPException(409, f"already {row['status']}")

    decide_pending(approval_id, body.decision, decided_by="board")

    # If approved, the Board is saying "execute despite Kevin's block".
    # We'll record the Board override in the principals' chat for audit.
    # Actual execution is deferred — Phase 2.1 implementation: we log the
    # override and leave re-execution for a future cycle (manual re-trigger).
    from fund.database import add_principal_message, add_board_alert
    add_principal_message(
        "board",
        f"Board {body.decision.upper()} Kevin's block on {row['symbol']} "
        f"({row['direction']} ${row['size_usd']:.0f}).",
        kind="chat",
        ref_id=approval_id,
    )
    return {"ok": True, "decision": body.decision, "approval": get_pending(approval_id)}


# ── Board alerts (inbox) ─────────────────────────────────────────────────────

@app.get("/api/alerts")
def alerts():
    return {"alerts": unread_board_alerts()}


@app.post("/api/alerts/{alert_id}/read")
def read_alert(alert_id: int):
    mark_alert_read(alert_id)
    return {"ok": True}


# ── Enhanced decisions endpoint that includes Kevin flags ───────────────────

@app.get("/api/decisions")
def decisions(limit: int = 20):
    """Like /api/threads but enriched with Kevin flags for each decision."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM manager_decisions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["kevin_flags"] = flags_for_decision(d["id"])
        out.append(d)
    return {"decisions": out}


# ── Dev runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    uvicorn.run("dashboard.main:app",
                host="0.0.0.0", port=8080, reload=False)

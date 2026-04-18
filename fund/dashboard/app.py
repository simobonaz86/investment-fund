"""FastAPI dashboard for Phase 2.2.

Routes:
  GET  /                       → main UI (HTML)
  GET  /api/health             → liveness
  GET  /api/org                → current roster + principals status
  GET  /api/budget             → current-month budget pools
  GET  /api/models             → model selections per role
  POST /api/models             → Board sets model for CEO/Kevin/HR;
                                 CEO sets for specialist_*
  GET  /api/chat?room=...      → chat messages, filter by 'principals' or 'board'
  POST /api/chat               → Board posts a message into the board room
  POST /api/chat/reply-now     → force-trigger CEO Board-inbox processing
  GET  /api/kevin-audit        → Kevin action log + any unsurfaced warnings
  GET  /api/reports            → last 20 reports (metadata)
  GET  /api/reports/{id}       → full markdown of a report
  POST /api/reports/run        → {"kind": "daily"} trigger ad-hoc report
  POST /api/hr/run             → trigger ad-hoc HR review
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from fund.agents.ceo import process_board_inbox
from fund.agents.hr import run_hr_review
from fund.agents.kevin import debug_gate
from fund.config import settings
from fund.database import (conn, current_org, get_budget_status, init_db,
                           kevin_unsurfaced, list_reports, post_chat,
                           recent_chat, set_model)
from fund.reports.generator import generate_report

log = logging.getLogger(__name__)

app = FastAPI(title="Investment Fund — Board Dashboard", version="2.2.0")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# ── Models ──────────────────────────────────────────────────────────────────

class ModelSelection(BaseModel):
    role: str
    model: str
    selected_by: str   # 'board' or 'ceo'


class ReportRun(BaseModel):
    kind: str   # daily | weekly | monthly | quarterly | ytd


class BoardMessage(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


# ── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def _startup() -> None:
    init_db()
    gate = debug_gate()
    if not gate["ok"]:
        log.error("Kevin debug gate FAILED: %s", gate["failures"])
    else:
        log.info("Kevin debug gate OK")


# ── HTML ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ── API ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.2.0"}


@app.get("/api/org")
def org():
    roster = current_org()
    return {"roster": roster, "principals": ["ceo", "kevin", "hr"]}


@app.get("/api/budget")
def budget():
    return get_budget_status() or {"error": "no pool for current month"}


@app.get("/api/models")
def models():
    with conn() as c:
        rows = c.execute(
            "SELECT role, model, selected_by, updated_at FROM model_selections"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/models")
def set_model_api(body: ModelSelection):
    try:
        set_model(body.role, body.model, body.selected_by)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True}


@app.get("/api/chat")
def chat(limit: int = 50,
         room: str | None = Query(None, pattern="^(principals|board)$")):
    """Recent chat messages. `room` filters by 'principals' or 'board'."""
    return recent_chat(limit=limit, chat_room=room)


@app.post("/api/chat")
def board_post(body: BoardMessage):
    """Board sends a message into the board room. CEO will pick it up
    on the next inbox poll (max 30 s) — or hit /api/chat/reply-now."""
    msg_id = post_chat("board", body.message.strip(),
                       chat_room="board", thread="board-directive")
    return {"ok": True, "id": msg_id}


@app.post("/api/chat/reply-now")
def board_reply_now():
    """Force-trigger CEO Board-inbox processing. Useful after posting
    a message when you don't want to wait for the 30-second poll."""
    return process_board_inbox(force=False)


@app.get("/api/kevin-audit")
def kevin_audit(limit: int = 100):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM kevin_audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    log_rows = [dict(r) for r in rows]
    unsurf = kevin_unsurfaced()
    return {"log": log_rows, "unsurfaced_warning_count": len(unsurf),
            "unsurfaced": unsurf}


@app.get("/api/reports")
def reports():
    items = list_reports(limit=20)
    # strip markdown from list response for speed
    for item in items:
        item.pop("content_markdown", None)
    return items


@app.get("/api/reports/{report_id}")
def report_detail(report_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM reports WHERE id=?",
                        (report_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="report not found")
    return dict(row)


@app.post("/api/reports/run")
def run_report(body: ReportRun):
    if body.kind not in {"daily", "weekly", "monthly", "quarterly", "ytd"}:
        raise HTTPException(status_code=400, detail="invalid kind")
    return generate_report(body.kind)


@app.post("/api/hr/run")
def run_hr():
    return run_hr_review()

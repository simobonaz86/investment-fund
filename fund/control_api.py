"""
Control API server.
Runs alongside the trading loop in the same container.
Lets you halt, resume, and inspect state via HTTP — no container restart needed.

Endpoints:
  GET  /status                      — current control state + weekly spend
  POST /stop?reason=...             — halt the loop
  POST /resume                      — clear halt
  GET  /spend                       — per-agent and total weekly spend
  GET  /decisions?limit=20          — recent manager decisions
  PATCH /control                    — update threshold/assets/max_position_usd
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from fund.config   import settings
from fund.database import (
    get_connection,
    read_control,
    set_halted,
    spend_breakdown_last_week,
    update_control,
    weekly_spend,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Control API listening on :%d", settings.control_port)
    yield


app = FastAPI(title="Investment Fund — Control API", lifespan=lifespan)


# ── Models ───────────────────────────────────────────────────────────────────

class ControlUpdate(BaseModel):
    assets_str:           Optional[str]   = None
    momentum_threshold:   Optional[float] = None
    confidence_threshold: Optional[float] = None
    max_position_usd:     Optional[float] = None
    check_interval_sec:   Optional[int]   = None
    cooldown_minutes:     Optional[int]   = None


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/status")
def status():
    ctrl  = read_control()
    total = weekly_spend()
    return {
        "control":      ctrl,
        "weekly_spend": round(total, 4),
        "weekly_cap":   settings.weekly_budget_total_usd,
        "pct_of_cap":   round(total / max(settings.weekly_budget_total_usd, 1e-9) * 100, 1),
    }


@app.post("/stop")
def stop(reason: str = "manual stop"):
    set_halted(True, reason=reason)
    log.warning("HALTED by control API: %s", reason)
    return {"halted": True, "reason": reason}


@app.post("/resume")
def resume():
    set_halted(False, reason="")
    log.info("RESUMED by control API")
    return {"halted": False}


@app.get("/spend")
def spend():
    breakdown = spend_breakdown_last_week()
    total     = sum(breakdown.values())
    caps = {
        "research":   settings.weekly_budget_research_usd,
        "risk":       settings.weekly_budget_risk_usd,
        "sentiment":  settings.weekly_budget_sentiment_usd,
        "execution":  settings.weekly_budget_execution_usd,
        "accountant": settings.weekly_budget_accountant_usd,
        "reflection": settings.weekly_budget_reflection_usd,
    }
    return {
        "total":     round(total, 4),
        "cap_total": settings.weekly_budget_total_usd,
        "by_agent":  {k: round(v, 4) for k, v in breakdown.items()},
        "caps":      caps,
    }


@app.get("/decisions")
def decisions(limit: int = 20):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM manager_decisions ORDER BY id DESC LIMIT ?",
            (min(limit, 100),),
        ).fetchall()
        return {"decisions": [dict(r) for r in rows]}


@app.patch("/control")
def patch_control(update: ControlUpdate):
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields provided")
    try:
        update_control(**fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"updated": fields, "current": read_control()}


@app.get("/health")
def health():
    return {"status": "ok"}

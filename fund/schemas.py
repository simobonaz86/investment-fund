"""
Pydantic schemas for structured agent output.
Length limits are generous — Sonnet-class models (especially with rich
CEO/Kevin system prompts) write longer justifications than Haiku.
We'd rather read the full reason than get a validation error.
"""
from typing import Literal
from pydantic import BaseModel, Field


class ResearchVerdict(BaseModel):
    verdict:    Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason:     str   = Field(max_length=800)


class RiskReport(BaseModel):
    assessment:           Literal["clear", "caution", "block"]
    recommended_size_usd: float = Field(ge=0.0)
    reason:               str   = Field(max_length=800)


class HiringPlan(BaseModel):
    """What the Manager decides at the start of a cycle: who to hire."""
    hire_research:  bool
    hire_risk:      bool
    hire_sentiment: bool = False
    reason:         str  = Field(max_length=800)


class ManagerDecision(BaseModel):
    """Final trade decision after all specialists have reported."""
    trade:     bool
    direction: Literal["BUY", "SELL", "N/A"]
    size_usd:  float = Field(ge=0.0)
    reason:    str   = Field(max_length=1000)


class ExecutionResult(BaseModel):
    status:     Literal["filled", "failed", "rejected"]
    fill_price: float = Field(ge=0.0)
    quantity:   float = Field(ge=0.0)
    total_usd:  float = Field(ge=0.0)


class ReflectionNote(BaseModel):
    outcome: Literal["win", "loss", "breakeven", "pending"]
    note:    str = Field(max_length=800)


# ── Phase 2.1: Governance schemas ─────────────────────────────────────────────

class CEOHiringPlan(BaseModel):
    """CEO's hiring decision — who to bring in and on what model tier."""
    hire_research:  bool
    hire_risk:      bool
    hire_sentiment: bool = False
    research_tier:  Literal["haiku", "sonnet", "opus"] = "haiku"
    risk_tier:      Literal["haiku", "sonnet", "opus"] = "haiku"
    sentiment_tier: Literal["haiku", "sonnet", "opus"] = "haiku"
    reason:         str = Field(max_length=800)


class KevinReview(BaseModel):
    """
    Auditor Kevin's review of a CEO decision before execution.

    Actions:
      • pass        — no concern, trade proceeds
      • flag_yellow — info-level concern, trade proceeds, Board informed
      • flag_red    — serious concern, trade proceeds, Board alerted for review
      • block       — trade halts until Board approves via dashboard
    """
    action:  Literal["pass", "flag_yellow", "flag_red", "block"]
    reason:  str = Field(max_length=800)
    concern_pattern: str | None = Field(default=None, max_length=300)


class KevinWeeklyAudit(BaseModel):
    """Kevin's weekly audit posted to the Principals' room."""
    grade:      Literal["A", "B", "C", "D", "F"]
    wins:       list[str] = Field(default_factory=list, max_length=5)
    concerns:   list[str] = Field(default_factory=list, max_length=5)
    pattern_flags:     list[str] = Field(default_factory=list, max_length=5)
    recommendation:    str = Field(max_length=1000)
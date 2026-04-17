"""
Pydantic schemas for structured agent output.
Length limits are generous — Sonnet writes richer justifications than Haiku,
and we'd rather read the full reason than get a validation error.
"""
from typing import Literal
from pydantic import BaseModel, Field


class ResearchVerdict(BaseModel):
    verdict:    Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason:     str   = Field(max_length=500)


class RiskReport(BaseModel):
    assessment:           Literal["clear", "caution", "block"]
    recommended_size_usd: float = Field(ge=0.0)
    reason:               str   = Field(max_length=500)


class HiringPlan(BaseModel):
    """What the Manager decides at the start of a cycle: who to hire."""
    hire_research:  bool
    hire_risk:      bool
    hire_sentiment: bool = False
    reason:         str  = Field(max_length=500)


class ManagerDecision(BaseModel):
    """Final trade decision after all specialists have reported."""
    trade:     bool
    direction: Literal["BUY", "SELL", "N/A"]
    size_usd:  float = Field(ge=0.0)
    reason:    str   = Field(max_length=600)


class ExecutionResult(BaseModel):
    status:     Literal["filled", "failed", "rejected"]
    fill_price: float = Field(ge=0.0)
    quantity:   float = Field(ge=0.0)
    total_usd:  float = Field(ge=0.0)


class ReflectionNote(BaseModel):
    outcome: Literal["win", "loss", "breakeven", "pending"]
    note:    str = Field(max_length=500)

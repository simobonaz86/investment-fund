"""
Pydantic schemas for structured agent output.
CrewAI Task accepts `output_pydantic=<Model>` which forces the LLM's
final answer to validate against the schema.  No more regex parsing.
"""
from typing import Literal, List
from pydantic import BaseModel, Field


class ResearchVerdict(BaseModel):
    verdict:    Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason:     str   = Field(max_length=160)


class RiskReport(BaseModel):
    assessment:     Literal["clear", "caution", "block"]
    recommended_size_usd: float = Field(ge=0.0)
    reason:         str = Field(max_length=160)


class HiringPlan(BaseModel):
    """What the Manager decides at the start of a cycle: who to hire."""
    hire_research:  bool
    hire_risk:      bool
    hire_sentiment: bool = False
    reason:         str  = Field(max_length=160)


class ManagerDecision(BaseModel):
    """Final trade decision after all specialists have reported."""
    trade:     bool
    direction: Literal["BUY", "SELL", "N/A"]
    size_usd:  float = Field(ge=0.0)
    reason:    str   = Field(max_length=200)


class ExecutionResult(BaseModel):
    status:     Literal["filled", "failed", "rejected"]
    fill_price: float = Field(ge=0.0)
    quantity:   float = Field(ge=0.0)
    total_usd:  float = Field(ge=0.0)


class ReflectionNote(BaseModel):
    outcome: Literal["win", "loss", "breakeven", "pending"]
    note:    str = Field(max_length=200)

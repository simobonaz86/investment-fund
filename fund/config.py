"""Phase 2.2 config — extends Phase 2.1 with HR, budget splits, reports."""
from __future__ import annotations

import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Core ────────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = Field(..., description="Anthropic API key")
    DB_PATH: str = "/data/fund.db"
    MARKET_SIM_URL: str = "http://market_sim:8001"
    LOG_LEVEL: str = "INFO"

    # ── Principals (always-on agents) ───────────────────────────────────────
    CEO_MODEL: str = "anthropic/claude-sonnet-4-5-20250929"
    KEVIN_MODEL: str = "anthropic/claude-haiku-4-5-20251001"
    HR_MODEL: str = "anthropic/claude-haiku-4-5-20251001"
    SPECIALIST_DEFAULT_MODEL: str = "anthropic/claude-haiku-4-5-20251001"

    # ── Monthly budget (USD, LLM spend only) ────────────────────────────────
    MONTHLY_BUDGET_USD: float = 100.0
    BUDGET_SPLIT_CEO: float = 0.70   # CEO pool (incl. specialists CEO hires)
    BUDGET_SPLIT_HR: float = 0.15
    BUDGET_SPLIT_KEVIN: float = 0.15

    # ── Trading ─────────────────────────────────────────────────────────────
    ASSETS: str = "SYN-A,SYN-B,SYN-C"
    MOMENTUM_THRESHOLD: float = 0.03
    CONFIDENCE_THRESHOLD: float = 0.70
    MAX_POSITION_USD: float = 1000.0
    CHECK_INTERVAL_SECONDS: int = 60

    # ── HR weekly cadence ───────────────────────────────────────────────────
    HR_REVIEW_DAY: str = "MON"          # day of week
    HR_REVIEW_HOUR_UTC: int = 9         # 09:00 UTC Monday

    # ── Reports schedule (UTC) ──────────────────────────────────────────────
    DAILY_REPORT_HOUR: int = 18
    WEEKLY_REPORT_DAY: str = "MON"
    WEEKLY_REPORT_HOUR: int = 9
    MONTHLY_REPORT_DAY: int = 1
    MONTHLY_REPORT_HOUR: int = 9
    BENCHMARK_SYMBOL: str = "SYN-A"     # used as simple benchmark

    # ── Dashboard ───────────────────────────────────────────────────────────
    DASHBOARD_HOST: str = "0.0.0.0"
    DASHBOARD_PORT: int = 8080

    # ── Kevin audit gate ────────────────────────────────────────────────────
    KEVIN_DEBUG_GATE: bool = True  # enforce action surfacing on startup

    @property
    def assets_list(self) -> list[str]:
        return [a.strip() for a in self.ASSETS.split(",") if a.strip()]

    @property
    def budget_ceo(self) -> float:
        return round(self.MONTHLY_BUDGET_USD * self.BUDGET_SPLIT_CEO, 2)

    @property
    def budget_hr(self) -> float:
        return round(self.MONTHLY_BUDGET_USD * self.BUDGET_SPLIT_HR, 2)

    @property
    def budget_kevin(self) -> float:
        return round(self.MONTHLY_BUDGET_USD * self.BUDGET_SPLIT_KEVIN, 2)


settings = Settings()

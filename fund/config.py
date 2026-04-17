"""
Fund configuration.
Only values that should not change at runtime live here.
Runtime-mutable knobs (thresholds, active assets, halt flag) live in the
`control` table in SQLite and are read fresh on every cycle.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── API ───────────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""

    # ── Per-agent models ──────────────────────────────────────────────────────
    # Manager synthesises — use Sonnet. Specialists are narrow — Haiku is plenty.
    manager_model:      str = "anthropic/claude-sonnet-4-6"
    research_model:     str = "anthropic/claude-haiku-4-5-20251001"
    risk_model:         str = "anthropic/claude-haiku-4-5-20251001"
    sentiment_model:    str = "anthropic/claude-haiku-4-5-20251001"
    execution_model:    str = "anthropic/claude-haiku-4-5-20251001"
    accountant_model:   str = "anthropic/claude-haiku-4-5-20251001"
    reflection_model:   str = "anthropic/claude-haiku-4-5-20251001"

    # ── Infra ─────────────────────────────────────────────────────────────────
    market_sim_url: str = "http://localhost:8001"
    db_path:        str = "data/fund.db"

    # ── Defaults seeded into the control table on first start ─────────────────
    default_assets_str:           str   = "SYN-A,SYN-B,SYN-C"
    default_momentum_threshold:   float = 0.030
    default_confidence_threshold: float = 0.70
    default_max_position_usd:     float = 1000.0
    default_check_interval:       int   = 60
    default_cooldown_minutes:     int   = 15

    # ── Budget caps (hard limits, not runtime-mutable for safety) ─────────────
    # Start at $1/week for testing.  Raise once Phase 1 is proven stable.
    weekly_budget_total_usd:      float = 1.00
    weekly_budget_research_usd:   float = 0.40
    weekly_budget_risk_usd:       float = 0.20
    weekly_budget_sentiment_usd:  float = 0.15
    weekly_budget_execution_usd:  float = 0.10
    weekly_budget_accountant_usd: float = 0.05
    weekly_budget_reflection_usd: float = 0.10

    # Hard per-cycle safety: don't let a rogue cycle drain the week
    max_cycle_spend_usd: float = 0.10

    # ── Paper fund starting cash ──────────────────────────────────────────────
    starting_cash_usd: float = 100_000.0

    # ── HTTP control port (for /stop and dashboard) ───────────────────────────
    control_port: int = 8002

    @property
    def default_assets(self) -> List[str]:
        return [a.strip() for a in self.default_assets_str.split(",")]


settings = Settings()

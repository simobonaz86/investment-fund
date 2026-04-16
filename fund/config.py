"""
Fund configuration.  All values are read from environment / .env file.
Change trading parameters here without touching agent code.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── API keys ──────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""

    # ── Models ────────────────────────────────────────────────────────────────
    #  Manager uses a slightly stronger model; specialists use Haiku for cost.
    manager_model: str    = "anthropic/claude-haiku-4-5-20251001"
    specialist_model: str = "anthropic/claude-haiku-4-5-20251001"

    # ── Infrastructure ────────────────────────────────────────────────────────
    market_sim_url: str = "http://localhost:8001"
    db_path: str        = "data/fund.db"

    # ── Trading universe ──────────────────────────────────────────────────────
    assets: List[str] = ["SYN-A", "SYN-B", "SYN-C"]   # Phase 0: 3 assets

    # ── Signal thresholds ─────────────────────────────────────────────────────
    momentum_threshold: float  = 0.030   # 3% price move triggers Research hire
    confidence_threshold: float = 0.70   # min Research confidence to trade
    max_position_usd: float    = 1000.0  # max $ per trade

    # ── Budget ────────────────────────────────────────────────────────────────
    monthly_budget_usd: float = 50.0     # hard monthly cap on agent API costs

    # ── Loop ──────────────────────────────────────────────────────────────────
    check_interval_seconds: int = 60     # how often to scan for signals


settings = Settings()

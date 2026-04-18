"""Per-asset metadata registry.

Each real ticker has a momentum threshold calibrated to its typical daily
volatility, plus exchange / currency / market-hours info. This replaces the
single global MOMENTUM_THRESHOLD from Phase 2.2 which was tuned for synthetic
GBM paths with σ≈0.20–0.50.

Typical real-world 1-hour moves:
  * Broad ETFs (VWRP, VUAA, VUSA, SSAC):   <0.5%
  * Sector/regional ETFs (EUDF, 5J50):     0.5–1.5%
  * US single-name stocks (NVDA, TSM):     1–3%
  * Commodity ETFs (SGLN, AGAP, COPB):     0.5–1.5%
  * Crypto (BTC-USD):                      2–6%

We set thresholds at roughly 2× expected noise so the Manager isn't asked to
decide on every wiggle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Exchange = Literal[
    "NASDAQ", "NYSE",        # US equities
    "LSE",                   # London
    "XETRA",                 # Frankfurt electronic
    "CPH",                   # Copenhagen (Nasdaq Nordic)
    "COMEX",                 # Gold futures
    "CRYPTO",                # 24/7
]

Currency = Literal["USD", "GBP", "EUR", "DKK"]


@dataclass(frozen=True)
class AssetMeta:
    """Static metadata for one tracked asset."""
    ticker: str                    # Yahoo Finance ticker
    name: str
    exchange: Exchange
    currency: Currency
    threshold: float               # |pct move| that triggers a scan
    asset_class: str               # equity | etf_broad | etf_sector | crypto | commodity
    notes: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# Simone's live universe (April 2026) — Interactive Investor + Revolut
# ═══════════════════════════════════════════════════════════════════════════

ASSETS: dict[str, AssetMeta] = {
    # ── Interactive Investor ────────────────────────────────────────────────
    "AMZN":     AssetMeta("AMZN",     "Amazon.com",                    "NASDAQ", "USD", 0.020, "equity"),
    "EQQQ.L":   AssetMeta("EQQQ.L",   "Invesco EQQQ NASDAQ-100",       "LSE",    "GBP", 0.012, "etf_broad"),
    "SPAG.L":   AssetMeta("SPAG.L",   "iShares Agribusiness",          "LSE",    "GBP", 0.015, "etf_sector"),
    "MINE.L":   AssetMeta("MINE.L",   "iShares Copper Miners",         "LSE",    "GBP", 0.020, "etf_sector"),
    "CS51.L":   AssetMeta("CS51.L",   "iShares EURO STOXX 50",         "LSE",    "GBP", 0.010, "etf_broad"),
    "SSAC.L":   AssetMeta("SSAC.L",   "iShares MSCI ACWI",             "LSE",    "GBP", 0.010, "etf_broad"),
    "SGLN.L":   AssetMeta("SGLN.L",   "iShares Physical Gold ETC",     "LSE",    "GBP", 0.012, "commodity"),
    "IGUS.L":   AssetMeta("IGUS.L",   "iShares S&P 500 GBP Hedged",    "LSE",    "GBP", 0.012, "etf_broad"),
    "BCOG.L":   AssetMeta("BCOG.L",   "L&G All Commodities",           "LSE",    "GBP", 0.012, "commodity"),
    "MUT.L":    AssetMeta("MUT.L",    "Murray Income Trust",           "LSE",    "GBP", 0.012, "equity"),
    "VMIG.L":   AssetMeta("VMIG.L",   "Vanguard FTSE 250",             "LSE",    "GBP", 0.012, "etf_broad"),
    "VWRP.L":   AssetMeta("VWRP.L",   "Vanguard FTSE All-World",       "LSE",    "GBP", 0.010, "etf_broad"),
    "VUSA.L":   AssetMeta("VUSA.L",   "Vanguard S&P 500 UCITS",        "LSE",    "GBP", 0.010, "etf_broad"),
    "AGAP.L":   AssetMeta("AGAP.L",   "WisdomTree Agriculture",        "LSE",    "GBP", 0.015, "commodity"),
    "COPB.L":   AssetMeta("COPB.L",   "WisdomTree Copper ETC",         "LSE",    "GBP", 0.015, "commodity"),

    # ── Revolut ─────────────────────────────────────────────────────────────
    "TSM":         AssetMeta("TSM",         "Taiwan Semiconductor ADR",   "NYSE",   "USD", 0.020, "equity"),
    "NVDA":        AssetMeta("NVDA",        "NVIDIA",                      "NASDAQ", "USD", 0.025, "equity"),
    "GOOGL":       AssetMeta("GOOGL",       "Alphabet Class A",            "NASDAQ", "USD", 0.020, "equity"),
    "META":        AssetMeta("META",        "Meta Platforms",              "NASDAQ", "USD", 0.025, "equity"),
    "VUAA.L":      AssetMeta("VUAA.L",      "Vanguard S&P 500 USD Acc",    "LSE",    "USD", 0.010, "etf_broad"),
    "EUDF.L":      AssetMeta("EUDF.L",      "WisdomTree Europe Defence",   "LSE",    "EUR", 0.015, "etf_sector"),
    "5J50.DE":     AssetMeta("5J50.DE",     "iShares Global Aerospace",    "XETRA",  "EUR", 0.015, "etf_sector"),
    "MAERSK-B.CO": AssetMeta("MAERSK-B.CO", "A.P. Moller-Maersk B",        "CPH",    "DKK", 0.020, "equity",
                             notes="Simone's day-job-adjacent holding"),
    "GC=F":        AssetMeta("GC=F",        "Gold (COMEX futures)",        "COMEX",  "USD", 0.012, "commodity"),
    "BTC-USD":     AssetMeta("BTC-USD",     "Bitcoin",                     "CRYPTO", "USD", 0.040, "crypto"),
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_meta(ticker: str) -> AssetMeta | None:
    return ASSETS.get(ticker.upper())


def threshold_for(ticker: str, fallback: float = 0.015) -> float:
    meta = get_meta(ticker)
    return meta.threshold if meta else fallback


def all_tickers() -> list[str]:
    return list(ASSETS.keys())


def tickers_by_asset_class(asset_class: str) -> list[str]:
    return [t for t, m in ASSETS.items() if m.asset_class == asset_class]

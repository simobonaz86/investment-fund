"""
Market data adapter (Phase 2).

Two data sources:
  • Sim — local FastAPI GBM generator (tickers SYN-*)
  • yfinance — real market data for LSE (.L), US, crypto (BTC-USD)

Ticker routing is automatic: SYN-* → sim, else yfinance.
Both paths return the same shape so agent tools don't care which backend ran.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta

import httpx
import numpy as np

log = logging.getLogger(__name__)


# ── Portfolio ticker → yfinance ticker mapping ────────────────────────────────
# Some LSE tickers need the .L suffix, some don't exist on yfinance
# and we map to a close proxy.
YF_TICKER_MAP: dict[str, str] = {
    # US single stocks
    "AMZN":    "AMZN",
    "NVDA":    "NVDA",
    "GOOGL":   "GOOGL",
    "META":    "META",
    "TSM":     "TSM",
    # LSE ETFs
    "VWRP":    "VWRP.L",
    "SGLN":    "SGLN.L",
    "VUSA":    "VUSA.L",
    "VUAA":    "VUAA.L",
    "VMIG":    "VMID.L",
    "IGUS":    "IGUS.L",
    "EQQQ":    "EQQQ.L",
    "SSAC":    "SSAC.L",
    "CS51":    "CS51.L",
    "BCOG":    "BCOG.L",
    "SPAG":    "SPAG.L",
    "MINE":    "MINE.L",
    "AGAP":    "AGAP.L",
    "COPB":    "COPB.L",
    "MUT":     "MUT.L",
    "5J50":    "IE5J.L",         # iShares MSCI China
    "DP4B":    "XMME.L",         # Xtrackers MSCI EM
    "EUDF":    "EUDF.L",
    "B5ZX1M7": None,             # Artemis fund — no yfinance feed; skip signals
    # Crypto
    "BTC":     "BTC-GBP",
    # Precious metals
    "XAU":     "GC=F",           # COMEX Gold futures as proxy
}

_SIM_URL  = os.getenv("MARKET_SIM_URL", "http://localhost:8001")
_USE_SIM_FOR = os.getenv("SIM_TICKERS", "SYN-").split(",")


def _is_sim_ticker(symbol: str) -> bool:
    return any(symbol.upper().startswith(p) for p in _USE_SIM_FOR)


# ── Sim path (Phase 0/1 code) ────────────────────────────────────────────────

def _sim_bars(symbol: str, limit: int = 40) -> list[dict]:
    r = httpx.get(f"{_SIM_URL}/v2/stocks/{symbol}/bars",
                  params={"limit": limit}, timeout=6.0)
    r.raise_for_status()
    return r.json().get("bars", [])


def _sim_quote(symbol: str) -> dict:
    r = httpx.get(f"{_SIM_URL}/v2/stocks/{symbol}/quotes/latest", timeout=4.0)
    r.raise_for_status()
    return r.json()["quote"]


# ── yfinance path (lazy import so sim-only setups don't need it) ─────────────

_yf_cache: dict[str, tuple[float, list[dict]]] = {}
_QUOTE_TTL = 120.0      # seconds — cache quotes briefly to avoid flooding YF


def _yf_bars(symbol: str, limit: int = 40) -> list[dict]:
    mapped = YF_TICKER_MAP.get(symbol.upper(), symbol)
    if mapped is None:
        raise ValueError(f"no market data feed available for {symbol}")

    cached = _yf_cache.get(f"bars:{mapped}")
    if cached and time.time() - cached[0] < _QUOTE_TTL:
        return cached[1][-limit:]

    import yfinance as yf
    hist = yf.Ticker(mapped).history(period="2d", interval="5m", auto_adjust=False)
    if hist.empty:
        hist = yf.Ticker(mapped).history(period="5d", interval="1h", auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"yfinance returned no data for {mapped}")

    bars = []
    for idx, row in hist.tail(limit).iterrows():
        bars.append({
            "t": idx.isoformat(),
            "o": float(row["Open"]),
            "h": float(row["High"]),
            "l": float(row["Low"]),
            "c": float(row["Close"]),
            "v": int(row["Volume"]) if not np.isnan(row["Volume"]) else 0,
        })

    _yf_cache[f"bars:{mapped}"] = (time.time(), bars)
    return bars


def _yf_quote(symbol: str) -> dict:
    mapped = YF_TICKER_MAP.get(symbol.upper(), symbol)
    if mapped is None:
        raise ValueError(f"no market data feed available for {symbol}")

    cached = _yf_cache.get(f"quote:{mapped}")
    if cached and time.time() - cached[0] < _QUOTE_TTL:
        return cached[1][0]

    import yfinance as yf
    t = yf.Ticker(mapped)
    fi = t.fast_info
    try:
        last = float(fi["last_price"])
    except Exception:
        hist = t.history(period="1d", interval="1m")
        if hist.empty:
            raise RuntimeError(f"no price for {mapped}")
        last = float(hist["Close"].iloc[-1])

    # 5 bp spread synthetic — yfinance doesn't give us L1 book
    spread = last * 0.0005
    quote = {
        "ap": round(last + spread, 6),
        "bp": round(last - spread, 6),
        "as": 0,
        "bs": 0,
        "t":  datetime.utcnow().isoformat() + "Z",
    }
    _yf_cache[f"quote:{mapped}"] = (time.time(), [quote])
    return quote


# ── Public interface used by tools ───────────────────────────────────────────

def get_bars(symbol: str, limit: int = 40) -> list[dict]:
    """Return last N OHLCV bars for symbol."""
    if _is_sim_ticker(symbol):
        return _sim_bars(symbol, limit)
    return _yf_bars(symbol, limit)


def get_quote(symbol: str) -> dict:
    """Return latest bid/ask quote for symbol."""
    if _is_sim_ticker(symbol):
        return _sim_quote(symbol)
    return _yf_quote(symbol)


def pct_change(symbol: str, lookback_bars: int = 12) -> float:
    """Percent change over the last N bars, for signal detection."""
    bars = get_bars(symbol, limit=lookback_bars + 2)
    if len(bars) < 2:
        return 0.0
    baseline = bars[0]["c"]
    current  = bars[-1]["c"]
    return (current - baseline) / baseline if baseline > 0 else 0.0

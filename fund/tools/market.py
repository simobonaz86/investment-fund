"""
Market data tools — used by the Research Analyst and Investment Manager agents.
All calls go to the local market simulator (Phase 0).
Swap MARKET_SIM_URL to Alpaca's endpoint in Phase 2; no agent code changes needed.
"""
import json
import os

import httpx
import numpy as np
from crewai.tools import tool

_SIM_URL = os.getenv("MARKET_SIM_URL", "http://localhost:8001")
_TIMEOUT = 6.0


# ── Tool 1: raw OHLCV bars ────────────────────────────────────────────────────

@tool("get_price_bars")
def get_price_bars(symbol: str) -> str:
    """
    Fetch the last 40 OHLCV price bars for a synthetic asset.
    Use symbols: SYN-A, SYN-B, SYN-C, SYN-D, or SYN-E.
    Returns JSON: { bars: [{t, o, h, l, c, v}, ...], symbol }.
    Each bar = 1 minute.  'c' = close price.
    """
    sym = symbol.strip().upper()
    try:
        r = httpx.get(
            f"{_SIM_URL}/v2/stocks/{sym}/bars",
            params={"limit": 40},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.text
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Tool 2: computed technical indicators ─────────────────────────────────────

@tool("calculate_indicators")
def calculate_indicators(symbol: str) -> str:
    """
    Calculate RSI-14, SMA-5, SMA-20, and 1-hour momentum for an asset.
    Use symbols: SYN-A, SYN-B, SYN-C, SYN-D, or SYN-E.
    Returns JSON with indicator values and plain-English signals.
    Combine with get_price_bars for a full technical picture.
    """
    sym = symbol.strip().upper()
    try:
        r = httpx.get(
            f"{_SIM_URL}/v2/stocks/{sym}/bars",
            params={"limit": 40},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        bars = r.json().get("bars", [])

        if len(bars) < 5:
            return json.dumps({"error": "not enough bars", "bars_received": len(bars)})

        closes = np.array([b["c"] for b in bars], dtype=float)
        current = float(closes[-1])

        # SMAs
        sma5  = float(np.mean(closes[-5:]))
        sma20 = float(np.mean(closes[-min(20, len(closes)):]))

        # RSI-14
        if len(closes) >= 15:
            deltas = np.diff(closes[-15:])
            gains  = deltas[deltas > 0]
            losses = -deltas[deltas < 0]
            avg_g  = float(np.mean(gains))  if len(gains)  > 0 else 0.0
            avg_l  = float(np.mean(losses)) if len(losses) > 0 else 1e-9
            rs  = avg_g / avg_l
            rsi = round(100.0 - 100.0 / (1.0 + rs), 1)
        else:
            rsi = 50.0

        # 1-hour momentum (last 60 bars vs first bar)
        lookback = min(60, len(closes) - 1)
        momentum_1h = (closes[-1] - closes[-lookback - 1]) / closes[-lookback - 1]

        # Signals
        trend_signal  = "bullish" if sma5 > sma20 else "bearish"
        rsi_signal    = "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral")
        momentum_bias = "positive" if momentum_1h > 0 else "negative"

        return json.dumps({
            "symbol":        sym,
            "current_price": round(current, 4),
            "sma5":          round(sma5, 4),
            "sma20":         round(sma20, 4),
            "rsi_14":        rsi,
            "momentum_1h_pct": round(momentum_1h * 100, 3),
            "trend_signal":  trend_signal,
            "rsi_signal":    rsi_signal,
            "momentum_bias": momentum_bias,
            "bars_used":     len(closes),
        })

    except Exception as e:
        return json.dumps({"error": str(e)})

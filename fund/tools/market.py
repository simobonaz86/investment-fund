"""
Market data tools — route through the market_data adapter.
Adapter auto-picks between sim (SYN-*) and yfinance (real tickers).
"""
import json
import numpy as np
from crewai.tools import tool
from fund.market_data import get_bars


@tool("get_price_bars")
def get_price_bars(symbol: str) -> str:
    """
    Fetch the last 40 OHLCV price bars for an asset.
    Works for synthetic tickers (SYN-A..E) and real ones
    (VWRP, SGLN, NVDA, AMZN, BTC, etc.).
    Returns JSON: { bars: [{t, o, h, l, c, v}, ...], symbol }.
    """
    try:
        bars = get_bars(symbol.strip().upper(), limit=40)
        return json.dumps({"bars": bars, "symbol": symbol.upper()})
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool("calculate_indicators")
def calculate_indicators(symbol: str) -> str:
    """
    Calculate RSI-14, SMA-5, SMA-20 and 1-hour momentum for an asset.
    Returns JSON with indicator values and plain-English signals.
    """
    try:
        bars = get_bars(symbol.strip().upper(), limit=40)
        if len(bars) < 5:
            return json.dumps({"error": "not enough bars", "bars_received": len(bars)})

        closes = np.array([b["c"] for b in bars], dtype=float)
        current = float(closes[-1])
        sma5  = float(np.mean(closes[-5:]))
        sma20 = float(np.mean(closes[-min(20, len(closes)):]))

        if len(closes) >= 15:
            deltas = np.diff(closes[-15:])
            gains  = deltas[deltas > 0]
            losses = -deltas[deltas < 0]
            avg_g  = float(np.mean(gains))  if len(gains)  > 0 else 0.0
            avg_l  = float(np.mean(losses)) if len(losses) > 0 else 1e-9
            rsi = round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 1)
        else:
            rsi = 50.0

        lookback = min(60, len(closes) - 1)
        momentum = (closes[-1] - closes[-lookback - 1]) / closes[-lookback - 1]

        return json.dumps({
            "symbol":          symbol.upper(),
            "current_price":   round(current, 4),
            "sma5":            round(sma5, 4),
            "sma20":           round(sma20, 4),
            "rsi_14":          rsi,
            "momentum_1h_pct": round(momentum * 100, 3),
            "trend_signal":    "bullish" if sma5 > sma20 else "bearish",
            "rsi_signal":      "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral"),
            "momentum_bias":   "positive" if momentum > 0 else "negative",
            "bars_used":       len(closes),
        })

    except Exception as e:
        return json.dumps({"error": str(e)})

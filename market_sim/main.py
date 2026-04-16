"""
Market Simulator — Phase 0
FastAPI server that exposes a synthetic OHLCV feed in Alpaca API format.
Swap MARKET_SIM_URL for Alpaca's real endpoint in Phase 2; agent code is unchanged.

Endpoints (Alpaca-compatible):
  GET /v2/stocks/{symbol}/bars
  GET /v2/stocks/{symbol}/quotes/latest
  GET /v2/stocks/snapshots?symbols=SYN-A,SYN-B
  GET /health
"""
import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query

# Allow running directly: python main.py
sys.path.insert(0, os.path.dirname(__file__))
from gbm import GBMEngine

# ── Asset universe ────────────────────────────────────────────────────────────
#  Each asset has a distinct risk/return profile so the Manager can observe
#  different behaviour patterns in Phase 0.
ASSET_CONFIGS: dict[str, dict] = {
    "SYN-A": {"S0": 100.0, "mu": 0.08, "sigma": 0.20, "seed": 1},  # stable blue-chip
    "SYN-B": {"S0":  50.0, "mu": 0.14, "sigma": 0.38, "seed": 2},  # high-growth, volatile
    "SYN-C": {"S0": 200.0, "mu": 0.05, "sigma": 0.13, "seed": 3},  # low-vol bond proxy
    "SYN-D": {"S0":  75.0, "mu": 0.18, "sigma": 0.50, "seed": 4},  # speculative
    "SYN-E": {"S0":  30.0, "mu": 0.10, "sigma": 0.25, "seed": 5},  # mid-vol
}

engines: dict[str, GBMEngine] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Spin up engines and pre-generate 200 bars of history on startup
    for symbol, cfg in ASSET_CONFIGS.items():
        eng = GBMEngine(symbol=symbol, **cfg)
        eng.advance(200)
        engines[symbol] = eng
        print(f"  {symbol}: ${eng.current_price:.2f} ({len(eng._closes)-1} bars pre-generated)")
    yield  # app runs
    # (cleanup on shutdown if needed)


app = FastAPI(title="Investment Fund — Market Simulator", lifespan=lifespan)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/v2/stocks/{symbol}/bars")
def get_bars(
    symbol: str,
    timeframe: str = "1Min",
    limit: int = Query(default=30, le=500),
):
    """
    Return OHLCV bars.  Alpaca schema: { bars: [...], symbol, next_page_token }.
    Calling this endpoint advances the price by one bar (real-time simulation).
    """
    symbol = symbol.upper()
    if symbol not in engines:
        raise HTTPException(404, f"Unknown symbol '{symbol}'. Available: {list(engines)}")

    engines[symbol].advance(1)   # tick forward
    bars = engines[symbol].get_bars(limit=limit)

    return {"bars": bars, "symbol": symbol, "next_page_token": None}


@app.get("/v2/stocks/{symbol}/quotes/latest")
def get_latest_quote(symbol: str):
    """
    Return latest bid/ask.  Alpaca schema: { quote: { ap, bp, as, bs, t } }.
    1 bp spread (realistic for liquid synthetic assets).
    """
    symbol = symbol.upper()
    if symbol not in engines:
        raise HTTPException(404, f"Unknown symbol '{symbol}'")

    price = engines[symbol].current_price
    spread = price * 0.0001          # 1 bp

    return {
        "quote": {
            "ap": round(price + spread, 4),    # ask price
            "bp": round(price - spread, 4),    # bid price
            "as": 500,                          # ask size
            "bs": 500,                          # bid size
            "t":  engines[symbol].current_time.isoformat() + "Z",
        },
        "symbol": symbol,
    }


@app.get("/v2/stocks/snapshots")
def get_snapshots(symbols: str = Query(..., description="Comma-separated symbols")):
    """
    Multi-symbol snapshot.  Alpaca schema used by momentum scanner.
    """
    result = {}
    for sym in [s.strip().upper() for s in symbols.split(",")]:
        if sym not in engines:
            continue
        eng = engines[sym]
        price = eng.current_price
        pct = eng.pct_change(lookback=60)
        result[sym] = {
            "latestTrade": {"p": round(price, 4)},
            "dailyBar": {
                "c":  round(price, 4),
                "pc": round(price / (1 + pct), 4) if pct != -1 else price,
            },
            "pct_change_1h": round(pct * 100, 3),
        }
    return result


@app.get("/health")
def health():
    return {
        "status": "ok",
        "assets": {
            sym: {"price": round(eng.current_price, 2), "bars": len(eng._closes) - 1}
            for sym, eng in engines.items()
        },
    }


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)

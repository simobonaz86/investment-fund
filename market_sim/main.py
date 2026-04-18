"""
Market Data Service — Phase 2.3
FastAPI server that proxies Yahoo Finance data in Alpaca API format.

Endpoints (Alpaca-compatible, identical shape to Phase 2.2):
  GET /v2/stocks/{symbol}/bars
  GET /v2/stocks/{symbol}/quotes/latest
  GET /v2/stocks/snapshots?symbols=AAPL,NVDA
  GET /health
"""
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

# Allow running directly: python main.py
sys.path.insert(0, os.path.dirname(__file__))
from yahoo import YahooEngine  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("market_sim")


def _load_universe() -> list[str]:
    """Read ASSETS env var; fall back to the synthetic trio for dev safety."""
    raw = os.getenv("ASSETS", "").strip()
    if not raw:
        log.warning("ASSETS env empty — falling back to SYN-A,SYN-B,SYN-C")
        return ["SYN-A", "SYN-B", "SYN-C"]
    return [s.strip() for s in raw.split(",") if s.strip()]


engines: dict[str, YahooEngine] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    universe = _load_universe()
    log.info("Initialising %d engines: %s", len(universe), universe)
    for symbol in universe:
        engines[symbol.upper()] = YahooEngine(symbol)
    # Warm up the cache for each symbol (sequential — <1s per ticker typical)
    for sym, eng in engines.items():
        try:
            p = eng.current_price
            stale = "stale" if eng.is_stale else "live"
            log.info("  %s: %.4f (%s)", sym, p, stale)
        except Exception as exc:
            log.warning("  %s: warm-up failed (%s)", sym, exc)
    yield
    log.info("Shutting down market data service")


app = FastAPI(title="Investment Fund — Market Data (Yahoo)", lifespan=lifespan)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/v2/stocks/{symbol}/bars")
def get_bars(
    symbol: str,
    timeframe: str = "1Min",
    limit: int = Query(default=30, le=500),
):
    """Alpaca-schema bars, served from the TTL cache."""
    sym = symbol.upper()
    if sym not in engines:
        raise HTTPException(404, f"Unknown symbol '{sym}'. "
                                 f"Available: {sorted(engines)}")
    bars = engines[sym].get_bars(limit=limit)
    return {"bars": bars, "symbol": sym, "next_page_token": None}


@app.get("/v2/stocks/{symbol}/quotes/latest")
def get_latest_quote(symbol: str):
    """Alpaca-schema quote. Includes `stale` flag when market closed."""
    sym = symbol.upper()
    if sym not in engines:
        raise HTTPException(404, f"Unknown symbol '{sym}'")
    q = engines[sym].get_latest_quote()
    return {"quote": q, "symbol": sym}


@app.get("/v2/stocks/snapshots")
def get_snapshots(symbols: str = Query(...,
                  description="Comma-separated symbols")):
    """Multi-symbol snapshot for the momentum scanner."""
    result = {}
    for sym in [s.strip().upper() for s in symbols.split(",")]:
        if sym not in engines:
            continue
        eng = engines[sym]
        bars = eng.get_bars(limit=65)
        if len(bars) < 2:
            continue
        current, baseline = bars[-1]["c"], bars[0]["c"]
        pct = (current - baseline) / baseline if baseline else 0.0
        result[sym] = {
            "latestTrade": {"p": round(current, 4)},
            "dailyBar": {
                "c":  round(current, 4),
                "pc": round(baseline, 4),
            },
            "pct_change_lookback": round(pct * 100, 3),
            "stale": eng.is_stale,
        }
    return result


@app.get("/health")
def health():
    return {
        "status": "ok",
        "provider": "yahoo",
        "assets": {
            sym: {
                "price": round(eng.current_price, 4),
                "stale": eng.is_stale,
            }
            for sym, eng in engines.items()
        },
    }


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)

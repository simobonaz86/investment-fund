"""Yahoo Finance market data adapter.

Fetches real 1-minute bars via yfinance, caches them in-memory for 60 seconds
per symbol, and exposes the same interface GBMEngine used so `main.py` doesn't
care which backend is active.

Design notes
────────────
* Yahoo's free tier isn't rate-limit-documented, but anecdotally ~2,000
  requests/hour per IP. With N symbols × 1 fetch/min = 60*N/hour, we're fine
  up to ~30 symbols. Beyond that we batch or slow the scan.
* yfinance returns data in the exchange's local timezone and native currency.
  We pass that through — the fund layer handles GBP/USD/EUR/DKK conversion.
* When markets are closed, yfinance returns the last cached intraday bars up
  to ~7 days old. We flag this as `is_stale=True` so the fund can skip signals.
* Crypto (BTC-USD) is 24/7; gold futures (GC=F) is 23/5; equities follow
  their exchange calendar. The market-hours gate in fund/market_hours.py
  decides whether to scan at all.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import yfinance as yf

log = logging.getLogger("market_sim.yahoo")


# ═══════════════════════════════════════════════════════════════════════════
# Cache entry
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _CacheEntry:
    bars: list[dict]
    fetched_at: float            # unix ts
    last_price: float
    is_stale: bool = False       # True when market closed / data old


# ═══════════════════════════════════════════════════════════════════════════
# Yahoo engine
# ═══════════════════════════════════════════════════════════════════════════

class YahooEngine:
    """Thread-safe TTL cache of 1-minute bars for one symbol."""

    TTL_SECONDS   = 60           # cache window per symbol
    HISTORY_DAYS  = 2            # rolling window of bars we keep
    INTERVAL      = "1m"         # Yahoo's finest intraday granularity
    STALE_SECONDS = 15 * 60      # bars older than 15 min → mark stale

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._cache: Optional[_CacheEntry] = None
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────────

    def get_bars(self, limit: int = 30) -> list[dict]:
        """Return the last N bars in Alpaca format: {t, o, h, l, c, v}."""
        self._refresh_if_needed()
        if not self._cache:
            return []
        return self._cache.bars[-limit:]

    def get_latest_quote(self) -> dict:
        """Return mid-price based bid/ask with 5 bp synthetic spread."""
        self._refresh_if_needed()
        if not self._cache or self._cache.last_price <= 0:
            return {"ap": 0.0, "bp": 0.0, "as": 0, "bs": 0,
                    "t": datetime.now(timezone.utc).isoformat() + "Z",
                    "stale": True}
        mid    = self._cache.last_price
        spread = mid * 0.0005   # 5 bp (real ETF spreads are wider than synthetic)
        return {
            "ap": round(mid + spread, 4),
            "bp": round(mid - spread, 4),
            "as": 500,
            "bs": 500,
            "t":  datetime.now(timezone.utc).isoformat() + "Z",
            "stale": self._cache.is_stale,
        }

    @property
    def current_price(self) -> float:
        self._refresh_if_needed()
        return self._cache.last_price if self._cache else 0.0

    @property
    def is_stale(self) -> bool:
        self._refresh_if_needed()
        return bool(self._cache.is_stale) if self._cache else True

    # ── Internals ───────────────────────────────────────────────────────────

    def _refresh_if_needed(self) -> None:
        with self._lock:
            now = time.time()
            if self._cache and (now - self._cache.fetched_at) < self.TTL_SECONDS:
                return                               # cache fresh
            try:
                self._cache = self._fetch_fresh()
            except Exception as exc:
                log.warning("yahoo fetch failed for %s: %s", self.symbol, exc)
                if not self._cache:
                    # first-fetch failure → empty placeholder so we don't crash
                    self._cache = _CacheEntry(
                        bars=[], fetched_at=now, last_price=0.0, is_stale=True)
                else:
                    # keep old cache but bump timestamp so we don't retry every call
                    self._cache.fetched_at = now
                    self._cache.is_stale = True

    def _fetch_fresh(self) -> _CacheEntry:
        """Hit Yahoo for the latest intraday bars.

        yfinance can fail in several ways:
          * return an empty DataFrame (market closed, illiquid ticker)
          * raise an exception (network error, rate limit, auth problem)
          * return a non-DataFrame object (rare internal state issue)

        We normalise all of these into a single try/except in the caller so
        the cache keeps serving last-known-good data instead of crashing.
        """
        ticker = yf.Ticker(self.symbol)

        df = self._safe_history(ticker, period=f"{self.HISTORY_DAYS}d",
                                interval=self.INTERVAL)
        if df is None or df.empty:
            # Fallback to daily bars (market closed, illiquid ticker, rate limit)
            df = self._safe_history(ticker, period="7d", interval="1d")

        if df is None or df.empty:
            raise RuntimeError(f"Yahoo returned no usable data for {self.symbol}")

        # Normalise to Alpaca-ish bar format
        bars: list[dict] = []
        for idx, row in df.iterrows():
            ts = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            bars.append({
                "t": ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "o": round(float(row["Open"]),  6),
                "h": round(float(row["High"]),  6),
                "l": round(float(row["Low"]),   6),
                "c": round(float(row["Close"]), 6),
                "v": int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
            })

        last_price = bars[-1]["c"] if bars else 0.0
        last_bar_ts = datetime.fromisoformat(bars[-1]["t"].replace("Z", "+00:00"))
        age_sec = (datetime.now(timezone.utc) - last_bar_ts).total_seconds()
        is_stale = age_sec > self.STALE_SECONDS

        return _CacheEntry(
            bars=bars,
            fetched_at=time.time(),
            last_price=last_price,
            is_stale=is_stale,
        )

    @staticmethod
    def _safe_history(ticker, **kwargs) -> Optional[pd.DataFrame]:
        """Call ticker.history and normalise every failure mode to None."""
        try:
            df = ticker.history(
                auto_adjust=False, prepost=False, actions=False, **kwargs)
        except Exception as exc:
            log.debug("ticker.history raised: %s", exc)
            return None
        if not isinstance(df, pd.DataFrame):
            log.debug("ticker.history returned non-DataFrame: %s", type(df))
            return None
        return df

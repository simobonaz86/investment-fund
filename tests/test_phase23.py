"""Phase 2.3 tests — Yahoo adapter, asset registry, market hours.

All tests here are offline (no network). The YahooEngine has its own failure
path that's exercised to ensure graceful degradation when Yahoo is unreachable.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
os.environ.setdefault("DB_PATH", "/tmp/test_phase23.db")

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "market_sim"))

from fund.assets import (ASSETS, AssetMeta, all_tickers, get_meta,  # noqa: E402
                          threshold_for, tickers_by_asset_class)
from fund.market_hours import filter_open, is_open  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# Asset registry tests
# ═══════════════════════════════════════════════════════════════════════════

def test_universe_size_and_types():
    assert len(ASSETS) == 25, f"expected 25 tickers, got {len(ASSETS)}"
    for ticker, meta in ASSETS.items():
        assert isinstance(meta, AssetMeta)
        assert meta.ticker == ticker, f"ticker mismatch for {ticker}"


def test_thresholds_are_sensible():
    for ticker, meta in ASSETS.items():
        assert 0.005 <= meta.threshold <= 0.10, (
            f"{ticker} threshold {meta.threshold} out of range [0.5%, 10%]")


def test_threshold_lookup_with_fallback():
    assert threshold_for("NVDA") == 0.025
    assert threshold_for("BTC-USD") == 0.040
    assert threshold_for("VWRP.L") == 0.010
    # Unknown ticker returns caller-supplied fallback
    assert threshold_for("UNKNOWN", fallback=0.025) == 0.025
    assert threshold_for("UNKNOWN") == 0.015                  # default fallback


def test_asset_class_filtering():
    crypto = tickers_by_asset_class("crypto")
    assert crypto == ["BTC-USD"]
    broad = tickers_by_asset_class("etf_broad")
    assert len(broad) >= 6
    assert "VWRP.L" in broad and "VUAA.L" in broad
    equities = tickers_by_asset_class("equity")
    assert "NVDA" in equities and "MAERSK-B.CO" in equities


def test_currency_and_exchange_coverage():
    currencies = {m.currency for m in ASSETS.values()}
    exchanges = {m.exchange for m in ASSETS.values()}
    assert currencies == {"USD", "GBP", "EUR", "DKK"}
    expected_exchanges = {"NASDAQ", "NYSE", "LSE", "XETRA", "CPH", "COMEX", "CRYPTO"}
    assert exchanges == expected_exchanges


# ═══════════════════════════════════════════════════════════════════════════
# Market hours tests
# ═══════════════════════════════════════════════════════════════════════════

# Reference timestamps (all UTC) — Wed 2026-04-15, Sat 2026-04-18, etc.
WED_US_OPEN    = datetime(2026, 4, 15, 15, 0, tzinfo=timezone.utc)
WED_US_CLOSED  = datetime(2026, 4, 15, 3, 0, tzinfo=timezone.utc)
WED_LSE_OPEN   = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
WED_LSE_CLOSED = datetime(2026, 4, 15, 19, 0, tzinfo=timezone.utc)
SAT            = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
FRI_CLOSE      = datetime(2026, 4, 17, 22, 30, tzinfo=timezone.utc)
SUN_EARLY      = datetime(2026, 4, 19, 10, 0, tzinfo=timezone.utc)
SUN_LATE       = datetime(2026, 4, 19, 23, 30, tzinfo=timezone.utc)
DAILY_BREAK    = datetime(2026, 4, 15, 22, 30, tzinfo=timezone.utc)


def test_nyse_nasdaq_hours():
    assert is_open(get_meta("NVDA"), WED_US_OPEN) is True
    assert is_open(get_meta("NVDA"), WED_US_CLOSED) is False
    assert is_open(get_meta("AMZN"), WED_US_OPEN) is True


def test_lse_hours():
    assert is_open(get_meta("VWRP.L"), WED_LSE_OPEN) is True
    assert is_open(get_meta("VWRP.L"), WED_LSE_CLOSED) is False


def test_xetra_hours():
    assert is_open(get_meta("5J50.DE"), WED_LSE_OPEN) is True
    # 19:00 UTC is past XETRA close (16:30 UTC)
    assert is_open(get_meta("5J50.DE"), WED_LSE_CLOSED) is False


def test_weekends_close_equities():
    for ticker in ("NVDA", "VWRP.L", "MAERSK-B.CO", "5J50.DE"):
        assert is_open(get_meta(ticker), SAT) is False, f"{ticker} should be closed Sat"


def test_crypto_always_open():
    assert is_open(get_meta("BTC-USD"), SAT) is True
    assert is_open(get_meta("BTC-USD"), WED_US_CLOSED) is True
    assert is_open(get_meta("BTC-USD"), SUN_EARLY) is True


def test_comex_gold_23h_with_daily_break():
    assert is_open(get_meta("GC=F"), FRI_CLOSE) is False      # Fri 22:30 closed
    assert is_open(get_meta("GC=F"), SAT) is False
    assert is_open(get_meta("GC=F"), SUN_EARLY) is False      # Sun pre-open
    assert is_open(get_meta("GC=F"), SUN_LATE) is True        # Sun 23:30 open
    assert is_open(get_meta("GC=F"), DAILY_BREAK) is False    # Wed 22:30 daily break


def test_filter_open_returns_only_open_tickers():
    tickers = list(ASSETS.keys())
    sat_open = filter_open(tickers, SAT)
    assert sat_open == ["BTC-USD"], f"Sat only BTC, got {sat_open}"
    wed_open = filter_open(tickers, WED_US_OPEN)
    # At 15 UTC Wed, all exchanges should be open
    assert len(wed_open) == 25


# ═══════════════════════════════════════════════════════════════════════════
# YahooEngine tests (offline — graceful degradation when Yahoo unreachable)
# ═══════════════════════════════════════════════════════════════════════════

def test_yahoo_engine_graceful_failure():
    """Sandbox blocks Yahoo → engine must return zero price + stale flag."""
    from yahoo import YahooEngine
    eng = YahooEngine("DOES-NOT-EXIST-XYZ")
    assert eng.current_price == 0.0
    assert eng.is_stale is True
    assert eng.get_bars(limit=5) == []
    q = eng.get_latest_quote()
    assert q["stale"] is True
    assert q["ap"] == 0.0 and q["bp"] == 0.0


def test_yahoo_engine_caches_failed_fetch():
    """Repeated failures should use cache, not retry on every call."""
    from yahoo import YahooEngine
    eng = YahooEngine("DOES-NOT-EXIST-XYZ")
    _ = eng.current_price                    # first fetch attempt
    t0 = time.time()
    for _ in range(5):
        _ = eng.current_price
    elapsed = time.time() - t0
    assert elapsed < 0.05, (
        f"Subsequent calls should be cache hits, not retries (took {elapsed:.3f}s)")


def test_yahoo_engine_quote_fields():
    """Quote shape must match Alpaca-compatible schema."""
    from yahoo import YahooEngine
    eng = YahooEngine("DOES-NOT-EXIST-XYZ")
    q = eng.get_latest_quote()
    assert set(q.keys()) == {"ap", "bp", "as", "bs", "t", "stale"}


# ═══════════════════════════════════════════════════════════════════════════
# Integration: detect_signals should respect market hours + per-asset thresholds
# ═══════════════════════════════════════════════════════════════════════════

def test_detect_signals_honours_market_hours_and_thresholds(monkeypatch):
    """Mock httpx to return fixed bars; ensure crew.detect_signals:
       * skips tickers whose exchange is closed
       * applies the ticker's own threshold, not the global fallback
    """
    from fund import crew

    # Force the scan to happen on a Saturday so only BTC is "open"
    monkeypatch.setattr(crew, "filter_open",
                        lambda tickers, now=None: ["BTC-USD"])

    # Fabricate a bars response with a 5% move (> BTC's 4% threshold)
    class _MockResp:
        def json(self):
            return {"bars": [{"c": 100.0}] * 30 + [{"c": 105.0}]}

    class _MockClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *args, **kwargs): return _MockResp()

    monkeypatch.setattr(crew.httpx, "Client",
                        lambda *a, **kw: _MockClient())
    # assets_list is a @property derived from ASSETS; patch the raw string
    monkeypatch.setattr(crew.settings, "ASSETS", "NVDA,VWRP.L,BTC-USD")

    signals = crew.detect_signals()
    # Only BTC-USD should produce a signal (5% > 4% threshold, others filtered out)
    assert len(signals) == 1
    assert signals[0].symbol == "BTC-USD"
    assert abs(signals[0].change_pct - 0.05) < 1e-6


def test_detect_signals_rejects_moves_below_threshold(monkeypatch):
    """A 0.5% move on VWRP.L (threshold 1%) should NOT produce a signal."""
    from fund import crew

    monkeypatch.setattr(crew, "filter_open",
                        lambda tickers, now=None: ["VWRP.L"])

    class _MockResp:
        def json(self):
            return {"bars": [{"c": 100.0}] * 30 + [{"c": 100.5}]}   # 0.5%

    class _MockClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *args, **kwargs): return _MockResp()

    monkeypatch.setattr(crew.httpx, "Client",
                        lambda *a, **kw: _MockClient())
    monkeypatch.setattr(crew.settings, "ASSETS", "VWRP.L")

    signals = crew.detect_signals()
    assert signals == [], "0.5% move should not signal on 1% threshold"

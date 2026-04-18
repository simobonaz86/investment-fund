"""Market hours gate.

Decides whether each exchange is "open enough" for momentum scanning.
We don't try to track every bank holiday — Yahoo returns stale bars on
closed days and `YahooEngine.is_stale` catches that case. This module
just filters out obvious out-of-hours scans to save Yahoo rate-limit.

All times are UTC for consistency (BST/CET drift handled implicitly by
the equity hours being specified in local-exchange terms → UTC offset).
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import NamedTuple

from fund.assets import ASSETS, AssetMeta


class Window(NamedTuple):
    open_utc: time
    close_utc: time
    weekend: bool   # True if weekends are also open (crypto)


# ═══════════════════════════════════════════════════════════════════════════
# Exchange calendars (standard time; DST drift ignored to keep it simple)
# ═══════════════════════════════════════════════════════════════════════════
#
# These windows are a pragmatic superset — we add a 30-minute buffer either
# side of core trading so we don't miss opening/closing auction moves on
# Yahoo's ~15-min delay.

EXCHANGE_HOURS: dict[str, Window] = {
    "NYSE":   Window(time(13, 00), time(21, 30), weekend=False),   # 09:30–16:00 ET
    "NASDAQ": Window(time(13, 00), time(21, 30), weekend=False),
    "LSE":    Window(time(7, 30),  time(17, 00), weekend=False),   # 08:00–16:30 London
    "XETRA":  Window(time(7, 30),  time(16, 30), weekend=False),   # 09:00–17:30 CET
    "CPH":    Window(time(7, 30),  time(16, 30), weekend=False),   # 09:00–17:00 CET
    "COMEX":  Window(time(22, 00), time(22, 00), weekend=False),   # 23h; see is_open()
    "CRYPTO": Window(time(0, 0),   time(23, 59, 59), weekend=True),
}


def is_open(meta: AssetMeta, now: datetime | None = None) -> bool:
    """True if the asset's exchange is within trading hours right now."""
    now = now or datetime.now(timezone.utc)
    w = EXCHANGE_HOURS.get(meta.exchange)
    if not w:
        return True                       # unknown exchange → don't block
    if meta.exchange == "CRYPTO":
        return True                       # 24/7, always open
    if meta.exchange == "COMEX":
        # Globex gold: 23h/day Sun 23:00 UTC → Fri 22:00 UTC, with daily break 22–23 UTC
        if now.weekday() == 5:                                     # Saturday
            return False
        if now.weekday() == 6 and now.time() < time(23, 0):        # Sun pre-open
            return False
        if now.weekday() == 4 and now.time() >= time(22, 0):       # Fri close
            return False
        return not (time(22, 0) <= now.time() < time(23, 0))       # daily break
    # Standard equity exchanges
    if not w.weekend and now.weekday() >= 5:                       # Sat=5 Sun=6
        return False
    t = now.time()
    return w.open_utc <= t <= w.close_utc


def filter_open(tickers: list[str], now: datetime | None = None) -> list[str]:
    """Return the subset of tickers whose exchange is currently open."""
    result = []
    for t in tickers:
        meta = ASSETS.get(t.upper())
        if meta and is_open(meta, now):
            result.append(t)
    return result

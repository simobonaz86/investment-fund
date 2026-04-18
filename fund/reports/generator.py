"""Report generator — daily / weekly / monthly / quarterly / YTD.

Each report includes:
  * P&L (USD + %) over the period
  * Sharpe ratio (daily returns)
  * Max drawdown
  * Benchmark (SYN-A buy-and-hold) comparison
  * Agent cost breakdown by role
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from fund.config import settings
from fund.database import conn, now_iso, save_report


# ── Data helpers ────────────────────────────────────────────────────────────

@dataclass
class PeriodReturns:
    pnl_usd: float
    pnl_pct: float
    sharpe: float | None
    max_drawdown: float | None
    daily_returns: list[float]


def _equity_curve(start_iso: str, end_iso: str) -> list[tuple[str, float]]:
    """Approximate equity curve from orders + last known prices.

    Each order's realized side updates cash; unrealized is mark-to-market at
    end-of-period. Returns [(day_iso, equity_usd), ...] day-end snapshots.
    """
    with conn() as c:
        orders = c.execute(
            """SELECT symbol, side, quantity, fill_price, created_at
               FROM orders WHERE status='filled'
               AND created_at >= ? AND created_at <= ?
               ORDER BY created_at""",
            (start_iso, end_iso),
        ).fetchall()
        positions = c.execute("SELECT symbol, quantity, last_price "
                              "FROM portfolio").fetchall()

    # Walk day by day; crude but sufficient for Phase 2.2 reporting
    start = datetime.fromisoformat(start_iso).date()
    end = datetime.fromisoformat(end_iso).date()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    cash = 0.0  # relative to period start
    holdings: dict[str, float] = {}
    curve = []
    for d in days:
        day_end = datetime(d.year, d.month, d.day, 23, 59, 59,
                           tzinfo=timezone.utc).isoformat(timespec="seconds")
        for o in orders:
            if o["created_at"] > day_end:
                continue
            qty = o["quantity"]
            sign = -1 if o["side"].upper() == "BUY" else 1
            cash += sign * qty * o["fill_price"]
            holdings[o["symbol"]] = holdings.get(o["symbol"], 0) + (
                qty if o["side"].upper() == "BUY" else -qty
            )
        mtm = sum(
            holdings.get(p["symbol"], 0) * p["last_price"]
            for p in positions
        )
        curve.append((d.isoformat(), cash + mtm))
    return curve


def _returns(curve: list[tuple[str, float]]) -> PeriodReturns:
    if len(curve) < 2:
        return PeriodReturns(0.0, 0.0, None, None, [])
    values = [v for _, v in curve]
    start_val = values[0] or 1e-9
    end_val = values[-1]
    pnl_usd = end_val - start_val
    pnl_pct = (end_val - start_val) / abs(start_val) * 100 if start_val else 0

    daily = []
    for i in range(1, len(values)):
        prev = values[i - 1] or 1e-9
        daily.append((values[i] - prev) / abs(prev))

    sharpe = _sharpe(daily) if len(daily) >= 5 else None
    max_dd = _max_drawdown(values)
    return PeriodReturns(pnl_usd, pnl_pct, sharpe, max_dd, daily)


def _sharpe(daily: list[float], rf: float = 0.0) -> float | None:
    if not daily:
        return None
    mean = sum(daily) / len(daily)
    var = sum((r - mean) ** 2 for r in daily) / len(daily)
    std = math.sqrt(var)
    if std == 0:
        return None
    return round((mean - rf) / std * math.sqrt(252), 3)


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return round(max_dd * 100, 3)


def _benchmark_return(start_iso: str, end_iso: str) -> float | None:
    """Buy-and-hold SYN-A return from start to end via market sim."""
    try:
        with httpx.Client(timeout=5.0) as client:
            r1 = client.get(
                f"{settings.MARKET_SIM_URL}/v2/stocks/"
                f"{settings.BENCHMARK_SYMBOL}/quotes/latest"
            )
            end_price = r1.json()["quote"]["ap"]
        # Use start of curve as start price (proxy)
        with conn() as c:
            row = c.execute(
                """SELECT fill_price FROM orders
                   WHERE symbol=? AND created_at >= ?
                   ORDER BY created_at LIMIT 1""",
                (settings.BENCHMARK_SYMBOL, start_iso),
            ).fetchone()
        start_price = row["fill_price"] if row else end_price
        if not start_price:
            return None
        return round((end_price - start_price) / start_price * 100, 3)
    except Exception:
        return None


def _cost_breakdown(start_iso: str, end_iso: str) -> dict[str, float]:
    with conn() as c:
        rows = c.execute(
            """SELECT agent_role, SUM(cost_usd) AS cost
               FROM agent_costs WHERE ts >= ? AND ts <= ?
               GROUP BY agent_role""",
            (start_iso, end_iso),
        ).fetchall()
    return {r["agent_role"]: round(r["cost"], 4) for r in rows}


# ── Period boundaries ───────────────────────────────────────────────────────

def _period_bounds(kind: str, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if kind == "daily":
        start = today
        end = today
    elif kind == "weekly":
        start = today - timedelta(days=today.weekday())
        end = today
    elif kind == "monthly":
        start = today.replace(day=1)
        end = today
    elif kind == "quarterly":
        q_start_month = 3 * ((today.month - 1) // 3) + 1
        start = today.replace(month=q_start_month, day=1)
        end = today
    elif kind == "ytd":
        start = today.replace(month=1, day=1)
        end = today
    else:
        raise ValueError(f"Unknown report kind: {kind}")
    return (
        datetime(start.year, start.month, start.day,
                 tzinfo=timezone.utc).isoformat(timespec="seconds"),
        datetime(end.year, end.month, end.day, 23, 59, 59,
                 tzinfo=timezone.utc).isoformat(timespec="seconds"),
    )


# ── Public API ──────────────────────────────────────────────────────────────

def generate_report(kind: str) -> dict:
    start_iso, end_iso = _period_bounds(kind)
    curve = _equity_curve(start_iso, end_iso)
    ret = _returns(curve)
    bench = _benchmark_return(start_iso, end_iso)
    costs = _cost_breakdown(start_iso, end_iso)

    md = _render_markdown(kind, start_iso, end_iso, ret, bench, costs)
    rid = save_report(
        kind=kind, period_start=start_iso, period_end=end_iso,
        pnl_usd=ret.pnl_usd, pnl_pct=ret.pnl_pct,
        sharpe=ret.sharpe, max_dd=ret.max_drawdown,
        bench_pnl_pct=bench, cost_breakdown=costs, content_md=md,
    )
    return {
        "report_id": rid, "kind": kind, "pnl_usd": ret.pnl_usd,
        "pnl_pct": ret.pnl_pct, "sharpe": ret.sharpe,
        "max_drawdown": ret.max_drawdown, "benchmark_pnl_pct": bench,
        "agent_costs": costs, "markdown": md,
    }


def _render_markdown(kind: str, start: str, end: str, r: PeriodReturns,
                     bench: float | None, costs: dict) -> str:
    lines = [
        f"# {kind.title()} Report",
        f"**Period:** {start[:10]} → {end[:10]}",
        "",
        "## Performance",
        f"- **P&L:** ${r.pnl_usd:,.2f} ({r.pnl_pct:+.2f}%)",
        f"- **Sharpe (ann.):** {r.sharpe if r.sharpe is not None else 'n/a'}",
        f"- **Max drawdown:** {r.max_drawdown if r.max_drawdown is not None else 'n/a'}%",
        f"- **Benchmark (buy-and-hold {settings.BENCHMARK_SYMBOL}):** "
        f"{f'{bench:+.2f}%' if bench is not None else 'n/a'}",
        "",
        "## Agent costs",
    ]
    total = 0.0
    for role, cost in sorted(costs.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {role}: ${cost:.4f}")
        total += cost
    lines.append(f"- **Total:** ${total:.4f}")
    return "\n".join(lines)

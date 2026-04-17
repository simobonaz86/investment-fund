"""
Paper broker tools — used by the Execution Agent.
All orders are simulated; fills are logged to SQLite.
In Phase 4, swap place_paper_order for the real Alpaca REST call.
"""
import json
import os
from datetime import datetime

import httpx
from crewai.tools import tool

from fund.config import settings
from fund.database import upsert_position, adjust_cash, get_cash, read_control
from fund.market_data import get_quote

_SIM_URL = os.getenv("MARKET_SIM_URL", "http://localhost:8001")
_SLIPPAGE = 0.0010    # 10 bp simulated slippage
_TIMEOUT  = 6.0


# ── Tool 1: place a paper order ───────────────────────────────────────────────

@tool("place_paper_order")
def place_paper_order(symbol: str, direction: str, amount_usd: float) -> str:
    """
    Place a simulated (paper) order on the market simulator.
    symbol     : asset ticker — SYN-A, SYN-B, SYN-C, SYN-D, or SYN-E
    direction  : 'BUY' or 'SELL'
    amount_usd : dollar amount to trade (must be <= max_position_usd in config)
    Returns JSON with status, fill_price, quantity, and total_usd.
    Do NOT call this unless the Investment Manager has explicitly approved the trade.
    """
    direction = direction.strip().upper()

    # ── Validation ────────────────────────────────────────────────────────────
    if direction not in ("BUY", "SELL"):
        return json.dumps({"status": "error", "message": "direction must be BUY or SELL"})

    if amount_usd <= 0:
        return json.dumps({"status": "error", "message": "amount_usd must be positive"})

    max_pos = read_control()["max_position_usd"]
    if amount_usd > max_pos:
        return json.dumps({
            "status": "error",
            "message": f"amount_usd {amount_usd:.2f} exceeds max allowed {max_pos:.2f}",
        })

    sym = symbol.strip().upper()

    try:
        # ── Get fill price via adapter (sim or yfinance) ──────────────────────
        quote = get_quote(sym)

        mid_price = (quote["ap"] + quote["bp"]) / 2.0

        # Apply slippage: BUY pays slightly more, SELL gets slightly less
        fill_price = (
            mid_price * (1 + _SLIPPAGE)
            if direction == "BUY"
            else mid_price * (1 - _SLIPPAGE)
        )
        fill_price = round(fill_price, 4)
        quantity   = round(amount_usd / fill_price, 6)
        total_usd  = round(quantity * fill_price, 2)

        # ── Cash check on BUY ─────────────────────────────────────────────────
        if direction == "BUY":
            current_cash = get_cash()
            if current_cash < total_usd:
                return json.dumps({
                    "status":  "rejected",
                    "message": f"insufficient cash: have ${current_cash:.2f}, need ${total_usd:.2f}",
                })

        # ── Update portfolio + cash ───────────────────────────────────────────
        delta = quantity if direction == "BUY" else -quantity
        upsert_position(sym, delta, fill_price)

        cash_delta = -total_usd if direction == "BUY" else total_usd
        new_cash = adjust_cash(cash_delta)

        # ── Build fill record ──────────────────────────────────────────────────
        fill = {
            "status":       "filled",
            "symbol":       sym,
            "direction":    direction,
            "fill_price":   fill_price,
            "quantity":     quantity,
            "total_usd":    total_usd,
            "cash_remaining": round(new_cash, 2),
            "slippage_pct": _SLIPPAGE * 100,
            "filled_at":    datetime.utcnow().isoformat() + "Z",
        }
        return json.dumps(fill)

    except httpx.HTTPStatusError as e:
        return json.dumps({"status": "error", "message": f"HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


# ── Tool 2: read current portfolio ────────────────────────────────────────────

@tool("get_portfolio_state")
def get_portfolio_state() -> str:
    """
    Read the current portfolio: all open positions with P&L.
    Returns JSON with a list of positions and total portfolio value.
    Use this before placing any order to check existing exposure.
    """
    try:
        import sqlite3

        db_path = os.getenv("DB_PATH", settings.db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM portfolio WHERE quantity > 0.0001 ORDER BY symbol"
        ).fetchall()
        conn.close()

        positions = []
        total_value = 0.0

        for row in rows:
            sym = row["symbol"]
            qty = float(row["quantity"])
            avg = float(row["avg_cost"])

            # Fetch current price via adapter
            try:
                q = get_quote(sym)
                price = q["bp"]
            except Exception:
                price = avg   # fallback to cost if data unavailable

            mv  = qty * price
            pnl = mv - (qty * avg)
            total_value += mv

            positions.append({
                "symbol":       sym,
                "quantity":     round(qty, 4),
                "avg_cost":     round(avg, 4),
                "current_price": round(price, 4),
                "market_value": round(mv, 2),
                "unrealised_pnl": round(pnl, 2),
            })

        return json.dumps({
            "cash_balance":  round(get_cash(), 2),
            "positions":     positions,
            "positions_market_value": round(total_value, 2),
            "total_equity":  round(get_cash() + total_value, 2),
            "n_positions":   len(positions),
        })

    except Exception as e:
        return json.dumps({"error": str(e)})

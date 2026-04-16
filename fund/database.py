"""
SQLite persistence layer.
Phase 0 schema: portfolio, orders, manager_decisions, agent_costs.
"""
import os
import sqlite3
from datetime import datetime
from fund.config import settings


def _ensure_dir():
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist.  Safe to call on every startup."""
    _ensure_dir()
    with get_connection() as conn:
        conn.executescript("""
            -- Open positions
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol      TEXT PRIMARY KEY,
                quantity    REAL NOT NULL DEFAULT 0,
                avg_cost    REAL NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL
            );

            -- All filled / attempted orders
            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                direction       TEXT    NOT NULL CHECK(direction IN ('BUY','SELL')),
                quantity        REAL    NOT NULL,
                fill_price      REAL,
                total_usd       REAL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                research_verdict TEXT,
                confidence      REAL,
                created_at      TEXT    NOT NULL,
                filled_at       TEXT
            );

            -- Manager decision log (full audit trail)
            CREATE TABLE IF NOT EXISTS manager_decisions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type      TEXT NOT NULL,
                symbol           TEXT NOT NULL,
                pct_change       REAL,
                research_verdict TEXT,
                confidence       REAL,
                trade_taken      INTEGER DEFAULT 0,   -- 1=yes 0=no
                direction        TEXT,
                size_usd         REAL,
                reason           TEXT,
                fill_price       REAL,
                created_at       TEXT NOT NULL
            );

            -- Agent API cost tracking (for Board spend dashboard)
            CREATE TABLE IF NOT EXISTS agent_costs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name  TEXT NOT NULL,
                task_name   TEXT NOT NULL,
                tokens_in   INTEGER,
                tokens_out  INTEGER,
                cost_usd    REAL,
                created_at  TEXT NOT NULL
            );
        """)
        conn.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def log_decision(
    symbol: str,
    pct_change: float,
    research_verdict: str | None,
    confidence: float | None,
    trade_taken: bool,
    direction: str | None,
    size_usd: float,
    reason: str,
    fill_price: float | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO manager_decisions
               (signal_type, symbol, pct_change, research_verdict, confidence,
                trade_taken, direction, size_usd, reason, fill_price, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "price_momentum", symbol, pct_change, research_verdict, confidence,
                1 if trade_taken else 0, direction, size_usd, reason, fill_price,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()


def get_portfolio() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio WHERE quantity > 0.0001 ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_position(symbol: str, delta_qty: float, fill_price: float) -> None:
    """Add (BUY) or reduce (SELL) a position."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity, avg_cost FROM portfolio WHERE symbol=?", (symbol,)
        ).fetchone()
        now = datetime.utcnow().isoformat()

        if delta_qty > 0:   # BUY
            if row:
                old_qty, old_cost = row["quantity"], row["avg_cost"]
                new_qty = old_qty + delta_qty
                new_avg = (old_qty * old_cost + delta_qty * fill_price) / new_qty
                conn.execute(
                    "UPDATE portfolio SET quantity=?, avg_cost=?, updated_at=? WHERE symbol=?",
                    (new_qty, round(new_avg, 6), now, symbol),
                )
            else:
                conn.execute(
                    "INSERT INTO portfolio (symbol, quantity, avg_cost, updated_at) VALUES (?,?,?,?)",
                    (symbol, delta_qty, fill_price, now),
                )
        else:               # SELL
            new_qty = max(0.0, (row["quantity"] if row else 0.0) + delta_qty)
            if row:
                conn.execute(
                    "UPDATE portfolio SET quantity=?, updated_at=? WHERE symbol=?",
                    (new_qty, now, symbol),
                )

        conn.commit()

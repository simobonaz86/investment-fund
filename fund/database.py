"""
SQLite persistence layer (Phase 1).

New tables vs Phase 0:
  control           single-row runtime state; dashboard writes, fund reads
  agent_costs       every LLM call logged with tokens + USD cost
  signal_cooldowns  per-asset cooldown after a hire
  lessons           post-trade reflection notes for the Manager to read
"""
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from fund.config import settings


# ── Model pricing (USD per 1M tokens) ─────────────────────────────────────────
MODEL_PRICING: dict[str, dict[str, float]] = {
    "anthropic/claude-sonnet-4-6":         {"in":  3.00, "out": 15.00},
    "anthropic/claude-opus-4-7":           {"in": 15.00, "out": 75.00},
    "anthropic/claude-opus-4-6":           {"in": 15.00, "out": 75.00},
    "anthropic/claude-haiku-4-5-20251001": {"in":  1.00, "out":  5.00},
}


def cost_for(model: str, tokens_in: int, tokens_out: int) -> float:
    p = MODEL_PRICING.get(model, {"in": 1.00, "out": 5.00})
    return (tokens_in / 1_000_000) * p["in"] + (tokens_out / 1_000_000) * p["out"]


# ── Connection helpers ───────────────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(settings.db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# ── Schema ───────────────────────────────────────────────────────────────────

def init_db() -> None:
    _ensure_dir()
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol     TEXT PRIMARY KEY,
                quantity   REAL NOT NULL DEFAULT 0,
                avg_cost   REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT    NOT NULL,
                direction        TEXT    NOT NULL CHECK(direction IN ('BUY','SELL')),
                quantity         REAL    NOT NULL,
                fill_price       REAL,
                total_usd        REAL,
                status           TEXT    NOT NULL DEFAULT 'pending',
                research_verdict TEXT,
                confidence       REAL,
                created_at       TEXT    NOT NULL,
                filled_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS manager_decisions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_type       TEXT NOT NULL,
                symbol            TEXT NOT NULL,
                pct_change        REAL,
                specialists_hired TEXT,
                research_verdict  TEXT,
                confidence        REAL,
                trade_taken       INTEGER DEFAULT 0,
                direction         TEXT,
                size_usd          REAL,
                reason            TEXT,
                fill_price        REAL,
                created_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_costs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name  TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                tokens_in   INTEGER NOT NULL,
                tokens_out  INTEGER NOT NULL,
                cost_usd    REAL    NOT NULL,
                task_ref    TEXT,
                created_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_costs_created
                ON agent_costs(created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_costs_agent_created
                ON agent_costs(agent_name, created_at);

            CREATE TABLE IF NOT EXISTS control (
                id                    INTEGER PRIMARY KEY CHECK (id = 1),
                halted                INTEGER NOT NULL DEFAULT 0,
                halt_reason           TEXT,
                assets_str            TEXT NOT NULL,
                momentum_threshold    REAL NOT NULL,
                confidence_threshold  REAL NOT NULL,
                max_position_usd      REAL NOT NULL,
                check_interval_sec    INTEGER NOT NULL,
                cooldown_minutes      INTEGER NOT NULL,
                updated_at            TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signal_cooldowns (
                symbol         TEXT PRIMARY KEY,
                cooldown_until TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lessons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT,
                decision_id INTEGER,
                outcome     TEXT,
                note        TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cash (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                balance    REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Board ↔ Manager chat history
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sender     TEXT NOT NULL,      -- 'board' | 'manager' | 'system'
                kind       TEXT NOT NULL,      -- 'chat' | 'daily' | 'weekly' | 'monthly' | 'quarterly'
                body       TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_created
                ON messages(created_at);

            -- Board directives active in the Manager's system prompt
            CREATE TABLE IF NOT EXISTS directives (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                text       TEXT NOT NULL,
                expires_at TEXT,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            -- Nightly equity snapshots for historical P&L
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                cash         REAL NOT NULL,
                positions_mv REAL NOT NULL,
                total_equity REAL NOT NULL,
                snapshot_at  TEXT NOT NULL
            );
        """)

        existing = conn.execute("SELECT id FROM control WHERE id=1").fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO control
                   (id, halted, assets_str, momentum_threshold, confidence_threshold,
                    max_position_usd, check_interval_sec, cooldown_minutes, updated_at)
                   VALUES (1, 0, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    settings.default_assets_str,
                    settings.default_momentum_threshold,
                    settings.default_confidence_threshold,
                    settings.default_max_position_usd,
                    settings.default_check_interval,
                    settings.default_cooldown_minutes,
                    datetime.utcnow().isoformat(),
                ),
            )

        # Seed starting cash (paper fund) — $100k
        cash_row = conn.execute("SELECT id FROM cash WHERE id=1").fetchone()
        if not cash_row:
            conn.execute(
                "INSERT INTO cash (id, balance, updated_at) VALUES (1, ?, ?)",
                (settings.starting_cash_usd, datetime.utcnow().isoformat()),
            )
        conn.commit()


# ── Control state ────────────────────────────────────────────────────────────

def read_control() -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM control WHERE id=1").fetchone()
        if not row:
            raise RuntimeError("control row missing — run init_db first")
        d = dict(row)
        d["assets"] = [a.strip() for a in d["assets_str"].split(",") if a.strip()]
        return d


def set_halted(halted: bool, reason: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE control SET halted=?, halt_reason=?, updated_at=? WHERE id=1",
            (1 if halted else 0, reason, datetime.utcnow().isoformat()),
        )
        conn.commit()


def update_control(**fields) -> None:
    allowed = {
        "assets_str", "momentum_threshold", "confidence_threshold",
        "max_position_usd", "check_interval_sec", "cooldown_minutes",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Unknown control fields: {bad}")

    sets   = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [datetime.utcnow().isoformat()]

    with get_connection() as conn:
        conn.execute(f"UPDATE control SET {sets}, updated_at=? WHERE id=1", values)
        conn.commit()


# ── Cost tracking ─────────────────────────────────────────────────────────────

def log_cost(agent_name: str, model: str, tokens_in: int, tokens_out: int, task_ref: str = "") -> float:
    cost = cost_for(model, tokens_in, tokens_out)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO agent_costs
               (agent_name, model, tokens_in, tokens_out, cost_usd, task_ref, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (agent_name, model, tokens_in, tokens_out, cost, task_ref,
             datetime.utcnow().isoformat()),
        )
        conn.commit()
    return cost


def weekly_spend(agent_name: Optional[str] = None) -> float:
    since = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with get_connection() as conn:
        if agent_name:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS s "
                "FROM agent_costs WHERE created_at >= ? AND agent_name = ?",
                (since, agent_name),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS s "
                "FROM agent_costs WHERE created_at >= ?",
                (since,),
            ).fetchone()
        return float(row["s"])


def spend_breakdown_last_week() -> dict[str, float]:
    since = (datetime.utcnow() - timedelta(days=7)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT agent_name, COALESCE(SUM(cost_usd),0) AS s "
            "FROM agent_costs WHERE created_at >= ? GROUP BY agent_name",
            (since,),
        ).fetchall()
        return {r["agent_name"]: float(r["s"]) for r in rows}


# ── Cooldown ──────────────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT cooldown_until FROM signal_cooldowns WHERE symbol=?", (symbol,)
        ).fetchone()
        if not row:
            return False
        return datetime.utcnow().isoformat() < row["cooldown_until"]


def set_cooldown(symbol: str, minutes: int) -> None:
    until = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO signal_cooldowns (symbol, cooldown_until)
               VALUES (?, ?)
               ON CONFLICT(symbol) DO UPDATE SET cooldown_until=excluded.cooldown_until""",
            (symbol, until),
        )
        conn.commit()


# ── Lessons ───────────────────────────────────────────────────────────────────

def add_lesson(symbol: str, decision_id: int | None, outcome: str, note: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO lessons (symbol, decision_id, outcome, note, created_at)
               VALUES (?,?,?,?,?)""",
            (symbol, decision_id, outcome, note, datetime.utcnow().isoformat()),
        )
        conn.commit()


def recent_lessons(symbol: str | None = None, limit: int = 5) -> list[dict]:
    with get_connection() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM lessons WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ── Existing helpers ──────────────────────────────────────────────────────────

def log_decision(
    symbol: str,
    pct_change: float,
    specialists_hired: str,
    research_verdict: str | None,
    confidence: float | None,
    trade_taken: bool,
    direction: str | None,
    size_usd: float,
    reason: str,
    fill_price: float | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO manager_decisions
               (signal_type, symbol, pct_change, specialists_hired, research_verdict,
                confidence, trade_taken, direction, size_usd, reason, fill_price, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("price_momentum", symbol, pct_change, specialists_hired, research_verdict,
             confidence, 1 if trade_taken else 0, direction, size_usd, reason, fill_price,
             datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def upsert_position(symbol: str, delta_qty: float, fill_price: float) -> None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity, avg_cost FROM portfolio WHERE symbol=?", (symbol,)
        ).fetchone()
        now = datetime.utcnow().isoformat()

        if delta_qty > 0:
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
        else:
            new_qty = max(0.0, (row["quantity"] if row else 0.0) + delta_qty)
            if row:
                conn.execute(
                    "UPDATE portfolio SET quantity=?, updated_at=? WHERE symbol=?",
                    (new_qty, now, symbol),
                )
        conn.commit()


def get_portfolio() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio WHERE quantity > 0.0001 ORDER BY symbol"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Cash ─────────────────────────────────────────────────────────────────────

def get_cash() -> float:
    with get_connection() as conn:
        row = conn.execute("SELECT balance FROM cash WHERE id=1").fetchone()
        return float(row["balance"]) if row else 0.0


def adjust_cash(delta: float) -> float:
    """Add (positive) or remove (negative) cash. Returns new balance."""
    with get_connection() as conn:
        row = conn.execute("SELECT balance FROM cash WHERE id=1").fetchone()
        if not row:
            raise RuntimeError("cash row missing — run init_db first")
        new_bal = float(row["balance"]) + delta
        conn.execute(
            "UPDATE cash SET balance=?, updated_at=? WHERE id=1",
            (new_bal, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return new_bal


def reset_cash(amount: float) -> float:
    """Reset the paper cash balance (used for a fund reset / testing)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE cash SET balance=?, updated_at=? WHERE id=1",
            (amount, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return amount


# ── Messages (Board ↔ Manager chat) ──────────────────────────────────────────

def add_message(sender: str, body: str, kind: str = "chat") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO messages (sender, kind, body, created_at) VALUES (?,?,?,?)",
            (sender, kind, body, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def recent_messages(limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ── Directives (standing orders for the Manager) ─────────────────────────────

def add_directive(text: str, expires_at: str | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO directives (text, expires_at, active, created_at) VALUES (?,?,1,?)",
            (text, expires_at, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def active_directives() -> list[dict]:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM directives
               WHERE active = 1 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY id DESC""",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def deactivate_directive(directive_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE directives SET active=0 WHERE id=?", (directive_id,))
        conn.commit()


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def save_snapshot(cash: float, positions_mv: float) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO portfolio_snapshots
               (cash, positions_mv, total_equity, snapshot_at)
               VALUES (?,?,?,?)""",
            (cash, positions_mv, cash + positions_mv, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def recent_snapshots(limit: int = 30) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ── Operators chat projection (reads manager_decisions + costs as "threads") ──

def operator_threads(limit: int = 20) -> list[dict]:
    """
    Project the decision log into chat-style threads for the dashboard.
    Each thread is a signal → agent exchange → outcome.
    """
    with get_connection() as conn:
        decisions = conn.execute(
            "SELECT * FROM manager_decisions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

        threads = []
        for d in decisions:
            # Find cost rows for this decision's symbol near the decision time
            costs = conn.execute(
                """SELECT agent_name, model, cost_usd, created_at
                   FROM agent_costs
                   WHERE task_ref LIKE ? AND created_at <= ?
                   ORDER BY id DESC LIMIT 8""",
                (f"%{d['symbol']}%", d["created_at"]),
            ).fetchall()
            threads.append({
                "decision": dict(d),
                "costs":    [dict(c) for c in costs],
            })

        return threads

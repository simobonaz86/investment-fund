"""Phase 2.2 database layer.

Additive schema on top of Phase 2.1. New tables:
  - budget_pools        : monthly allocation per principal (CEO/HR/Kevin)
  - hr_reviews          : weekly HR org recommendations
  - reports             : daily/weekly/monthly/quarterly/YTD report archive
  - kevin_audit_log     : every flag/block/escalate action Kevin takes
  - model_selections    : which model each role is currently running
  - principals_chat     : CEO ↔ Kevin messages (+ HR on weekly cadence)
  - org_state           : current specialist roster (dynamic team)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fund.config import settings

SCHEMA = """
-- ── Phase 0/1/2.1 tables (idempotent) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio (
    symbol TEXT PRIMARY KEY,
    quantity REAL NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL DEFAULT 0,
    last_price REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    fill_price REAL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manager_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    research_verdict TEXT,
    confidence REAL,
    trade_taken INTEGER NOT NULL DEFAULT 0,
    direction TEXT,
    size_usd REAL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_role TEXT NOT NULL,        -- ceo | kevin | hr | research | risk | sentiment | execution | accountant
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    ts TEXT NOT NULL
);

-- ── Phase 2.2 new tables ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS budget_pools (
    month TEXT PRIMARY KEY,           -- 'YYYY-MM'
    ceo_allocated REAL NOT NULL,
    hr_allocated REAL NOT NULL,
    kevin_allocated REAL NOT NULL,
    ceo_spent REAL NOT NULL DEFAULT 0,
    hr_spent REAL NOT NULL DEFAULT 0,
    kevin_spent REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hr_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    ceo_hiring_summary TEXT,          -- JSON: counts + costs per role
    ceo_decision_count INTEGER,
    cost_efficiency REAL,             -- cost per decision
    recommendations TEXT,             -- free-text org advice
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,               -- daily | weekly | monthly | quarterly | ytd
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    pnl_usd REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    sharpe REAL,
    max_drawdown REAL,
    benchmark_pnl_pct REAL,
    agent_cost_breakdown TEXT,        -- JSON {role: cost}
    content_markdown TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kevin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,             -- flag_yellow | flag_red | block_trade | escalate_board | concern
    target_type TEXT,                 -- decision | trade | pattern | model_choice
    target_id TEXT,                   -- e.g. manager_decisions.id or NULL
    surfaced_in_chat INTEGER NOT NULL DEFAULT 0,
    surfaced_in_dashboard INTEGER NOT NULL DEFAULT 0,
    board_notified INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_selections (
    role TEXT PRIMARY KEY,            -- ceo | kevin | hr | specialist_default | specialist_research | etc
    model TEXT NOT NULL,
    selected_by TEXT NOT NULL,        -- board | ceo | system
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS principals_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,             -- ceo | kevin | hr | board
    message TEXT NOT NULL,
    chat_room TEXT NOT NULL DEFAULT 'principals',  -- principals | board
    thread TEXT,                      -- context tag, e.g. 'decision:42'
    read_by_ceo INTEGER NOT NULL DEFAULT 0,        -- only used for board-room messages
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_room_id
    ON principals_chat(chat_room, id);
CREATE INDEX IF NOT EXISTS idx_chat_unread_board
    ON principals_chat(chat_room, sender, read_by_ceo);

CREATE TABLE IF NOT EXISTS org_state (
    role TEXT PRIMARY KEY,            -- research | risk | sentiment | execution | accountant
    status TEXT NOT NULL,             -- active | idle | dismissed
    model TEXT,
    hired_at TEXT,
    last_active_at TEXT
);
"""


@contextmanager
def conn():
    Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate_chat(c)
        _seed_budget_pool(c)
        _seed_model_selections(c)


def _migrate_chat(c: sqlite3.Connection) -> None:
    """Add chat_room/read_by_ceo to existing principals_chat tables (Phase 2.1 → 2.2)."""
    cols = {row["name"] for row in
            c.execute("PRAGMA table_info(principals_chat)").fetchall()}
    if "chat_room" not in cols:
        c.execute("ALTER TABLE principals_chat ADD COLUMN "
                  "chat_room TEXT NOT NULL DEFAULT 'principals'")
    if "read_by_ceo" not in cols:
        c.execute("ALTER TABLE principals_chat ADD COLUMN "
                  "read_by_ceo INTEGER NOT NULL DEFAULT 0")
    # Backfill: anything previously routed via thread='board' should sit in
    # the board room
    c.execute(
        "UPDATE principals_chat SET chat_room='board' "
        "WHERE thread='board' AND chat_room='principals'"
    )


def _seed_budget_pool(c: sqlite3.Connection) -> None:
    """Ensure current month has an allocated budget pool."""
    month = current_month()
    row = c.execute("SELECT 1 FROM budget_pools WHERE month=?", (month,)).fetchone()
    if row:
        return
    c.execute(
        """INSERT INTO budget_pools
           (month, ceo_allocated, hr_allocated, kevin_allocated, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (month, settings.budget_ceo, settings.budget_hr,
         settings.budget_kevin, now_iso()),
    )


def _seed_model_selections(c: sqlite3.Connection) -> None:
    """Board defaults for permanent agents; CEO can override specialist later."""
    defaults = [
        ("ceo",                settings.CEO_MODEL,                "board"),
        ("kevin",              settings.KEVIN_MODEL,              "board"),
        ("hr",                 settings.HR_MODEL,                 "board"),
        ("specialist_default", settings.SPECIALIST_DEFAULT_MODEL, "ceo"),
    ]
    for role, model, sb in defaults:
        c.execute(
            """INSERT OR IGNORE INTO model_selections
               (role, model, selected_by, updated_at) VALUES (?, ?, ?, ?)""",
            (role, model, sb, now_iso()),
        )


# ── Budget helpers ──────────────────────────────────────────────────────────

def record_spend(role: str, model: str, in_tok: int, out_tok: int,
                 cost_usd: float) -> None:
    """Log raw spend + update current-month pool for the relevant pool owner."""
    pool_role = _role_to_pool(role)
    with conn() as c:
        c.execute(
            """INSERT INTO agent_costs
               (agent_role, model, input_tokens, output_tokens, cost_usd, ts)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (role, model, in_tok, out_tok, cost_usd, now_iso()),
        )
        c.execute(
            f"""UPDATE budget_pools
                SET {pool_role}_spent = {pool_role}_spent + ?, updated_at = ?
                WHERE month = ?""",
            (cost_usd, now_iso(), current_month()),
        )


def _role_to_pool(role: str) -> str:
    """Map agent role → budget pool. Specialists charge to CEO pool."""
    role = role.lower()
    if role == "hr":
        return "hr"
    if role == "kevin":
        return "kevin"
    # ceo + all specialists → ceo pool
    return "ceo"


def get_budget_status() -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM budget_pools WHERE month=?", (current_month(),)
        ).fetchone()
    if not row:
        return {}
    return {
        "month": row["month"],
        "ceo":   {"allocated": row["ceo_allocated"],   "spent": row["ceo_spent"]},
        "hr":    {"allocated": row["hr_allocated"],    "spent": row["hr_spent"]},
        "kevin": {"allocated": row["kevin_allocated"], "spent": row["kevin_spent"]},
    }


def budget_remaining(pool: str) -> float:
    s = get_budget_status()
    if not s or pool not in s:
        return 0.0
    return max(0.0, s[pool]["allocated"] - s[pool]["spent"])


# ── Chat (principals + Board rooms) ─────────────────────────────────────────

def post_chat(sender: str, message: str,
              chat_room: str = "principals",
              thread: str | None = None) -> int:
    """Post one message into a single chat room.

    Use post_to_both() if a message belongs in both rooms (e.g. Kevin
    escalations or HR weekly reviews — visible to both Board and principals).
    """
    if chat_room not in ("principals", "board"):
        raise ValueError(f"chat_room must be 'principals' or 'board', got {chat_room!r}")
    with conn() as c:
        cur = c.execute(
            """INSERT INTO principals_chat
               (sender, message, chat_room, thread, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (sender, message, chat_room, thread, now_iso()),
        )
        return cur.lastrowid


def post_to_both(sender: str, message: str,
                 thread: str | None = None) -> tuple[int, int]:
    """Convenience: write the same message to both rooms (returns both IDs)."""
    return (
        post_chat(sender, message, "principals", thread),
        post_chat(sender, message, "board", thread),
    )


def recent_chat(limit: int = 50, chat_room: str | None = None) -> list[dict]:
    """Most recent messages, optionally filtered by room."""
    with conn() as c:
        if chat_room is None:
            rows = c.execute(
                "SELECT * FROM principals_chat ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM principals_chat WHERE chat_room=? "
                "ORDER BY id DESC LIMIT ?",
                (chat_room, limit),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Board → CEO inbox ───────────────────────────────────────────────────────

def unread_board_for_ceo(limit: int = 20) -> list[dict]:
    """Board messages the CEO hasn't read yet, oldest first."""
    with conn() as c:
        rows = c.execute(
            """SELECT * FROM principals_chat
               WHERE chat_room='board' AND sender='board' AND read_by_ceo=0
               ORDER BY id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_board_read(message_ids: list[int]) -> int:
    """Mark a batch of Board → CEO messages as read."""
    if not message_ids:
        return 0
    placeholders = ",".join("?" * len(message_ids))
    with conn() as c:
        cur = c.execute(
            f"UPDATE principals_chat SET read_by_ceo=1 "
            f"WHERE id IN ({placeholders}) AND chat_room='board'",
            tuple(message_ids),
        )
        return cur.rowcount


# ── Kevin audit ─────────────────────────────────────────────────────────────

def log_kevin_action(action: str, target_type: str, target_id: str | None,
                     reason: str, surfaced_chat: bool,
                     surfaced_dash: bool, board_notified: bool) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO kevin_audit_log
               (action, target_type, target_id, surfaced_in_chat,
                surfaced_in_dashboard, board_notified, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (action, target_type, target_id,
             int(surfaced_chat), int(surfaced_dash),
             int(board_notified), reason, now_iso()),
        )
        return cur.lastrowid


def kevin_unsurfaced() -> list[dict]:
    """Debug gate: Kevin actions NOT surfaced anywhere — these are silent bugs."""
    with conn() as c:
        rows = c.execute(
            """SELECT * FROM kevin_audit_log
               WHERE surfaced_in_chat = 0 AND surfaced_in_dashboard = 0
               AND action IN ('flag_yellow','flag_red','block_trade','escalate_board')
               ORDER BY id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


# ── Model selection ─────────────────────────────────────────────────────────

def get_model(role: str) -> str:
    with conn() as c:
        row = c.execute(
            "SELECT model FROM model_selections WHERE role=?", (role,)
        ).fetchone()
    return row["model"] if row else settings.SPECIALIST_DEFAULT_MODEL


def set_model(role: str, model: str, selected_by: str) -> None:
    """Authority: board may set any; ceo may set only 'specialist_*'."""
    if selected_by == "ceo" and not role.startswith("specialist"):
        raise PermissionError(f"CEO cannot set model for '{role}'")
    with conn() as c:
        c.execute(
            """INSERT INTO model_selections (role, model, selected_by, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(role) DO UPDATE SET
                 model=excluded.model,
                 selected_by=excluded.selected_by,
                 updated_at=excluded.updated_at""",
            (role, model, selected_by, now_iso()),
        )


# ── Org state ───────────────────────────────────────────────────────────────

def upsert_agent(role: str, status: str, model: str | None = None) -> None:
    with conn() as c:
        existing = c.execute(
            "SELECT role FROM org_state WHERE role=?", (role,)
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE org_state SET status=?, model=COALESCE(?, model),
                   last_active_at=? WHERE role=?""",
                (status, model, now_iso(), role),
            )
        else:
            c.execute(
                """INSERT INTO org_state
                   (role, status, model, hired_at, last_active_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (role, status, model, now_iso(), now_iso()),
            )


def current_org() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM org_state WHERE status != 'dismissed'"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Reports ─────────────────────────────────────────────────────────────────

def save_report(kind: str, period_start: str, period_end: str,
                pnl_usd: float, pnl_pct: float, sharpe: float | None,
                max_dd: float | None, bench_pnl_pct: float | None,
                cost_breakdown: dict, content_md: str) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO reports
               (kind, period_start, period_end, pnl_usd, pnl_pct,
                sharpe, max_drawdown, benchmark_pnl_pct,
                agent_cost_breakdown, content_markdown, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, period_start, period_end, pnl_usd, pnl_pct,
             sharpe, max_dd, bench_pnl_pct,
             json.dumps(cost_breakdown), content_md, now_iso()),
        )
        return cur.lastrowid


def list_reports(limit: int = 20) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── HR reviews ──────────────────────────────────────────────────────────────

def save_hr_review(week_start: str, summary: dict, count: int,
                   efficiency: float, recs: str) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO hr_reviews
               (week_start, ceo_hiring_summary, ceo_decision_count,
                cost_efficiency, recommendations, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (week_start, json.dumps(summary), count,
             efficiency, recs, now_iso()),
        )
        return cur.lastrowid


def latest_hr_review() -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM hr_reviews ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None

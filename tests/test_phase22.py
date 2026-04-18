"""Phase 2.2 smoke tests.

Verifies without any LLM calls:
  * Full schema migration
  * Budget pool seeding + spend tracking
  * Kevin action surfacing (all 4 action types)
  * Unsurfaced detection catches silent actions
  * Model authority enforcement (CEO cannot set ceo/kevin/hr)
  * Report period-bounds math
  * HR review gathers data correctly
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Point config at temp DB before importing anything else
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-dummy"
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name

sys.path.insert(0, str(Path(__file__).parent.parent))

from fund import database as db  # noqa: E402
from fund.agents import kevin  # noqa: E402
from fund.reports import generator  # noqa: E402


def reset():
    db_path = Path(os.environ["DB_PATH"])
    # SQLite WAL-mode leaves -wal and -shm sidecar files that survive unlink.
    # Clean them too so state doesn't leak across tests.
    for p in (db_path, db_path.with_suffix(db_path.suffix + "-wal"),
              db_path.with_suffix(db_path.suffix + "-shm")):
        p.unlink(missing_ok=True)
    db.init_db()


def test_schema_and_seeding():
    reset()
    status = db.get_budget_status()
    assert status, "budget pool seeded for current month"
    assert status["ceo"]["allocated"] == 70.0
    assert status["hr"]["allocated"] == 15.0
    assert status["kevin"]["allocated"] == 15.0
    # model selections
    assert db.get_model("ceo").startswith("anthropic/")
    assert db.get_model("kevin").startswith("anthropic/")
    assert db.get_model("hr").startswith("anthropic/")
    print("✓ schema + seeding")


def test_budget_pool_tracking():
    reset()
    db.record_spend("ceo", "m", 100, 50, 0.50)
    db.record_spend("research", "m", 200, 100, 1.00)  # specialist → ceo pool
    db.record_spend("kevin", "m", 50, 25, 0.20)
    db.record_spend("hr", "m", 30, 15, 0.10)
    s = db.get_budget_status()
    assert abs(s["ceo"]["spent"] - 1.50) < 1e-9
    assert abs(s["kevin"]["spent"] - 0.20) < 1e-9
    assert abs(s["hr"]["spent"] - 0.10) < 1e-9
    assert db.budget_remaining("ceo") == round(70 - 1.50, 2)
    print("✓ budget pool tracking")


def test_kevin_actions_all_surface():
    reset()
    f_id = kevin.flag("yellow", "decision", "42", "momentum stale")
    r_id = kevin.flag("red", "decision", "43", "position >10% of book")
    b_id = kevin.block_trade("44", "breaches max position")
    e_id = kevin.escalate_to_board("3 silent rejects in 1h",
                                   "decisions: 41,42,43")
    c_id = kevin.concern("consider dropping SYN-B from universe",
                         "general", None)

    with db.conn() as c:
        for aid in (f_id, r_id, b_id, e_id, c_id):
            row = c.execute(
                "SELECT * FROM kevin_audit_log WHERE id=?", (aid,)
            ).fetchone()
            assert row["surfaced_in_chat"] == 1, \
                f"id {aid} surfaced_in_chat"
            assert row["surfaced_in_dashboard"] == 1, \
                f"id {aid} surfaced_in_dashboard"

        # Principals chat: flag × 2 + block + escalate + concern = 5
        p_count = c.execute(
            "SELECT COUNT(*) AS n FROM principals_chat "
            "WHERE sender='kevin' AND chat_room='principals'"
        ).fetchone()["n"]
        assert p_count == 5, f"expected 5 kevin msgs in principals, got {p_count}"

        # Board chat: block (1) + escalate (1) = 2
        b_count = c.execute(
            "SELECT COUNT(*) AS n FROM principals_chat "
            "WHERE sender='kevin' AND chat_room='board'"
        ).fetchone()["n"]
        assert b_count == 2, f"expected 2 kevin msgs in board, got {b_count}"

    # No unsurfaced
    assert db.kevin_unsurfaced() == []
    print("✓ kevin actions — principals=5, board=2, all audited")


def test_kevin_unsurfaced_detection():
    reset()
    # Simulate a bug — log an action directly with surface flags False
    db.log_kevin_action("flag_red", "decision", "99",
                        "bug: forgot to post to chat",
                        surfaced_chat=False, surfaced_dash=False,
                        board_notified=False)
    silent = db.kevin_unsurfaced()
    assert len(silent) == 1, "silent action detected"
    assert silent[0]["target_id"] == "99"
    print("✓ unsurfaced detection catches silent Kevin actions")


def test_kevin_debug_gate():
    reset()
    result = kevin.debug_gate()
    assert result["ok"], f"debug gate failed: {result.get('failures')}"
    # Debug rows should be cleaned up
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM kevin_audit_log "
            "WHERE target_type='debug_gate'"
        ).fetchone()
        assert row["n"] == 0, "debug rows cleaned up"
    print("✓ kevin debug gate passes + cleans up")


def test_model_authority():
    reset()
    # Board may set principals
    db.set_model("ceo", "anthropic/claude-opus-4-7", "board")
    db.set_model("kevin", "anthropic/claude-haiku-4-5-20251001", "board")
    db.set_model("hr", "anthropic/claude-haiku-4-5-20251001", "board")

    # CEO may set specialist_*
    db.set_model("specialist_research", "anthropic/claude-haiku-4-5-20251001",
                 "ceo")

    # CEO may NOT set principals
    for role in ("ceo", "kevin", "hr"):
        try:
            db.set_model(role, "anthropic/claude-opus-4-7", "ceo")
        except PermissionError:
            continue
        raise AssertionError(f"CEO should not set {role}")
    print("✓ model authority: Board-only for principals; CEO for specialist_*")


def test_report_period_bounds():
    from datetime import datetime, timezone
    now = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)

    for kind in ("daily", "weekly", "monthly", "quarterly", "ytd"):
        s, e = generator._period_bounds(kind, now=now)
        assert s <= e, f"{kind}: start <= end"
        assert s.startswith("2026"), f"{kind}: in 2026"

    s, _ = generator._period_bounds("quarterly", now=now)
    assert s.startswith("2026-04-01"), "Q2 starts April 1"

    s, _ = generator._period_bounds("ytd", now=now)
    assert s.startswith("2026-01-01"), "YTD starts Jan 1"
    print("✓ report period bounds (daily/weekly/monthly/quarterly/ytd)")


def test_report_math():
    curve = [("2026-04-10", 10000.0), ("2026-04-11", 10100.0),
             ("2026-04-12", 10050.0), ("2026-04-13", 10200.0),
             ("2026-04-14", 10150.0), ("2026-04-15", 10300.0),
             ("2026-04-16", 10250.0)]
    r = generator._returns(curve)
    assert abs(r.pnl_usd - 250.0) < 1e-9
    assert abs(r.pnl_pct - 2.5) < 1e-9
    assert r.sharpe is not None
    assert r.max_drawdown > 0
    print(f"✓ report math: pnl={r.pnl_pct:.2f}% sharpe={r.sharpe} "
          f"max_dd={r.max_drawdown}%")


def test_hr_data_gathering():
    reset()
    # Seed some activity
    db.record_spend("ceo", "m", 100, 50, 1.00)
    db.record_spend("research", "m", 200, 100, 2.00)
    db.record_spend("kevin", "m", 50, 25, 0.40)
    with db.conn() as c:
        c.execute("""INSERT INTO manager_decisions
                     (symbol, research_verdict, confidence, trade_taken,
                      direction, size_usd, reason, created_at)
                     VALUES ('SYN-A','BUY',0.85,1,'BUY',500,'ok',?)""",
                  (db.now_iso(),))

    from fund.agents import hr
    data = hr._gather_week_data()
    assert data["decisions"] == 1
    assert data["trades_executed"] == 1
    assert "research" in data["specialists"]
    assert data["specialists"]["research"]["cost_usd"] == 2.0
    assert data["principals"]["ceo"] == 1.0
    print("✓ HR gathers week data correctly")


def test_principals_chat():
    reset()
    db.post_chat("ceo", "proposing BUY SYN-B $800",
                 chat_room="principals", thread="decision:1")
    db.post_chat("kevin", "⚠ momentum weak — concerns noted",
                 chat_room="principals", thread="decision:1")
    db.post_chat("hr", "weekly review posted",
                 chat_room="principals", thread="week:2026-W16")
    msgs = db.recent_chat(limit=10)
    assert len(msgs) == 3
    senders = {m["sender"] for m in msgs}
    assert senders == {"ceo", "kevin", "hr"}
    # Every message must have chat_room field
    assert all(m["chat_room"] == "principals" for m in msgs)
    print("✓ principals chat records CEO / Kevin / HR (chat_room set)")


def test_chat_room_filter():
    reset()
    # Post 2 principals, 3 board
    db.post_chat("ceo", "proposing BUY", chat_room="principals")
    db.post_chat("kevin", "concern", chat_room="principals")
    db.post_chat("board", "stop trading SYN-B", chat_room="board")
    db.post_chat("ceo", "acknowledged", chat_room="board")
    db.post_chat("board", "why?", chat_room="board")

    p = db.recent_chat(limit=50, chat_room="principals")
    b = db.recent_chat(limit=50, chat_room="board")
    both = db.recent_chat(limit=50)

    assert len(p) == 2, f"principals count {len(p)}"
    assert len(b) == 3, f"board count {len(b)}"
    assert len(both) == 5, f"total {len(both)}"
    assert all(m["chat_room"] == "principals" for m in p)
    assert all(m["chat_room"] == "board" for m in b)

    # Bad room rejected
    try:
        db.post_chat("ceo", "x", chat_room="invalid")
        raise AssertionError("should have raised")
    except ValueError:
        pass
    print("✓ chat room filter (principals=2, board=3) + validation")


def test_board_inbox_flow():
    reset()
    # Board posts 3 messages
    m1 = db.post_chat("board", "cut SYN-B exposure", chat_room="board")
    m2 = db.post_chat("board", "what's the Sharpe MTD?", chat_room="board")
    # CEO replies in between (this should NOT count as Board inbox)
    db.post_chat("ceo", "on it", chat_room="board")
    m3 = db.post_chat("board", "also HR cadence?", chat_room="board")

    unread = db.unread_board_for_ceo()
    unread_ids = [m["id"] for m in unread]
    assert unread_ids == [m1, m2, m3], f"got {unread_ids}"
    assert all(m["sender"] == "board" for m in unread)

    # Mark first two read
    n = db.mark_board_read([m1, m2])
    assert n == 2

    still = db.unread_board_for_ceo()
    assert len(still) == 1 and still[0]["id"] == m3

    # Marking already-read is idempotent
    n2 = db.mark_board_read([m1])
    assert n2 == 1, "update still matches row (idempotent)"
    # But unread count stays 1
    assert len(db.unread_board_for_ceo()) == 1
    print("✓ board inbox: unread list, mark_read, idempotent")


def test_board_post_does_not_leak_to_principals():
    reset()
    db.post_chat("board", "restrict to SYN-A", chat_room="board")
    p = db.recent_chat(limit=50, chat_room="principals")
    assert len(p) == 0, "board msg must not appear in principals"
    b = db.recent_chat(limit=50, chat_room="board")
    assert len(b) == 1
    print("✓ board post isolated from principals room")


def test_post_to_both():
    reset()
    p_id, b_id = db.post_to_both("hr", "weekly review posted",
                                 thread="week:2026-W16")
    assert p_id != b_id
    p = db.recent_chat(limit=50, chat_room="principals")
    b = db.recent_chat(limit=50, chat_room="board")
    assert len(p) == 1 and len(b) == 1
    assert p[0]["message"] == b[0]["message"] == "weekly review posted"
    assert p[0]["thread"] == b[0]["thread"] == "week:2026-W16"
    print("✓ post_to_both writes one row per room, same content")


def test_ceo_decision_broadcasts():
    reset()
    from fund.agents import ceo
    # Each helper should post a CEO message to principals only
    ceo.announce_decision(42, "SYN-B", "BUY", 800.0, "3% momentum")
    ceo.announce_hold(43, "SYN-A", "confidence too low")
    ceo.announce_hire("research", "anthropic/claude-haiku-4-5-20251001",
                      "on-demand deep dive")
    ceo.announce_dismiss("research", "verdict returned")
    ceo.acknowledge_kevin("decision:42", "trimmed size to $500")

    p = db.recent_chat(limit=50, chat_room="principals")
    b = db.recent_chat(limit=50, chat_room="board")
    assert len(p) == 5, f"expected 5 CEO msgs in principals, got {len(p)}"
    assert len(b) == 0, "CEO announcements should NOT leak to board"
    assert all(m["sender"] == "ceo" for m in p)

    # Check threading is preserved
    threads = {m["thread"] for m in p}
    assert "decision:42" in threads
    assert "decision:43" in threads
    assert "hire:research" in threads
    print("✓ ceo.announce_* broadcasts 5 decisions to principals only")


def test_process_board_inbox_noop_when_empty():
    reset()
    from fund.agents import ceo
    # No messages → should be a no-op without LLM call
    result = ceo.process_board_inbox()
    assert result["processed"] == 0
    assert result["reply_id"] is None
    print("✓ ceo.process_board_inbox returns no-op on empty inbox")


if __name__ == "__main__":
    test_schema_and_seeding()
    test_budget_pool_tracking()
    test_kevin_actions_all_surface()
    test_kevin_unsurfaced_detection()
    test_kevin_debug_gate()
    test_model_authority()
    test_report_period_bounds()
    test_report_math()
    test_hr_data_gathering()
    test_principals_chat()
    test_chat_room_filter()
    test_board_inbox_flow()
    test_board_post_does_not_leak_to_principals()
    test_post_to_both()
    test_ceo_decision_broadcasts()
    test_process_board_inbox_noop_when_empty()
    print("\nALL PHASE 2.2 TESTS PASSED")

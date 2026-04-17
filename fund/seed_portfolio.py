"""
Seed the paper fund with the Board's real portfolio positions.
Run once after the DB is initialised:

    python -m fund.seed_portfolio

Idempotent — safe to re-run; skips symbols that already have positions.
"""
import logging
import sys

from fund.database import get_connection, init_db, reset_cash, get_cash

log = logging.getLogger(__name__)

# ── Real portfolio (from Board's statement) ───────────────────────────────────
# (symbol, name, market_value_gbp, avg_cost_estimate_gbp)
POSITIONS: list[tuple[str, str, float, float]] = [
    # ISA / trading account
    ("AMZN",    "Amazon.com Inc",                 3137.66,   184.55),
    ("B5ZX1M7", "Artemis Global Income",         10630.71,     3.60),
    ("EQQQ",    "Invesco EQQQ NASDAQ-100",        6669.46,   476.39),
    ("SPAG",    "iShares Agribusiness",           7161.00,    44.98),
    ("MINE",    "iShares Copper Miners",          2123.25,     7.55),
    ("CS51",    "iShares Core EURO STOXX 50",     4387.68,   195.50),
    ("SSAC",    "iShares MSCI ACWI",             13459.35,    80.67),
    ("SGLN",    "iShares Physical Gold ETC",     40778.10,    71.16),
    ("IGUS",    "iShares S&P 500 GBP Hedged",     3549.26,   152.04),
    ("BCOG",    "L&G All Commodities",           15305.36,    14.70),
    ("MUT",     "Murray Income Trust",            7290.45,     9.13),
    ("VMIG",    "Vanguard FTSE 250",              6331.01,    41.86),
    ("VWRP",    "Vanguard FTSE All-World",       55152.42,   125.73),
    ("VUSA",    "Vanguard S&P 500",              15714.77,    93.85),
    ("AGAP",    "WisdomTree Agriculture",        10257.25,     4.72),
    ("COPB",    "WisdomTree Copper",              2328.00,    37.18),
    # Additional holdings (estimated averages)
    ("TSM",     "Taiwan Semiconductor",          10000.00,   180.00),
    ("NVDA",    "NVIDIA Corp",                    6974.00,   135.00),
    ("5J50",    "iShares MSCI China",             5111.00,     6.50),
    ("VUAA",    "Vanguard S&P 500 USD Acc",       4640.00,    95.00),
    ("GOOGL",   "Alphabet Inc",                   4210.00,   150.00),
    ("META",    "Meta Platforms",                 4027.00,   450.00),
    ("DP4B",    "Xtrackers MSCI EM",              2996.00,    58.00),
    ("EUDF",    "iShares EUR Div",                1731.00,    43.00),
    ("XAU",     "Gold (additional)",             12745.00,  1900.00),
    ("BTC",     "Bitcoin",                        8746.00, 47000.00),
]

STARTING_CASH_GBP = 100_000.0


def seed(force: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    init_db()

    with get_connection() as conn:
        existing = {
            r["symbol"] for r in
            conn.execute("SELECT symbol FROM portfolio WHERE quantity > 0.0001").fetchall()
        }

    if existing and not force:
        log.info("Portfolio already has %d positions: %s", len(existing), existing)
        log.info("Pass force=True to overwrite, or run with --force")
        return

    with get_connection() as conn:
        if force:
            conn.execute("DELETE FROM portfolio")

        for symbol, name, mv_gbp, avg_cost in POSITIONS:
            # Quantity derived from MV / avg_cost
            quantity = round(mv_gbp / avg_cost, 6) if avg_cost > 0 else 1.0
            conn.execute(
                """INSERT OR REPLACE INTO portfolio
                   (symbol, quantity, avg_cost, updated_at)
                   VALUES (?,?,?, datetime('now'))""",
                (symbol, quantity, avg_cost),
            )
            log.info("  %-8s  %s qty=%.4f  avg=£%.2f  mv=£%.2f", symbol, name[:30], quantity, avg_cost, mv_gbp)
        conn.commit()

    reset_cash(STARTING_CASH_GBP)
    log.info("Cash reset to £%.2f", get_cash())
    log.info("Seeded %d positions", len(POSITIONS))


if __name__ == "__main__":
    force = "--force" in sys.argv
    seed(force=force)

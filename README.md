# Investment Fund — Phase 0

Autonomous multi-agent investment fund built with CrewAI + Claude.
Phase 0 scope: Investment Manager · Research Analyst · Execution Agent · simulated market data.

---

## Quick start (local, two terminals)

```bash
# 1. Install
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY

# 3. Start market simulator  (Terminal 1)
cd market_sim
python main.py                    # http://localhost:8001

# 4. Run trading loop  (Terminal 2)
cd ..
python run.py
```

## Quick start (Docker)

```bash
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY

docker compose up --build
```

---

## How Phase 0 works

```
Every 60 s
  ↓
detect_signals()          Scan SYN-A, SYN-B, SYN-C for moves ≥ 3%
  ↓ (signal found)
Phase 1 Crew
  Research Analyst  →  get_price_bars + calculate_indicators → VERDICT / CONFIDENCE
  Investment Manager → checks portfolio → TRADE: YES|NO

  ↓ (TRADE: YES, confidence ≥ 0.70)
Phase 2 Crew
  Execution Agent   →  place_paper_order → STATUS / FILL_PRICE / QUANTITY

  ↓
log_decision()            Audit trail → data/fund.db (manager_decisions table)
```

The two-phase design means the Execution Agent is never instantiated unless the
Manager explicitly approves.  This maps directly to the "hiring" spec.

---

## Project layout

```
investment_fund/
├── market_sim/
│   ├── gbm.py          GBM price engine (drift + vol configurable per asset)
│   └── main.py         FastAPI server — Alpaca-compatible schema
├── fund/
│   ├── config.py       All settings from .env (pydantic-settings)
│   ├── database.py     SQLite: portfolio, orders, manager_decisions, agent_costs
│   ├── tools/
│   │   ├── market.py   get_price_bars, calculate_indicators
│   │   └── broker.py   place_paper_order, get_portfolio_state
│   ├── agents/
│   │   ├── research.py   Research Analyst
│   │   ├── execution.py  Execution Agent
│   │   └── manager.py    Investment Manager
│   └── crew.py         Signal detection + two-phase trading loop
└── run.py              Entry point
```

---

## Phase 0 gate check (go/no-go)

Run the simulator and fund for 10 cycles, then query the DB:

```bash
sqlite3 data/fund.db "
  SELECT symbol, research_verdict, confidence, trade_taken, direction, reason
  FROM manager_decisions
  ORDER BY created_at DESC LIMIT 10;
"
```

Gate passes when:
- Every row where `trade_taken = 1` has a `research_verdict` of BUY or SELL
- Every row where `trade_taken = 0` has either HOLD verdict or confidence < 0.70
- No `trade_taken = 1` rows with `confidence < 0.70`

If the above holds: the Manager is correctly requiring Research before every trade.
Move to Phase 1 (Risk Manager + Alpaca paper API).

---

## Swapping to real market data (Phase 2)

1. Set `MARKET_SIM_URL=https://data.alpaca.markets` in `.env`
2. Add `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` to `.env`
3. Update `fund/tools/market.py` to add Alpaca auth headers — no agent code changes
4. Remove the `market_sim` service from `docker-compose.yml`

The `/v2/stocks/{symbol}/bars` and `/v2/stocks/{symbol}/quotes/latest` endpoint
shapes are identical between the simulator and real Alpaca, by design.

---

## Tuning

| Parameter             | Default | Effect                                              |
|-----------------------|---------|-----------------------------------------------------|
| `MOMENTUM_THRESHOLD`  | 0.03    | Lower → more signals, higher agent costs            |
| `CONFIDENCE_THRESHOLD`| 0.70    | Lower → more trades, higher risk                    |
| `MAX_POSITION_USD`    | 1000    | Hard per-trade cap enforced in broker tool          |
| `CHECK_INTERVAL_SECONDS` | 60   | Lower → more reactive, more API calls              |
| `SPECIALIST_MODEL`    | Haiku   | Upgrade to Sonnet for better research quality       |

---

## Kill switch

The Board kill switch is Phase 1 scope. For Phase 0 emergency stop:

```bash
docker compose stop fund     # stops trading loop immediately
# or: Ctrl-C in the terminal running run.py
```

Pending orders: there are none in Phase 0 — each order fills synchronously
before the loop sleeps. No open orders can be left dangling.

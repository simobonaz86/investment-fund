# Investment Fund — Phase 2.2

Phase 2.2 = Phase 2.1 + HR + scheduled reports + Kevin debug gate +
model selector UI + budget pools (70/15/15) + Board dashboard.

## What's new vs Phase 2.1

| Feature | What it does |
| --- | --- |
| **HR agent** | Weekly only (Mon 09:00 UTC). Reviews CEO hiring patterns + cost efficiency, posts org recommendations to Board. **Advisory only — does NOT hire/fire/block.** |
| **Budget pools 70/15/15** | Monthly LLM budget split: 70% CEO (incl. all specialists), 15% HR, 15% Kevin. Hard tracked in `budget_pools` table. |
| **Kevin hardened** | All 4 actions (`flag_yellow`, `flag_red`, `block_trade`, `escalate_board`) GUARANTEE chat + dashboard surfacing. Startup self-test (`debug_gate`) verifies every action wires end-to-end. |
| **Model selector UI** | Board sets CEO / Kevin / HR / `specialist_default` models; CEO sets `specialist_*` models. Authority enforced in `database.set_model()` — CEO cannot override principals. |
| **Scheduled reports** | Daily 18:00, Weekly Mon 09:30, Monthly 1st 09:00, Quarterly Jan/Apr/Jul/Oct 1st 09:30, YTD 1st 10:00 — all UTC. Each report contains P&L, Sharpe, max drawdown, benchmark vs `SYN-A`, agent cost breakdown by role. |
| **Board dashboard** | FastAPI on port 8080. Live budget bars, model selector, principals' chat feed, Kevin audit log with silent-bug warning, reports table, ad-hoc HR/report triggers. |
| **24/7 scanning** | Market hours deferred. Scan runs continuously. |

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     Board Dashboard :8080                    │
│  budget │ models │ chat │ Kevin audit │ reports │ HR trigger │
└──────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┴─────────────────┐
            │                                   │
        SQLite DB                       APScheduler
        /data/fund.db                   • HR weekly
        ───────────                     • Daily report
        budget_pools                    • Weekly/Monthly/Q/YTD
        manager_decisions
        agent_costs                     ┌──────────────────┐
        kevin_audit_log     ◄───────── │  Fund container  │
        principals_chat                 │  (Phase 2.1 loop) │
        hr_reviews                      │  CEO + Kevin      │
        reports                         └──────────────────┘
        model_selections                         │
        org_state                                ▼
                                         market_sim :8001
```

## Files added (vs Phase 2.1)

```
fund/
├── agents/
│   ├── kevin.py          ← hardened, with surfacing + debug_gate
│   └── hr.py             ← NEW, weekly cadence, advisory
├── reports/
│   ├── __init__.py       ← NEW
│   └── generator.py      ← NEW, P&L/Sharpe/DD/benchmark/cost
├── dashboard/
│   ├── __init__.py       ← NEW
│   ├── __main__.py       ← NEW, uvicorn launcher + scheduler
│   ├── app.py            ← NEW, FastAPI routes
│   ├── templates/
│   │   └── index.html    ← NEW
│   └── static/
│       ├── app.css       ← NEW, dark navy
│       └── app.js        ← NEW, polls every 5s
├── scheduler.py          ← NEW, APScheduler cron jobs
├── config.py             ← extended (HR, budget split, schedules)
└── database.py           ← +6 tables: budget_pools, hr_reviews,
                            reports, kevin_audit_log,
                            model_selections, principals_chat,
                            org_state

Dockerfile.dashboard      ← NEW
docker-compose.yml        ← +dashboard service
tests/test_phase22.py     ← NEW, 10 smoke tests
```

## Deploy on Sentinel

You already have Phase 2.1 running at `/opt/investment-fund`. To layer 2.2:

```bash
cd /opt/investment-fund

# Stop current stack
docker compose down

# Pull Phase 2.2 (assumes the tarball is uploaded)
tar -xzf /tmp/investment_fund_phase22.tar.gz --strip-components=1

# Append the 4 new env vars to the existing .env
cat >> .env << 'EOF'
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
MONTHLY_BUDGET_USD=100.0
KEVIN_DEBUG_GATE=true
EOF

# Build + start
docker compose up -d --build
docker compose ps
```

Then open the dashboard:
- Tailscale: `http://100.102.177.53:8080`
- Public:    `http://5.161.54.186:8080`

Verify Kevin's debug gate passed in the dashboard logs:
```bash
docker compose logs dashboard | grep "debug gate"
# expect: "Kevin debug gate OK"
```

## Tests

```bash
python tests/test_phase22.py
```

10 tests, all run without LLM calls (use temp SQLite). Verifies:
schema, budget pools, all 4 Kevin actions surface, silent-action
detection, debug gate, model authority, report period math + Sharpe,
HR data gathering, principals chat.

## Phase 2.2 gate check

Before declaring 2.2 done, all 5 must hold:

1. `docker compose logs dashboard` shows "Kevin debug gate OK"
2. Dashboard accessible at port 8080 over Tailscale
3. `POST /api/hr/run` produces a row in `hr_reviews` and a chat msg from `hr`
4. `POST /api/reports/run {"kind":"daily"}` returns a markdown report
5. `sqlite3 /data/fund.db "SELECT * FROM budget_pools"` shows current month with allocations 70/15/15

## What's NOT in 2.2 (deferred)

- Market hours enforcement (24/7 for now — Phase 2.3)
- HR with hire/fire authority (stays advisory)
- LangGraph migration (Phase 3)
- Live trading with real capital (Phase 4)

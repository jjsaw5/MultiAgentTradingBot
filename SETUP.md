# Setup

The system runs end to end with **zero credentials**. Start there, confirm the
pipeline works, then add real data sources one at a time.

## Requirements

- Python 3.11 or newer
- Optional: PostgreSQL 14+ (SQLite is the default)
- Optional: Docker

---

## Install

```bash
git clone https://github.com/jjsaw5/MultiAgentTradingBot.git
cd MultiAgentTradingBot

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"            # add ",postgres" and/or ",llm" as needed
cp .env.example .env
```

Extras:

| Extra | Adds | When |
| --- | --- | --- |
| `dev` | pytest, ruff, black | Always, for development |
| `postgres` | psycopg | Using PostgreSQL |
| `llm` | anthropic | Using the real LLM reasoning path |

---

## First run

```bash
python run_market_scan.py --stage MARKET_OPEN
```

You should see a market summary, ranked trades with full score breakdowns,
rejected candidates with reasons, and a red banner confirming the data is
synthetic. The run is written to `./data/matb.db`.

Without `--stage` the workflow stage comes from the wall clock. Outside market
hours every candidate is correctly rejected as non-actionable, which is the
system working, not failing — see
[premarket versus live market](ARCHITECTURE.md#premarket-versus-live-market).

Verify the install:

```bash
pytest -q          # 163 tests
ruff check app tests
```

---

## Commands

```bash
matb scan                                  # scan using the live session stage
matb scan --stage PREMARKET                # force premarket
matb scan --universe NVDA,AMD,MSFT         # restrict the universe
matb scan --markdown-out reports_out/x.md  # also write markdown
matb scan --no-save --no-audit             # quick look, nothing persisted

matb config                                # active settings and methodology
matb init-db                               # create tables
matb report <run_id>                       # re-render a stored run
matb decide <recommendation_id> APPROVED --by you --notes "..."
```

`run_market_scan.py` is the same pipeline with a smaller flag set, for when you
want one command and no CLI to learn.

### HTTP API

```bash
uvicorn app.api.main:app --reload
```

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness; confirms order execution is not implemented |
| `GET /config` | Active settings (secrets redacted) and methodology |
| `POST /scans` | Run a scan |
| `GET /runs` | List runs |
| `GET /runs/{id}` | Run detail with recommendations |
| `GET /runs/{id}/audit` | Agent traces, score components, provider calls, quality flags |
| `POST /recommendations/{id}/decision` | Record a human decision |
| `POST /decisions/{id}/execution` | Record a fill **you** made |
| `POST /executions/{id}/result` | Record the outcome |

There is no endpoint that places, modifies, or cancels an order, because no such
capability exists in the codebase.

---

## Configuration

Two files, with distinct jobs.

**`.env`** — environment and credentials. Never committed.

**`config/methodology.yaml`** — the trading rulebook: weights, thresholds,
classification bands, hard-rejection limits, contract preferences, market
schedule. Committed, reviewed, and fingerprinted into every run.

Change methodology by editing the YAML. It is validated on load — unknown keys
are rejected, weights must total 100, bands must descend — so a typo fails
loudly instead of silently reverting to a default.

```bash
matb config    # shows what is actually in force, including the fingerprint
```

---

## Adding real data

Add one provider at a time and compare against a mock run before trusting it.

### Anthropic (LLM reasoning)

```bash
pip install -e ".[llm]"
```
```bash
LLM_BACKEND=anthropic
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-opus-5
```

Without this the agents use their documented heuristic path. Every run records
`reasoning_mode` in `agent_runs`, so an offline scan is never mistaken for a
reasoned one.

### Financial Modeling Prep

```bash
FMP_BACKEND=rest
FMP_API_KEY=your-key
```

Needs a plan covering quotes, historical prices, the earnings calendar, the
economic calendar, stock news, and sector performance.

### Unusual Whales

```bash
UNUSUAL_WHALES_BACKEND=rest
UNUSUAL_WHALES_API_KEY=your-token
```

Optional. Without it the options-flow component scores zero and says so.

### Robinhood

```bash
ROBINHOOD_BACKEND=mcp
```

Requires a runtime that can reach Robinhood's MCP server and inject a
`tool_caller` into `RobinhoodMCPProvider`. A plain background process cannot, so
this stays on `mock` until that runtime exists. Only the read-only tools in
`READ_ONLY_TOOLS` are callable.

---

## PostgreSQL

```bash
pip install -e ".[postgres]"
createdb matb
```
```bash
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/matb
```
```bash
matb init-db
```

The schema is portable — the same SQLAlchemy models run on both engines. The
MVP uses `create_all()`; introduce Alembic before the first schema change that
has to preserve real recorded trades.

---

## Docker

```bash
docker compose up --build        # Postgres + API on :8000
docker compose run --rm api python run_market_scan.py --stage MARKET_OPEN
```

Credentials come from your shell environment or `.env`; nothing is baked into
the image.

---

## Recording what you did

The point of the system is to evaluate the methodology against reality, which
only works if outcomes get recorded.

```bash
matb scan --stage MARKET_OPEN            # note the run_id
matb report <run_id>                     # recommendation_ids
matb decide <recommendation_id> ENTERED --by you --notes "filled at 8.65"
```

Then record the fill and, later, the exit — via the API or directly through
`app/services/decisions.py`. Captured: entry time and price, underlying price,
quantity, stop, target, exit, P&L, and maximum favourable and adverse excursion.

---

## Troubleshooting

**Every candidate is rejected with "Stage is POSTMARKET".** Working as intended
outside market hours. Use `--stage MARKET_OPEN` to see the full path.

**"is not in the synthetic universe".** The mock providers cover twelve tickers
and refuse to invent data for others. Use `--universe` with tickers from
`app/providers/mock_market.py`, or switch to a live backend.

**`ENABLE_ORDER_EXECUTION=true` raises at startup.** Also intended. The system
implements no order submission; the flag exists so that any future execution
module is fail-closed by default.

**Scores differ between runs on the same day.** They should not with a fixed
`MOCK_SEED`. If they do, check that `--trading-day` is pinned — the synthetic
world is seeded by `(MOCK_SEED, trading_day)`.

**Provider unavailable warnings.** Expected when a backend has no credentials.
The run continues; the affected component scores zero and records why, rather
than assuming neutrality.

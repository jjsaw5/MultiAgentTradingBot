# Multi-Agent Options Trading Research System

A research pipeline that continuously studies market conditions, generates
options trade ideas, validates them against hard market data, scores them with
deterministic rules, and presents a ranked, fully auditable report for **human**
review.

> **This system does not trade.** It contains no order-execution code path, and
> a startup assertion refuses to boot any data provider that exposes an
> order-placing method. Every recommendation requires a human decision.

---

## The design principle

The whole architecture follows one rule:

```
AI generates hypotheses.
APIs provide evidence.
Code validates and scores.
Human makes the final trading decision.
```

LLMs are used for research, interpretation, reasoning, catalyst identification,
hypothesis generation, and explanation. They are **not** used to assign the
final confidence number. That comes from `app/scoring/`, in pure Python, from
measured data.

This is enforced structurally rather than by instruction:

- `TradeCandidate` has **no** numeric confidence field. A model cannot anchor
  the score because there is nowhere to put a number.
- `ValidatorAssessment` — the only thing the validating LLM emits — contains no
  numeric fields at all. Prices, greeks, and open interest are fetched by code
  and attached as facts.
- Every point awarded is stored with the measurement that produced it, so any
  score can be re-derived from the database alone.

---

## Pipeline

```
Orchestrator  (run_id, stage = PREMARKET | MARKET_OPEN)
      │
      ├─ Agent 1  Market Intelligence   →  MarketBrief
      ├─ Agent 2  Opportunity Generator →  TradeCandidate[]  (≤10, may be empty)
      ├─ Agent 3  Trade Validator       →  ValidationReport[]
      │
      ├─ Contract Selection   (deterministic)  →  ProposedTrade
      ├─ Risk / Reward Model  (deterministic)  →  RiskReward
      ├─ Hard Rejection Rules (deterministic)  →  vetoes
      ├─ Scoring Engine       (deterministic)  →  ScoreBreakdown /100
      │
      └─ Ranked Report  →  Console / Markdown  →  PostgreSQL / SQLite
                                                        │
                                        Human decision → execution → outcome
```

---

## Quick start

Runs end to end with **zero credentials** using deterministic mock providers.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

python run_market_scan.py --stage MARKET_OPEN
```

You will get a market summary, ranked trades with a full score breakdown,
rejected candidates with the reason each failed, and a run persisted to SQLite.

```bash
matb scan --stage MARKET_OPEN     # same thing, more options
matb config                        # active methodology and backends
matb report <run_id>               # re-render a stored run
matb decide <reco_id> APPROVED     # record a human decision
uvicorn app.api.main:app --reload  # HTTP API on :8000
```

See [SETUP.md](SETUP.md) for the full installation, Postgres, Docker, and
credential walkthrough.

---

## What it produces

```
╭──── #1  AMD  BULL CALL SPREAD   74/100 -- Good candidate ────╮
│ Underlying     158.23        Expiration   2026-10-09 (62 DTE) │
│ Long strike    160.00 CALL   Short strike 180.00 CALL         │
│ Debit          8.70 conservative / 8.40 mid                   │
│ Max loss       $870          Max profit   $1,130              │
│ Breakeven      168.70 (6.62% move)   Reward/risk  1.12        │
╰───────────────────────────────────────────────────────────────╯
  Catalyst          EARNINGS: AMD scheduled earnings on 2026-09-17
  Options flow      Directional premium aligned with the thesis is 67% …
  Invalidation      A daily close below 157.16 invalidates the thesis.

  technical_setup   17.0/20
      +6.0  trend_alignment          [price=158.20 sma20=153.92 sma50=150.09]
      +4.0  key_level_respected      [support=157.16, 0.66% away, holding]
      +3.0  relative_volume          [rvol=2.42]
      +4.0  momentum                 [RSI14=58.1 zone=[45,70], MACD confirming]
      -3.0  blocking_level_proximity [resistance=159.19 only 0.63% away]
      +3.0  expected_move_feasible   [16.0% vs 28.4% ATR-projected, ratio 0.56]
```

Every point is traceable to a named rule and the value that triggered it. That
is the point of the exercise: a ranked list you cannot audit tells you nothing
about whether the methodology works.

---

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, agent boundaries, database schema, extension points |
| [METHODOLOGY.md](METHODOLOGY.md) | How a trade is found, validated, structured, and judged |
| [SCORING.md](SCORING.md) | Every scoring rule and hard-rejection rule, with its configuration key |
| [DATA_SOURCES.md](DATA_SOURCES.md) | FMP, Unusual Whales, Robinhood, news — what each supplies and what is mocked |
| [SETUP.md](SETUP.md) | Installation, configuration, credentials, Docker, running |

---

## Safety controls

| Requirement | How it is enforced |
| --- | --- |
| No autonomous order execution | No execution module exists; `ENABLE_ORDER_EXECUTION=true` raises at startup |
| No order tools exposed to agents | `assert_no_execution_surface()` at provider wiring; MCP tools restricted to a read-only allowlist |
| Never fabricate market data | Missing values stay `None` plus a `MissingData` record; mock providers raise on unknown tickers |
| Clearly mark stale data | `stale` flags, session-aware staleness limits, `STALE_QUOTES` hard rejection outside market hours |
| Preserve data timestamps | Every value carries `Provenance` with `as_of` and `retrieved_at` |
| Treat missing data as missing | Scoring rules record `unscored_due_to_missing_data` and award zero — never a default |
| Require human review | The report is the terminal output; decisions are recorded, never taken |
| Keep credentials out of source | `.env` only, `SecretStr`, `safe_dict()` for logging |
| Never log secrets | structlog redaction processor; provider audit records strip key parameters |
| Explainable and auditable | Every point stored with its measurement; methodology snapshotted per run |

These are covered by tests in `tests/test_safety.py`, including a structural
sweep of the whole package for order-submitting functions.

---

## Status

**Milestone 1: complete.** A working vertical slice — market intelligence,
candidate generation, validation, deterministic scoring, ranked report, and
database persistence — running end to end, with 227 tests covering scoring
rules, hard rejections, risk arithmetic, indicators, provider mappings, safety
invariants, and the full pipeline.

**Verified against live credentials:** Financial Modeling Prep and Unusual
Whales both work end to end against real keys — including news, so no separate
newswire subscription is needed — and the Robinhood MCP mapping is pinned
against recorded live responses. Each integration is covered by fixture tests
built from real payloads.

Not yet built: the Risk Reviewer agent, the screening funnel, automatic
shadow-tracking of recommendations, the performance analytics engine, and
scheduling. See [ARCHITECTURE.md](ARCHITECTURE.md#roadmap) for what comes next.

## Licence

Private project. Not investment advice.

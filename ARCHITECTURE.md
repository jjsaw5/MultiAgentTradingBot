# Architecture

## Contents

- [Design principle, enforced structurally](#design-principle-enforced-structurally)
- [Component map](#component-map)
- [Data flow](#data-flow)
- [Agent boundaries](#agent-boundaries)
- [Premarket versus live market](#premarket-versus-live-market)
- [Provider layer](#provider-layer)
- [Determinism and reproducibility](#determinism-and-reproducibility)
- [Database schema](#database-schema)
- [Observability](#observability)
- [Extension points](#extension-points)
- [Design decisions and their trade-offs](#design-decisions-and-their-trade-offs)
- [Known weaknesses](#known-weaknesses)
- [Roadmap](#roadmap)

---

## Design principle, enforced structurally

> AI generates hypotheses. APIs provide evidence. Code validates and scores.
> Human makes the final decision.

Stating that principle in a prompt is not enough — a model asked for a
"confidence" will produce one, and it will look authoritative. The separation is
therefore built into the type system:

| Boundary | Enforcement |
| --- | --- |
| Agents cannot score | `TradeCandidate` has no numeric confidence field. `tests/test_safety.py` asserts this. |
| Agents cannot supply market data | `ValidatorAssessment` — the validator's entire output schema — has no numeric fields. Prices and greeks are fetched by code and attached. |
| Agents cannot pass prose | Every inter-agent message is a validated Pydantic model with enum-constrained fields. |
| Agents cannot trade | No execution module exists. `assert_no_execution_surface()` rejects any provider with an order method, at wiring time. |
| Scoring cannot hide its reasoning | Every `ScoreReason` requires a `measurement` string. A test asserts no point is awarded without one. |

---

## Component map

```
app/
├── config/
│   ├── settings.py          Environment + credentials (SecretStr, safe_dict)
│   └── methodology.py       Typed, frozen, fingerprinted trading rulebook
├── models/                  Pydantic domain models — the contracts between stages
│   ├── enums.py             Controlled vocabularies
│   ├── common.py            Provenance, Observed, MissingData, DataQualityFlag
│   ├── market_brief.py      Agent 1 output
│   ├── trade_candidate.py   Agent 2 output
│   ├── validation.py        Agent 3 output
│   ├── market_data.py       Measured facts (quotes, chains, flow, technicals)
│   ├── trade_structure.py   Legs, defined-risk arithmetic
│   ├── scoring.py           ScoreReason → ScoreComponent → ScoreBreakdown
│   └── report.py            The human-facing artefact
├── providers/               One interface per data role
│   ├── base.py              ABCs, request auditing, execution-surface assertion
│   ├── registry.py          Wiring: which implementation backs which role
│   ├── mock_market.py       Deterministic synthetic world shared by all mocks
│   ├── fmp/                 mock.py + rest.py
│   ├── unusual_whales/      mock.py + rest.py
│   ├── robinhood/           mock.py + mcp.py (read-only tool allowlist)
│   └── news/                mock.py
├── agents/
│   ├── llm.py               LLMClient protocol, Anthropic + Scripted backends
│   ├── base.py              AgentRunRecord tracing
│   ├── market_intelligence/ Agent 1
│   ├── opportunity_generator/ Agent 2
│   └── trade_validator/     Agent 3
├── scoring/
│   ├── context.py           The only thing a rule may read
│   ├── components/          One module per score category
│   └── engine.py            Composition, classification, ranking
├── rules/
│   └── hard_rejections.py   Vetoes — separate from scoring by design
├── services/
│   ├── orchestrator.py      The pipeline
│   ├── technicals.py        Indicator computation
│   ├── pricing.py           Black-Scholes and greeks
│   ├── risk.py              Reward/risk modelling
│   ├── contract_selection.py Deterministic strike and expiration choice
│   ├── market_calendar.py   Session awareness, staleness policy
│   ├── persistence.py       Writes a complete, reproducible run
│   └── decisions.py         Human decision, execution, outcome tracking
├── database/                SQLAlchemy 2.0 models and session management
├── reports/                 Console (rich) and Markdown renderers
├── api/                     FastAPI: trigger, read, record. Never execute.
└── cli.py                   matb scan | report | decide | config | init-db
```

---

## Data flow

```
                     ┌───────────────────────────────────┐
                     │  ProviderBundle                   │
                     │  FMP · Unusual Whales · Robinhood │
                     │  · News                           │
                     └────────────────┬──────────────────┘
                                      │ measured facts, each with Provenance
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
┌───────────────┐            ┌────────────────┐          ┌──────────────────┐
│ Agent 1       │            │ Agent 2        │          │ Agent 3          │
│ Market Intel  │──Brief────▶│ Opportunity    │──Cand───▶│ Trade Validator  │
│               │            │ Generator      │          │ (adversarial)    │
└───────────────┘            └────────────────┘          └────────┬─────────┘
                                                                  │
                                        ValidationReport + measured bundle
                                                                  │
                                                                  ▼
                                              ┌───────────────────────────────┐
                                              │ DETERMINISTIC ZONE            │
                                              │  contract_selection           │
                                              │  risk (Black-Scholes)         │
                                              │  hard_rejections (vetoes)     │
                                              │  scoring (8 components /100)  │
                                              └───────────────┬───────────────┘
                                                              ▼
                                                    Ranked TradeReport
                                                              ▼
                                             Console · Markdown · Database
                                                              ▼
                                                   Human decision → outcome
```

Nothing crosses into the deterministic zone except validated structured data.
Once inside, no LLM is consulted again.

---

## Agent boundaries

| | Agent 1 | Agent 2 | Agent 3 |
| --- | --- | --- | --- |
| **Question** | What could move things? | What is worth trading? | Why might this be wrong? |
| **Input** | Provider evidence pack | MarketBrief + price structure | Candidate + measured data |
| **Output** | `MarketBrief` | `CandidateSet` (≤10, may be empty) | `ValidatorAssessment` |
| **May state numbers?** | Only from evidence | Only expected move, with a stated basis | **No numeric fields at all** |
| **May pick contracts?** | No | No | No |
| **May score?** | No | No | No |

Agent 2 is explicitly permitted to disagree with Agent 1, and Agent 3 with both.
Each agent's `considered_and_discarded` / disconfirming observations are stored,
so disagreement is data rather than friction.

**Zero candidates is a valid answer.** `CandidateSet` refuses to validate an
empty result with no `no_trade_rationale`, which turns "no trade" into a
deliberate, recorded decision instead of a silent failure.

### The two reasoning backends

`LLM_BACKEND=anthropic` sends the evidence to Claude and validates the response
against the Pydantic schema, with one repair round-trip on a validation failure.

`LLM_BACKEND=scripted` uses no model at all. Each agent falls back to a
documented heuristic path that derives the same structures from provider data
with explicit rules. This is what makes the pipeline runnable with zero
credentials — and every `agent_runs` row records `reasoning_mode`, so an offline
scan can never be mistaken for a reasoned one.

---

## Premarket versus live market

Option quotes before the options market opens are not prices anyone can
transact at. The system treats this as a first-class concept, not a caveat.

| | `PREMARKET` | `MARKET_OPEN` |
| --- | --- | --- |
| Agents 1 and 2 run | Yes | Yes |
| Agent 3 runs | Yes | Yes |
| Structure assembled | Yes, provisionally | Yes |
| Scored | Yes | Yes |
| **Presented as an entry** | **No** | Yes |

In `PREMARKET`, the `stale_quotes` hard rule fires on every candidate, so the
report shows what you *would* trade at the open along with an explicit
statement that the quotes are not actionable. `MarketCalendar` derives the stage
from the wall clock; `--stage` forces it for testing and backfills.

---

## Provider layer

Four roles, each an abstract base class. The rest of the system depends only on
the interface.

| Role | ABC | Backends | Supplies |
| --- | --- | --- | --- |
| Market data | `MarketDataProvider` | `mock`, `rest` (FMP) | Quotes, history, earnings and economic calendars, news, sector performance |
| Options market | `OptionsMarketProvider` | `mock`, `mcp` (Robinhood) | Chains, contract quotes, greeks, positions, account |
| Options flow | `OptionsFlowProvider` | `mock`, `rest` (Unusual Whales) | Directional premium, side of market, sweeps, greek flow, IV rank |
| News | `NewsProvider` | `mock` | Market-wide headlines with publisher and URL |

Swapping a vendor means adding a class and a branch in `registry.py`. Nothing
outside `app/providers/` changes.

**Read-only by construction.** `assert_no_execution_surface()` inspects every
provider at wiring time and raises if any public method name contains
`place_order`, `submit_order`, `buy`, `sell`, `execute_trade`, `exercise`, or
`cancel_order`. The Robinhood MCP adapter additionally refuses any tool outside
an explicit `READ_ONLY_TOOLS` allowlist, before the call is made.

**Optional providers degrade honestly.** If Unusual Whales is unavailable, the
options-flow component scores **zero** and records why. It does not score
"neutral", because absent confirmation is not the same as confirmation.

---

## Determinism and reproducibility

Three properties, each tested:

1. **Same seed, same scan.** `MOCK_SEED` plus the trading day fully determines
   the synthetic market. `test_the_run_is_reproducible_for_a_given_seed` asserts
   two runs produce identical tickers and scores.
2. **Same data, same score.** A `ScoringContext` is a closed value: rules may
   not call providers, an LLM, or a clock. Re-scoring stored data reproduces the
   original number.
3. **Same methodology, provably.** `Methodology.fingerprint()` is a SHA-256 of
   the whole rulebook, stored on every run and every score. A recommendation
   from three months ago can be re-derived under the rules that were in force
   then, not today's.

The mock providers deliberately disagree slightly — Robinhood's synthetic price
sits ~4bp off FMP's — so cross-provider reconciliation is exercised rather than
trivially satisfied.

---

## Database schema

SQLite in development, PostgreSQL in production, same models. JSON columns use
the generic `sqlalchemy.JSON` type rather than JSONB.

**Runs and observability**
`market_runs` · `agent_runs` · `data_provider_requests` · `data_quality_flags`

**Market intelligence**
`market_briefs` · `market_events` · `economic_events` · `news_items` · `stock_catalysts`

**Candidates and validation**
`trade_candidates` · `trade_validations` · `technical_snapshots` ·
`options_flow_snapshots` · `option_contract_snapshots`

**Scoring and recommendations**
`score_components` · `trade_recommendations`

**Human decisions and outcomes**
`trade_decisions` · `trade_executions` · `trade_results`

Two choices worth calling out:

- **`score_components` stores every rule that fired**, with its points and the
  measurement string, as JSON. This is what makes "why did this score 74?"
  answerable months later without re-running anything.
- **Rejected candidates are persisted with the same fidelity as accepted ones.**
  "How often did the trades we passed on actually work?" is one of the questions
  this system exists to answer, and it is unanswerable if rejects are discarded.

`Base.metadata.create_all()` is used for the MVP. Introduce Alembic before the
first schema change that has to preserve real recorded trades.

---

## Observability

Every run stores:

- **Per agent** — name, start, end, duration, status, LLM backend, reasoning
  mode, input summary, output summary, providers queried, providers failed,
  missing data, warnings, errors.
- **Per provider call** — provider, backend, operation, parameters (key-like
  parameters stripped), duration, success, error.
- **Per run** — the full methodology snapshot, provider backends, universe, and
  notes.
- **Data quality flags** — mock data in use, stale quotes, provider price
  disagreement, provider unavailable.

`GET /runs/{run_id}/audit` returns all of it in one response.

Logging is structlog with a redaction processor that masks any field whose name
resembles a credential. Settings are only ever emitted through `safe_dict()`.

---

## Extension points

| To add… | Change |
| --- | --- |
| A technical indicator | Field on `TechnicalSnapshot`, computation in `services/technicals.py`, rule in `scoring/components/technical.py` |
| A scoring rule | Append a `ScoreReason` in the relevant component; add its parameters to `methodology.yaml` |
| A scoring category | New module in `scoring/components/`, add to `COMPONENTS`, add a weight (weights must still total 100 — validated at load) |
| A hard rejection rule | A function in `rules/hard_rejections.py`, append to `RULES` |
| A data provider | Implement the ABC, add a branch in `registry.py` |
| A strategy type | `StrategyType` enum, structuring in `contract_selection.py`, arithmetic in `trade_structure.py`, allowlist in `methodology.yaml` |
| An agent | Model for its output, module under `agents/`, definition in `.claude/agents/`, call in the orchestrator |

---

## Design decisions and their trade-offs

**Heuristic fallback instead of a fake LLM.** A "scripted LLM" returning canned
JSON would let the pipeline run offline while hiding that no reasoning occurred.
Instead `ScriptedLLMClient` raises if called, and agents take a documented
heuristic path that is labelled as such in `agent_runs.reasoning_mode`.
*Trade-off:* two code paths per agent to maintain.

**One synthetic market shared by all mocks.** Mocks that each invented their own
numbers would never disagree realistically, making reconciliation untestable.
*Trade-off:* the mock universe is a fixed 12 tickers; unknown tickers raise
rather than being synthesised.

**Reward/risk modelled at the holding horizon, not at expiration.** Legs are
re-priced with Black-Scholes at the planned exit with IV held constant. Expiry
intrinsic value would systematically flatter long-premium trades by ignoring the
time value you actually sell back. *Trade-off:* the model assumes flat IV, which
is wrong around events; `iv_contraction_sensitivity` is reported separately so
the assumption is visible rather than buried.

**Risk measured to the invalidation level, not to zero.** The denominator of
reward/risk is the loss taken at the stop, not the full debit, because that is
the loss a managed trade actually incurs. Dividing by the whole premium turned
a "1.0 reward/risk" floor into a demand for +100% return and rejected almost
every long-premium trade. *Trade-off:* it depends on the candidate supplying a
machine-readable `invalidation_price`, and a too-tight stop would game the
ratio — so stops inside 1 ATR are widened for modelling and the adjustment is
reported.

**Vertical widths are searched, not assumed.** Spread width is configured in
dollars, which does not scale with underlying price, so the delta-targeted
width can be unaffordable on an expensive name. The selector builds every viable
width and lets the budget filter choose. *Trade-off:* more structures built per
expiration, and a delta-fit tiebreaker is needed so selection does not drift to
odd strikes.

**Conservative entry pricing.** Cost assumes paying the ask and selling the bid.
A mid fill is an assumption, not a fact. Both are reported.
*Trade-off:* every reward/risk figure is pessimistic by roughly half the spread.

**Hard rules separate from scoring.** A veto is not a very large negative score.
Keeping them separate means an untradable spread cannot be outvoted by a
beautiful thesis, and the report can say "82 points, but the market is
untradable" — more useful than either number alone.

**Worst-leg liquidity scoring.** A vertical is only as tradable as its worse
side, so spread, volume, and open interest are all scored on the worst leg
rather than averaged.

---

## Known weaknesses

Listed because they are real, not because they are theoretical.

1. **No holiday calendar.** `MarketCalendar` handles weekends and session hours;
   US market holidays are not hard-coded, because an incomplete holiday table is
   worse than none. A holiday currently reads as a normal trading day.
2. **VIX is proxied.** No VIX feed is wired, so the volatility regime is derived
   from SPY ATR and is explicitly labelled as a proxy in the brief.
3. **Flat-IV assumption in the risk model.** See above. Most wrong exactly where
   it matters most — into an event.
4. **`create_all()` migrations.** Fine now, will not survive the first schema
   change against real recorded trades.
5. **Sector classification comes from the mock universe.** With live FMP data,
   sector mapping needs a real source; `_sector_of` currently consults the
   synthetic scenario table.
6. **Single-name scoring only.** Nothing looks across the ranked set for
   correlated exposure — five semiconductor calls score as five independent
   trades. This is what the Risk Reviewer is designed to catch.
7. **The heuristic path is for testing, not production — measured.** On a live
   five-ticker scan the heuristic path classified 36 of 130 catalysts as
   tradable (28%), all of them from vendor-typed feeds. The LLM path on the
   same data produced 21 catalysts of which 13 were tradable (62%) — and
   crucially, types the heuristic path cannot reach at all: `MAJOR_CONTRACT`,
   `TECHNICAL_BREAKOUT`, `LITIGATION`, `SEC_FILING`, `EXECUTIVE_CHANGE`. It
   also *selected* rather than enumerating, which is the actual job.

   `LLM_BACKEND=anthropic` is the production configuration. The heuristic path
   exists so the pipeline runs and is testable without credentials.

8. **A full LLM scan is slow: roughly 400 seconds for two tickers**, dominated
   by Agent 1 (~250s). Agent 3 runs once per candidate, so a ten-candidate day
   is materially longer. This is acceptable for a research pipeline run once or
   twice a day, and it is why the screening funnel matters: Agent 1 should
   receive a curated ~50 names, not a raw universe. Parallelising Agent 3
   across candidates is the obvious next optimisation.

9. **LLM output length is variable, so failures are intermittent.** The same
   prompt fit inside the token budget on one run and overflowed on the next.
   Three mitigations are in place — the model is asked for judgement only and
   never to echo the evidence back, the evidence pack sent to it is bounded,
   and a truncated response retries once with double the budget — but a run
   that still overflows falls back to heuristics rather than failing, and says
   so in `agent_runs.warnings`. Check that field before trusting a scan.

---

## Roadmap

**Milestone 2 — real data.** Done and verified against live credentials: FMP,
Unusual Whales, news, and the Anthropic reasoning path (all three agents).
Remaining: Robinhood MCP wired through a runtime with tool access — the last
mock, and the only thing standing between the pipeline and a real
recommendation. Then the screening funnel, and automatic shadow-tracking of
every recommendation including rejects, so an outcome dataset accumulates
without waiting on manual entry.

**Milestone 3 — the Risk Reviewer.** Wire `.claude/agents/risk-reviewer.md`
into the pipeline with demotion-only authority and cross-candidate correlation
detection.

**Milestone 4 — the performance engine.** The schema already supports it. Do
scores of 80+ outperform 70-79? Which components predict? Which catalysts,
strategies, expirations, and delta ranges work, and in which volatility regimes?
How often did rejected trades work? Statistical analysis recommends methodology
changes; **the system never self-modifies its scoring rules.**

**Milestone 5 — scheduling and UI.** APScheduler for premarket and market-open
runs; a local web view over the existing API.

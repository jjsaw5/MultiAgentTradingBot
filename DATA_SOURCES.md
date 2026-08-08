# Data Sources

Four provider roles, each behind an abstract interface. The rest of the system
depends only on the interface, so swapping a vendor is a change inside
`app/providers/` and nowhere else.

| Role | Interface | Vendor | Backends | Status |
| --- | --- | --- | --- | --- |
| Market data | `MarketDataProvider` | Financial Modeling Prep | `mock`, `rest` | **Verified against a live key** |
| Options market | `OptionsMarketProvider` | Robinhood | `mock`, `mcp` | **Mapping verified against live MCP responses**; needs a runtime with tool access to run |
| Options flow | `OptionsFlowProvider` | Unusual Whales | `mock`, `rest` | REST client written, untested against a live key |
| News | `NewsProvider` | — | `mock` | Interface only; no vendor wired |

Select a backend per provider in `.env`:

```bash
FMP_BACKEND=mock              # mock | rest
UNUSUAL_WHALES_BACKEND=mock   # mock | rest
ROBINHOOD_BACKEND=mock        # mock | mcp
NEWS_BACKEND=mock             # mock
```

---

## Financial Modeling Prep

**Role.** Underlying prices, price history, calendars, company news, sector
performance. The reference price for cross-provider reconciliation.

**The v3 API is retired.** Keys issued after FMP's cutover receive HTTP 403
with a "Legacy Endpoint" message on every `/api/v3/*` path. This client targets
`/stable/*` exclusively, and the field mapping below was read off live
responses rather than documentation.

**Used for**

| Data | Method | Endpoint | Consumed by |
| --- | --- | --- | --- |
| Current quote | `get_quote` | `/stable/quote` | Agents 1–3, technicals, price reconciliation |
| Historical prices and volume | `get_price_history` | `/stable/historical-price-eod/full` | All indicators, relative strength |
| Earnings, per ticker | `get_next_earnings` | `/stable/earnings` | Earnings blackout rule |
| Earnings, market-wide | `get_earnings_calendar` | `/stable/earnings-calendar` | Calendar context only — see the cap below |
| Economic calendar | `get_economic_calendar` | `/stable/economic-calendar` | Macro events, risk events |
| Company news | `get_company_news` | `/stable/news/stock` | Catalyst identification |
| Sector performance | `get_sector_performance` | `/stable/sector-performance-snapshot` | Sector bias |

**The market-wide earnings calendar is silently truncated at 4,000 rows.** A
ticker that reports inside the requested window can simply be absent — NVDA was,
in testing, which would have disabled the earnings blackout rule without any
error. `get_next_earnings` therefore overrides the base implementation and
queries the per-symbol `/stable/earnings` endpoint instead. The market-wide call
logs a warning when it hits the cap.

**The stable quote carries no average volume.** Rather than spend a second
request per ticker, it is left absent and relative volume is derived from the
price history already in hand, in `services/technicals.py`.

**The economic calendar is global.** Rows are filtered to the configured country
(`US`) before use, and FMP's free-text event names are mapped onto the short
codes (`CPI`, `FOMC`, `PCE`, …) that the event-risk config is written in.

**Enabling it.** `FMP_BACKEND=rest` and `FMP_API_KEY=...`. The key is passed as
a query parameter (FMP's scheme) and is stripped from every audit record; the
client never logs a full request URL.

**Notes.** 4xx responses fail immediately; 5xx and timeouts retry with
exponential backoff up to `PROVIDER_MAX_RETRIES`. The client sets
`relevance_confidence` to a neutral 0.5 on news items — classifying relevance is
the agent's job, and a provider asserting an interpretation would corrupt the
evidence chain.

**MCP.** FMP publishes an MCP server. The provider interface is the right seam
for it, but this milestone ships the REST client because a background scheduler
cannot reach an interactively-authenticated MCP server. `FMP_BACKEND=mcp` raises
a clear error rather than silently degrading.

---

## Unusual Whales

**Role.** Options-market intelligence: what the options tape is doing, and
whether it corroborates a thesis.

**Used for**

| Data | Field on `FlowSnapshot` |
| --- | --- |
| Call / put premium | `call_premium`, `put_premium` |
| Bullish / bearish premium | `bullish_premium`, `bearish_premium` → `directional_premium_share` |
| Side of market | `ask_side_premium`, `bid_side_premium` → `ask_side_share` |
| Sweeps, blocks, large trades | `sweep_count`, `block_count`, `large_trade_count` |
| Multi-leg share | `multileg_share` |
| Volume and open interest | `total_volume`, `total_open_interest` → `volume_oi_ratio` |
| Greek flow | `net_delta_flow`, `net_gamma_flow`, `net_vega_flow` |
| Gamma exposure | `gamma_exposure` |
| Dark pool | `dark_pool_notional`, `dark_pool_bias` |
| Implied volatility | `iv_rank`, `iv30` |
| Expected move | `expected_move_pct` |

**Enabling it.** `UNUSUAL_WHALES_BACKEND=rest` and `UNUSUAL_WHALES_API_KEY=...`
(bearer token).

**How the data is treated.** The schema deliberately separates raw premium
totals from side-of-market attribution, because a large print is not directional
information on its own. `multileg_share` exists specifically so the scoring
engine can suppress sweep credit when single-leg inference is unreliable. See
[SCORING.md](SCORING.md#component-4--options-flow-confirmation-15).

**Field-name drift.** Unusual Whales has revised field names across versions, so
`_first()` tolerates several spellings per field and returns `None` when nothing
matches. A missing field stays missing — a fabricated flow number would silently
move a trade's score.

**Optional by design.** If the provider is unavailable, the flow component
scores **zero** and records that it was unscored. It never scores "neutral",
because absent confirmation is not confirmation.

---

## Robinhood

**Role.** The executable-market view — what you could actually transact at, and
what you already hold.

**Used for**

| Data | MCP tool |
| --- | --- |
| Option chains | `get_option_chains`, `get_option_instruments` |
| Option quotes: bid, ask, greeks, IV, volume, OI | `get_option_quotes` |
| Option historical prices | `get_option_historicals` |
| Underlying price | `get_equity_quotes` |
| Current positions | `get_option_positions`, `get_equity_positions` |
| Account and options level | `get_accounts`, `get_portfolio` |

### Payload details that are easy to get wrong

Each of these was wrong in the adapter before it was checked against the live
server, and each is now pinned by a fixture test in
`tests/test_robinhood_mcp.py`:

1. **Everything is wrapped in `{"data": {...}}`**, and the inner key varies by
   tool — `accounts`, `instruments`, `results`. Reaching for a top-level
   `results` key silently yields nothing.
2. **Quotes sit one level deeper**, as `results[].quote`, paired with a
   `results[].close` carrying the official prior-session close.
3. **Greeks are per share, not per contract.** Robinhood reports theta as
   `-0.15`; this system denominates greeks per contract, so each is scaled by
   `trade_value_multiplier`. Unscaled, theta burden and vega exposure would be
   wrong by 100× and every long-premium trade would look costless to hold.
   Delta and gamma are dimensionless and are *not* scaled.
4. **Contracts carry no OCC symbol** — only `chain_symbol`, `strike_price` and
   `expiration_date` — so the symbol is composed locally.
5. **Timestamps carry nanosecond precision and a `Z` suffix**, which
   `datetime.fromisoformat` rejects outright.
6. **`get_option_instruments` has no min/max expiration filter.** The window is
   resolved from `get_option_chains` metadata and passed as an explicit date
   list, with local filtering as a fallback.
7. **`option_level` is a string** (`"option_level_3"`), and `get_accounts` does
   not report buying power reliably — so it is left absent rather than read
   from a field that means something else. No account identifier is copied into
   the summary.

### Order placement is not implemented

This is enforced in three independent places:

1. **No execution module exists.** `tests/test_safety.py` sweeps every module in
   the package for order-submitting functions.
2. **`assert_no_execution_surface()`** runs at provider wiring and raises if any
   provider exposes a public method whose name contains `place_order`,
   `submit_order`, `buy`, `sell`, `execute_trade`, `exercise`, or `cancel_order`.
3. **`READ_ONLY_TOOLS`** — the MCP adapter refuses any tool outside an explicit
   allowlist *before* the call is made. `place_option_order`,
   `place_equity_order`, `cancel_option_order`, and `exercise_option` are not on
   it.

`ENABLE_ORDER_EXECUTION=true` raises at startup rather than enabling anything.

**Enabling it.** `ROBINHOOD_BACKEND=mcp`, with a `tool_caller` injected by a
runtime that has MCP access. Robinhood's MCP tools are reachable from an
MCP-capable client session, not from an arbitrary background Python process —
the adapter takes the caller as a dependency rather than pretending otherwise.
Without one it raises a clear message pointing at `mock`.

---

## Web and news research

**Role.** External market news that is not in a financial data vendor's feed.

Every item preserves: source, URL, headline, published time, retrieved time,
ticker association, catalyst classification, and relevance confidence.

**Social media is out of scope.** Not by policy alone — structurally. The schema
requires a publisher and a URL, and `evidence_quality` cannot honestly exceed
`UNVERIFIED` for an unattributed post. Scoring gives `UNVERIFIED` zero credit.

**Status.** Only the mock backend ships. For production, point `NewsProvider` at
a reputable newswire; prefer primary sources (filings, agency releases, exchange
notices) over aggregators.

---

## Mock providers

The system runs end to end with **zero credentials**. All mocks read from one
deterministic synthetic market (`app/providers/mock_market.py`) rather than each
inventing numbers, which is what makes cross-provider reconciliation testable.

**Deterministic.** The world is a pure function of `(MOCK_SEED, trading_day)`.
Same seed, same date, byte-identical scan — asserted by
`test_the_run_is_reproducible_for_a_given_seed`.

**Realistic.** Prices follow seeded geometric Brownian motion anchored to a
scenario price. Option chains are generated with Black-Scholes across a strike
and expiry grid with a volatility skew, a term-structure slope, an earnings
bump, and a liquidity model that widens spreads on cheap far-OTM contracts.

**Twelve tickers, each with a purpose.** The universe is hand-authored so a demo
run exercises distinct outcomes: NVDA and AMD as trending names with catalysts,
MSFT as a steady uptrend, META as a range with no clean edge, TSLA with elevated
IV into imminent earnings, XOM as a downtrend, SOFI with deliberately illiquid
option markets. A single scan therefore triggers liquidity rejections,
reward/risk rejections, earnings blackouts, and structures that cannot be
assembled — rather than a uniformly clean result that tests nothing.

**They refuse to invent.** `get_quote("NOTAREALTICKER")` raises rather than
synthesising a plausible price.

**They are labelled.** Every mock request record carries `backend="mock"`, and a
run with all-mock providers emits a `MOCK_DATA` data-quality flag that appears
in red at the top of the console report and in the markdown output.

---

## Data quality handling

**Provenance on everything.** Every value carries a `Provenance` with provider,
endpoint, `as_of`, and `retrieved_at`.

**Staleness is session-aware.** `MarketCalendar` sets the limit — 300 seconds
during market hours, 24 hours otherwise — and stale inputs cost the freshness
point and are named in the report.

**Missing is missing.** A provider that cannot obtain a value returns `None` and
a `MissingData` record. Scoring rules record `unscored_due_to_missing_data` and
award zero. Nothing is estimated, averaged, or back-filled.

**Cross-provider reconciliation.** Agent 3 collects the underlying price from
every provider that supplies one and computes the maximum disagreement.
Agreement within 0.5% earns points; unreconciled disagreement beyond 2% is a
hard rejection. The mock providers deliberately differ by ~4bp so this path is
exercised in every offline run.

**Every call is audited.** `data_provider_requests` records provider, backend,
operation, parameters (key-like parameters stripped), duration, success, and
error, per run.

---

## Credentials

All credentials live in `.env`, are typed as `SecretStr`, and are only rendered
through `Settings.safe_dict()`, which reports `***set***` / `***unset***`. The
structlog pipeline masks any field whose name resembles a credential, and a test
asserts `.env.example` ships no values for credential-shaped keys.

| Variable | Needed for | Milestone 1 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | LLM reasoning path | Optional — heuristic fallback runs without it |
| `FMP_API_KEY` | Live market data | Optional — mock backend |
| `UNUSUAL_WHALES_API_KEY` | Live options flow | Optional — mock backend |
| Robinhood MCP access | Live chains and quotes | Optional — mock backend |
| `DATABASE_URL` | Postgres instead of SQLite | Optional |

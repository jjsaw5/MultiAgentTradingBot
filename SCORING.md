# Scoring

Every number in this document lives in `config/methodology.yaml`. Nothing here
is hard-coded in application logic; if you find a threshold in a `.py` file,
that is a bug.

The scoring engine is pure Python. No LLM is consulted after validation
completes. Given the same stored data and the same methodology fingerprint, a
score reproduces exactly.

---

## Two independent judgements

A candidate faces two separate gates, and they do not negotiate.

**Hard rejection rules** (`app/rules/hard_rejections.py`) are vetoes. A trade
that fails one is rejected regardless of its score. All failing rules are
reported, not just the first, so the report can explain the full picture.

**The scoring engine** (`app/scoring/`) awards up to 100 points across eight
components. The score is computed and reported even for hard-rejected trades —
"82 points, but the bid/ask is 40% wide" is more useful feedback than a bare
rejection.

A high score can never override a hard failure. This is asserted directly by
`test_a_high_score_cannot_override_a_hard_rejection`.

---

## Weights

| Component | Points | What it asks |
| --- | ---: | --- |
| Catalyst Strength | 15 | Is there a real, timely, material reason to move? |
| Market / Sector Alignment | 10 | With the tide or against it? |
| Technical Setup | 20 | Does price structure support the thesis? |
| Options Flow Confirmation | 15 | Does the tape corroborate it? |
| IV / Greeks Structure | 10 | Are you paying a sane price for the exposure? |
| Contract Liquidity | 10 | Can you actually get in and out? |
| Risk / Reward | 15 | Is the payoff worth the risk? |
| Data Agreement / Quality | 5 | How much do we trust the inputs? |
| **Total** | **100** | |

Weights are validated at load: `score_weights` must sum to exactly 100 or the
config refuses to load.

## Classification bands

| Score | Classification |
| ---: | --- |
| 90–100 | Exceptional |
| 80–89 | High conviction |
| 70–79 | Good candidate |
| 60–69 | Watchlist / conditional |
| < 60 | Reject |

`min_presentable_score` (default 60) decides what reaches the ranked list.
Everything below it — and everything hard-rejected — is still scored, still
explained, and still persisted.

---

## Component 1 — Catalyst Strength (15)

Config: `scoring.catalyst_strength`

| Rule | Points | Trigger |
| --- | ---: | --- |
| `catalyst_importance` | 0 → 6.0 | `importance_points` × the catalyst's 0–1 importance |
| `evidence_quality` | 0 → 4.0 | CONFIRMED_FACT 4.0 · REPORTED 2.5 · INTERPRETATION 1.0 · RUMOR 0 · UNVERIFIED 0 |
| `catalyst_recency` | 0 / 1.0 / 2.0 | ≤ 2 days full · ≤ 5 days partial · older nothing |
| `catalyst_timing` | 0 / 3.0 | The dated event falls inside the holding period |
| `supporting_catalysts` | 0 → 1.5 | 0.5 each, capped |
| `already_priced_in` | −3.0 | The validator concluded the move has been absorbed |
| `catalyst_contradicted` | zeroes the component | Validator verdict is CONTRADICTED |

If the candidate's catalyst is not in the MarketBrief at all, importance is
recorded as unscored and evidence quality falls to UNVERIFIED — an agent cannot
earn points for a catalyst it invented.

## Component 2 — Market / Sector Alignment (10)

Config: `scoring.market_alignment`

| Rule | Points |
| --- | ---: |
| `spy_alignment` | aligned 4.0 · neutral 2.0 · fighting 0 |
| `qqq_alignment` | aligned 2.0 · neutral 1.0 · fighting 0 |
| `sector_alignment` | aligned 2.0 · neutral 0 · fighting −1.0 |
| `relative_strength_20d` | 2.0 when 20-day relative strength versus SPY favours the direction |

Relative strength is mirrored for direction: a bullish trade wants outperformance,
a bearish trade wants relative weakness. Same threshold, opposite sign.

## Component 3 — Technical Setup (20)

Config: `scoring.technical_setup`

| Rule | Points | Trigger |
| --- | ---: | --- |
| `trend_alignment` | 6.0 / 3.0 / 0 | Price/SMA20/SMA50 fully stacked · right side of SMA20 only · wrong side |
| `key_level_respected` | 4.0 | Holding support (bullish) or rejecting resistance (bearish) |
| `relative_volume` | 3.0 / 2.0 / 0 / −1.0 | ≥1.5 · ≥1.2 · unremarkable · <0.8 |
| `momentum` | 4.0 / 2.0 / 0 | RSI in the directional zone **and** MACD confirming · one of the two · neither |
| `overextension_penalty` | −2.0 | RSI ≥ 78 bullish, ≤ 22 bearish — chasing |
| `blocking_level_proximity` | −3.0 / −1.0 / 0 | Opposing level within 1% · within 3% · clear runway |
| `expected_move_feasible` | 3.0 | Expected move ≤ 2.5 × ATR projected over the holding period |

The ATR projection scales by √time, the standard way to extend a daily range
over a holding window. A thesis demanding a move the underlying has never made
in that timeframe earns nothing here.

## Component 4 — Options Flow Confirmation (15)

Config: `scoring.options_flow`

Flow is corroboration, never proof. Three guards encode that:

- credit scales with the **share** of directionally-attributed premium above a
  0.50 floor, so a 50/50 tape earns nothing;
- flow clearly opposing the thesis (aligned share ≤ 0.35) draws −6.0;
- when multi-leg share exceeds 45%, sweep credit is suppressed and a caveat is
  attached, because sweep counts in multi-leg-dominated tape do not imply
  direction.

| Rule | Points |
| --- | ---: |
| `directional_premium` | 0 → 6.0, scaled above the floor |
| `side_of_market` | 0 → 3.0 — ask-side for bullish, bid-side for bearish |
| `sweeps` | 2.0 at ≥ 3 sweeps, suppressed if flow is multi-leg-dominated |
| `new_positioning` | 2.0 when volume ≥ open interest |
| `delta_flow_consistency` | 2.0 when net delta flow agrees with the thesis |
| `flow_contradicts_thesis` | −6.0 |

**Missing flow scores zero, not neutral.** Absence of confirmation is not
confirmation, and the component records that it was unscored rather than
silently awarding partial credit.

## Component 5 — IV / Greeks Structure (10)

Config: `scoring.iv_greeks`

Long-premium trades are short volatility risk: buying a 90th-percentile IV rank
and being right on direction can still lose. Debit verticals are partially
hedged by the short leg, so they get a different band table.

| IV rank | Long premium | Debit vertical |
| --- | ---: | ---: |
| < 30 | 4.0 | 3.0 |
| 30–50 | 3.0 | 3.5 |
| 50–70 | 1.5 | 3.0 |
| ≥ 70 | 0 | 2.0 |

| Rule | Points | Trigger |
| --- | ---: | --- |
| `iv_vs_expected_move` | 2.0 | Thesis move within ±50% of the IV-implied move |
| `theta_burden` | 2.0 | Theta × holding days ≤ 35% of premium |
| `vega_exposure` | 2.0 | Net vega ≤ 3% of premium per IV point |

## Component 6 — Contract Liquidity (10)

Config: `scoring.contract_liquidity`. All three rules score the **worst leg** —
a vertical is only as tradable as its worse side.

| Rule | Points |
| --- | ---: |
| `bid_ask_spread` | ≤2% → 4.0 · ≤5% → 3.0 · ≤10% → 1.5 · wider → 0 |
| `open_interest` | ≥2500 → 3.0 · ≥750 → 1.5 · below → 0 |
| `contract_volume` | ≥750 → 3.0 · ≥200 → 1.5 · below → 0 |

## Component 7 — Risk / Reward (15)

Config: `scoring.risk_reward`, `risk_model`

Reward/risk is modelled by re-pricing every leg with Black-Scholes at the end of
the intended holding period, with the underlying at the thesis target and IV
unchanged. Entry cost assumes paying the ask and selling the bid.

**Risk is measured to the invalidation level, not to zero:**

```
reward_to_risk = (value at target − cost) / (cost − value at invalidation)
```

You do not lose the full premium on a losing trade — you exit when the thesis
breaks. Measuring reward-to-target against risk-to-zero compares two different
things and systematically penalises long premium for a loss that would never be
taken. When no invalidation level is available the denominator falls back to the
full debit and `method_notes` says so.

Two guards keep this honest:

- **The stop is modelled part-way through the holding period**
  (`risk_model.invalidation_exit_fraction`, default 1/3). A level that breaks
  usually breaks early; charging the trade a full holding period of decay on a
  stop-out overstates the loss.
- **A stop closer than `risk_model.min_stop_distance_atr` (default 1.0) ATR is
  widened for modelling.** A stop inside ordinary daily noise is not a stop —
  it would be taken out by routine movement, and it flatters reward/risk by
  shrinking the denominator toward zero. The adjustment is reported.

`return_on_premium_at_target` is reported alongside — profit as a multiple of
the full debit — but scores no points. It answers a different question: *what do
I make on the premium*, versus *what do I risk to make it*.

| Rule | Points |
| --- | ---: |
| `reward_to_risk` | ≥3.0 → 8.0 · ≥2.0 → 6.0 · ≥1.5 → 4.0 · ≥1.0 → 2.0 · below → 0 |
| `return_on_premium_at_target` | 0.0 — reported for context, scores nothing |
| `max_loss_within_budget` | 3.0 when total cost ≤ `max_premium_per_trade_usd` |
| `breakeven_vs_expected_move` | 4.0 when breakeven needs ≤50% of the expected move · 2.0 at ≤80% · 0 beyond |

The breakeven rule is the one that most often exposes a bad structure: a trade
that must capture 80% of its own expected move just to break even has no margin
for being slightly early or slightly wrong.

## Component 8 — Data Agreement / Quality (5)

Config: `scoring.data_quality`

| Rule | Points | Trigger |
| --- | ---: | --- |
| `provider_coverage` | 2.0 | Every expected provider responded |
| `price_agreement` | 2.0 | Cross-provider underlying disagreement ≤ 0.5% |
| `data_freshness` | 1.0 | No input exceeded the session's staleness limit |

A recommendation corroborated by two live sources is worth more than the same
recommendation from one provider on yesterday's quotes. This component makes
that explicit rather than leaving it as a footnote.

---

## Hard rejection rules

Config: `hard_rejections`. Evaluated in `app/rules/hard_rejections.py`.

| Code | Rule | Default |
| --- | --- | --- |
| `SPREAD_TOO_WIDE` | Worst-leg bid/ask spread | > 10% of mid |
| `INSUFFICIENT_VOLUME` | Worst-leg contract volume | < 100 |
| `INSUFFICIENT_OPEN_INTEREST` | Worst-leg open interest | < 250 |
| `MISSING_CRITICAL_DATA` | Any of `required_fields` absent | underlying price, bid, ask, expiration, strike |
| `CATALYST_NOT_VALIDATED` | Validator verdict CONTRADICTED or DATA_UNAVAILABLE | — |
| `EARNINGS_BLACKOUT` | Earnings inside holding period + buffer | 3 days before, 1 after — waived when earnings *is* the thesis |
| `PROVIDER_DISAGREEMENT` | Unreconciled cross-provider price gap | > 2% |
| `REWARD_RISK_TOO_LOW` | Modelled reward/risk, measured to the invalidation level | < 1.0 |
| `PREMIUM_EXCEEDS_LIMIT` | Total cost of the position | > $500 |
| `EXCESSIVE_THETA` | Theta × holding days as a share of premium | > 60% |
| `NO_TRADABLE_CONTRACT` | Selection could not assemble a structure | — |
| `STRATEGY_NOT_ALLOWED` | Strategy outside the configured allowlist | — |
| `STALE_QUOTES` | Stage is not `MARKET_OPEN` | — |

---

## Reading a score

The console report prints every rule that fired, its points, and the measurement
that triggered it. Nothing is hidden and nothing is summarised away:

```
technical_setup             17.0/20
     +6.0  trend_alignment           [price=158.20 sma20=153.92 sma50=150.09 (fully stacked)]
     +4.0  key_level_respected       [support=157.16, price=158.20 (0.66% away, holding)]
     +3.0  relative_volume           [rvol=2.42]
     +4.0  momentum                  [RSI14=58.1 zone=[45.0,70.0], MACD=0.579/0.193]
     -3.0  blocking_level_proximity  [resistance=159.19 only 0.63% away]
     +3.0  expected_move_feasible    [expected 16.0% vs 28.4% ATR-projected over 60d (ratio 0.56)]
```

Rules that could not run appear too, so a component weakened by missing data is
visibly different from one weakened by bad data:

```
catalyst_strength           12.9/15
     ...
      0.0  catalyst_recency (no published_at timestamp available)
```

Everything above is written to `score_components` and readable through
`GET /runs/{run_id}/audit` or `matb report <run_id>`.

---

## Changing the methodology

Edit `config/methodology.yaml`. The loader validates on read:

- unknown keys are rejected (`extra="forbid"`) — a typo fails loudly rather than
  silently reverting to a default;
- weights must total 100;
- classification bands must be ordered by descending minimum;
- the whole file is frozen once loaded.

Every run stores the full config plus a `methodology_fingerprint`. Change a
threshold and the fingerprint changes, so scans from before and after a change
are distinguishable in the database and can be analysed separately.

**The system never modifies its own scoring rules.** The performance engine
described in ARCHITECTURE.md is designed to *recommend* changes from stored
outcomes; applying them is a human edit to this file.

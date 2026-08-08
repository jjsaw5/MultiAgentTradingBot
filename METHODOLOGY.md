# Methodology

How a trade goes from "something happened" to a ranked recommendation, and why
each step is shaped the way it is.

For the exact points and thresholds, see [SCORING.md](SCORING.md). For the
component layout, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## The premise

Most bad options trades are not bad directional calls. They are correct
directional calls expressed through the wrong instrument: too much premium, too
little time, too wide a market, or an implied move that already priced in the
thesis before you arrived.

So the methodology separates two questions that are easy to conflate:

1. **Is something going to move?** — research, reasoning, judgement. LLM work.
2. **Is there a trade in it?** — arithmetic on measurable quantities. Code work.

An LLM answering question 2 will produce a confident number with no mechanism
behind it. Code answering question 1 produces nothing at all. Keeping them apart
is the entire design.

---

## Stage 1 — Find what could move

**Agent 1, Market Intelligence.**

The question is deliberately broad: what conditions, events, news items,
catalysts, or scheduled events have a meaningful chance of moving the market, a
sector, or a stock within the relevant horizon?

The agent works from an **evidence pack** assembled by code — index quotes and
indicators, the economic calendar, the earnings calendar, sector performance,
company news, market headlines. It may interpret that pack. It may not add
measurements to it.

Every catalyst is classified on five axes, because each one changes what the
catalyst is worth downstream:

| Axis | Why it matters |
| --- | --- |
| Scope (market / sector / company) | Determines what the trade is actually a bet on |
| Direction | Feeds alignment scoring |
| Importance (0–1) | Scales catalyst credit directly |
| Time horizon | Decides whether it lands inside the holding period |
| Evidence quality | A rumour and a filing are not worth the same |

That last axis is doing real work. `CONFIRMED_FACT` earns 4 points; `RUMOR`
earns nothing. An agent that cannot distinguish the two is a liability, so the
distinction is a required, enumerated field rather than a stylistic preference.

**Anything the agent could not establish goes in `unavailable_data`.** Saying
"I could not determine the VIX level" is correct behaviour and is scored as
such; inventing one is a critical failure.

---

## Stage 2 — Find what is worth trading

**Agent 2, Opportunity Generator.**

Agent 2 receives the brief plus measured price structure for every ticker that
has a catalyst. It is explicitly not required to agree: a catalyst rated highly
by Agent 1 can be discarded here if it is stale, already absorbed, or
contradicted by the tape. Discards are recorded with reasons.

**An opportunity requires a nameable reason the asset could move during the
holding period.** Not "good company". Not "in an uptrend". A specific,
dated, sourced reason.

Four choices are made here, and only four:

**Direction.** From the catalyst's expected direction. If the catalyst is
directionally neutral — an earnings date is not inherently bullish — the trade
is only taken when price structure is unambiguous (fully stacked 20/50). A
neutral catalyst plus a mixed chart is a pass.

**Holding period.** Anchored to the catalyst date where one exists. A catalyst
41 days out implies a longer hold than one 3 days out, and the expected move is
then sized over *that* window rather than a default one.

**Expected move.** ATR projected over the holding period by √time, scaled by
catalyst importance, bounded to a plausible range. The basis is recorded on the
model, so a reviewer can see whether the number came from realised volatility, a
historical earnings move, or an estimate.

**Strategy.** Elevated IV rank (≥ 50) pushes toward a debit vertical rather than
a naked long: the short leg partially hedges an IV contraction, which is the
most common way a directionally correct long-premium trade loses.

The candidate cap is configuration (default 10), and **an empty result is a
valid answer**. `CandidateSet` refuses to validate an empty set without a
`no_trade_rationale`, so declining to trade is a recorded decision rather than a
silent failure. A weak candidate costs more than a missed one — it consumes a
validation pass and risks reaching a human with an implied endorsement.

---

## Stage 3 — Try to break it

**Agent 3, Trade Validator.**

The instruction is: *assume this might be wrong, and look for data that says so.*

Before the agent is consulted, code fetches everything measurable — quotes from
two independent providers, indicators, the option chain, flow aggregates, the
earnings date — and reconciles the underlying price across providers. Those are
attached to the report as facts.

The agent then supplies interpretation across six categories: price structure,
market alignment, catalyst validity, options flow, contract quality, and
risk/reward. Its output schema contains no numeric fields, so it cannot move a
price, a greek, or an open-interest figure.

Two behaviours matter more than the rest:

**Missing data produces `DATA_UNAVAILABLE`, not `INCONCLUSIVE`.** The
distinction is the difference between "we looked and could not tell" and "we
could not look". Downstream, the first withholds credit and the second also
raises a data-quality flag.

**Flow is interpreted, not counted.** A large print is not directional
information. It may be a hedge, a roll, or one leg of a spread. So:

- credit scales with the *share* of directionally-attributed premium above a
  50% floor — a coin-flip tape earns nothing;
- flow clearly opposing the thesis is penalised, not merely uncredited;
- when multi-leg flow dominates, sweep credit is suppressed and a caveat is
  attached, because sweep counts in that tape do not imply direction;
- volume well below open interest is called out as consistent with existing
  positions rather than new conviction.

---

## Stage 4 — Build the actual trade

**Deterministic. No agent involvement.**

`contract_selection.py` turns direction, timing, and magnitude into a specific
structure.

**Expiration** must clear three constraints: the configured DTE window, the
catalyst date plus a buffer, and — importantly — the holding period plus an
extrinsic-value buffer (default 21 days). Without that last constraint the
selector happily picks an expiration a day or two past the planned exit, which
makes every long-premium trade look like a theta disaster purely because it was
modelled at expiry.

**Strikes** target configured deltas: ~0.45 for a long single option, ~0.55/0.30
for a vertical, with the width band and a maximum debit-as-share-of-width
enforced.

**Cheapness is not a selection criterion.** Candidates are ranked by tradability
— worst-leg spread, then open interest, then volume — and premium only matters
through the configured budget cap. A far-OTM lottery ticket is cheap and almost
always wrong.

`risk.py` then prices the structure by re-pricing every leg with Black-Scholes
at the end of the holding period, underlying at the thesis target, IV held
constant. Two assumptions are made explicit rather than buried:

- **Entry cost assumes paying the ask and selling the bid.** A mid fill is an
  assumption, not a fact; both figures are reported.
- **IV is held flat**, which is wrong around events. The dollar impact of a
  5-point IV contraction is therefore reported separately, so the assumption is
  visible.

---

## Stage 5 — Veto, then judge

Two independent gates, in that order.

**Hard rejection rules** are absolute. Spread too wide, contract too thin,
critical data missing, catalyst unvalidated, earnings inside the window,
providers irreconcilable, reward/risk below minimum, premium over budget, theta
burn excessive, no tradable structure, stale quotes. All failures are reported,
not just the first.

**The scoring engine** awards up to 100 points across eight components, and does
so *even for hard-rejected trades*. "82 points, but the market is untradable" is
a more useful diagnosis than either half alone — it tells you the research was
sound and the instrument was not.

A strong score can never override a hard failure. The rules engine is a separate
module for exactly this reason: a veto is not a very large negative number.

---

## Stage 6 — Present, decide, record

The report gives the market summary, the ranked trades with complete
specifications and full score breakdowns, and the rejected candidates with
their reasons.

The human decides. The system records: `APPROVED`, `REJECTED`, `WATCHED`,
`ENTERED`, `SKIPPED`. If entered, the fill, quantity, stop, target, exit, P&L,
and maximum favourable and adverse excursion.

**Rejected candidates are stored with the same fidelity as accepted ones.** "How
often did the trades we passed on actually work?" is one of the questions this
system exists to answer, and it is unanswerable if rejects are thrown away.

---

## Premarket versus live market

Option quotes before the options market opens are not prices anyone can
transact at.

**Premarket** runs the full pipeline and assembles a provisional structure, so
you know what you would trade at the open — but the `stale_quotes` hard rule
fires on everything, and nothing is presented as an entry.

**Market open** re-fetches the underlying price, chain, bid/ask, volume, open
interest, IV, greeks, and flow. Only then are contracts and scores final.

---

## What this methodology deliberately does not do

- **No naked short options, no undefined risk.** Every allowed strategy is a net
  debit with a known maximum loss.
- **No autonomous execution.** There is no execution module. A startup assertion
  rejects any provider exposing an order method.
- **No estimated data.** Missing stays missing. Nothing is filled with an
  average or a typical value.
- **No self-modifying rules.** The performance engine is designed to *recommend*
  methodology changes from stored outcomes. Applying one is a human edit to
  `config/methodology.yaml`.

---

## How to change it

Every threshold is in `config/methodology.yaml`, validated on load: unknown keys
are rejected, weights must total 100, bands must descend, and the whole object
is frozen afterwards.

Each run stores the full config and a fingerprint of it, so a recommendation
made under an older methodology can be re-derived under the rules that were
actually in force at the time — and scans from before and after a change can be
analysed separately rather than pooled into a misleading average.

# Product roadmap — U.S.-first, globally aware, portfolio-led

> **One system, two contracts:** a quiet monitoring brief and a comprehensive
> IBKR Daily View. U.S. holdings lead; major global markets provide the context
> that can change their risk, valuation or catalysts.

---

## Product strategy

The product follows the holder's money, with a deliberate information order:

1. U.S. portfolio holdings and watch-only names.
2. U.S. market internals and macro transmission: rates, inflation, labour,
   dollar, volatility, energy and policy.
3. Major global markets that affect the book: Europe, Asia-Pacific, global
   trade, currencies and commodities.
4. SEC filings and institutional disclosures, explicitly labelled as delayed.

This is not a generic world-news summary. A global item earns space when it
changes the context for a configured holding, factor or research candidate.

---

## Current state

| Dimension | Status | Notes |
|---|---|---|
| U.S./global monitoring | Shipped | Base feeds plus dynamic per-symbol Yahoo feeds, price moves, entity filtering and 14-day dedupe. |
| SEC institutional context | Shipped | Isolated 13F diff with filed-date framing and no copy-the-manager language. |
| IBKR Daily View | Shipped (local CSV/config) | P&L, allocation, exposure, P/E, PEG, growth, technicals, action screen and cash deployment. |
| Direct read-only IBKR sync | Not shipped | No credential or token surface exists. CSV is the privacy-first path. |
| Fundamentals depth | Partial | Public quote fields degrade cleanly; margins, revisions and forward calendars need stronger sources. |
| ETF look-through / overlap | Not shipped | Current factor tags are user-supplied; fund constituent overlap is not yet calculated. |
| Correctness hardening | Shipped | CSV metadata survives broker merges; incomplete FX/value and stale snapshots block calls; position and shared sector caps constrain deployment. |
| Continuous integration | Shipped | GitHub Actions runs the offline suite for pushes and pull requests. |

---

## Guardrails

1. **Two outputs remain separate.** The monitoring brief never emits a trade
   call. The portfolio view may emit a rule-based action, but the rule and its
   evidence must be printed.
2. **No autonomous trading.** No order placement, order preview, account login
   or broker session management.
3. **Private portfolio data stays local.** It is never placed in the semantic
   brief archive or committed by the application.
4. **A wrong number is worse than no number.** Price, FX and fundamental fields
   degrade to `unavailable`; no implicit 1:1 FX conversion.
5. **One failed source loses one field.** It never sinks the whole daily run.
6. **13F is disclosure, not intent.** Always show the filing date and lag.
7. **A recommendation needs evidence.** Fewer than two usable valuation,
   growth or technical data points defaults to HOLD.

---

## P0 — Harden what now exists

### P0.1 Data-freshness ledger

Every displayed price, FX rate, valuation field and headline should carry a
source and as-of time in the internal report model.

**Acceptance:** stale or missing values are visible; no formatter can silently
drop an as-of warning; static and composed outputs agree on the raw figures.

### P0.2 Portfolio import fixtures

Expand IBKR import coverage across Activity Statement variants, multi-account
exports, options, fractional shares and non-USD listings.

**Acceptance:** a malformed section loses only its rows; duplicate symbols are
aggregated with quantity-weighted average cost; short positions and options are
labelled rather than treated as long common stock.

### P0.3 Recommendation regression suite

Freeze representative BUY/HOLD/SELL cases so a change to one threshold cannot
quietly flip unrelated holdings.

**Acceptance:** test cases cover insufficient data, concentration overrides,
negative growth, high PEG, strong growth, trend breaks and research candidates;
every label has at least one human-readable reason.

---

## P1 — Make the view genuinely institutional

### P1.1 Fundamentals pack

Per holding: trailing/forward P/E, PEG, revenue and earnings growth, free cash
flow, margins, ROIC, leverage, estimate revisions, beta and market cap.

**Acceptance:** every field names its source and date; sector-relative
percentiles are kept separate from absolute bands; negative-earnings P/E and
non-positive-growth PEG are explicitly `not meaningful`.

### P1.2 Benchmark and factor attribution

Separate what came from market beta, sector, style factor and stock-specific
movement.

**Acceptance:** each holding has a configured home benchmark; the report shows
daily and trailing relative return; missing factor data does not become zero.

### P1.3 ETF look-through and overlap

Reveal duplicated exposure across broad funds, thematic ETFs and direct
holdings.

**Acceptance:** top constituent overlap is sourced and dated; the report can
state, for example, that a direct holding is also material inside two funds;
the exposure table avoids double-counting cash.

### P1.4 Catalyst calendar

Earnings dates, ex-dividend dates, investor days, index rebalances, Fed/ECB/BoJ
meetings and material regulatory deadlines.

**Acceptance:** a short forward window, provisional dates labelled, source on
every event, portfolio items cannot be displaced by generic macro events.

### P1.5 Portfolio-level risk

Volatility, beta, drawdown, correlation clusters, scenario shocks and
currency exposure.

**Acceptance:** calculations disclose their lookback and benchmark; scenarios
are arithmetic shocks rather than forecasts; incomplete history is labelled.

---

## P2 — Make it continuous

### P2.1 Read-only IBKR Flex Query adapter

An optional adapter may retrieve positions and realized P&L without enabling
trading permissions.

**Acceptance:** strictly read-only scope; credentials remain environment-only;
the CSV path stays supported; sync failure falls back to the most recent local
snapshot with a visible timestamp.

### P2.2 Historical thesis tracking

Store user-authored reasons for owning a security and surface them only when a
tracked fact changes.

**Acceptance:** thesis text is never model-generated; a new fact is shown next
to the dated thesis; the system does not rewrite the user's belief.

### P2.3 Decision journal

Record daily labels, evidence and optional user decisions so consistency can
be audited.

**Acceptance:** label changes include the exact input or threshold that caused
them; performance is evaluated without hindsight edits; no automatic orders.

### P2.4 Research-candidate discovery

Surface repeated supply-chain, peer and co-mention relationships outside the
portfolio.

**Acceptance:** candidates are capped, evidence-linked and separate from BUY;
adding one to the configured list is always manual.

---

## Output contracts

### Monitoring brief

**What happened → why → portfolio relevance → what changed → what to ignore.**

No recommendation, target or position sizing.

### IBKR Daily View

**Portfolio snapshot → holdings → valuation/quality → exposure → technicals →
catalysts → risks → BUY/HOLD/SELL → exact deployable-cash plan → research
candidates.**

The action screen is decision support, not an order. Missing evidence narrows
the claim rather than being filled by prose.

---

## Definition of done for every future feature

1. U.S. holdings work first.
2. At least one major non-U.S. listing is tested with its native suffix and FX.
3. Source and as-of behaviour is explicit.
4. Failure degrades locally.
5. Private portfolio values are not archived.
6. Monitoring and decision-support contracts are not blurred.
7. Tests are offline and deterministic.

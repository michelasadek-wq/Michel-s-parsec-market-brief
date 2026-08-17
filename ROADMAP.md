# Roadmap

> Where this goes next: from a several-times-daily monitor that tells you what
> moved, to a personal investment-intelligence system that tells you **what
> actually changed** — across the US and global markets you hold or watch.
> Still no signals.

**Scope decision (2026-08):** the product is now **US + global markets only**.
The earlier MENA-first framing (Tadawul/EGX sources, Arabic entity matching)
is retired by owner decision; the research that supported it remains in
`docs/research/` as history. The portfolio-first principle survives the
change: the brief follows what the holder owns, wherever it is listed among
the supported markets.

---

## Current state

| Dimension | Score | Why |
|---|---|---|
| Monitoring engine | **8/10** | US/global feeds, entity filtering, 14-day dedupe with crash-safe persistence, market-wide radar, share-count-based 13F diffs, archive feedback, graceful degradation at every stage, 119 mocked tests. It reliably tells you what moved and what was published. |
| Personal investment intelligence | **5.5–6/10** | It knows a *watchlist*, not a *portfolio*. No position sizes, no cost basis, no relative performance, no fundamentals, no forward calendar, no memory of what you believed last month. |

The gap is the roadmap. Everything below closes it without touching the
guardrails.

---

## Guardrails

These are constraints, not backlog items. No phase may relax them.

1. **No signals. Ever.** No buy/sell/hold, no price targets, no fair values, no
   entry/exit levels, no conviction language. The evidence is settled:
   [docs/research/evidence-review.md](docs/research/evidence-review.md) — the
   headline LLM news-trading result needs ~190% daily turnover and goes
   unprofitable at 20 bps of costs; agent-framework Sharpe decays 51–62% past
   training cutoff; out-of-sample LLM alpha runs +20.7% → −1.0%. Large-cap news
   is priced in milliseconds; a thrice-daily brief is hours late **by design**.
2. **The product is attention routing and panic prevention.** Nothing about
   your holdings escapes you, and nothing in the brief pushes you to act. That
   is the whole value proposition. Judge every proposed feature against it.
3. **Discovery is data, not tips.** The radar (day gainers, trending tickers)
   and any future discovery surface report observed market activity with a
   number and a source. Nothing discovered is ever ranked, endorsed, or framed
   as an opportunity.
4. **Not an autonomous trading bot.** No broker integration, no order
   placement, no position automation, no "paper trading" mode that becomes one.
5. **One strong model composes.** No debate panel, no consensus round — see
   [Rejected directions](#explicitly-rejected-directions).
6. **Degrade, never fail.** A dead source loses one source. A missing model
   loses the prose, never the data. Any new layer inherits this.
7. **A wrong number is worse than no number.** News-only entries exist for
   exactly this reason. New data types get the same treatment.

---

## P0 — Make it personal

*Goal: the brief stops being about a watchlist and starts being about the
holder's money.*

### P0.1 Portfolio model (positions, not just tickers)

Extend `config.yaml` so an entry can carry quantity, cost basis, currency, and
account tag, while remaining optional — a pure watchlist entry must still work
unchanged.

**Acceptance:** a configured position renders daily P&L in local currency and
in portfolio base currency; a `news-only` or quantity-less entry is unaffected;
FX conversion for USD/GBP/other listing currencies is explicit and sourced,
never assumed 1:1; no position data is written to any archive that leaves the
machine.

### P0.2 Portfolio-relative movement

Report a move by its weight in the portfolio, not just its percentage.

**Acceptance:** the brief ranks movers by contribution to portfolio value
change, not by raw percent; a 6% move in a 0.5% position ranks below a 1.5%
move in a 30% position; the static digest carries the same ranking with no
prose.

### P0.3 Benchmark and sector relative performance

A stock down 2% on a day the Nasdaq is down 3% is a *different fact* from the
same move on a flat tape.

**Acceptance:** every priced mover shows its move against its home index
(S&P 500 / Nasdaq for US, FTSE All-World for the global ETFs) and, where a
mapping exists, its sector; the relative figure is labelled as such and never
framed as strength or weakness worth acting on.

### P0.4 Macro block

A compact macro block: Fed decisions and minutes, USD index, VIX, US 10Y, and
the mega-cap moves large enough to set risk appetite.

**Acceptance:** the block is capped at a fixed small number of lines and never
displaces portfolio items; it is suppressed entirely on a quiet macro day; it
names transmission to the watchlist ("TSMC guidance next to AVGO") rather than
reciting market internals for their own sake.

### P0.5 "What actually changed?"

The framing that carries the whole product. Distinguish *new information* from
*price noise* and from *a story you were already told*.

**Acceptance:** the brief has a distinct section that only fires when something
genuinely new landed — a disclosure, a guidance change, a filing, a first-time
headline on a name; the archive feedback loop (last 3 briefs) is what proves
novelty, and a story resurfacing under a new URL and a new headline is
suppressed; on a genuinely quiet run the section prints "nothing new" and the
brief stays short.

---

## P1 — Make it analytical

*Goal: enough fundamental and forward-looking context to judge a move, without
ever judging it for the reader.*

### P1.1 Fundamentals pack

Per priced holding: P/E, forward P/E, PEG, free cash flow, revenue and earnings
growth, margins, ROIC, leverage, and estimate revisions.

**Acceptance:** every field degrades to "unavailable" rather than to a stale or
guessed value; every number carries its source and as-of date; no field is
rendered with a verdict attached ("cheap", "expensive", "attractive") — the
compose prompt's forbidden-language list is extended to cover valuation
adjectives explicitly.

### P1.2 Catalyst calendar

Forward-looking dates: earnings, dividends and ex-dates, index reviews, Fed
and central-bank meetings.

**Acceptance:** the brief shows a short forward window (e.g. next 10 days) for
watchlist names only; each entry names its source; an unconfirmed or
provisional date is labelled as such; the calendar never says what a catalyst
implies.

### P1.3 Importance scoring

Rank items so the cap on brief length cuts the right things.

**Acceptance:** scoring is a documented, inspectable function of portfolio
weight, move size relative to the instrument's own volatility, source rank
(disclosure > ticker news > macro), and novelty; the score is reproducible from
the inputs and dumped in `--verbose`; it is never a model's opinion of what
matters.

### P1.4 Volatility-aware thresholds

A 2% day is unremarkable for one name and extraordinary for another.

**Acceptance:** the mover threshold becomes per-instrument, derived from
trailing realised volatility with a documented floor and ceiling; a
configuration override remains available; the static digest still prints raw
percentages so nothing is hidden by the smoothing.

---

## P2 — Make it remember

*Goal: continuity across months, and a widened lens — still research, never
recommendation.*

### P2.1 Historical thesis tracking

Record what you believed about a holding and when, then surface it when the
facts move against it.

**Acceptance:** a thesis is user-authored text with a date, stored locally and
never generated by the model; the brief resurfaces it only when a tracked fact
in it changes (a guidance revision, a margin trend break, a disclosure); the
resurfacing is neutral — it restates the recorded belief and the new fact side
by side and stops there.

### P2.2 Expanded institutional / smart-money intelligence

Widen beyond the current 13F diff: more tracked managers, position-size deltas,
and concentration changes.

**Acceptance:** every line stays in past tense as a filed fact ("disclosed",
"no longer listed"), with the filing date and the as-of quarter end shown so
the up-to-45-day lag is visible; diffs stay share-count-based so price
appreciation is never dressed up as buying; the existing test asserting no
advice-flavoured verb is extended to cover every new line type; the module
stays isolated and cannot sink the brief.

### P2.3 Research-candidate discovery

Deepen the radar: surface names outside the watchlist that keep appearing in
the same context as holdings — supply-chain neighbours, sector peers, repeat
co-mentions — alongside the existing gainers/trending feed.

**Acceptance:** output is explicitly labelled **research candidates**, capped at
a small number, and phrased as "appeared N times alongside X this month" —
never as an opportunity, an idea, or anything with an implied direction; adding
a candidate to the watchlist is a manual config edit, never automatic.

### P2.4 Optional fact-disagreement check

If a second model is ever added, this is the only permitted shape.

**Acceptance:** a different-family model reads the composed brief and flags
*factual* contradictions against the fetched data only; it does not rewrite,
score, rank, or opine; on disagreement the brief falls through to the static
digest rather than shipping an arbitrated version.

---

## The output contract

Whatever phase ships, the brief answers these in order, and stops:

**What happened → why → does it matter for this portfolio → what actually
changed → what deserves attention.**

There is no sixth question. "What opportunities are emerging?" is answered
only by the radar and P2.3 research candidates — labelled, capped, and
directionless.

---

## Explicitly rejected directions

| Rejected | Reason |
|---|---|
| **Signals, scores, or buy/sell/hold calls** | The edge does not survive transaction costs and does not survive out-of-sample testing: [docs/research/evidence-review.md](docs/research/evidence-review.md). This cadence is context, not alpha. |
| **Autonomous trading, broker integration, order placement** | Out of scope permanently. This is an intelligence and research system; it never touches an account. |
| **Multi-model debate / consensus panels** | Measurably worse: deliberative consensus scored ~76% against 82.4% for the best single model and 83.4% for independent confidence-weighted aggregation, via persuasive-error propagation ([docs/research/evidence-review.md](docs/research/evidence-review.md)). One strong model composes. |
| **Real-time / streaming cadence** | Large-cap news is priced in milliseconds; chasing it converts a calm monitor into an anxiety machine and contradicts the panic-prevention purpose. Three scheduled views a day is the ceiling. |
| **A hosted dashboard / web UI** | Not a phase. The product is one short brief you actually read. A dashboard is another tab to ignore. |

---

## How to propose a change

Open a GitHub Issue. A proposal that lands is one that states:

1. **Which phase** it belongs to (P0/P1/P2), or why it needs a new one.
2. **How it serves attention routing or panic prevention** — the two jobs.
3. **Its acceptance criteria**, in the style used above: observable, testable,
   and specific about the degraded case.
4. **Its guardrail check** — confirmation it introduces no signal, no advice
   language, and no autonomous action.

Proposals that decouple the brief from the holder's portfolio, add
recommendations, or introduce a debate panel are settled questions; reopen
them only with new evidence that contradicts the research docs directly.

---

Not investment advice.

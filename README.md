# parsec-market-brief

A US and global markets monitor for a fixed watchlist, run several times a
day. Each run fetches close prices, sweeps US/global news feeds, pulls a
market-wide radar (the day's biggest gainers and most-searched tickers),
checks SEC EDGAR for new institutional 13F filings, and prints one short
brief: **what moved, what changed, what to ignore.**

It is a monitoring tool. It does not produce signals, and that is a design
constraint rather than a missing feature — see [Guardrails](#guardrails).

---

## Why it exists

Following a portfolio plus the whole tape is a daily half-hour of
tab-flipping, and the failure mode is not missing a headline — it is reading
forty and retaining none. This collapses that into one screen: only the names
you actually hold or watch, only the moves past a threshold that matters for
that instrument, only the headlines that name something on the list, a radar
line for what the wider market is chasing today, and nothing you were already
shown this fortnight.

The hard part is not fetching. It is **suppression** — the layers below exist
to keep a quiet stretch looking quiet.

---

## Architecture

```
                     ┌─────────────────────────────────────────┐
                     │              config.yaml                │
                     │  watchlist · macro keywords · trackers  │
                     │      schedule times · radar knobs       │
                     └────────────────────┬────────────────────┘
                                          │
         ┌──────────────────┬─────────────┼──────────────┬──────────────────┐
         │                  │             │              │                  │
  ┌──────▼───────┐  ┌───────▼────────┐    │     ┌────────▼───────┐ ┌────────▼────────┐
  │  PRICE LAYER │  │   NEWS LAYER   │    │     │     RADAR      │ │  SMART MONEY    │
  │              │  │                │    │     │                │ │                 │
  │ Yahoo chart  │  │ 6 RSS/Atom     │    │     │ Yahoo day      │ │ SEC EDGAR 13F   │
  │ last vs prev │  │ feeds: Google  │    │     │ gainers +      │ │ quarterly, and  │
  │ close        │  │ News · CNBC ·  │    │     │ trending       │ │ silent on ~99%  │
  │              │  │ MarketWatch ·  │    │     │ tickers —      │ │ of days         │
  │ thin symbols:│  │ Yahoo          │    │     │ observed       │ │                 │
  │ single bar → │  │                │    │     │ activity, as   │ │ parse info table│
  │ read meta    │  │ require_xml    │    │     │ DATA, never    │ │ diff SHARES vs  │
  │ marks        │  │ guard rejects  │    │     │ picks          │ │ stored quarter  │
  └──────┬───────┘  │ HTML-at-200    │    │     └────────┬───────┘ └────────┬────────┘
         │          └───────┬────────┘    │              │                  │
         │                  │             │              │                  │
         │         ┌────────▼────────┐    │              │                  │
         │         │  DEDUPE (14d)   │    │              │                  │
         │         │  url hash → TTL │    │              │                  │
         │         └────────┬────────┘    │              │                  │
         │                  │             │              │                  │
         │         ┌────────▼────────┐    │              │                  │
         │         │  ENTITY FILTER  │    │              │                  │
         │         │ whole-token     │    │              │                  │
         │         │ match           │    │              │                  │
         │         │ rank: disclosure│    │              │                  │
         │         │ > ticker > macro│    │              │                  │
         │         └────────┬────────┘    │              │                  │
         │                  │             │              │                  │
         └───────────┬──────┴─────────────┴──────────────┴──────────────────┘
                     │
           ┌─────────▼──────────┐
           │    COMPOSE         │
           │                    │
           │  claude CLI?  ──── yes ──▶ narrative brief
           │       │                     │
           │       no                    ▼
           │       │              header check: does it
           │       │              start with "*📊"?
           │       │                     │
           │       │              no ────┘
           │       ▼              │
           │  STATIC DIGEST ◀─────┘
           │  (plain data dump) │
           └─────────┬──────────┘
                     │
           ┌─────────▼──────────┐
           │      SENDER        │
           │ ConsoleSender      │  ← default
           │ WhatsAppSender     │  ← documented stub
           └─────────┬──────────┘
                     │
           ┌─────────▼──────────┐
           │  ARCHIVE + SEEN    │
           │  data/briefs/*.md  │──┐
           │  seen-set persisted│  │
           │  only AFTER        │  │
           │  delivery          │  │
           └────────────────────┘  │
                     ▲             │
                     └─────────────┘
               last 3 briefs feed back into
               the prompt — kills the story
               that resurfaces under a new
               headline (URL hashing can't)
```

Every stage degrades instead of failing. One dead feed loses one source; a bad
symbol loses one quote and prints `price unavailable`; a dead SEC or radar
endpoint means "nothing new"; a missing model loses the prose but never the
data.

---

## Quickstart

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml     # then edit the watchlist
python -m market_brief --once
```

That prints the current brief to the console and exits. There is no daemon on
purpose — scheduling belongs to cron or a systemd timer, and a monitor you
cannot run by hand is a monitor you cannot trust.

| Flag | Effect |
|---|---|
| `--once` | Run the pipeline once and print the brief (the default). |
| `--no-compose` | Skip the narrative composer; print the static digest. |
| `--verbose` / `-v` | Log every feed, quote, and fallback to stderr. |

Tests:

```bash
pytest          # 119 tests, no network — every fetch is mocked
```

### Scheduling

The intended cadence is three runs a day — a pre-US-open view, a mid-session
view, and an after-close view. `schedule_times` in config.yaml records the
cadence (in the configured timezone); cron actually enforces it. For the
default `10:00 / 18:00 / 23:00` in `Asia/Dubai` (UTC+4, no DST):

```cron
0 6,14,19 * * *  cd /path/to/parsec-market-brief && python -m market_brief --once
```

### Configuration

`config.yaml` is the only input. If it is absent, `config.example.yaml` is read
instead (with a warning) so a fresh clone runs before it is configured.

A watchlist entry is either **priced** (has a `symbol`, gets a quote every run
and can be flagged as a mover) or **news-only** (has a `name` and aliases, is
matched in headlines, and is never quoted). News-only exists because **a wrong
symbol is worse than no symbol** — it prints a confident number for a
different instrument.

Environment overrides: `MARKET_BRIEF_CONFIG` (config path),
`MARKET_BRIEF_DATA_DIR` (state directory).

---

## Guardrails

The philosophy is **monitoring, not signals**. The tool reports what the market
did and what was published about it. It never says what to do about any of it.

This is enforced in five places, not one:

1. **The compose prompt** forbids buy/sell/hold calls in any wording, price
   targets, fair values, entry/exit levels, forecasts, and conviction language
   ("attractive", "cheap", "overvalued"). Every claim must attribute to a price
   move or a headline; if an item cannot be written without a recommendation,
   it is dropped.

2. **A positive header check** on the model's output. Blocklisting error text
   is leaky — a runner's failure modes return plain prose with assorted
   prefixes, and any of them would otherwise ship *as the brief*. So output is
   accepted **only** if it is shaped like the brief the prompt mandates, and
   anything else falls through to the static digest.

3. **The static digest** — the real output, and what you get with no model
   installed — is a plain data dump with no interpretation in it at all. It
   cannot drift into advice because it never writes a sentence.

4. **Radar framing.** The radar answers "what is the market chasing today" —
   day gainers and most-searched tickers, rendered as a number and a source
   and nothing else. The prompt forbids presenting any radar name as a pick,
   an opportunity, or something to act on, and a test asserts the rendered
   radar contains no advice-flavoured word. Discovery is data; what to do
   with it is not this tool's business.

5. **13F framing.** A 13F is a backward-looking regulatory disclosure of what a
   manager held at a past quarter end, published up to 45 days late. Every line
   is rendered in past tense as a filed fact ("disclosed", "no longer listed"),
   never as a move to mirror — and the quarter-over-quarter diff runs on
   **share counts**, not dollar values, so a position that merely rose in price
   is not dressed up as buying. A test asserts the rendered output contains no
   advice-flavoured verb.

The ordering matters: a model that cannot be reached costs you prose, never
correctness, and never an unreviewed sentence about money.

---

## Layout

```
market_brief/
  brief.py         pipeline: scan → compose → deliver, plus the prompt
  radar.py         market-wide day gainers + trending tickers (data only)
  smart_money.py   SEC EDGAR 13F watcher (isolated; cannot sink the brief)
  config.py        config.yaml loader + watchlist/tracker normalisation
  senders.py       ConsoleSender (default) · WhatsAppSender (stub)
  claude.py        optional narrative composer via the `claude` CLI
  __main__.py      the CLI
tests/             119 tests, fully mocked
docs/              partner brief + research (historical)
```

Dependencies are `httpx`, `PyYAML`, and `tzdata`. There is no market-data SDK,
no broker integration, and no database.

---

## Notes for whoever runs this next

- **All dates are reader-local**, not server-local (`timezone:` in config) —
  the same clock `schedule_times` is written in. On a UTC host the date would
  otherwise roll at the wrong hour and the dedupe window would compare
  against tomorrow.
- **`tzdata` is a real dependency**, not padding: `zoneinfo` ships no bundled
  IANA database, so Windows hosts and slim containers have none.
- **The seen-set is persisted only after delivery**, not during the scan. A
  run that dies mid-pipeline does not consume its headlines — they surface
  again on the next run instead of vanishing into the TTL window unreported.
- **Matching is whole-token, not substring.** Short tickers ("ARM"-sized)
  occur inside ordinary words, and every false positive costs a slot in a
  capped brief.
- **The seen-set has a 14-day TTL**, so a headline cannot resurface for a
  fortnight but nothing is blocklisted permanently.
- **The radar is a bonus section**, isolated exactly like the 13F watcher: it
  can fail, be rate-limited, or be switched off (`radar_enabled: false`)
  without costing the watchlist part of the brief anything.
- **`WhatsAppSender` is a deliberate stub.** The gateway is deployment-specific
  and shipping a half-configured one invites sending a brief to the wrong
  number. The HTTP contract is documented in its docstring; secrets belong in
  the environment, never in `config.yaml`.

---

All rights reserved © 2026 ParSec / Omar Mosallam. Proprietary — see `LICENSE`.
Not investment advice.

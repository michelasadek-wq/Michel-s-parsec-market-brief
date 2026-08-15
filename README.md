# parsec-market-brief

A U.S.-first market monitor with major global-market coverage and a separate,
comprehensive **IBKR Daily View**.

The repository now has two deliberately different outputs:

1. **Market brief** — terse pre-market monitoring: what moved, what changed,
   what to ignore. It never emits a trading call.
2. **IBKR Daily View** — private portfolio decision support: profit and loss,
   allocation, concentration, sector/region/factor exposure, trailing and
   forward P/E, PEG, growth, technical context, transparent BUY/HOLD/SELL
   labels, and exact deployment amounts for cash the user marks deployable.

Neither feature logs in to a broker or places orders.

---

## Coverage

The default information hierarchy is:

- **U.S. first:** portfolio tickers, S&P 500, Nasdaq, Dow, the Federal Reserve,
  Treasury yields, inflation, employment, the dollar and volatility.
- **Major global markets:** Europe (STOXX 600, FTSE, DAX, ECB), Asia-Pacific
  (Nikkei, Hang Seng, CSI 300, ASX and Bank of Japan), commodities and global
  trade policy.
- **Portfolio-led ticker news:** the app generates a Yahoo headline feed for
  every configured symbol instead of hard-coding a few companies.
- **Institutional disclosures:** SEC EDGAR 13F changes, always shown as delayed
  filed facts rather than a move to copy.

Any Yahoo Finance listing suffix can be used in the watchlist. Common examples
include `.L` (London), `.DE` (Germany), `.T` (Tokyo), `.HK` (Hong Kong), `.TO`
(Toronto) and `.AX` (Australia). The tool preserves the configured symbol and
never guesses another listing.

---

## Quickstart

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m market_brief --once
```

Run the private portfolio view after configuring its input:

```bash
python -m market_brief --view
```

Run both outputs:

```bash
python -m market_brief --all
```

| Flag | Effect |
|---|---|
| `--once` | Run the U.S./global monitoring brief (default). |
| `--view` | Run the comprehensive IBKR Daily View only. |
| `--all` | Run both reports. |
| `--no-compose` | Skip the optional model composer for the monitoring brief. |
| `--verbose` / `-v` | Log each feed, quote and graceful fallback. |

Scheduling remains outside the process: cron, a systemd timer or the deployment
platform should invoke the command. The example uses `08:00` and `08:15`
America/New_York so the reports are available before the U.S. cash session.

Tests are fully mocked:

```bash
pytest
```

---

## Configure the market brief

`config.yaml` controls the watchlist, model, macro terms and SEC trackers. A
priced row needs a Yahoo symbol. A row may be `watch_only`, and a name without a
trusted symbol may be `news_only`.

```yaml
timezone: "America/New_York"

market_brief:
  enabled: true
  schedule_time: "08:00"
  watchlist:
    - {symbol: "QQQM", name: "Invesco NASDAQ 100 ETF", market: us}
    - {symbol: "TSM", name: "Taiwan Semiconductor", aliases: ["TSMC"], market: us}
    - {symbol: "VWRA.L", name: "Vanguard FTSE All-World UCITS ETF", market: global}
    - {symbol: "7203.T", name: "Toyota Motor", market: asia_pacific, watch_only: true}
  macro_keywords: ["Federal Reserve", "Treasury yield", "VIX", "ECB", "oil"]
```

The monitoring pipeline is:

```text
watchlist → global feeds + per-ticker feeds → entity filtering → 14-day URL
dedupe → prices + SEC filings → static digest or optional narrative → delivery
```

Every layer degrades independently. One dead feed loses one source; a bad quote
prints `price unavailable`; a failed SEC request removes only the 13F section;
a missing model falls back to deterministic text.

---

## Configure the IBKR Daily View

The view accepts any one of:

- a read-only IBKR Activity Flex Query retrieved with environment secrets;
- a local IBKR Activity Statement CSV containing the `Open Positions` section;
- a conventional positions CSV; or
- explicit rows in the private `config.yaml`.

### Option A — read-only IBKR Flex Web Service

Enable Flex input in the private config:

```yaml
portfolio_view:
  enabled: true
  base_currency: "USD"
  ibkr_flex:
    enabled: true
```

Provide the saved Activity Flex Query credentials only through environment
variables or encrypted repository secrets:

```bash
export IBKR_FLEX_TOKEN="..."
export IBKR_FLEX_QUERY_ID="..."
python -m market_brief --view --no-compose
```

The client uses IBKR's official version 3 two-step flow, sets a User-Agent,
polls while statement generation is in progress, and parses the report in
memory. Tokens, Query IDs, reference codes, raw XML and account identifiers are
not logged or archived. Flex mode is read-only and contains no order endpoint.

### Option B — IBKR Activity Statement

Export a CSV from IBKR and point the private config to it:

```yaml
portfolio_view:
  enabled: true
  ibkr_csv: "data/ibkr-activity.csv"
  base_currency: "USD"
  deployable_cash: 500
  max_position_pct: 20
  max_sector_pct: 35
  max_snapshot_age_days: 3
```

The parser recognises IBKR's sectioned `Open Positions,Header` and
`Open Positions,Data` rows. It also understands common columns such as Symbol,
Quantity, Cost Price, Close Price, Value and Unrealized P/L.

### Option C — simple CSV

```csv
Symbol,Description,Quantity,Average Price,Currency,Current Price,Unrealized P/L,Realized P/L
QQQM,Invesco NASDAQ 100 ETF,10,210,USD,225,150,25
```

### Option D — config rows

```yaml
portfolio_view:
  enabled: true
  base_currency: "USD"
  deployable_cash: 500
  positions:
    - symbol: "QQQM"
      quantity: 10
      average_cost: 210
      currency: "USD"
      sector: "Diversified"
      region: "United States"
      asset_class: "ETF"
      factors: ["Growth", "Large Cap", "Technology tilt"]
      realized_pnl: 25
```

Config metadata is merged into matching Flex/CSV positions, which lets the broker
remain the source for quantity/cost/value while config provides sector, region
and factor classifications. In live Flex mode, config-only symbols are not
treated as holdings, so a closed position cannot reappear from stale metadata.

Both report sections honour their `enabled` switch. A configured CSV path that
does not exist is a CLI error instead of a successful empty report.

Do not commit `config.yaml` or an IBKR export. Keep only
`config.example.yaml` in version control.

---

## What “IBKR Daily View” contains

The report follows the comprehensive portfolio review contract:

1. **Portfolio snapshot** — securities value, deployable cash, daily P&L,
   unrealized P&L, realized P&L and their combined supplied total. The combined
   figure is shown only when both inputs are complete; it is not presented as
   TWR, MWR or guaranteed lifetime performance.
2. **Every holding** — value, weight, daily and open-position P&L, trailing/forward
   P/E, PEG, earnings-growth estimate, price versus 50/200-day averages and
   distance from the 52-week high.
3. **Exposure** — sector, region, asset class themes/factors, top-three
   concentration and Herfindahl index.
4. **Risks and gaps** — configured limit breaches, missing realized P&L,
   unavailable FX and incomplete fundamental data.
5. **Context and catalysts** — fresh portfolio-linked and macro headlines.
6. **BUY/HOLD/SELL** — a reproducible scoring rule, reasons for the score and
   exact amounts for deployable cash, subject to the concentration cap.
7. **Research candidates** — non-held names scored with the same rule; adding a
   name never creates a broker position.

The deterministic screen gives one point for each of: P/E at or below 30x, PEG
at or below 1.5, earnings growth of at least 15%, profit margin of at least 15%
and price at or above the 200-day average. It subtracts points for demanding
valuation, negative growth/margins, a material break below the 200-day average,
and concentration-limit breaches. Fewer than two usable evidence fields always
defaults to HOLD. The complete reason list is printed beside the label.

The bands are intentionally transparent and configurable; they are not a claim
of sector-relative fair value. A high-growth semiconductor and a utility should
not be compared on P/E alone.

---

## Privacy and guardrails

- No IBKR username, password or trading session is accepted.
- Optional Flex access accepts only a read-only report token and Query ID from
  environment variables; neither value is logged or stored by the application.
- No order-placement code exists.
- Portfolio rows and P&L are not written to `data/briefs/`.
- The market brief stays monitoring-only and cannot copy portfolio-view calls.
- IBKR Daily View recommendations are a rules screen, never an automatic order.
- Missing data is labelled unavailable; no FX, price or fundamental is guessed.
- Incomplete account value blocks allocation percentages, BUY/SELL calls and
  cash deployment instead of silently omitting positions.
- CSV snapshot dates are shown; stale snapshots force HOLD under the configured
  maximum-age rule.
- 13F filings are backward-looking and may arrive up to 45 days after quarter
  end. They are never treated as current holdings or a signal to imitate.

---

## Layout

```text
market_brief/
  brief.py             U.S./global monitoring pipeline and composer prompt
  portfolio_view.py    IBKR Flex/CSV parsing, analytics, risk and action screen
  smart_money.py       isolated SEC EDGAR 13F watcher
  config.py            safe YAML loading and both feature configurations
  senders.py           console sender and documented WhatsApp stub
  claude.py            optional monitoring-brief composer
  __main__.py          --once / --view / --all CLI
tests/                 offline mocked test suite
docs/                  roadmap, source evaluation and partner notes
```

Dependencies are `httpx`, `PyYAML`, `tzdata`, `pytest` and `pytest-asyncio`.
There is no market-data SDK, database or order-capable broker integration.

---

All rights reserved © 2026 ParSec / Omar Mosallam. Proprietary — see `LICENSE`.
Not investment advice.

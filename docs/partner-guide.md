# Partner guide

Everything a collaborator needs to work safely in this U.S.-first / global
market-monitoring repository.

---

## 1. Repository workflow

- Work on a branch and open a pull request into `main`.
- Keep feature changes additive unless deletion is explicitly part of scope.
- Run `pytest` before review; all network calls in tests must be mocked.
- Keep source/as-of behaviour visible for every new numeric field.
- A failed source must degrade locally rather than sink the daily run.

Use a personal fork for experiments that should not affect the shared roadmap.
Sync from upstream when useful and contribute back only the parts that have a
clear test and product contract.

---

## 2. Two outputs, two contracts

### Market brief

The monitoring brief covers configured U.S. holdings first, then the major
global context that can affect them. It is terse and never says BUY, HOLD or
SELL. An optional model may compose the prose, but only from fetched facts; a
shape or runtime failure falls back to the static digest.

### IBKR Daily View

The separate view reads a local portfolio snapshot and reports:

- daily, realized and unrealized P&L;
- position weights and concentration;
- sector, region and factor exposure;
- trailing/forward P/E, PEG and earnings growth;
- price-trend context;
- rule-based BUY/HOLD/SELL labels and exact deployable-cash amounts;
- non-held research candidates.

Every action prints its score and reasons. It is decision support, not an order.

---

## 3. Supply an IBKR snapshot

The safest current path is a local Activity Statement CSV:

1. In IBKR Client Portal, open **Performance & Reports → Statements**.
2. Generate an Activity Statement that includes **Open Positions**.
3. Download it as CSV.
4. Store it outside version control, for example `data/ibkr-activity.csv`.
5. Point the private `config.yaml` to it:

```yaml
portfolio_view:
  enabled: true
  ibkr_csv: "data/ibkr-activity.csv"
  base_currency: "USD"
  deployable_cash: 0
  max_snapshot_age_days: 3
```

Then run:

```bash
python -m market_brief --view
```

The app accepts no IBKR username, password, token or live session. A future
read-only Flex adapter is a roadmap item, not a hidden capability.

The report displays the statement date when IBKR supplies one and otherwise
labels the CSV file-modified date. A snapshot older than the configured limit
forces HOLD and prevents cash deployment.

---

## 4. Add classification metadata

CSV exports usually do not contain the portfolio taxonomy needed for exposure
analysis. Add matching symbol rows in private config; the broker numbers remain
authoritative and the metadata is merged by ticker.

```yaml
portfolio_view:
  positions:
    - symbol: "QQQM"
      sector: "Diversified"
      region: "United States"
      asset_class: "ETF"
      factors: ["Growth", "Large Cap", "Technology tilt"]
```

Do not commit real quantities, average costs, account identifiers or exports.

---

## 5. Safety boundaries

- No trade execution or broker write permission.
- No private portfolio values in the brief archive.
- No guessed price, FX rate or fundamental.
- Missing valuation/growth data defaults to HOLD.
- Missing FX or market value makes the account total unavailable and blocks
  allocation-dependent recommendations.
- Position and sector limits are hard constraints on cash deployment, including
  shared capacity across multiple candidates in the same sector.
- 13F filings are delayed disclosures, not current-manager intent.
- Monitoring prose cannot inherit calls from the portfolio-view module.
- Model debate is not used; factual disagreement should fall back to static
  data rather than persuasive arbitration.

---

## 6. Review checklist

Before approving a change, verify:

1. Does it serve U.S. holdings first and major global context second?
2. Is its source and as-of behaviour explicit?
3. Does a bad response degrade to unavailable?
4. Are private fields kept out of archives and prompts?
5. Are market brief and portfolio-view action semantics still separate?
6. Are tests deterministic and offline?

Not investment advice.

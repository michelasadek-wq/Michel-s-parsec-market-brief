# Source evaluation — U.S. and major global markets

Live-source behaviour changes, so this file records the intended hierarchy and
the failure mode the code must preserve. A source is useful only when it can
degrade without corrupting the report.

## Portfolio prices and technical marks

| Source | Use | Constraint |
|---|---|---|
| Yahoo Finance chart endpoint | Daily closes, currency, 50/200-day and 52-week context | Public and unofficial; listing suffix must be configured exactly; missing data stays unavailable. |
| IBKR Activity Statement CSV | Quantity, average cost, value, realized/unrealized P&L | Local export only; schemas vary by report; no broker login. |
| Yahoo Finance FX chart | Explicit close for conversion to portfolio base currency | Try direct and inverse pairs; never assume 1:1. |

## U.S. market and official policy

| Source | Use | Constraint |
|---|---|---|
| Federal Reserve press-release XML feed | Official monetary-policy and regulatory releases | Content-type is asserted; HTML-at-200 is rejected. |
| SEC EDGAR submissions and filing documents | 13F filing detection and position diffs | Requires contact in User-Agent; holdings are delayed and must be framed as filed facts. |
| Google News U.S. market/macro queries | Broad discovery across indices, rates, inflation and labour | Aggregated headlines, not primary evidence; entity filter and dedupe required. |
| Yahoo per-symbol headline RSS | First-class coverage for every configured ticker | Dynamic and capped to bound runtime; a dead ticker feed loses only that source. |

## Major global context

| Source | Use | Constraint |
|---|---|---|
| Google News Europe query | STOXX, FTSE, DAX and ECB context | Discovery layer; only portfolio/macro matches survive. |
| Google News Asia-Pacific query | Nikkei, Hang Seng, CSI and ASX context | Same filtering and dedupe contract. |
| Google News global-macro query | Oil, dollar, trade policy and cross-market shocks | Capped below holding-specific news. |
| Yahoo exchange-suffix quotes | Non-U.S. listing prices | Symbols are never inferred or silently substituted. |

## Rejected shortcuts

- Scraping exchange web pages with no stable machine interface.
- Treating a search headline as verified fundamental data.
- Filling a failed FX conversion with 1.0.
- Comparing absolute P/E bands as if they were sector-relative fair value.
- Treating a delayed 13F filing as a manager's current position or intent.
- Sending broker exports into the model composer or semantic brief archive.

## Next source work

1. A licensed fundamentals source with margins, ROIC, leverage and revisions.
2. A sourced corporate-actions and earnings calendar.
3. ETF constituent histories for look-through and overlap.
4. Optional read-only IBKR Flex Query ingestion, with CSV retained as fallback.

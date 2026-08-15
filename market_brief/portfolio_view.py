"""Comprehensive, local-first IBKR Daily View.

This is deliberately separate from :mod:`market_brief.brief`:

* the market brief is a terse monitoring surface and never emits a signal;
* this module is a portfolio decision-support surface with transparent,
  rule-based BUY/HOLD/SELL labels and exact deployment amounts.

Positions may come from a local IBKR Activity Statement CSV or from config.
No broker credentials are requested, no order endpoint exists, and portfolio
details are never written to the market-brief archive.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import brief, config

logger = logging.getLogger(__name__)

_YAHOO_QUOTE_URL = (
    "https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
)
_YAHOO_FX_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?range=5d&interval=1d"
)
_HTTP_TIMEOUT = 15

_NUMBER_FIELDS = {
    "quantity", "average_cost", "current_price", "market_value",
    "unrealized_pnl", "realized_pnl", "trailing_pe", "forward_pe", "peg",
    "earnings_growth_pct", "revenue_growth_pct", "profit_margin_pct",
    "debt_to_equity", "price_to_book", "dividend_yield_pct", "beta",
    "fifty_day_average", "two_hundred_day_average", "fifty_two_week_high",
    "fifty_two_week_low", "market_cap",
}

_CSV_ALIASES = {
    "symbol": ("symbol", "ticker", "financial instrument"),
    "name": ("description", "name", "financial instrument description"),
    "quantity": ("quantity", "position", "qty"),
    "average_cost": (
        "average price", "average cost", "avg price", "cost price",
        "cost basis price",
    ),
    "currency": ("currency", "currency primary"),
    "current_price": ("close price", "current price", "mark price", "price"),
    "market_value": ("value", "market value", "position value"),
    "unrealized_pnl": (
        "unrealized p/l", "unrealized pnl", "unrealized profit/loss",
    ),
    "realized_pnl": ("realized p/l", "realized pnl", "realized profit/loss"),
    "asset_class": ("asset category", "asset class", "security type"),
    "account": ("account", "account id", "accountid"),
}


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _number(value) -> float | None:
    """Parse broker-style numbers: commas, percent signs, and parentheses."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"--", "N/A", "n/a"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace("%", "")
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _row_value(row: dict, aliases: tuple[str, ...]):
    normalised = {_normalise_key(key): value for key, value in row.items()}
    for alias in aliases:
        if _normalise_key(alias) in normalised:
            return normalised[_normalise_key(alias)]
    return None


def _normalise_broker_row(row: dict, source: str) -> dict | None:
    symbol = str(_row_value(row, _CSV_ALIASES["symbol"]) or "").strip()
    if not symbol:
        return None
    raw = {
        "symbol": symbol,
        "name": str(_row_value(row, _CSV_ALIASES["name"]) or symbol).strip(),
        "currency": str(_row_value(row, _CSV_ALIASES["currency"]) or "USD")
        .strip().upper(),
        "asset_class": str(
            _row_value(row, _CSV_ALIASES["asset_class"]) or "Equity"
        ).strip(),
        "account": str(_row_value(row, _CSV_ALIASES["account"]) or "").strip(),
        "sector": "Unclassified",
        "region": "Unclassified",
        "factors": [],
        "watch_only": False,
        "data_source": source,
        "as_of": "",
    }
    for field in (
        "quantity", "average_cost", "current_price", "market_value",
        "unrealized_pnl", "realized_pnl",
    ):
        raw[field] = _number(_row_value(row, _CSV_ALIASES[field]))
    if raw["quantity"] in (None, 0):
        return None
    return raw


def load_ibkr_csv(path: str | Path) -> list[dict]:
    """Load a simple positions CSV or IBKR Activity Statement CSV.

    IBKR Activity Statements are sectioned rather than rectangular. This
    parser recognises ``Open Positions,Header`` / ``Open Positions,Data`` rows
    and also accepts a conventional one-header-row export. Unknown columns are
    ignored; malformed rows degrade independently.
    """
    path = Path(path)
    if not path.exists():
        logger.warning("IBKR CSV %s does not exist", path)
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except Exception as exc:
        logger.warning("Could not read IBKR CSV %s: %s", path, exc)
        return []
    if not rows:
        return []

    output: list[dict] = []
    first = [_normalise_key(cell) for cell in rows[0]]
    if "symbol" in first or "ticker" in first:
        headers = rows[0]
        for values in rows[1:]:
            mapping = dict(zip(headers, values))
            parsed = _normalise_broker_row(mapping, f"IBKR CSV: {path.name}")
            if parsed:
                output.append(parsed)
        return aggregate_positions(output)

    section_headers: dict[str, list[str]] = {}
    for values in rows:
        if len(values) < 2:
            continue
        section = values[0].strip()
        kind = values[1].strip().casefold()
        if kind == "header":
            section_headers[section.casefold()] = values
            continue
        if kind != "data" or section.casefold() != "open positions":
            continue
        headers = section_headers.get(section.casefold())
        if not headers:
            continue
        mapping = dict(zip(headers, values))
        parsed = _normalise_broker_row(mapping, f"IBKR Activity Statement: {path.name}")
        if parsed:
            output.append(parsed)
    return aggregate_positions(output)


def aggregate_positions(rows: list[dict]) -> list[dict]:
    """Aggregate duplicate symbols while preserving weighted average cost."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("symbol", "")).strip()].append(row)

    output = []
    for symbol, parts in grouped.items():
        if not symbol:
            continue
        base = dict(parts[0])
        quantities = [p.get("quantity") for p in parts]
        base["quantity"] = sum(q for q in quantities if isinstance(q, (int, float)))
        weighted = [
            (abs(p["quantity"]), p.get("average_cost"))
            for p in parts
            if isinstance(p.get("quantity"), (int, float))
            and isinstance(p.get("average_cost"), (int, float))
        ]
        weight = sum(item[0] for item in weighted)
        if weight:
            base["average_cost"] = sum(q * cost for q, cost in weighted) / weight
        for field in ("market_value", "unrealized_pnl", "realized_pnl"):
            values = [p.get(field) for p in parts]
            base[field] = (
                sum(v for v in values if isinstance(v, (int, float)))
                if any(isinstance(v, (int, float)) for v in values)
                else None
            )
        output.append(base)
    return output


def merge_positions(config_rows: list[dict], csv_rows: list[dict]) -> list[dict]:
    """Merge private broker numbers with config-only classification metadata."""
    configured = {row["symbol"]: row for row in config_rows}
    merged: list[dict] = []
    for broker in csv_rows:
        row = dict(configured.get(broker["symbol"], {}))
        for key, value in broker.items():
            if value is not None and value != "":
                row[key] = value
        row.setdefault("name", broker["symbol"])
        row.setdefault("sector", "Unclassified")
        row.setdefault("region", "Unclassified")
        row.setdefault("factors", [])
        row["watch_only"] = False
        merged.append(row)
    broker_symbols = {row["symbol"] for row in csv_rows}
    merged.extend(row for row in config_rows if row["symbol"] not in broker_symbols)
    return aggregate_positions(merged)


def _raw(value):
    if isinstance(value, dict):
        return value.get("raw")
    return value


def fetch_fundamentals(symbol: str) -> dict:
    """Fetch a compact valuation/technical pack; failure returns unavailable."""
    out = {field: None for field in _NUMBER_FIELDS - {
        "quantity", "average_cost", "current_price", "market_value",
        "unrealized_pnl", "realized_pnl",
    }}
    out.update({
        "ok": False,
        "source": "Yahoo Finance quote",
        "as_of": datetime.now(timezone.utc).date().isoformat(),
    })
    try:
        response = httpx.get(
            _YAHOO_QUOTE_URL.format(symbol=symbol),
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers=brief._HTTP_HEADERS,
        )
        response.raise_for_status()
        results = ((response.json().get("quoteResponse") or {}).get("result") or [])
        if not results:
            return out
        data = results[0]
        mapping = {
            "trailing_pe": "trailingPE",
            "forward_pe": "forwardPE",
            "peg": "pegRatio",
            "price_to_book": "priceToBook",
            "dividend_yield_pct": "trailingAnnualDividendYield",
            "beta": "beta",
            "fifty_day_average": "fiftyDayAverage",
            "two_hundred_day_average": "twoHundredDayAverage",
            "fifty_two_week_high": "fiftyTwoWeekHigh",
            "fifty_two_week_low": "fiftyTwoWeekLow",
            "market_cap": "marketCap",
        }
        for field, yahoo_field in mapping.items():
            out[field] = _number(_raw(data.get(yahoo_field)))
        if out["dividend_yield_pct"] is not None:
            out["dividend_yield_pct"] *= 100
        trailing_eps = _number(_raw(data.get("epsTrailingTwelveMonths")))
        forward_eps = _number(_raw(data.get("epsForward")))
        if trailing_eps and forward_eps and trailing_eps > 0:
            out["earnings_growth_pct"] = (forward_eps / trailing_eps - 1) * 100
        if (
            out["peg"] is None
            and out["forward_pe"] is not None
            and out["earnings_growth_pct"] is not None
            and out["earnings_growth_pct"] > 0
        ):
            out["peg"] = out["forward_pe"] / out["earnings_growth_pct"]
        timestamp = _number(data.get("regularMarketTime"))
        if timestamp:
            out["as_of"] = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        out["ok"] = True
    except Exception as exc:
        logger.warning("Fundamentals unavailable for %s: %s", symbol, exc)
    return out


def fetch_fx_rate(source: str, target: str) -> dict:
    """Fetch a sourced FX close, trying direct then inverse; never assume 1:1."""
    source, target = source.upper(), target.upper()
    today = datetime.now(timezone.utc).date().isoformat()
    if source == target:
        return {
            "ok": True, "rate": 1.0, "pair": f"{source}/{target}",
            "source": "identity", "as_of": today,
        }
    for ticker, invert in ((f"{source}{target}=X", False), (f"{target}{source}=X", True)):
        try:
            response = httpx.get(
                _YAHOO_FX_URL.format(symbol=ticker),
                timeout=_HTTP_TIMEOUT,
                follow_redirects=True,
                headers=brief._HTTP_HEADERS,
            )
            response.raise_for_status()
            result = (((response.json().get("chart") or {}).get("result") or [None])[0])
            if not result:
                continue
            closes = ((((result.get("indicators") or {}).get("quote") or [{}])[0])
                      .get("close") or [])
            closes = [value for value in closes if isinstance(value, (int, float))]
            if not closes or closes[-1] == 0:
                continue
            rate = 1 / closes[-1] if invert else closes[-1]
            timestamps = result.get("timestamp") or []
            as_of = today
            if timestamps:
                as_of = datetime.fromtimestamp(timestamps[-1], timezone.utc).date().isoformat()
            return {
                "ok": True, "rate": float(rate), "pair": f"{source}/{target}",
                "source": "Yahoo Finance FX close", "as_of": as_of,
            }
        except Exception:
            continue
    logger.warning("FX rate unavailable for %s/%s", source, target)
    return {
        "ok": False, "rate": None, "pair": f"{source}/{target}",
        "source": "Yahoo Finance FX close", "as_of": today,
    }


def _merge_fundamentals(row: dict, fetched: dict) -> dict:
    """Config overrides win over public data and remain explicitly sourced."""
    merged = dict(fetched)
    supplied = False
    for field in _NUMBER_FIELDS:
        if row.get(field) is not None:
            merged[field] = row[field]
            supplied = True
    if supplied:
        merged["source"] = row.get("data_source") or "config override"
        merged["as_of"] = row.get("as_of") or merged.get("as_of")
        merged["ok"] = True
    return merged


def _convert(value: float | None, fx: dict) -> float | None:
    if value is None or not fx.get("ok"):
        return None
    return value * fx["rate"]


def analyse_position(row: dict, quote: dict, fundamentals: dict,
                     base_currency: str, fx: dict) -> dict:
    """Calculate one holding/candidate without mutating private source data."""
    out = dict(row)
    out["quote_ok"] = bool(quote.get("ok"))
    out["price"] = quote.get("last") if quote.get("ok") else row.get("current_price")
    out["previous_close"] = quote.get("prev") if quote.get("ok") else None
    out["daily_change_pct"] = quote.get("change_pct") if quote.get("ok") else None
    out["price_currency"] = (
        str(quote.get("currency") or row.get("currency") or base_currency).upper()
    )
    out["fx"] = fx
    out["base_currency"] = base_currency
    out["fundamentals"] = _merge_fundamentals(row, fundamentals)

    quantity = row.get("quantity")
    price = out["price"]
    is_holding = not row.get("watch_only") and isinstance(quantity, (int, float))
    market_local = row.get("market_value")
    if market_local is None and is_holding and isinstance(price, (int, float)):
        market_local = quantity * price
    cost_local = None
    if is_holding and isinstance(row.get("average_cost"), (int, float)):
        cost_local = quantity * row["average_cost"]
    unrealized_local = row.get("unrealized_pnl")
    if unrealized_local is None and market_local is not None and cost_local is not None:
        unrealized_local = market_local - cost_local
    daily_local = None
    if is_holding and quote.get("ok"):
        daily_local = quantity * (quote["last"] - quote["prev"])

    out.update({
        "is_holding": is_holding,
        "market_value_local": market_local,
        "market_value_base": _convert(market_local, fx),
        "cost_value_local": cost_local,
        "cost_value_base": _convert(cost_local, fx),
        "unrealized_pnl_local": unrealized_local,
        "unrealized_pnl_base": _convert(unrealized_local, fx),
        "realized_pnl_local": row.get("realized_pnl"),
        "realized_pnl_base": _convert(row.get("realized_pnl"), fx),
        "daily_pnl_local": daily_local,
        "daily_pnl_base": _convert(daily_local, fx),
        "allocation_pct": 0.0,
    })
    out["total_return_pct"] = (
        unrealized_local / cost_local * 100
        if unrealized_local is not None and cost_local not in (None, 0)
        else None
    )
    return out


def _sum_complete(rows: list[dict], field: str) -> tuple[float, bool]:
    holdings = [row for row in rows if row.get("is_holding")]
    values = [row.get(field) for row in holdings]
    return (
        sum(value for value in values if isinstance(value, (int, float))),
        bool(holdings) and all(isinstance(value, (int, float)) for value in values),
    )


def portfolio_summary(rows: list[dict], deployable_cash: float,
                      base_currency: str) -> dict:
    holdings = [row for row in rows if row.get("is_holding")]
    market_value, market_complete = _sum_complete(holdings, "market_value_base")
    cost_value, cost_complete = _sum_complete(holdings, "cost_value_base")
    daily, daily_complete = _sum_complete(holdings, "daily_pnl_base")
    unrealized, unrealized_complete = _sum_complete(holdings, "unrealized_pnl_base")
    realized, realized_complete = _sum_complete(holdings, "realized_pnl_base")
    account_value = market_value + deployable_cash

    for row in holdings:
        value = row.get("market_value_base")
        row["allocation_pct"] = (
            value / account_value * 100
            if isinstance(value, (int, float)) and account_value
            else 0.0
        )

    total_profit = None
    if unrealized_complete and realized_complete:
        total_profit = unrealized + realized
    return {
        "base_currency": base_currency,
        "positions": len(holdings),
        "securities_value": market_value,
        "securities_value_complete": market_complete,
        "cost_value": cost_value,
        "cost_value_complete": cost_complete,
        "deployable_cash": deployable_cash,
        "account_value": account_value,
        "daily_pnl": daily,
        "daily_pnl_complete": daily_complete,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_complete": unrealized_complete,
        "realized_pnl": realized,
        "realized_pnl_complete": realized_complete,
        "total_profit": total_profit,
        "total_profit_complete": total_profit is not None,
    }


def exposure(rows: list[dict], field: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    denominator = sum(
        row.get("market_value_base") or 0
        for row in rows if row.get("is_holding")
    )
    if not denominator:
        return {}
    for row in rows:
        if not row.get("is_holding") or row.get("market_value_base") is None:
            continue
        labels = row.get(field)
        if not isinstance(labels, list):
            labels = [labels or "Unclassified"]
        for label in labels or ["Unclassified"]:
            totals[str(label)] += row["market_value_base"]
    return {
        key: value / denominator * 100
        for key, value in sorted(totals.items(), key=lambda item: item[1], reverse=True)
    }


def _pe_assessment(value: float | None) -> str:
    if value is None or value <= 0:
        return "unavailable/not meaningful"
    if value < 15:
        return "low absolute multiple"
    if value <= 25:
        return "moderate absolute multiple"
    if value <= 40:
        return "elevated absolute multiple"
    return "high absolute multiple"


def _peg_assessment(value: float | None) -> str:
    if value is None or value <= 0:
        return "unavailable/not meaningful"
    if value < 1:
        return "growth may exceed the multiple"
    if value <= 2:
        return "multiple broadly balanced with growth"
    return "multiple is demanding versus growth"


def technical_context(row: dict) -> str:
    facts = row.get("fundamentals") or {}
    price = row.get("price")
    if not isinstance(price, (int, float)):
        return "price unavailable"
    parts = []
    for label, field in (("50d", "fifty_day_average"), ("200d", "two_hundred_day_average")):
        average = facts.get(field)
        if isinstance(average, (int, float)) and average:
            parts.append(f"{(price / average - 1) * 100:+.1f}% vs {label}")
    high = facts.get("fifty_two_week_high")
    if isinstance(high, (int, float)) and high:
        parts.append(f"{(price / high - 1) * 100:+.1f}% vs 52w high")
    return "; ".join(parts) or "trend data unavailable"


def score_position(row: dict, sector_pct: float, max_position_pct: float,
                   max_sector_pct: float, min_buy_score: int) -> dict:
    """Transparent action screen; every point is returned as a reason."""
    facts = row.get("fundamentals") or {}
    score = 0
    reasons: list[str] = []
    data_points = 0

    pe = facts.get("forward_pe") or facts.get("trailing_pe")
    if isinstance(pe, (int, float)) and pe > 0:
        data_points += 1
        if pe <= 30:
            score += 1
            reasons.append("P/E is at or below the screen's 30x ceiling")
        elif pe > 50:
            score -= 1
            reasons.append("P/E is above 50x")

    peg = facts.get("peg")
    if isinstance(peg, (int, float)) and peg > 0:
        data_points += 1
        if peg <= 1.5:
            score += 1
            reasons.append("PEG is 1.5 or lower")
        elif peg > 2.5:
            score -= 1
            reasons.append("PEG is above 2.5")

    growth = facts.get("earnings_growth_pct")
    if isinstance(growth, (int, float)):
        data_points += 1
        if growth >= 15:
            score += 1
            reasons.append("earnings growth is at least 15%")
        elif growth < 0:
            score -= 1
            reasons.append("earnings growth is negative")

    margin = facts.get("profit_margin_pct")
    if isinstance(margin, (int, float)):
        data_points += 1
        if margin >= 15:
            score += 1
            reasons.append("profit margin is at least 15%")
        elif margin < 0:
            score -= 1
            reasons.append("profit margin is negative")

    price = row.get("price")
    average_200 = facts.get("two_hundred_day_average")
    if isinstance(price, (int, float)) and isinstance(average_200, (int, float)):
        data_points += 1
        if price >= average_200:
            score += 1
            reasons.append("price is above the 200-day average")
        elif price < average_200 * 0.85:
            score -= 1
            reasons.append("price is more than 15% below the 200-day average")

    allocation = row.get("allocation_pct", 0)
    if row.get("is_holding") and allocation > max_position_pct:
        score -= 2
        reasons.append(f"position is above the {max_position_pct:.0f}% concentration limit")
    if row.get("is_holding") and sector_pct > max_sector_pct:
        score -= 1
        reasons.append(f"sector is above the {max_sector_pct:.0f}% exposure limit")

    if data_points < 2:
        action = "HOLD"
        reasons.append("fewer than two valuation/growth/technical data points")
    elif score >= min_buy_score and allocation <= max_position_pct:
        action = "BUY"
    elif score <= -2:
        action = "SELL"
    else:
        action = "HOLD"
    return {
        "action": action,
        "score": score,
        "data_points": data_points,
        "reasons": reasons or ["no screen threshold was crossed"],
    }


def deployment_plan(rows: list[dict], summary: dict, cash: float,
                    max_position_pct: float) -> list[dict]:
    """Allocate all deployable cash across BUY rows, bounded by concentration."""
    if cash <= 0 or summary.get("account_value", 0) <= 0:
        return []
    eligible = []
    cap_value = summary["account_value"] * max_position_pct / 100
    for row in rows:
        decision = row.get("decision") or {}
        if decision.get("action") != "BUY":
            continue
        current = row.get("market_value_base") or 0
        room = max(0.0, cap_value - current)
        if room:
            eligible.append({
                "row": row,
                "room": room,
                "weight": max(1, decision.get("score", 1)),
                "amount": 0.0,
            })
    remaining = cash
    active = eligible
    while remaining > 0.005 and active:
        total_weight = sum(item["weight"] for item in active)
        spent = 0.0
        next_active = []
        for item in active:
            proposed = remaining * item["weight"] / total_weight
            available = item["room"] - item["amount"]
            addition = min(proposed, available)
            item["amount"] += addition
            spent += addition
            if available - addition > 0.005:
                next_active.append(item)
        if spent <= 0.005:
            break
        remaining -= spent
        active = next_active
    return [
        {
            "symbol": item["row"]["symbol"],
            "name": item["row"].get("name") or item["row"]["symbol"],
            "amount": round(item["amount"], 2),
            "score": item["row"]["decision"]["score"],
            "new_position": not item["row"].get("is_holding"),
        }
        for item in eligible if item["amount"] >= 0.01
    ]


def _portfolio_watchlist(rows: list[dict]) -> list[dict]:
    return [
        {
            "symbol": row["symbol"], "name": row.get("name", row["symbol"]),
            "aliases": [], "market": row.get("region", "global"),
            "watch_only": row.get("watch_only", False), "news_only": False,
        }
        for row in rows
    ]


def build_report(positions: list[dict], candidates: list[dict] | None = None,
                 *, base_currency: str = "USD", deployable_cash: float = 0,
                 max_position_pct: float = 20, max_sector_pct: float = 35,
                 min_buy_score: int = 3, quote_fetcher=brief.fetch_quote,
                 fundamentals_fetcher=fetch_fundamentals,
                 fx_fetcher=fetch_fx_rate, headline_fetcher=None) -> dict:
    """Build a complete view. Injected fetchers keep every test offline."""
    candidates = candidates or []
    source_rows = [dict(row, watch_only=False) for row in positions]
    source_rows += [dict(row, watch_only=True) for row in candidates]
    fx_cache: dict[tuple[str, str], dict] = {}
    analysed = []
    for row in source_rows:
        quote_data = quote_fetcher(row["symbol"])
        facts = fundamentals_fetcher(row["symbol"])
        currency = str(
            quote_data.get("currency") or row.get("currency") or base_currency
        ).upper()
        key = (currency, base_currency)
        if key not in fx_cache:
            fx_cache[key] = fx_fetcher(*key)
        analysed.append(analyse_position(
            row, quote_data, facts, base_currency, fx_cache[key]
        ))

    summary = portfolio_summary(analysed, deployable_cash, base_currency)
    sectors = exposure(analysed, "sector")
    regions = exposure(analysed, "region")
    factors = exposure(analysed, "factors")
    for row in analysed:
        row["decision"] = score_position(
            row,
            sectors.get(row.get("sector", "Unclassified"), 0),
            max_position_pct,
            max_sector_pct,
            min_buy_score,
        )

    plan = deployment_plan(analysed, summary, deployable_cash, max_position_pct)
    holdings = sorted(
        [row for row in analysed if row.get("is_holding")],
        key=lambda row: row.get("market_value_base") or 0,
        reverse=True,
    )
    research = [row for row in analysed if not row.get("is_holding")]
    top_three = sum(row.get("allocation_pct", 0) for row in holdings[:3])
    invested = summary.get("securities_value") or 0
    hhi = sum(
        ((row.get("market_value_base") or 0) / invested) ** 2
        for row in holdings
    ) if invested else 0

    warnings = []
    if holdings and holdings[0].get("allocation_pct", 0) > max_position_pct:
        warnings.append(
            f"{holdings[0]['symbol']} exceeds the {max_position_pct:.0f}% position limit"
        )
    if sectors and next(iter(sectors.values())) > max_sector_pct:
        name, pct = next(iter(sectors.items()))
        warnings.append(f"{name} sector exposure is {pct:.1f}%")
    if top_three > 60:
        warnings.append(f"top three holdings are {top_three:.1f}% of account value")
    if not summary["realized_pnl_complete"]:
        warnings.append("realized P&L is incomplete; total lifetime profit is unavailable")
    if any(not row.get("fx", {}).get("ok") for row in holdings):
        warnings.append("at least one non-base-currency position lacks an FX conversion")

    news = []
    if headline_fetcher:
        try:
            watchlist = _portfolio_watchlist(analysed)
            raw_news = headline_fetcher(watchlist)
            news = brief.filter_items(
                raw_news,
                watchlist,
                config.MARKET_BRIEF_MACRO_KEYWORDS,
                max_items=8,
            )
        except Exception as exc:
            logger.warning("Portfolio headlines unavailable: %s", exc)

    return {
        "as_of": config.today().isoformat(),
        "summary": summary,
        "holdings": holdings,
        "research_candidates": research,
        "sector_exposure": sectors,
        "region_exposure": regions,
        "factor_exposure": factors,
        "top_three_pct": top_three,
        "hhi": hhi,
        "warnings": warnings,
        "deployment_plan": plan,
        "headlines": news,
        "method": {
            "min_buy_score": min_buy_score,
            "max_position_pct": max_position_pct,
            "max_sector_pct": max_sector_pct,
        },
    }


def _money(value: float | None, currency: str, complete: bool = True) -> str:
    if value is None:
        return "unavailable"
    prefix = "" if complete else "partial "
    return f"{prefix}{value:+,.2f} {currency}"


def _metric(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:,.2f}{suffix}"


def _exposure_line(values: dict[str, float]) -> str:
    if not values:
        return "unavailable"
    return " · ".join(f"{name} {pct:.1f}%" for name, pct in list(values.items())[:6])


def format_view(report: dict) -> str:
    """Render the user's comprehensive, evidence-first daily portfolio view."""
    summary = report["summary"]
    currency = summary["base_currency"]
    lines = [f"*IBKR DAILY VIEW — {report['as_of']}*", ""]
    lines += [
        "*1. Portfolio snapshot*",
        f"- Account value: {summary['account_value']:,.2f} {currency} "
        f"({summary['securities_value']:,.2f} invested + "
        f"{summary['deployable_cash']:,.2f} deployable cash)",
        f"- Daily P&L: {_money(summary['daily_pnl'], currency, summary['daily_pnl_complete'])}",
        f"- Unrealized P&L: {_money(summary['unrealized_pnl'], currency, summary['unrealized_pnl_complete'])}",
        f"- Realized P&L: {_money(summary['realized_pnl'], currency, summary['realized_pnl_complete'])}",
        f"- Total profit to date: {_money(summary['total_profit'], currency, summary['total_profit_complete'])}",
        "",
        "*2. Holdings — size, performance, valuation and call*",
    ]
    if not report["holdings"]:
        lines.append("- No positions loaded.")
    for row in report["holdings"]:
        facts = row.get("fundamentals") or {}
        decision = row["decision"]
        lines.append(
            f"- {row['symbol']} — *{decision['action']}* (score {decision['score']:+d}; "
            f"{row['allocation_pct']:.1f}%): value "
            f"{_money(row.get('market_value_base'), currency).lstrip('+')}; "
            f"price {_metric(row.get('price'))} {row.get('price_currency', '')}; "
            f"avg cost {_metric(row.get('average_cost'))} {row.get('currency', '')}; "
            f"day {_money(row.get('daily_pnl_base'), currency)}; "
            f"unrealized {_money(row.get('unrealized_pnl_base'), currency)} "
            f"({_metric(row.get('total_return_pct'), '%')})"
        )
        lines.append(
            f"  P/E T/F {_metric(facts.get('trailing_pe'))}/{_metric(facts.get('forward_pe'))} "
            f"({_pe_assessment(facts.get('forward_pe') or facts.get('trailing_pe'))}); "
            f"PEG {_metric(facts.get('peg'))} ({_peg_assessment(facts.get('peg'))}); "
            f"earnings/revenue growth {_metric(facts.get('earnings_growth_pct'), '%')}/"
            f"{_metric(facts.get('revenue_growth_pct'), '%')}"
        )
        lines.append(
            f"  Quality: margin {_metric(facts.get('profit_margin_pct'), '%')}; "
            f"D/E {_metric(facts.get('debt_to_equity'))}; beta "
            f"{_metric(facts.get('beta'))}; dividend "
            f"{_metric(facts.get('dividend_yield_pct'), '%')}. "
            f"Technical: {technical_context(row)}. Why: "
            + "; ".join(decision["reasons"][:3])
        )
        lines.append(
            f"  Data: {facts.get('source', 'unavailable')} as of "
            f"{facts.get('as_of') or 'unknown'}; FX "
            f"{row.get('fx', {}).get('source', 'unavailable')} as of "
            f"{row.get('fx', {}).get('as_of', 'unknown')}"
        )

    lines += [
        "",
        "*3. Exposure and diversification*",
        f"- Sectors: {_exposure_line(report['sector_exposure'])}",
        f"- Regions: {_exposure_line(report['region_exposure'])}",
        f"- Factors/themes: {_exposure_line(report['factor_exposure'])}",
        f"- Top three: {report['top_three_pct']:.1f}% · HHI: {report['hhi']:.3f}",
        "",
        "*4. Risks and data gaps*",
    ]
    lines.extend(f"- {warning}" for warning in report["warnings"])
    if not report["warnings"]:
        lines.append("- No configured concentration limit is breached.")
    lines.append(
        "- ETF constituent overlap and return correlations are not calculated "
        "in this version; factor tags are the current overlap proxy."
    )

    lines += ["", "*5. Market context and catalysts*"]
    if report["headlines"]:
        for item in report["headlines"][:8]:
            matched = ", ".join(item.get("matched", []))
            label = f" [{matched}]" if matched else ""
            lines.append(f"- {item.get('title', '')}{label}")
    else:
        lines.append("- No matching fresh headline was available in this run.")

    lines += ["", "*6. Actions and cash deployment*"]
    for action in ("BUY", "HOLD", "SELL"):
        names = [row["symbol"] for row in report["holdings"] if row["decision"]["action"] == action]
        lines.append(f"- {action}: {', '.join(names) if names else 'none'}")
    if report["deployment_plan"]:
        for item in report["deployment_plan"]:
            kind = "new position" if item["new_position"] else "add"
            lines.append(
                f"- Deploy {item['amount']:,.2f} {currency} to {item['symbol']} "
                f"({kind}; score {item['score']:+d})"
            )
    elif summary["deployable_cash"]:
        lines.append("- No BUY passed the evidence and concentration rules; cash is not forced into a HOLD/SELL.")
    else:
        lines.append("- No deployable cash was supplied.")

    lines += ["", "*7. Research candidates outside the portfolio*"]
    if report["research_candidates"]:
        for row in report["research_candidates"]:
            facts = row.get("fundamentals") or {}
            lines.append(
                f"- {row['symbol']} — *{row['decision']['action']}* "
                f"(score {row['decision']['score']:+d}); P/E F "
                f"{_metric(facts.get('forward_pe'))}; PEG {_metric(facts.get('peg'))}; "
                f"{technical_context(row)}"
            )
    else:
        lines.append("- None configured.")

    lines += [
        "",
        "*Method*",
        "- BUY/HOLD/SELL is a transparent rules screen using valuation, growth, "
        "trend, position size and sector concentration; it is not an order.",
        "- P/E bands are absolute, not sector-relative. PEG is meaningful only "
        "when earnings growth is positive. Missing data defaults to HOLD.",
        "- This repository has no IBKR login or trade execution capability. "
        "Review data freshness and suitability before acting.",
    ]
    return "\n".join(lines)


def configured_positions() -> list[dict]:
    csv_rows = (
        load_ibkr_csv(config.PORTFOLIO_VIEW_IBKR_CSV)
        if config.PORTFOLIO_VIEW_IBKR_CSV else []
    )
    return merge_positions(config.PORTFOLIO_VIEW_POSITIONS, csv_rows)


def build_configured_report() -> dict:
    return build_report(
        configured_positions(),
        config.PORTFOLIO_VIEW_CANDIDATES,
        base_currency=config.PORTFOLIO_VIEW_BASE_CURRENCY,
        deployable_cash=config.PORTFOLIO_VIEW_DEPLOYABLE_CASH,
        max_position_pct=config.PORTFOLIO_VIEW_MAX_POSITION_PCT,
        max_sector_pct=config.PORTFOLIO_VIEW_MAX_SECTOR_PCT,
        min_buy_score=config.PORTFOLIO_VIEW_MIN_BUY_SCORE,
        headline_fetcher=brief.fetch_headlines,
    )


async def run_and_notify(sender=None) -> str:
    """Build, deliver, and return the view; never archive private positions."""
    if sender is None:
        from . import senders
        sender = senders.get_sender(config.DELIVERY_BACKEND, **config.DELIVERY_OPTIONS)
    report = await asyncio.to_thread(build_configured_report)
    text = format_view(report)
    try:
        await sender.send(text, "IBKR Daily View")
    except Exception as exc:
        logger.warning("IBKR Daily View delivery failed: %s", exc)
    return text

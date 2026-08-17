"""Radar — market-wide discovery: which US names are unusually active today.

Two public Yahoo Finance sources, both free and both unofficial:

  * the day-gainers screener — biggest % risers in the regular session;
  * the trending list — the tickers most looked-up on Yahoo right now.

DATA ONLY. A name being hot is an observation about the market, not about
whether it is worth owning. Nothing here ranks, scores, or endorses; the brief
renders these as "what the crowd is chasing today" and the compose prompt
forbids framing any of them as a pick or an opportunity. That constraint is
the same one the rest of the pipeline lives under — see brief.py.

Like every other layer, this one degrades: any failure returns [] and the
brief simply has no radar section that day.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_TRENDING_URL = "https://query1.finance.yahoo.com/v1/finance/trending/US?count={count}"
_SCREENER_URL = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    "?scrIds=day_gainers&count={count}"
)

_HTTP_TIMEOUT = 15

# Same browser UA story as the feeds: Yahoo serves junk to a default client.
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*;q=0.8",
}

#: Cap on radar entries carried into one brief.
_MAX_ENTRIES = 8


def _get_json(url: str) -> dict:
    resp = httpx.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True,
                     headers=_HTTP_HEADERS)
    resp.raise_for_status()
    return resp.json()


def _num(raw) -> float | None:
    """Yahoo ships numbers either bare or as {"raw": n, "fmt": "..."}."""
    if isinstance(raw, dict):
        raw = raw.get("raw")
    return float(raw) if isinstance(raw, (int, float)) else None


def fetch_day_gainers(count: int = 8) -> list[dict]:
    """Top % gainers of the day. [] on any failure."""
    try:
        data = _get_json(_SCREENER_URL.format(count=count))
        quotes = (((data.get("finance") or {}).get("result") or [{}])[0]
                  .get("quotes") or [])
    except Exception as e:
        logger.warning(f"Radar: day-gainers screener failed: {e}")
        return []
    out = []
    for q in quotes[:count]:
        symbol = str(q.get("symbol", "")).strip()
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": str(q.get("shortName") or q.get("longName") or "").strip(),
            "change_pct": _num(q.get("regularMarketChangePercent")),
            "sources": ["day gainers"],
        })
    return out


def fetch_trending(count: int = 8) -> list[str]:
    """Most looked-up tickers on Yahoo right now. [] on any failure."""
    try:
        data = _get_json(_TRENDING_URL.format(count=count))
        quotes = (((data.get("finance") or {}).get("result") or [{}])[0]
                  .get("quotes") or [])
        return [str(q.get("symbol", "")).strip()
                for q in quotes[:count] if str(q.get("symbol", "")).strip()]
    except Exception as e:
        logger.warning(f"Radar: trending list failed: {e}")
        return []


def collect(quote_fn=None, count: int = 5) -> list[dict]:
    """Merged radar list: gainers first, then trending names not already in it.

    ``quote_fn`` (symbol -> quote dict, e.g. brief.fetch_quote) is used to put
    a day move next to trending names, which arrive as bare symbols. It is
    injected rather than imported to keep this module import-clean and the
    tests network-free. Without it trending names carry no percentage — shown
    as observed attention, with no number invented for them.
    """
    entries = fetch_day_gainers(count)
    have = {e["symbol"] for e in entries}

    for symbol in fetch_trending(count):
        if len(entries) >= _MAX_ENTRIES:
            break
        if symbol in have:
            for e in entries:
                if e["symbol"] == symbol:
                    e["sources"].append("trending")
            continue
        entry = {"symbol": symbol, "name": "", "change_pct": None,
                 "sources": ["trending"]}
        if quote_fn:
            try:
                q = quote_fn(symbol)
                if q.get("ok"):
                    entry["change_pct"] = q.get("change_pct")
            except Exception:
                pass  # a bare trending symbol is still worth listing
        entries.append(entry)
        have.add(symbol)

    return entries[:_MAX_ENTRIES]


def format_radar(entries: list[dict]) -> str:
    """Render radar entries as observed activity — a number and a source, only.

    No adjectives, no ranking language: "NVDA +5.2% today (day gainers)" is a
    fact; anything warmer than that would read as a tip.
    """
    lines = []
    for e in entries:
        name = f" ({e['name']})" if e.get("name") else ""
        pct = e.get("change_pct")
        move = f" {pct:+.2f}% today" if isinstance(pct, (int, float)) else ""
        src = ", ".join(e.get("sources", []))
        lines.append(f"- {e.get('symbol', '')}{name}:{move or ' active'} [{src}]")
    return "\n".join(lines)

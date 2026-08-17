"""Market brief — U.S.-first daily global-market monitor.

Scans U.S. and major global-market feeds plus Yahoo close prices for the
configured watchlist, then writes a short "what moved · what changed · what to
ignore" digest.

MONITORING ONLY. This module never produces investment advice: the compose
prompt forbids buy/sell/hold calls, price targets, and conviction language, and
the static fallback is a plain data dump. Anything that reads like a
recommendation is a bug, not a feature.

The pipeline is three stages — ``scan`` (fetch) → ``compose_brief`` (render) →
a sender (deliver) — and every stage degrades rather than fails: one dead feed
loses one source, a dead price endpoint loses one quote, a missing model loses
the prose but not the data. HTTP goes through httpx; there is no market-data
SDK and no broker integration, by design.
"""

import asyncio
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from urllib.parse import quote

import httpx

from . import config

logger = logging.getLogger(__name__)

# Persistent dedup store. Entries are {hash: iso-first-seen-date}; anything
# older than _SEEN_TTL_DAYS is dropped on load, so a headline can't resurface
# for two weeks but nothing is blocklisted forever.
_SEEN_FILE = config.DATA_DIR / "market_brief_seen.json"
_SEEN_TTL_DAYS = 14

#: Past briefs are archived here, newest last, and fed back into the compose
#: prompt so a story that resurfaces under a new headline is not re-reported.
_BRIEFS_DIR = config.DATA_DIR / "briefs"

# Every fetch goes out with a browser UA + Accept. Several public finance feeds
# serve reduced or non-XML responses to a default client UA.
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}

_HTTP_TIMEOUT = 15

# Base news sources — (label, url, is_disclosure, require_xml). Per-symbol Yahoo
# feeds are added at runtime from the configured watchlist, so U.S. and global
# holdings receive first-class coverage without hard-coded ticker assumptions.
# `require_xml` protects against endpoints that return an HTML page with HTTP
# 200; those responses are rejected before parsing.
FEEDS = [
    ("Federal Reserve: press releases",
     "https://www.federalreserve.gov/feeds/press_all.xml", True, True),
    ("Google News: U.S. markets",
     "https://news.google.com/rss/search?q=%28S%26P+500+OR+Nasdaq+OR+Dow%29+"
     "when%3A1d&hl=en-US&gl=US&ceid=US%3Aen", False, False),
    ("Google News: U.S. macro",
     "https://news.google.com/rss/search?q=%28Federal+Reserve+OR+Treasury+yield+"
     "OR+U.S.+inflation+OR+jobs+report%29+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen",
     False, False),
    ("Google News: Europe markets",
     "https://news.google.com/rss/search?q=%28STOXX+600+OR+FTSE+100+OR+DAX+OR+"
     "ECB%29+when%3A1d&hl=en-GB&gl=GB&ceid=GB%3Aen", False, False),
    ("Google News: Asia-Pacific markets",
     "https://news.google.com/rss/search?q=%28Nikkei+225+OR+Hang+Seng+OR+CSI+300+"
     "OR+ASX+200%29+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen", False, False),
    ("Google News: global macro",
     "https://news.google.com/rss/search?q=%28oil+OR+dollar+index+OR+global+"
     "markets+OR+trade+tariff%29+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen",
     False, False),
]

_MAX_TICKER_FEEDS = 30


def feeds_for_watchlist(watchlist: list[dict] | None = None) -> list[tuple]:
    """Base feeds plus one Yahoo headline feed per unique priced symbol.

    Dynamic feeds keep the monitor portfolio-led: a Tokyo, London, Frankfurt,
    Toronto, Hong Kong, or U.S. symbol gets the same ticker-specific treatment.
    The cap bounds runtime for unusually large lists.
    """
    feeds = list(FEEDS)
    seen: set[str] = set()
    for entry in watchlist or []:
        symbol = str(entry.get("symbol", "")).strip()
        if not symbol or entry.get("news_only") or symbol in seen:
            continue
        seen.add(symbol)
        feeds.append((
            f"Yahoo: {symbol}",
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(symbol)}",
            False,
            False,
        ))
        if len(seen) >= _MAX_TICKER_FEEDS:
            break
    return feeds

# Yahoo's public chart endpoint — two daily closes are all the price layer
# needs, and it costs no dependency (yfinance would).
_YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?range=5d&interval=1d"
)

# A "move" worth calling out, in percent. Broad/defensive instruments barely
# move, so a 2% gate would mean they never appear at all.
_MOVE_THRESHOLD_DEFAULT = 2.0
_MOVE_THRESHOLD_LOW_BETA = 1.0
_LOW_BETA_SYMBOLS = {"AGG", "BND", "VTI", "VOO", "VT", "VWRA.L"}

# Candidate-pool ordering: a regulator filing about a held name beats press
# coverage of it, which beats macro/policy background.
_RANK_ORDER = {"disclosure": 0, "ticker": 1, "macro": 2}

# The compose prompt embeds untrusted feed text — headlines and summaries
# fetched from the open web. The composer needs no tools (it is handed all its
# data inline), so every tool is denied outright rather than filtered. This is
# an allowlist of nothing, which is the only shape that stays correct as new
# tools appear.
_COMPOSE_DISALLOWED_TOOLS = [
    "Agent", "Bash", "BashOutput", "Edit", "Glob", "Grep", "KillShell",
    "NotebookEdit", "Read", "SlashCommand", "Task", "TodoWrite", "WebFetch",
    "WebSearch", "Write",
]


def _load_seen() -> dict:
    """Load {hash: iso-first-seen-date} of already-reported items, pruned.

    Backward-compatible with a bare list of hashes (treated as first seen
    today), so an older state file upgrades cleanly.
    """
    if not _SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(_SEEN_FILE.read_text(encoding="utf-8"))
        raw = data.get("seen", {})
        today = config.today().isoformat()
        if isinstance(raw, list):  # legacy format → migrate
            raw = {h: today for h in raw}
        cutoff = config.today() - timedelta(days=_SEEN_TTL_DAYS)
        out = {}
        for h, seen_on in raw.items():
            try:
                if date.fromisoformat(seen_on) >= cutoff:
                    out[h] = seen_on
            except (ValueError, TypeError):
                out[h] = today  # unparseable date → keep, re-stamp today
        return out
    except Exception:
        return {}


def _save_seen(seen: dict):
    """Persist seen hashes with first-seen dates. TTL pruning happens on load."""
    _SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SEEN_FILE.write_text(
        json.dumps({"seen": seen, "updated": config.now().isoformat()},
                   indent=2),
        encoding="utf-8",
    )


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _mark_if_new(seen: dict, url: str, today: str) -> bool:
    """Return True if this URL hasn't been reported yet; if so, record it."""
    if not url:
        return False
    h = _url_hash(url)
    if h in seen:
        return False
    seen[h] = today
    return True


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # drop XML namespace


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_feed(xml_text: str, label: str, is_disclosure: bool = False,
               max_items: int = 25) -> list[dict]:
    """Parse an RSS 2.0 or Atom body into item dicts.

    Handles both link shapes: RSS `<link>text</link>` and Atom
    `<link href="...">`. Returns [] on unparseable input rather than raising —
    a malformed feed is one dead source, not a dead brief.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning(f"Market brief: feed '{label}' is not parseable XML: {e}")
        return []

    items = []
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    for entry in entries[:max_items]:
        title = ""
        link = ""
        summary = ""
        for child in entry:
            lt = _local(child.tag)
            if lt == "title" and child.text:
                title = _strip_html(child.text)
            elif lt == "link":
                href = child.get("href")  # Atom link carries href attr
                if href:
                    if not link or child.get("rel") in (None, "alternate"):
                        link = href
                elif child.text and child.text.strip():  # RSS link is text
                    link = child.text.strip()
            elif lt in ("description", "summary", "content", "encoded") and not summary:
                # itertext(), not .text: a feed that ships raw (well-formed)
                # markup inside <description> parses into child ELEMENTS, so
                # .text is None and the summary would silently come out empty.
                # Some publisher feeds use full-text <content:encoded> blocks.
                summary = _strip_html("".join(child.itertext()))[:300]
        if not title or not link:
            continue
        items.append({
            "source": label,
            "title": title,
            "url": link,
            "summary": summary,
            "is_disclosure": is_disclosure,
        })
    return items


def _fetch_feed(label: str, url: str, is_disclosure: bool,
                require_xml: bool) -> list[dict]:
    """Fetch and parse one feed. Any failure returns [] and is logged."""
    try:
        resp = httpx.get(
            url,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers=_HTTP_HEADERS,
        )
        resp.raise_for_status()
        if require_xml:
            # Some publishers answer a wrong or renamed feed path with a normal
            # HTML page at HTTP 200. Reject it loudly instead of silently losing
            # a source.
            ctype = (resp.headers.get("content-type") or "").lower()
            if "xml" not in ctype:
                logger.warning(
                    f"Market brief: feed '{label}' returned non-XML "
                    f"content-type {ctype!r} — rejected"
                )
                return []
        return parse_feed(resp.text, label, is_disclosure=is_disclosure)
    except Exception as e:
        logger.warning(f"Market brief: feed '{label}' failed: {e}")
        return []


def fetch_headlines(watchlist: list[dict] | None = None) -> list[dict]:
    """Fetch every configured feed. One dead feed never kills the brief."""
    items = []
    feeds = feeds_for_watchlist(watchlist)
    for label, url, is_disclosure, require_xml in feeds:
        items.extend(_fetch_feed(label, url, is_disclosure, require_xml))
    logger.info(f"Market brief: {len(items)} raw headlines from {len(feeds)} feeds")
    return items


def _entry_terms(entry: dict) -> list[str]:
    """Every string that means "this watchlist entry" — symbol, name, aliases."""
    terms = [str(entry.get("symbol", "")), str(entry.get("name", ""))]
    terms += [str(a) for a in entry.get("aliases", [])]
    # The bare ticker root ("7203.T" → "7203") is common in local exchange
    # coverage and ETF references.
    symbol = str(entry.get("symbol", ""))
    if "." in symbol:
        terms.append(symbol.split(".")[0])
    return [t.strip() for t in terms if t and t.strip()]


def _matches(term: str, haystack: str) -> bool:
    """Whole-token, case-insensitive match of `term` inside `haystack`.

    A plain substring test is wrong here: the watchlist carries short names
    ("AI", "CAT", "ON") that occur inside ordinary
    words and numbers, and every false positive costs a slot in a capped brief.
    Lookarounds rather than \\b allow terms ending in exchange suffix punctuation
    (for example ``7203.T``) to anchor correctly.
    """
    if not term:
        return False
    pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
    return re.search(pattern, haystack, re.IGNORECASE) is not None


def filter_items(items: list[dict], watchlist: list[dict],
                 macro_keywords: list[str],
                 max_items: int = 25) -> list[dict]:
    """Keep only items about a watchlist name or a macro keyword, then rank.

    Matching is whole-token and case-insensitive on title + summary, and covers
    aliases. Ranking is official releases first, then ticker-specific coverage,
    then macro/policy background, so the cap trims the least specific items.
    """
    kept = []
    keywords = [str(k).strip() for k in macro_keywords if str(k).strip()]

    for item in items:
        haystack = f"{item.get('title', '')} {item.get('summary', '')}"
        matched = []
        for entry in watchlist:
            for term in _entry_terms(entry):
                if _matches(term, haystack):
                    matched.append(entry.get("name") or entry.get("symbol", ""))
                    break
        macro_hits = [k for k in keywords if _matches(k, haystack)]

        if matched:
            rank = "disclosure" if item.get("is_disclosure") else "ticker"
        elif macro_hits:
            rank = "macro"
        else:
            continue

        out = dict(item)
        out["rank"] = rank
        out["matched"] = matched or macro_hits
        kept.append(out)

    kept.sort(key=lambda i: _RANK_ORDER.get(i["rank"], 3))
    return kept[:max_items]


def move_threshold(symbol: str) -> float:
    """Percent move that counts as notable for this symbol."""
    return (_MOVE_THRESHOLD_LOW_BETA if symbol in _LOW_BETA_SYMBOLS
            else _MOVE_THRESHOLD_DEFAULT)


def fetch_quote(symbol: str) -> dict:
    """Last close vs previous close for one symbol via Yahoo's chart endpoint.

    Never raises: a delisted/renamed/unknown symbol comes back as ``ok: False``
    and the brief prints "price unavailable" for it. Exchange suffixes are
    preserved exactly; the monitor never guesses a local listing symbol.
    """
    quote = {
        "symbol": symbol,
        "last": None,
        "prev": None,
        "change_pct": None,
        "currency": "",
        "ok": False,
        "flagged": False,
    }
    try:
        resp = httpx.get(
            _YAHOO_CHART_URL.format(symbol=symbol),
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers=_HTTP_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        result = ((data.get("chart") or {}).get("result") or [])
        if not result:
            logger.info(f"Market brief: no chart data for {symbol}")
            return quote
        block = result[0]
        meta = block.get("meta") or {}
        quote["currency"] = meta.get("currency", "") or ""
        closes = (((block.get("indicators") or {}).get("quote") or [{}])[0]
                  .get("close") or [])
        closes = [c for c in closes if isinstance(c, (int, float))]
        if len(closes) >= 2:
            last, prev = closes[-1], closes[-2]
        else:
            # Some thin or non-U.S. listings return one usable bar for a short
            # range; Yahoo's metadata may still carry both closing marks.
            last, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose")
            if not (isinstance(last, (int, float)) and isinstance(prev, (int, float))):
                logger.info(f"Market brief: fewer than 2 closes for {symbol}")
                return quote
        if not prev:
            return quote
        change = (last - prev) / prev * 100
        quote.update({
            "last": last,
            "prev": prev,
            "change_pct": round(change, 2),
            "ok": True,
            "flagged": abs(change) >= move_threshold(symbol),
        })
    except Exception as e:
        logger.warning(f"Market brief: price fetch failed for {symbol}: {e}")
    return quote


def fetch_prices(watchlist: list[dict]) -> list[dict]:
    """Quotes for every PRICED watchlist entry, each independently fault-tolerant.

    News-only entries are skipped outright — they exist to be matched in
    headlines, and their Yahoo symbols are unverified. Quoting a guessed symbol
    would print a confident number for the wrong instrument, which is worse
    than printing nothing.
    """
    prices = []
    for entry in watchlist:
        symbol = str(entry.get("symbol", "")).strip()
        if not symbol or entry.get("news_only"):
            continue
        quote = fetch_quote(symbol)
        quote["name"] = entry.get("name", symbol)
        quote["market"] = entry.get("market", "")
        quote["watch_only"] = bool(entry.get("watch_only"))
        prices.append(quote)
    return prices


def fetch_smart_money() -> list[dict]:
    """13F events, if any. Total: any failure means "nothing new" and no more.

    Isolated behind its own try/except because it is a bonus section on a
    quarterly cadence — the daily brief must never be delayed or lost because
    the SEC endpoint was slow, moved, or served something unexpected.
    """
    try:
        from . import smart_money
        return smart_money.collect_events()
    except Exception:
        logger.exception("Market brief: smart-money check failed — continuing without it")
        return []


def scan() -> dict:
    """Fetch prices + new (unseen) matching headlines + 13F events. Synchronous."""
    watchlist = config.MARKET_BRIEF_WATCHLIST
    seen = _load_seen()
    today = config.today().isoformat()

    prices = fetch_prices(watchlist)

    raw = fetch_headlines(watchlist)
    fresh = [i for i in raw if _mark_if_new(seen, i.get("url", ""), today)]
    items = filter_items(
        fresh,
        watchlist,
        config.MARKET_BRIEF_MACRO_KEYWORDS,
        max_items=config.MARKET_BRIEF_MAX_ITEMS,
    )
    _save_seen(seen)

    smart = fetch_smart_money()

    logger.info(
        f"Market brief scan: {len(items)} relevant items "
        f"(of {len(fresh)} new / {len(raw)} raw), {len(prices)} quotes, "
        f"{len(smart)} 13F event(s)"
    )
    return {"prices": prices, "items": items, "smart_money": smart}


def format_prices(prices: list[dict]) -> str:
    """Compact price table for the prompt and the static fallback."""
    lines = []
    for p in prices:
        name = p.get("name") or p.get("symbol", "")
        tag = " (watch only)" if p.get("watch_only") else ""
        if not p.get("ok"):
            lines.append(f"- {name} [{p.get('symbol', '')}]{tag}: price unavailable")
            continue
        flag = " ⚠︎ move" if p.get("flagged") else ""
        lines.append(
            f"- {name} [{p.get('symbol', '')}]{tag}: {p['last']:.2f} "
            f"{p.get('currency', '')}".rstrip()
            + f" ({p['change_pct']:+.2f}% vs prev close){flag}"
        )
    return "\n".join(lines)


def items_to_text(items: list[dict]) -> str:
    """Convert filtered headlines into a compact text block for the composer."""
    lines = []
    for i, item in enumerate(items, 1):
        matched = ", ".join(item.get("matched", []))
        tag = f" <{matched}>" if matched else ""
        lines.append(
            f"{i}. [{item.get('rank', '')}] [{item.get('source', '')}]"
            f"{tag} {item.get('title', '')}"
        )
        if item.get("summary"):
            lines.append(f"   {item['summary']}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
    return "\n".join(lines)


def format_digest(prices: list[dict], items: list[dict],
                  max_items: int = 6, smart_money: list[dict] | None = None) -> str:
    """Static fallback digest — used when no composer is available.

    Deliberately a plain data dump: no interpretation at all, so the fallback
    can't drift into anything that reads like advice.
    """
    lines = ["*📊 Market brief*", ""]

    moved = [p for p in prices if p.get("flagged")]
    if moved:
        lines.append("*Moves*")
        lines.append(format_prices(moved))
    else:
        lines.append("_No watchlist move past its threshold._")
    lines.append("")

    # Only present when a filing actually landed — on ~99% of days, absent.
    if smart_money:
        from . import smart_money as _sm
        lines.append("*Smart money*")
        lines.append(_sm.format_events(smart_money))
        lines.append("")

    if items:
        lines.append("*Headlines*")
        for item in items[:max_items]:
            title = item.get("title", "")
            if len(title) > 90:
                title = title[:87] + "..."
            lines.append(f"- {title}")
            if item.get("url"):
                lines.append(f"  {item['url']}")
    else:
        lines.append("_No matching headlines._")

    return "\n".join(lines).strip()


MARKET_BRIEF_PROMPT = """\
You are writing the *daily pre-market brief* — a short message for one person, \
about the instruments he already holds or watches. He wants situational \
awareness before the market opens: what moved, what changed, what to ignore.

ABSOLUTE GUARDRAIL — this is the MONITORING brief, never advice. The separate
IBKR Daily View is the portfolio decision-support surface; do not imitate it:
- NEVER write a buy, sell, hold, add, trim, exit, or "take profit" call, in any wording.
- NEVER give a price target, fair value, entry/exit level, or forecast.
- NEVER use conviction language ("strong", "attractive", "cheap", "overvalued", \
"opportunity", "risky bet"). Report and attribute; do not judge.
- Attribute every claim to the price move or the headline it came from. If the \
data does not say why something moved, say the move happened and stop.
- No advice, no recommendations, no implied ones. If an item cannot be written \
without a recommendation, drop it.

PORTFOLIO CONTEXT (holdings and watch-only names):
{portfolio}

PRICES (last close vs previous close; ⚠︎ marks a move past its threshold):
{prices}

HEADLINES matched to those names or to macro/policy keywords:
{items}

SMART MONEY — institutional 13F filings disclosed since the last brief. These \
are BACKWARD-LOOKING regulatory disclosures of what a manager held at a past \
quarter end, published with a delay of up to 45 days. Report them as facts that \
were filed ("X disclosed", "X filed", "no longer listed"), never as a move to \
follow, mirror, or act on, and never imply the manager still holds any of it:
{smart_money}

RECENT BRIEFS (do not repeat these — same story, same angle = skip):
{recent_briefs}

Write it like this:
- First line: `*📊 Market brief*` then the date.
- Lead with the single most important thing that happened, one or two lines.
- Then short bullets grouped by market (U.S., Europe, Asia-Pacific, Global) — \
only the markets that actually have something. One line each: what moved or \
what changed, and the one fact behind it.
- If, and ONLY if, the SMART MONEY block above has content, add one short \
`*Smart money:*` line stating what was filed. If it says "(nothing new)", omit \
the section entirely — do not mention 13Fs, do not say it was quiet.
- Last line starts with `Ignore:` — the noise that looks important but is not, \
one line.
- Prices without data: say "price unavailable", never guess a number. \
News-only names have no price at all — cover them from headlines only.
- English, terse, factual. Chat formatting (*bold*, - bullets), no markdown \
headers, no emojis beyond the header. AT MOST 20 lines and under 1300 characters.
- A quiet day is a short brief. Do not pad.
"""


def _portfolio_text(watchlist: list[dict]) -> str:
    """One line per watchlist entry for the prompt's context block."""
    lines = []
    for entry in watchlist:
        name = entry.get("name") or entry.get("symbol", "")
        market = entry.get("market", "")
        if entry.get("news_only"):
            tag = "news only, not priced"
        elif entry.get("watch_only"):
            tag = "watch only"
        else:
            tag = "held"
        symbol = entry.get("symbol", "")
        label = f" [{symbol}]" if symbol else ""
        lines.append(f"- {name}{label} — {market}, {tag}")
    return "\n".join(lines)


def _recent_briefs_text(limit: int = 3, max_chars: int = 2000) -> str:
    """Text of the last few briefs, for cross-day de-duplication.

    Briefs are archived as data/briefs/<date>.md (see archive_brief). Feeding
    them back lets the composer skip a story that resurfaces under a different
    headline — the semantic repeat URL hashing can't catch.
    """
    try:
        files = sorted(_BRIEFS_DIR.glob("*.md"))[-limit:]
    except Exception:
        return ""
    chunks = []
    for f in reversed(files):  # newest first
        try:
            chunks.append(f.read_text(encoding="utf-8").strip())
        except OSError:
            continue
    return "\n\n---\n\n".join(chunks).strip()[:max_chars]


def archive_brief(text: str, day: str = "") -> None:
    """Persist a brief so later runs can avoid repeating it. Never raises."""
    if not text:
        return
    try:
        _BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = day or config.today().isoformat()
        (_BRIEFS_DIR / f"{stamp}.md").write_text(text, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Market brief: could not archive the brief: {e}")


async def compose_brief(prices: list[dict], items: list[dict],
                        claude_runner,
                        smart_money: list[dict] | None = None) -> tuple[str, bool]:
    """Compose the brief text from prices + filtered headlines.

    Returns (brief_text, used_model); falls back to the static digest when no
    composer is configured, when it fails, or when it hands back anything that
    is not shaped like a brief.
    """
    brief = ""
    used_claude = False
    if claude_runner:
        from . import smart_money as _sm
        prompt = MARKET_BRIEF_PROMPT.format(
            portfolio=_portfolio_text(config.MARKET_BRIEF_WATCHLIST) or "(none)",
            prices=format_prices(prices) or "(no prices available)",
            items=items_to_text(items) or "(no matching headlines)",
            smart_money=_sm.format_events(smart_money or []) or "(nothing new)",
            recent_briefs=_recent_briefs_text() or "(none)",
        )
        try:
            # Needs several turns: read the prices + headlines + recent briefs,
            # then reason about relevance before writing. A 1-turn budget dies
            # on the turn limit, and a runner that returns its error text
            # rather than raising would then have that text shipped AS the
            # brief. The prompt embeds untrusted feed text, so tool use is
            # denied outright.
            brief, _ = await claude_runner.run(
                prompt,
                model=config.COMPOSE_MODEL,
                max_turns=6,
                disallowed_tools=_COMPOSE_DISALLOWED_TOOLS,
            )
            brief = (brief or "").strip()
            # A model runner's error paths return plain text with assorted
            # prefixes (⚠️, ⏳, raw CLI output). Blocklisting is leaky —
            # accept ONLY output shaped like the brief the prompt mandates,
            # whose first line must start with the bold 📊 header.
            if brief.lstrip().startswith("*📊"):
                used_claude = True
            else:
                if brief:
                    logger.warning(
                        "Market brief: compose returned non-brief text, "
                        "using static format"
                    )
                brief = ""
        except Exception:
            logger.exception(
                "Market brief: compose failed, falling back to static format"
            )

    if not brief:
        brief = format_digest(prices, items, smart_money=smart_money)
    return brief, used_claude


async def run_scan_and_notify(sender=None, claude_runner=None) -> str:
    """Full pipeline: scan → compose → deliver → archive. Returns the brief.

    `sender` defaults to whatever config selects (console unless told
    otherwise), and `claude_runner` to the `claude` CLI if it is installed. A
    delivery failure is logged, not raised: the brief is still archived and
    still returned to the caller.
    """
    if sender is None:
        from . import senders
        sender = senders.get_sender(config.DELIVERY_BACKEND, **config.DELIVERY_OPTIONS)

    # scan() does blocking HTTP — run in a thread so an async caller keeps its
    # event loop while ~9 feeds and ~9 quotes are fetched.
    data = await asyncio.to_thread(scan)
    prices, items = data["prices"], data["items"]
    smart = data.get("smart_money") or []
    if not prices and not items and not smart:
        logger.info("Market brief: nothing to report today")
        return ""

    brief, used_claude = await compose_brief(
        prices, items, claude_runner, smart_money=smart,
    )
    if not brief:
        logger.warning("Market brief: composed an empty brief — not sending")
        return ""

    try:
        await sender.send(brief, "market brief")
    except Exception as e:
        logger.warning(f"Market brief: delivery failed: {e}")

    archive_brief(brief)
    logger.info(
        f"Market brief: {'composed' if used_claude else 'static'} brief "
        f"({len(items)} items, {len(prices)} quotes, {len(smart)} 13F event(s))"
    )
    return brief

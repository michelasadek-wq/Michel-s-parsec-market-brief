"""Tests for the U.S./global market brief — feed parsing (including the
HTML-at-200 trap), dynamic ticker feeds, dedup, filtering, prices, composer
guardrails and config parsing.

No network: every fetch is mocked, every feed comes from a fixture string.
"""

import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_brief import brief as market_brief
from market_brief import config


# ── Fixtures ────────────────────────────────────────────────────────


WATCHLIST = [
    {"symbol": "AAPL", "name": "Apple",
     "aliases": ["Apple Inc"], "market": "us", "watch_only": False},
    {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF",
     "aliases": ["Vanguard Total Market"], "market": "us", "watch_only": False},
    {"symbol": "SAP.DE", "name": "SAP",
     "aliases": ["SAP SE"], "market": "europe", "watch_only": False},
    {"symbol": "TSM", "name": "TSMC",
     "aliases": ["Taiwan Semiconductor"], "market": "us", "watch_only": False},
]

MACRO_KEYWORDS = ["federal reserve", "treasury yield", "fed rate", "trade tariff"]


def _rss(items: list[tuple[str, str, str]]) -> str:
    """Build an RSS 2.0 body from (title, link, description) triples."""
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<description>{d}</description>"
        f"<pubDate>Tue, 12 Aug 2026 06:00:00 +0000</pubDate></item>"
        for t, u, d in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>Feed</title>{body}</channel></rss>"
    )


def _atom(items: list[tuple[str, str, str]]) -> str:
    """Build an Atom body from (title, href, summary) triples."""
    body = "".join(
        f"<entry><title>{t}</title>"
        f'<link rel="alternate" href="{u}"/>'
        f"<summary>{d}</summary></entry>"
        for t, u, d in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>{body}</feed>'
    )


#: What a bad/renamed feed can serve: HTTP 200 with an HTML body.
HTML_FEED_TRAP = (
    "<!DOCTYPE html><html><head><title>Publisher</title></head>"
    "<body><div class='news'>Latest news</div></body></html>"
)


def _mock_response(text: str, content_type: str = "application/rss+xml",
                   payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload or {})
    return resp


def _chart_payload(closes: list, currency: str = "USD") -> dict:
    return {
        "chart": {
            "result": [{
                "meta": {"currency": currency},
                "indicators": {"quote": [{"close": closes}]},
            }],
            "error": None,
        }
    }


@pytest.fixture
def seen_file(tmp_path, monkeypatch):
    """Point the dedup store at tmp so tests never touch real state."""
    f = tmp_path / "data" / "market_brief_seen.json"
    monkeypatch.setattr(market_brief, "_SEEN_FILE", f)
    return f


@pytest.fixture
def watchlist_config(monkeypatch):
    """Give config the test watchlist regardless of the real config.yaml."""
    monkeypatch.setattr(config, "MARKET_BRIEF_WATCHLIST", WATCHLIST)
    monkeypatch.setattr(config, "MARKET_BRIEF_MACRO_KEYWORDS", MACRO_KEYWORDS)
    monkeypatch.setattr(config, "MARKET_BRIEF_MAX_ITEMS", 25)


# ── parse_feed ──────────────────────────────────────────────────────


class TestParseFeed:
    def test_parses_rss_fixture(self):
        xml = _rss([
            ("Apple reports Q2 results", "https://example.com/a", "Apple said..."),
            ("DAX closes higher", "https://example.com/b", "German index up 0.4%"),
        ])
        items = market_brief.parse_feed(xml, "Test feed")

        assert len(items) == 2
        assert items[0]["title"] == "Apple reports Q2 results"
        assert items[0]["url"] == "https://example.com/a"
        assert items[0]["summary"].startswith("Apple")
        assert items[0]["source"] == "Test feed"
        assert items[0]["is_disclosure"] is False

    def test_parses_atom_fixture(self):
        xml = _atom([("Broadcom update", "https://example.com/c", "AVGO news")])
        items = market_brief.parse_feed(xml, "Yahoo: AVGO")

        assert len(items) == 1
        assert items[0]["url"] == "https://example.com/c"
        assert items[0]["title"] == "Broadcom update"

    def test_disclosure_flag_is_carried(self):
        xml = _rss([("Fed board release", "https://example.com/d", "official release")])
        items = market_brief.parse_feed(
            xml, "Federal Reserve: press releases", is_disclosure=True
        )
        assert items[0]["is_disclosure"] is True

    def test_html_body_yields_no_items(self):
        """An HTML page must never parse into headlines."""
        assert market_brief.parse_feed(HTML_FEED_TRAP, "Official feed") == []

    def test_items_without_title_or_link_are_skipped(self):
        xml = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<item><title>No link here</title></item>"
            "<item><link>https://example.com/e</link></item>"
            "<item><title>Good</title><link>https://example.com/f</link></item>"
            "</channel></rss>"
        )
        items = market_brief.parse_feed(xml, "Test feed")
        assert [i["title"] for i in items] == ["Good"]

    def test_strips_html_from_summary(self):
        xml = _rss([("T", "https://example.com/g", "<p>Apple <b>rose</b></p>")])
        assert market_brief.parse_feed(xml, "f")[0]["summary"] == "Apple rose"


# ── _fetch_feed ─────────────────────────────────────────────────────


class TestFetchFeed:
    def test_rejects_non_xml_content_type_when_required(self):
        """A publisher can answer a bad path with HTML at 200; reject it."""
        resp = _mock_response(HTML_FEED_TRAP, content_type="text/html; charset=utf-8")
        with patch("httpx.get", return_value=resp):
            items = market_brief._fetch_feed(
                "Federal Reserve: press releases", "https://example.com/feed",
                True, True,
            )
        assert items == []

    def test_accepts_xml_content_type(self):
        xml = _rss([("Fed disclosure", "https://example.com/h", "release")])
        resp = _mock_response(xml, content_type="text/xml; charset=utf-8")
        with patch("httpx.get", return_value=resp):
            items = market_brief._fetch_feed(
                "Federal Reserve: press releases", "https://example.com/feed",
                True, True,
            )
        assert len(items) == 1
        assert items[0]["is_disclosure"] is True

    def test_content_type_not_checked_when_not_required(self):
        xml = _rss([("Fed holds rates", "https://example.com/i", "macro")])
        resp = _mock_response(xml, content_type="text/html")
        with patch("httpx.get", return_value=resp):
            items = market_brief._fetch_feed("Google News: macro", "https://n/x", False, False)
        assert len(items) == 1

    def test_dead_feed_returns_empty(self):
        with patch("httpx.get", side_effect=RuntimeError("connection reset")):
            assert market_brief._fetch_feed("Global feed", "https://e/feed/", False, False) == []

    def test_sends_browser_headers(self):
        resp = _mock_response(_rss([("t", "https://example.com/j", "d")]))
        with patch("httpx.get", return_value=resp) as mock_get:
            market_brief._fetch_feed("Global feed", "https://e/feed/", False, False)
        headers = mock_get.call_args.kwargs["headers"]
        assert "Mozilla/5.0" in headers["User-Agent"]
        assert "xml" in headers["Accept"]

    def test_one_dead_feed_never_kills_the_scan(self):
        good = _mock_response(_rss([("Apple news", "https://example.com/k", "d")]))

        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] % 2:
                raise RuntimeError("boom")
            return good

        with patch("httpx.get", side_effect=flaky):
            items = market_brief.fetch_headlines()

        assert calls["n"] == len(market_brief.FEEDS)
        assert len(items) > 0

    def test_dynamic_ticker_feeds_are_unique_and_capped(self):
        watchlist = WATCHLIST + [dict(WATCHLIST[0])]
        feeds = market_brief.feeds_for_watchlist(watchlist)
        ticker_feeds = [feed for feed in feeds if feed[0].startswith("Yahoo:")]
        assert len(ticker_feeds) == len(WATCHLIST)
        assert any("SAP.DE" in label for label, *_ in ticker_feeds)


# ── seen-set dedup ──────────────────────────────────────────────────


class TestSeenSet:
    def test_marks_new_url_once(self, seen_file):
        seen = {}
        assert market_brief._mark_if_new(seen, "https://example.com/a", "2026-08-12") is True
        assert market_brief._mark_if_new(seen, "https://example.com/a", "2026-08-12") is False
        assert len(seen) == 1

    def test_empty_url_is_never_new(self, seen_file):
        assert market_brief._mark_if_new({}, "", "2026-08-12") is False

    def test_roundtrip(self, seen_file, monkeypatch):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 12))
        market_brief._save_seen({"abc123": "2026-08-12"})
        assert market_brief._load_seen() == {"abc123": "2026-08-12"}

    def test_prunes_entries_older_than_ttl(self, seen_file, monkeypatch):
        today = date(2026, 8, 12)
        monkeypatch.setattr(config, "today", lambda: today)
        stale = (today - timedelta(days=market_brief._SEEN_TTL_DAYS + 1)).isoformat()
        fresh = (today - timedelta(days=market_brief._SEEN_TTL_DAYS - 1)).isoformat()
        seen_file.parent.mkdir(parents=True, exist_ok=True)
        seen_file.write_text(
            json.dumps({"seen": {"old": stale, "new": fresh}}), encoding="utf-8"
        )

        loaded = market_brief._load_seen()
        assert "old" not in loaded
        assert loaded["new"] == fresh

    def test_migrates_legacy_list_format(self, seen_file, monkeypatch):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 12))
        seen_file.parent.mkdir(parents=True, exist_ok=True)
        seen_file.write_text(json.dumps({"seen": ["a", "b"]}), encoding="utf-8")
        assert market_brief._load_seen() == {"a": "2026-08-12", "b": "2026-08-12"}

    def test_corrupt_file_starts_fresh(self, seen_file):
        seen_file.parent.mkdir(parents=True, exist_ok=True)
        seen_file.write_text("NOT JSON!!!", encoding="utf-8")
        assert market_brief._load_seen() == {}


# ── entity / macro filter ───────────────────────────────────────────


def _item(title, summary="", source="Test", is_disclosure=False):
    return {
        "source": source,
        "title": title,
        "url": f"https://example.com/{abs(hash(title)) % 10000}",
        "summary": summary,
        "is_disclosure": is_disclosure,
    }


class TestFilterItems:
    def test_matches_english_alias(self):
        items = [_item("Apple Inc posts higher quarterly profit")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "ticker"
        assert "Apple" in kept[0]["matched"]

    def test_matches_global_listing_alias(self):
        items = [_item("SAP SE raises cloud outlook")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert "SAP" in kept[0]["matched"]

    def test_matches_name_case_insensitively(self):
        items = [_item("tsmc raises capex guidance")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert "TSMC" in kept[0]["matched"]

    def test_matches_symbol_root(self):
        items = [_item("SAP.DE software shares rise")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1

    def test_matches_body_not_just_title(self):
        items = [_item("Quarterly wrap", summary="Taiwan Semiconductor beat estimates")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1

    def test_macro_keyword_match_is_ranked_macro(self):
        items = [_item("Trade tariff on semiconductors takes effect")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "macro"

    def test_global_macro_keyword_match(self):
        items = [_item("Treasury yield rises before auction")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "macro"

    def test_unrelated_item_is_dropped(self):
        items = [_item("Local football club signs new striker")]
        assert market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS) == []

    def test_short_name_does_not_match_inside_a_word(self):
        """A short ticker can hide inside ordinary words."""
        wl = [{"symbol": "ON", "name": "ON", "aliases": [], "market": "us"}]
        items = [
            _item("London stocks close higher"),
            _item("Online retailer opens a store"),
            _item("Contractor wins tender"),
        ]
        assert market_brief.filter_items(items, wl + WATCHLIST, MACRO_KEYWORDS) == []

    def test_short_name_still_matches_as_a_word(self):
        wl = [{"symbol": "ON", "name": "ON", "aliases": [], "market": "us"}]
        items = [_item("ON reports automotive chip growth")]
        assert len(market_brief.filter_items(items, wl, MACRO_KEYWORDS)) == 1

    def test_disclosure_outranks_ticker_and_macro(self):
        items = [
            _item("Fed rate decision due Wednesday"),
            _item("TSMC in the press again"),
            _item("Apple board disclosure filed", is_disclosure=True),
        ]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert [i["rank"] for i in kept] == ["disclosure", "ticker", "macro"]

    def test_disclosure_mentioning_a_watchlist_name_is_kept(self):
        """An official disclosure naming a holding must survive filtering."""
        items = [_item(
            "Apple announces a material company filing",
            summary="Apple Inc disclosed updated information.",
            source="Official disclosure",
            is_disclosure=True,
        )]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "disclosure"
        assert "Apple" in kept[0]["matched"]

    def test_news_only_names_are_matched_like_any_other(self):
        wl = config._watchlist_entries([
            {"name": "SpaceX", "aliases": ["Starship"],
             "market": "us", "news_only": True},
            {"name": "OpenAI", "aliases": ["ChatGPT"],
             "market": "global", "news_only": True},
        ])
        items = [
            _item("Starship completes test flight"),
            _item("ChatGPT launches new enterprise feature"),
        ]
        kept = market_brief.filter_items(items, wl, MACRO_KEYWORDS)
        assert len(kept) == 2
        assert all(i["rank"] == "ticker" for i in kept)

    def test_global_macro_keywords_match(self):
        keywords = ["oil", "dollar index"]
        items = [
            _item("Oil prices climb on export curbs"),
            _item("Dollar index falls before inflation data"),
        ]
        kept = market_brief.filter_items(items, [], keywords)
        assert len(kept) == 2

    def test_caps_at_max_items(self):
        items = [_item(f"Apple story {i}") for i in range(30)]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS, max_items=5)
        assert len(kept) == 5

    def test_empty_watchlist_still_keeps_macro(self):
        items = [_item("Federal Reserve holds rates"), _item("Apple results")]
        kept = market_brief.filter_items(items, [], MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "macro"


# ── price layer ─────────────────────────────────────────────────────


class TestPrices:
    def test_change_percent_and_no_flag_below_threshold(self):
        resp = _mock_response("", payload=_chart_payload([100.0, 101.0]))
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("AAPL")
        assert q["ok"] is True
        assert q["last"] == 101.0
        assert q["prev"] == 100.0
        assert q["change_pct"] == 1.0
        assert q["flagged"] is False
        assert q["currency"] == "USD"

    def test_flags_move_at_or_above_two_percent(self):
        resp = _mock_response("", payload=_chart_payload([100.0, 97.5]))
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("TSM")
        assert q["change_pct"] == -2.5
        assert q["flagged"] is True

    def test_low_beta_symbols_flag_at_one_percent(self):
        resp = _mock_response("", payload=_chart_payload([100.0, 101.2]))
        with patch("httpx.get", return_value=resp):
            low = market_brief.fetch_quote("VWRA.L")
            high = market_brief.fetch_quote("TSM")
        assert low["flagged"] is True     # 1.2% ≥ 1.0 threshold
        assert high["flagged"] is False   # 1.2% < 2.0 threshold

    def test_threshold_table(self):
        assert market_brief.move_threshold("VWRA.L") == 1.0
        assert market_brief.move_threshold("VTI") == 1.0
        assert market_brief.move_threshold("AAPL") == 2.0

    def test_nulls_in_closes_are_ignored(self):
        resp = _mock_response("", payload=_chart_payload([None, 50.0, None, 55.0]))
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("SAP.DE")
        assert q["ok"] is True
        assert q["change_pct"] == 10.0

    def test_unknown_symbol_is_tolerated(self):
        """A wrong global listing symbol must degrade, never crash the brief."""
        resp = _mock_response("", payload={"chart": {"result": None, "error": "Not Found"}})
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("BAD.DE")
        assert q["ok"] is False
        assert q["change_pct"] is None

    def test_http_failure_is_tolerated(self):
        with patch("httpx.get", side_effect=RuntimeError("timeout")):
            q = market_brief.fetch_quote("TSM")
        assert q["ok"] is False

    def test_single_close_is_not_enough(self):
        resp = _mock_response("", payload=_chart_payload([42.0]))
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("TSM")
        assert q["ok"] is False

    def test_news_only_entries_are_never_priced(self):
        """A guessed private-company symbol is worse than no quote at all."""
        wl = [
            {"symbol": "TSM", "name": "TSMC", "aliases": [], "news_only": False},
            {"symbol": "", "name": "SpaceX",
             "aliases": ["Starship"], "news_only": True},
            {"symbol": "OPENAI", "name": "OpenAI",
             "aliases": ["ChatGPT"], "news_only": True},
        ]
        resp = _mock_response("", payload=_chart_payload([10.0, 11.0]))
        with patch("httpx.get", return_value=resp) as mock_get:
            prices = market_brief.fetch_prices(wl)

        assert [p["name"] for p in prices] == ["TSMC"]
        assert mock_get.call_count == 1

    def test_fetch_prices_annotates_entries(self):
        resp = _mock_response("", payload=_chart_payload([10.0, 11.0]))
        with patch("httpx.get", return_value=resp):
            prices = market_brief.fetch_prices(WATCHLIST)
        assert len(prices) == len(WATCHLIST)
        assert prices[0]["name"] == "Apple"
        assert prices[0]["market"] == "us"

    def test_unavailable_price_is_labelled(self):
        text = market_brief.format_prices([
            {"symbol": "BAD.DE", "name": "Unknown listing", "ok": False, "watch_only": True},
        ])
        assert "price unavailable" in text


# ── compose ─────────────────────────────────────────────────────────


class TestComposeBrief:
    @pytest.fixture
    def payload(self):
        prices = [{
            "symbol": "TSM", "name": "TSMC", "ok": True, "last": 200.0,
            "prev": 190.0, "change_pct": 5.26, "currency": "USD",
            "flagged": True, "watch_only": False, "market": "us",
        }]
        items = [_item("TSMC beats estimates")]
        items[0]["rank"] = "ticker"
        items[0]["matched"] = ["TSMC"]
        return prices, items

    @pytest.mark.asyncio
    async def test_uses_claude_output_when_clean(self, payload, watchlist_config):
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("*📊 Market brief*\nTSMC closed +5.3%.", None))

        brief, used_claude = await market_brief.compose_brief(prices, items, runner)

        assert used_claude is True
        assert "TSMC closed" in brief

    @pytest.mark.asyncio
    async def test_error_fallback_text_is_never_shipped(self, payload, watchlist_config):
        """A runner that returns its ⚠️ fallback as text — must not ship it."""
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("⚠️ Claude is unavailable right now.", None))

        brief, used_claude = await market_brief.compose_brief(prices, items, runner)

        assert used_claude is False
        assert "⚠️ Claude is unavailable" not in brief
        assert "Market brief" in brief  # static digest header

    @pytest.mark.asyncio
    async def test_rate_limit_text_is_never_shipped(self, payload, watchlist_config):
        """A ⏳ rate-limit reply has no ⚠️ prefix — the positive header
        check must reject it all the same."""
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(
            return_value=("⏳ You've hit the Claude usage limit. Try again later.", None)
        )

        brief, used_claude = await market_brief.compose_brief(prices, items, runner)

        assert used_claude is False
        assert "usage limit" not in brief
        assert "Market brief" in brief  # static digest header

    @pytest.mark.asyncio
    async def test_compose_denies_tool_use(self, payload, watchlist_config):
        """Untrusted feed text rides in the prompt — the compose call must
        deny every tool."""
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("*📊 Market brief*\nok", None))

        await market_brief.compose_brief(prices, items, runner)

        kwargs = runner.run.call_args.kwargs
        assert kwargs["disallowed_tools"] == market_brief._COMPOSE_DISALLOWED_TOOLS

    @pytest.mark.asyncio
    async def test_exception_falls_back_to_static(self, payload, watchlist_config):
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(side_effect=RuntimeError("session died"))

        brief, used_claude = await market_brief.compose_brief(prices, items, runner)

        assert used_claude is False
        assert brief

    @pytest.mark.asyncio
    async def test_no_runner_uses_static_digest(self, payload, watchlist_config):
        prices, items = payload
        brief, used_claude = await market_brief.compose_brief(prices, items, None)
        assert used_claude is False
        assert "TSMC" in brief

    @pytest.mark.asyncio
    async def test_prompt_carries_the_no_advice_guardrail(self, payload, watchlist_config):
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("ok", None))

        await market_brief.compose_brief(prices, items, runner)

        prompt = runner.run.call_args[0][0]
        lowered = prompt.lower()
        assert "never write a buy, sell, hold" in lowered
        assert "price target" in lowered
        assert "monitoring" in lowered
        # Portfolio context, prices and headlines all reach the model
        assert "TSMC" in prompt
        assert "+5.26%" in prompt or "5.26" in prompt
        assert "TSMC beats estimates" in prompt
        assert runner.run.call_args.kwargs["max_turns"] == 6
        assert runner.run.call_args.kwargs["model"] == config.COMPOSE_MODEL

    @pytest.mark.asyncio
    async def test_smart_money_reaches_the_prompt_as_context_not_advice(
        self, payload, watchlist_config
    ):
        """The 13F section must read as something that was FILED, never as a
        move to make. This is the guardrail that keeps a stale regulatory
        disclosure from turning into an implied recommendation."""
        prices, items = payload
        events = [{
            "name": "Berkshire Hathaway",
            "form": "13F-HR",
            "filing_date": "2026-08-12",
            "url": "https://www.sec.gov/Archives/edgar/data/1067983/x-index.htm",
            "top": [{"issuer": "APPLE INC", "value": 5000}],
            "added": [{"issuer": "NEW CO", "delta": 900, "is_new": True}],
            "exited": [{"issuer": "GONE CO", "delta": 700, "is_gone": True}],
        }]
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("ok", None))

        await market_brief.compose_brief(prices, items, runner, smart_money=events)

        prompt = runner.run.call_args[0][0]
        lowered = prompt.lower()

        # 1) the no-advice guardrail is still there, in full
        assert "never write a buy, sell, hold" in lowered
        assert "price target" in lowered
        assert "monitoring" in lowered

        # 2) the smart-money block is rendered as disclosed fact
        assert "Berkshire Hathaway filed 13F-HR" in prompt
        assert "Largest disclosed positions" in prompt
        assert "newly disclosed" in prompt
        assert "no longer listed" in prompt

        # 3) and the instruction around it forbids treating it as a signal
        assert "backward-looking" in lowered
        assert "never as a move to follow, mirror, or act on" in lowered

        # 4) no imperative/advice verb anywhere in the rendered 13F facts
        from market_brief import smart_money
        rendered = smart_money.format_events(events).lower()
        for banned in ("buy", "sell", "follow suit", "copy", "should", "recommend"):
            assert banned not in rendered, f"advice-flavoured word: {banned!r}"

    @pytest.mark.asyncio
    async def test_smart_money_section_says_nothing_new_when_empty(
        self, payload, watchlist_config
    ):
        """Most days there is no filing — the model is told to omit the section."""
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("ok", None))

        await market_brief.compose_brief(prices, items, runner, smart_money=[])

        prompt = runner.run.call_args[0][0]
        assert "(nothing new)" in prompt
        assert "omit the section entirely" in prompt

    def test_static_digest_includes_smart_money_only_when_present(self):
        prices = [{"symbol": "TSM", "name": "TSMC", "ok": True, "last": 1.0,
                   "prev": 1.0, "change_pct": 0.0, "currency": "USD",
                   "flagged": False, "watch_only": False}]
        events = [{
            "name": "Berkshire Hathaway", "form": "13F-HR",
            "filing_date": "2026-08-12", "url": "https://sec.gov/x",
            "top": [{"issuer": "APPLE INC", "value": 5000}],
            "added": [], "exited": [],
        }]

        without = market_brief.format_digest(prices, [])
        with_events = market_brief.format_digest(prices, [], smart_money=events)

        assert "Smart money" not in without
        assert "Smart money" in with_events
        assert "filed 13F-HR" in with_events

    def test_static_digest_reports_no_move_honestly(self):
        prices = [{"symbol": "TSM", "name": "TSMC", "ok": True, "last": 1.0,
                   "prev": 1.0, "change_pct": 0.0, "currency": "USD",
                   "flagged": False, "watch_only": False}]
        text = market_brief.format_digest(prices, [])
        assert "No watchlist move" in text
        assert "No matching headlines" in text


# ── smart-money isolation ───────────────────────────────────────────


class TestSmartMoneyIsolation:
    def test_scan_includes_the_smart_money_key(self, monkeypatch, seen_file,
                                               watchlist_config):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
        monkeypatch.setattr(market_brief, "fetch_prices", lambda wl: [])
        monkeypatch.setattr(market_brief, "fetch_headlines", lambda watchlist=None: [])
        monkeypatch.setattr("market_brief.smart_money.collect_events", lambda: [{"name": "X"}])

        data = market_brief.scan()
        assert data["smart_money"] == [{"name": "X"}]

    def test_a_broken_13f_check_never_breaks_the_brief(self, monkeypatch, seen_file,
                                                       watchlist_config):
        """Quarterly bonus section — it must not be able to sink the daily job."""
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
        monkeypatch.setattr(market_brief, "fetch_prices", lambda wl: [])
        monkeypatch.setattr(market_brief, "fetch_headlines", lambda watchlist=None: [])

        def boom():
            raise RuntimeError("SEC exploded")

        monkeypatch.setattr("market_brief.smart_money.collect_events", boom)

        data = market_brief.scan()  # must not raise
        assert data["smart_money"] == []


# ── config parsing ──────────────────────────────────────────────────


class TestWatchlistConfig:
    def test_normalises_a_yaml_watchlist(self):
        raw = [
            {"symbol": "AAPL", "name": "Apple",
             "aliases": ["Apple Inc"], "market": "us"},
            {"symbol": "SAP.DE", "name": "SAP", "aliases": ["SAP SE"],
             "market": "europe", "watch_only": True},
        ]
        out = config._watchlist_entries(raw)

        assert len(out) == 2
        assert out[0]["symbol"] == "AAPL"
        assert out[0]["aliases"] == ["Apple Inc"]
        assert out[0]["watch_only"] is False
        assert out[1]["watch_only"] is True

    def test_entry_without_symbol_becomes_news_only(self):
        out = config._watchlist_entries([
            {"name": "SpaceX", "aliases": ["Starship"], "market": "us"},
            {"symbol": "TSM"},
        ])
        assert len(out) == 2
        assert out[0]["symbol"] == ""
        assert out[0]["news_only"] is True   # no symbol → news-only by construction
        assert out[1]["news_only"] is False

    def test_explicit_news_only_flag_is_honoured(self):
        out = config._watchlist_entries([
            {"symbol": "OPENAI", "name": "OpenAI", "news_only": True},
        ])
        assert out[0]["news_only"] is True

    def test_entry_with_neither_symbol_nor_name_is_dropped(self):
        out = config._watchlist_entries([{"aliases": ["???"]}, {"symbol": "TSM"}])
        assert [e["symbol"] for e in out] == ["TSM"]

    def test_name_defaults_to_symbol(self):
        assert config._watchlist_entries([{"symbol": "AVGO"}])[0]["name"] == "AVGO"

    def test_single_alias_string_is_accepted(self):
        out = config._watchlist_entries([{"symbol": "TSM", "aliases": "TSMC"}])
        assert out[0]["aliases"] == ["TSMC"]

    def test_non_list_and_non_dict_are_ignored(self):
        assert config._watchlist_entries("AAPL") == []
        assert config._watchlist_entries([1, 2, 3]) == []
        assert config._watchlist_entries(None) == []

    def test_macro_keywords_use_the_shared_trigger_list(self):
        out = config._trigger_list(
            ["Federal Reserve", "Treasury yield", ""], "market_brief.macro_keywords"
        )
        assert out == ["federal reserve", "treasury yield"]

    def test_config_exposes_the_market_brief_knobs(self):
        for name in (
            "MARKET_BRIEF_ENABLED", "MARKET_BRIEF_SCHEDULE_TIME",
            "MARKET_BRIEF_WATCHLIST", "MARKET_BRIEF_MACRO_KEYWORDS",
            "MARKET_BRIEF_MAX_ITEMS",
        ):
            assert hasattr(config, name), f"config.{name} is missing"
        assert ":" in config.MARKET_BRIEF_SCHEDULE_TIME

    def test_schedule_time_survives_a_sexagesimal_yaml_int(self):
        """Unquoted `08:00` in YAML parses as int 480 — must degrade, not crash."""
        assert config._parse_hhmm(480, "08:00") == "08:00"

    @pytest.mark.parametrize("value", ["24:00", "08:60", "banana", "8:5"])
    def test_schedule_time_rejects_invalid_values(self, value):
        assert config._parse_hhmm(value, "08:00") == "08:00"

    def test_schedule_time_is_canonical(self):
        assert config._parse_hhmm("8:05", "08:00") == "08:05"

    def test_positive_integer_setting_degrades_safely(self):
        assert config._positive_int("bad", 25, "max_items", "market_brief") == 25
        assert config._positive_int(0, 25, "max_items", "market_brief") == 25
        assert config._positive_int("12", 25, "max_items", "market_brief") == 12

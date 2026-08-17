"""Tests for the market brief — feed parsing (incl. the HTML-at-200 trap),
seen-set dedup + TTL prune + persist-after-delivery, entity/macro filtering,
price math and move thresholds, the compose guard, radar wiring, and watchlist
config parsing.

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
    {"symbol": "TSM", "name": "TSMC",
     "aliases": ["Taiwan Semiconductor"], "market": "us", "watch_only": False},
    {"symbol": "AVGO", "name": "Broadcom",
     "aliases": ["Broadcom"], "market": "us", "watch_only": False},
    {"symbol": "VWRA.L", "name": "Vanguard FTSE All-World (VWRA)",
     "aliases": ["VWRA"], "market": "lse", "watch_only": False},
    {"symbol": "NVDA", "name": "NVIDIA",
     "aliases": ["Nvidia"], "market": "us", "watch_only": False},
]

MACRO_KEYWORDS = ["fed rate", "trump tariff", "cpi", "treasury yields"]


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


#: What a misbehaving feed host serves on a bad/renamed path: HTTP 200, HTML.
HTML_TRAP = (
    "<!DOCTYPE html><html><head><title>News site</title></head>"
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
    # Radar is exercised explicitly where it matters; keep it out of the
    # generic pipeline fixtures so their fetch counts stay deterministic.
    monkeypatch.setattr(config, "MARKET_BRIEF_RADAR_ENABLED", False)


# ── parse_feed ──────────────────────────────────────────────────────


class TestParseFeed:
    def test_parses_rss_fixture(self):
        xml = _rss([
            ("TSMC reports Q2 results", "https://example.com/a", "TSMC said..."),
            ("Nasdaq closes higher", "https://example.com/b", "Index up 0.4%"),
        ])
        items = market_brief.parse_feed(xml, "Test feed")

        assert len(items) == 2
        assert items[0]["title"] == "TSMC reports Q2 results"
        assert items[0]["url"] == "https://example.com/a"
        assert items[0]["summary"].startswith("TSMC said")
        assert items[0]["source"] == "Test feed"
        assert items[0]["is_disclosure"] is False

    def test_parses_atom_fixture(self):
        xml = _atom([("Broadcom update", "https://example.com/c", "AVGO news")])
        items = market_brief.parse_feed(xml, "Yahoo: AVGO")

        assert len(items) == 1
        assert items[0]["url"] == "https://example.com/c"
        assert items[0]["title"] == "Broadcom update"

    def test_disclosure_flag_is_carried(self):
        xml = _rss([("Board disclosure", "https://example.com/d", "filing")])
        items = market_brief.parse_feed(xml, "Disclosures", is_disclosure=True)
        assert items[0]["is_disclosure"] is True

    def test_html_body_yields_no_items(self):
        """The trap: an HTML page must never parse into headlines."""
        assert market_brief.parse_feed(HTML_TRAP, "Broken feed") == []

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
        xml = _rss([("T", "https://example.com/g", "<p>TSMC <b>rose</b></p>")])
        assert market_brief.parse_feed(xml, "f")[0]["summary"] == "TSMC rose"


# ── _fetch_feed ─────────────────────────────────────────────────────


class TestFetchFeed:
    def test_rejects_non_xml_content_type_when_required(self):
        """A host that answers a bad path with HTML at HTTP 200 — reject it."""
        resp = _mock_response(HTML_TRAP, content_type="text/html; charset=utf-8")
        with patch("httpx.get", return_value=resp):
            items = market_brief._fetch_feed(
                "Strict feed", "https://example.com/rss/x", True, True,
            )
        assert items == []

    def test_accepts_xml_content_type(self):
        xml = _rss([("Board disclosure", "https://example.com/h", "filing")])
        resp = _mock_response(xml, content_type="text/xml; charset=utf-8")
        with patch("httpx.get", return_value=resp):
            items = market_brief._fetch_feed(
                "Strict feed", "https://example.com/rss/x", True, True,
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
            assert market_brief._fetch_feed("CNBC: markets", "https://c/feed/", False, False) == []

    def test_sends_browser_headers(self):
        resp = _mock_response(_rss([("t", "https://example.com/j", "d")]))
        with patch("httpx.get", return_value=resp) as mock_get:
            market_brief._fetch_feed("CNBC: markets", "https://c/feed/", False, False)
        headers = mock_get.call_args.kwargs["headers"]
        assert "Mozilla/5.0" in headers["User-Agent"]
        assert "xml" in headers["Accept"]

    def test_one_dead_feed_never_kills_the_scan(self):
        good = _mock_response(_rss([("TSMC news", "https://example.com/k", "d")]))

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


# ── seen-set persistence timing ─────────────────────────────────────


def _one_headline_pipeline(monkeypatch):
    """Wire scan()'s fetchers to one matching headline and nothing else."""
    monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
    monkeypatch.setattr(market_brief, "fetch_prices", lambda wl: [])
    monkeypatch.setattr(
        market_brief, "fetch_headlines",
        lambda: [{"source": "Test", "title": "TSMC beats estimates",
                  "url": "https://example.com/tsm", "summary": "",
                  "is_disclosure": False}],
    )
    monkeypatch.setattr("market_brief.smart_money.collect_events", lambda: [])


class TestSeenPersistenceTiming:
    """A headline is only "consumed" once a brief was actually delivered.

    scan() must NOT write the seen-set: a run that dies between scan and send
    would otherwise eat that slot's headlines for the whole TTL window with
    nothing ever reported.
    """

    def test_scan_does_not_persist_the_seen_set(self, seen_file, monkeypatch,
                                                watchlist_config):
        _one_headline_pipeline(monkeypatch)
        data = market_brief.scan()

        assert len(data["items"]) == 1
        assert not seen_file.exists()          # nothing persisted yet
        assert data["seen"]                    # but handed to the caller

    @pytest.mark.asyncio
    async def test_run_persists_the_seen_set_after_delivery(
        self, seen_file, monkeypatch, watchlist_config, tmp_path
    ):
        _one_headline_pipeline(monkeypatch)
        monkeypatch.setattr(market_brief, "_BRIEFS_DIR", tmp_path / "briefs")
        sender = MagicMock()
        sender.send = AsyncMock(return_value=True)

        text = await market_brief.run_scan_and_notify(sender=sender)

        assert "TSMC" in text
        sender.send.assert_awaited_once()
        saved = json.loads(seen_file.read_text(encoding="utf-8"))["seen"]
        assert market_brief._url_hash("https://example.com/tsm") in saved

    @pytest.mark.asyncio
    async def test_interrupted_headlines_resurface_on_the_next_run(
        self, seen_file, monkeypatch, watchlist_config
    ):
        """Simulates a crash after scan: nothing saved → next scan sees the
        same headline as new instead of silently dropping it."""
        _one_headline_pipeline(monkeypatch)
        market_brief.scan()          # run 1 "crashed" before delivery
        data = market_brief.scan()   # run 2

        assert len(data["items"]) == 1


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
    def test_matches_alias(self):
        items = [_item("Broadcom lands new hyperscaler order")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "ticker"
        assert "Broadcom" in kept[0]["matched"]

    def test_matches_name_case_insensitively(self):
        items = [_item("tsmc raises capex guidance")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert "TSMC" in kept[0]["matched"]

    def test_matches_symbol_root(self):
        """"VWRA.L" should also match the bare "VWRA" the press writes."""
        items = [_item("VWRA sees record weekly inflows")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1

    def test_matches_body_not_just_title(self):
        items = [_item("Quarterly wrap", summary="Taiwan Semiconductor beat estimates")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1

    def test_macro_keyword_match_is_ranked_macro(self):
        items = [_item("Trump tariff on semiconductors takes effect")]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "macro"

    def test_unrelated_item_is_dropped(self):
        items = [_item("Local football club signs new striker")]
        assert market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS) == []

    def test_short_name_does_not_match_inside_a_word(self):
        """Short tickers ("ARM", "CPI") hide inside ordinary words — a
        substring match would flood the brief with false positives."""
        wl = [{"symbol": "ARM", "name": "Arm Holdings", "aliases": [], "market": "us"}]
        items = [
            _item("Farmers brace for drought season"),
            _item("Pharma giant beats estimates"),
            _item("Consumers spent more on armchairs"),
        ]
        assert market_brief.filter_items(items, wl, []) == []

    def test_short_name_still_matches_as_a_word(self):
        wl = [{"symbol": "ARM", "name": "Arm Holdings", "aliases": [], "market": "us"}]
        items = [_item("ARM reports record licensing revenue")]
        assert len(market_brief.filter_items(items, wl, [])) == 1

    def test_disclosure_outranks_ticker_and_macro(self):
        items = [
            _item("Fed rate decision due Wednesday"),
            _item("TSMC in the press again"),
            _item("Board disclosure filed for Broadcom", is_disclosure=True),
        ]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS)
        assert [i["rank"] for i in kept] == ["disclosure", "ticker", "macro"]

    def test_news_only_names_are_matched_like_any_other(self):
        wl = config._watchlist_entries([
            {"name": "OpenAI", "aliases": ["ChatGPT"], "market": "us",
             "news_only": True},
        ])
        items = [_item("OpenAI signs data-center deal")]
        kept = market_brief.filter_items(items, wl, MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "ticker"

    def test_caps_at_max_items(self):
        items = [_item(f"TSMC story {i}") for i in range(30)]
        kept = market_brief.filter_items(items, WATCHLIST, MACRO_KEYWORDS, max_items=5)
        assert len(kept) == 5

    def test_empty_watchlist_still_keeps_macro(self):
        items = [_item("Fed rate cut expected"), _item("TSMC results")]
        kept = market_brief.filter_items(items, [], MACRO_KEYWORDS)
        assert len(kept) == 1
        assert kept[0]["rank"] == "macro"


# ── price layer ─────────────────────────────────────────────────────


class TestPrices:
    def test_change_percent_and_no_flag_below_threshold(self):
        resp = _mock_response("", payload=_chart_payload([100.0, 101.0]))
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("AVGO")
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
        assert market_brief.move_threshold("TSM") == 2.0
        assert market_brief.move_threshold("AVGO") == 2.0

    def test_nulls_in_closes_are_ignored(self):
        resp = _mock_response("", payload=_chart_payload([None, 50.0, None, 55.0]))
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("NVDA")
        assert q["ok"] is True
        assert q["change_pct"] == 10.0

    def test_unknown_symbol_is_tolerated(self):
        """A wrong symbol must degrade, never crash the brief."""
        resp = _mock_response("", payload={"chart": {"result": None, "error": "Not Found"}})
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("WRONG")
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

    def test_single_close_falls_back_to_meta_marks(self):
        """Thinly traded listings can return one bar; the meta block still
        carries both closes."""
        payload = _chart_payload([42.0])
        payload["chart"]["result"][0]["meta"].update(
            {"regularMarketPrice": 42.0, "chartPreviousClose": 40.0}
        )
        resp = _mock_response("", payload=payload)
        with patch("httpx.get", return_value=resp):
            q = market_brief.fetch_quote("THIN.L")
        assert q["ok"] is True
        assert q["change_pct"] == 5.0

    def test_news_only_entries_are_never_priced(self):
        """A guessed symbol would print a confident number for the wrong
        instrument — worse than no number at all."""
        wl = [
            {"symbol": "TSM", "name": "TSMC", "aliases": [], "news_only": False},
            {"symbol": "", "name": "OpenAI", "aliases": ["ChatGPT"], "news_only": True},
            {"symbol": "GUESS", "name": "Guessed Co", "aliases": [], "news_only": True},
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
        assert prices[0]["name"] == "TSMC"
        assert prices[0]["market"] == "us"

    def test_unavailable_price_is_labelled(self):
        text = market_brief.format_prices([
            {"symbol": "WRONG", "name": "Guessed Co", "ok": False, "watch_only": True},
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
    async def test_radar_reaches_the_prompt_as_observation_not_tip(
        self, payload, watchlist_config
    ):
        """Radar names are market-wide activity, not picks. The block must be
        present, framed as data, and fenced by the same no-advice guardrail."""
        prices, items = payload
        radar = [{"symbol": "SMCI", "name": "Super Micro", "change_pct": 11.4,
                  "sources": ["day gainers", "trending"]}]
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("ok", None))

        await market_brief.compose_brief(prices, items, runner, radar=radar)

        prompt = runner.run.call_args[0][0]
        lowered = prompt.lower()
        assert "SMCI" in prompt
        assert "+11.40% today" in prompt
        assert "not holdings and not" in lowered
        assert "never frame any of them as a pick" in lowered

    @pytest.mark.asyncio
    async def test_empty_radar_says_nothing_notable(self, payload, watchlist_config):
        prices, items = payload
        runner = MagicMock()
        runner.run = AsyncMock(return_value=("ok", None))

        await market_brief.compose_brief(prices, items, runner, radar=[])

        prompt = runner.run.call_args[0][0]
        assert "(nothing notable)" in prompt

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

    def test_static_digest_includes_radar_only_when_present(self):
        prices = [{"symbol": "TSM", "name": "TSMC", "ok": True, "last": 1.0,
                   "prev": 1.0, "change_pct": 0.0, "currency": "USD",
                   "flagged": False, "watch_only": False}]
        radar = [{"symbol": "SMCI", "name": "Super Micro", "change_pct": 11.4,
                  "sources": ["day gainers"]}]

        without = market_brief.format_digest(prices, [])
        with_radar = market_brief.format_digest(prices, [], radar=radar)

        assert "Radar" not in without
        assert "Radar" in with_radar
        assert "SMCI" in with_radar
        assert "not the watchlist" in with_radar

    def test_static_digest_reports_no_move_honestly(self):
        prices = [{"symbol": "TSM", "name": "TSMC", "ok": True, "last": 1.0,
                   "prev": 1.0, "change_pct": 0.0, "currency": "USD",
                   "flagged": False, "watch_only": False}]
        text = market_brief.format_digest(prices, [])
        assert "No watchlist move" in text
        assert "No matching headlines" in text


# ── bonus-section isolation ─────────────────────────────────────────


class TestBonusSectionIsolation:
    def test_scan_includes_the_smart_money_key(self, monkeypatch, seen_file,
                                               watchlist_config):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
        monkeypatch.setattr(market_brief, "fetch_prices", lambda wl: [])
        monkeypatch.setattr(market_brief, "fetch_headlines", lambda: [])
        monkeypatch.setattr("market_brief.smart_money.collect_events", lambda: [{"name": "X"}])

        data = market_brief.scan()
        assert data["smart_money"] == [{"name": "X"}]

    def test_a_broken_13f_check_never_breaks_the_brief(self, monkeypatch, seen_file,
                                                       watchlist_config):
        """Quarterly bonus section — it must not be able to sink the job."""
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
        monkeypatch.setattr(market_brief, "fetch_prices", lambda wl: [])
        monkeypatch.setattr(market_brief, "fetch_headlines", lambda: [])

        def boom():
            raise RuntimeError("SEC exploded")

        monkeypatch.setattr("market_brief.smart_money.collect_events", boom)

        data = market_brief.scan()  # must not raise
        assert data["smart_money"] == []

    def test_a_broken_radar_never_breaks_the_brief(self, monkeypatch, seen_file,
                                                   watchlist_config):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
        monkeypatch.setattr(config, "MARKET_BRIEF_RADAR_ENABLED", True)
        monkeypatch.setattr(market_brief, "fetch_prices", lambda wl: [])
        monkeypatch.setattr(market_brief, "fetch_headlines", lambda: [])
        monkeypatch.setattr("market_brief.smart_money.collect_events", lambda: [])

        def boom(**kwargs):
            raise RuntimeError("Yahoo exploded")

        monkeypatch.setattr("market_brief.radar.collect", boom)

        data = market_brief.scan()  # must not raise
        assert data["radar"] == []

    def test_radar_respects_the_config_switch(self, monkeypatch, seen_file,
                                              watchlist_config):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
        monkeypatch.setattr(market_brief, "fetch_prices", lambda wl: [])
        monkeypatch.setattr(market_brief, "fetch_headlines", lambda: [])
        monkeypatch.setattr("market_brief.smart_money.collect_events", lambda: [])
        monkeypatch.setattr(config, "MARKET_BRIEF_RADAR_ENABLED", False)

        with patch("market_brief.radar.collect",
                   side_effect=AssertionError("must not fetch")):
            data = market_brief.scan()
        assert data["radar"] == []


# ── config parsing ──────────────────────────────────────────────────


class TestWatchlistConfig:
    def test_normalises_a_yaml_watchlist(self):
        raw = [
            {"symbol": "TSM", "name": "TSMC",
             "aliases": ["Taiwan Semiconductor"], "market": "us"},
            {"symbol": "NVDA", "name": "NVIDIA", "aliases": ["Nvidia"],
             "market": "us", "watch_only": True},
        ]
        out = config._watchlist_entries(raw)

        assert len(out) == 2
        assert out[0]["symbol"] == "TSM"
        assert out[0]["aliases"] == ["Taiwan Semiconductor"]
        assert out[0]["watch_only"] is False
        assert out[1]["watch_only"] is True

    def test_entry_without_symbol_becomes_news_only(self):
        out = config._watchlist_entries([
            {"name": "OpenAI", "aliases": ["ChatGPT"], "market": "us"},
            {"symbol": "TSM"},
        ])
        assert len(out) == 2
        assert out[0]["symbol"] == ""
        assert out[0]["news_only"] is True   # no symbol → news-only by construction
        assert out[1]["news_only"] is False

    def test_explicit_news_only_flag_is_honoured(self):
        out = config._watchlist_entries([
            {"symbol": "GUESS", "name": "Guessed Co", "news_only": True},
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
        assert config._watchlist_entries("TSM") == []
        assert config._watchlist_entries([1, 2, 3]) == []
        assert config._watchlist_entries(None) == []

    def test_macro_keywords_use_the_shared_trigger_list(self):
        out = config._trigger_list(
            ["Fed Rate", "Treasury Yields", ""], "market_brief.macro_keywords"
        )
        assert out == ["fed rate", "treasury yields"]

    def test_config_exposes_the_market_brief_knobs(self):
        for name in (
            "MARKET_BRIEF_ENABLED", "MARKET_BRIEF_SCHEDULE_TIMES",
            "MARKET_BRIEF_SCHEDULE_TIME", "MARKET_BRIEF_WATCHLIST",
            "MARKET_BRIEF_MACRO_KEYWORDS", "MARKET_BRIEF_MAX_ITEMS",
            "MARKET_BRIEF_RADAR_ENABLED", "MARKET_BRIEF_RADAR_COUNT",
        ):
            assert hasattr(config, name), f"config.{name} is missing"
        assert all(":" in t for t in config.MARKET_BRIEF_SCHEDULE_TIMES)
        assert config.MARKET_BRIEF_SCHEDULE_TIME == config.MARKET_BRIEF_SCHEDULE_TIMES[0]

    def test_schedule_time_survives_a_sexagesimal_yaml_int(self):
        """Unquoted `08:00` in YAML parses as int 480 — must degrade, not crash."""
        assert config._parse_hhmm(480, "08:00") == "08:00"

    def test_schedule_times_accepts_a_list_and_drops_junk(self):
        defaults = ["10:00", "18:00", "23:00"]
        assert config._parse_schedule_times(["10:00", 480, "23:00"], defaults) == \
            ["10:00", "23:00"]
        assert config._parse_schedule_times(None, defaults) == defaults
        assert config._parse_schedule_times([480], defaults) == defaults
        assert config._parse_schedule_times("18:00", defaults) == ["18:00"]

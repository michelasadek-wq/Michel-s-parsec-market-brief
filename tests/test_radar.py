"""Tests for the market-wide radar — day-gainers/trending parsing, the merge,
graceful failure, and the observed-not-endorsed phrasing of its output.

No network: every Yahoo call is mocked.
"""

from unittest.mock import MagicMock, patch

from market_brief import radar


def _resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.json = MagicMock(return_value=payload)
    r.raise_for_status = MagicMock()
    return r


def _screener(quotes: list[dict]) -> dict:
    return {"finance": {"result": [{"quotes": quotes}]}}


class TestFetchDayGainers:
    def test_parses_bare_and_wrapped_numbers(self):
        payload = _screener([
            {"symbol": "SMCI", "shortName": "Super Micro",
             "regularMarketChangePercent": 11.4},
            {"symbol": "PLTR", "shortName": "Palantir",
             "regularMarketChangePercent": {"raw": 7.2, "fmt": "7.20%"}},
        ])
        with patch("httpx.get", return_value=_resp(payload)):
            out = radar.fetch_day_gainers()
        assert [(e["symbol"], e["change_pct"]) for e in out] == \
            [("SMCI", 11.4), ("PLTR", 7.2)]
        assert out[0]["sources"] == ["day gainers"]

    def test_quotes_without_symbol_are_skipped(self):
        payload = _screener([{"shortName": "Nameless"}, {"symbol": "OK"}])
        with patch("httpx.get", return_value=_resp(payload)):
            out = radar.fetch_day_gainers()
        assert [e["symbol"] for e in out] == ["OK"]

    def test_failure_returns_empty(self):
        with patch("httpx.get", side_effect=RuntimeError("blocked")):
            assert radar.fetch_day_gainers() == []
        with patch("httpx.get", return_value=_resp({})):
            assert radar.fetch_day_gainers() == []


class TestFetchTrending:
    def test_parses_symbols(self):
        payload = _screener([{"symbol": "NVDA"}, {"symbol": "BTC-USD"}])
        with patch("httpx.get", return_value=_resp(payload)):
            assert radar.fetch_trending() == ["NVDA", "BTC-USD"]

    def test_failure_returns_empty(self):
        with patch("httpx.get", side_effect=RuntimeError("blocked")):
            assert radar.fetch_trending() == []


class TestCollect:
    def test_merges_gainers_and_trending(self, monkeypatch):
        monkeypatch.setattr(radar, "fetch_day_gainers", lambda count: [
            {"symbol": "SMCI", "name": "Super Micro", "change_pct": 11.4,
             "sources": ["day gainers"]},
        ])
        monkeypatch.setattr(radar, "fetch_trending", lambda count: ["SMCI", "NVDA"])

        def quote_fn(symbol):
            return {"ok": True, "change_pct": 3.3}

        out = radar.collect(quote_fn=quote_fn)

        by_symbol = {e["symbol"]: e for e in out}
        # overlap is annotated, not duplicated
        assert by_symbol["SMCI"]["sources"] == ["day gainers", "trending"]
        # trending-only name got its move from the quote_fn
        assert by_symbol["NVDA"]["change_pct"] == 3.3
        assert by_symbol["NVDA"]["sources"] == ["trending"]

    def test_trending_survives_a_dead_quote_fn(self, monkeypatch):
        monkeypatch.setattr(radar, "fetch_day_gainers", lambda count: [])
        monkeypatch.setattr(radar, "fetch_trending", lambda count: ["NVDA"])

        def boom(symbol):
            raise RuntimeError("quote endpoint down")

        out = radar.collect(quote_fn=boom)
        assert out[0]["symbol"] == "NVDA"
        assert out[0]["change_pct"] is None

    def test_caps_total_entries(self, monkeypatch):
        monkeypatch.setattr(radar, "fetch_day_gainers", lambda count: [
            {"symbol": f"G{i}", "name": "", "change_pct": 1.0,
             "sources": ["day gainers"]} for i in range(6)
        ])
        monkeypatch.setattr(radar, "fetch_trending",
                            lambda count: [f"T{i}" for i in range(6)])
        assert len(radar.collect()) == radar._MAX_ENTRIES


class TestFormatRadar:
    def test_renders_moves_as_observations(self):
        text = radar.format_radar([
            {"symbol": "SMCI", "name": "Super Micro", "change_pct": 11.4,
             "sources": ["day gainers", "trending"]},
            {"symbol": "NVDA", "name": "", "change_pct": None,
             "sources": ["trending"]},
        ])
        assert "- SMCI (Super Micro): +11.40% today [day gainers, trending]" in text
        assert "- NVDA: active [trending]" in text

    def test_never_uses_advice_language(self):
        text = radar.format_radar([
            {"symbol": "SMCI", "name": "Super Micro", "change_pct": 11.4,
             "sources": ["day gainers"]},
        ]).lower()
        for banned in ("buy", "sell", "hot", "pick", "opportunity", "should",
                       "recommend", "bullish", "watch this", "don't miss"):
            assert banned not in text, f"advice-flavoured word in radar: {banned!r}"

    def test_empty_renders_empty(self):
        assert radar.format_radar([]) == ""

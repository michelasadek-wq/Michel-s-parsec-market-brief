"""Offline tests for the separate comprehensive IBKR Daily View."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_brief import config, portfolio_view


def _row(**overrides):
    raw = {
        "symbol": "AAPL",
        "name": "Apple",
        "quantity": 10,
        "average_cost": 100,
        "currency": "USD",
        "sector": "Technology",
        "region": "United States",
        "factors": ["Quality", "Large Cap"],
        "realized_pnl": 50,
    }
    raw.update(overrides)
    return config._portfolio_entries([raw])[0]


def _quote(symbol="AAPL", last=150.0, prev=148.0, currency="USD"):
    return {
        "symbol": symbol, "ok": True, "last": last, "prev": prev,
        "change_pct": round((last / prev - 1) * 100, 2), "currency": currency,
        "flagged": False,
    }


def _facts(**overrides):
    data = {
        "ok": True,
        "source": "test",
        "as_of": "2026-08-15",
        "trailing_pe": 25.0,
        "forward_pe": 22.0,
        "peg": 1.2,
        "earnings_growth_pct": 20.0,
        "profit_margin_pct": 25.0,
        "two_hundred_day_average": 130.0,
        "fifty_day_average": 145.0,
        "fifty_two_week_high": 160.0,
    }
    data.update(overrides)
    return data


def _fx(source="USD", target="USD"):
    return {
        "ok": True, "rate": 1.0, "pair": f"{source}/{target}",
        "source": "identity", "as_of": "2026-08-15",
    }


class TestIbkrCsv:
    def test_simple_csv(self, tmp_path):
        path = tmp_path / "positions.csv"
        path.write_text(
            "Symbol,Description,Quantity,Average Price,Currency,Current Price,"
            "Unrealized P/L,Realized P/L\n"
            "AAPL,Apple Inc,10,100,USD,150,500,50\n",
            encoding="utf-8",
        )
        rows = portfolio_view.load_ibkr_csv(path)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["quantity"] == 10
        assert rows[0]["average_cost"] == 100
        assert rows[0]["unrealized_pnl"] == 500
        assert "IBKR CSV" in rows[0]["data_source"]

    def test_activity_statement_open_positions_section(self, tmp_path):
        path = tmp_path / "activity.csv"
        path.write_text(
            "Statement,Header,Field Name,Field Value\n"
            "Open Positions,Header,DataDiscriminator,Asset Category,Currency,"
            "Symbol,Quantity,Cost Price,Close Price,Value,Unrealized P/L\n"
            "Open Positions,Data,Summary,Stocks,USD,MSFT,2,400,420,840,40\n",
            encoding="utf-8",
        )
        rows = portfolio_view.load_ibkr_csv(path)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "MSFT"
        assert rows[0]["market_value"] == 840
        assert rows[0]["asset_class"] == "Stocks"

    def test_duplicate_symbols_use_weighted_average_cost(self):
        rows = portfolio_view.aggregate_positions([
            {"symbol": "AAPL", "quantity": 2, "average_cost": 100,
             "market_value": 300, "unrealized_pnl": 100},
            {"symbol": "AAPL", "quantity": 1, "average_cost": 130,
             "market_value": 150, "unrealized_pnl": 20},
        ])
        assert rows[0]["quantity"] == 3
        assert rows[0]["average_cost"] == 110
        assert rows[0]["market_value"] == 450
        assert rows[0]["unrealized_pnl"] == 120

    def test_config_metadata_enriches_broker_numbers(self):
        broker = [{
            "symbol": "AAPL", "name": "Apple Inc", "quantity": 5,
            "average_cost": 120, "currency": "USD", "data_source": "IBKR",
        }]
        merged = portfolio_view.merge_positions([_row(quantity=1)], broker)
        assert merged[0]["quantity"] == 5
        assert merged[0]["sector"] == "Technology"
        assert merged[0]["factors"] == ["Quality", "Large Cap"]


class TestCalculations:
    def test_complete_profit_and_allocation(self):
        report = portfolio_view.build_report(
            [_row()],
            base_currency="USD",
            deployable_cash=500,
            max_position_pct=90,
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
        )
        holding = report["holdings"][0]
        assert holding["market_value_base"] == 1500
        assert holding["daily_pnl_base"] == 20
        assert holding["unrealized_pnl_base"] == 500
        assert holding["allocation_pct"] == 75
        assert report["summary"]["total_profit"] == 550
        assert report["summary"]["total_profit_complete"] is True

    def test_missing_realized_pnl_makes_total_profit_unavailable(self):
        report = portfolio_view.build_report(
            [_row(realized_pnl=None)],
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
        )
        assert report["summary"]["unrealized_pnl"] == 500
        assert report["summary"]["total_profit"] is None
        assert any("realized P&L" in warning for warning in report["warnings"])

    def test_non_base_currency_uses_sourced_fx(self):
        row = _row(symbol="SAP.DE", currency="EUR", quantity=2, average_cost=100)
        fx = lambda *_: {
            "ok": True, "rate": 1.2, "pair": "EUR/USD",
            "source": "test FX", "as_of": "2026-08-15",
        }
        report = portfolio_view.build_report(
            [row],
            quote_fetcher=lambda _: _quote("SAP.DE", 120, 110, "EUR"),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=fx,
            max_position_pct=100,
        )
        holding = report["holdings"][0]
        assert holding["market_value_base"] == 288
        assert holding["daily_pnl_base"] == 24
        assert holding["fx"]["source"] == "test FX"

    def test_fx_failure_does_not_assume_one_to_one(self):
        row = _row(symbol="7203.T", currency="JPY")
        failed = lambda *_: {
            "ok": False, "rate": None, "pair": "JPY/USD",
            "source": "test FX", "as_of": "2026-08-15",
        }
        report = portfolio_view.build_report(
            [row],
            quote_fetcher=lambda _: _quote("7203.T", 3000, 2950, "JPY"),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=failed,
        )
        assert report["holdings"][0]["market_value_base"] is None
        assert any("FX conversion" in warning for warning in report["warnings"])

    def test_exposure_and_concentration(self):
        rows = [
            _row(symbol="AAPL", quantity=10, sector="Technology", factors=["Quality"]),
            _row(symbol="MSFT", quantity=5, sector="Technology", factors=["Quality"]),
        ]
        quotes = {"AAPL": _quote("AAPL", 150, 149), "MSFT": _quote("MSFT", 300, 299)}
        report = portfolio_view.build_report(
            rows,
            quote_fetcher=lambda symbol: quotes[symbol],
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
            max_position_pct=60,
            max_sector_pct=35,
        )
        assert report["sector_exposure"]["Technology"] == 100
        assert report["factor_exposure"]["Quality"] == 100
        assert any("Technology sector" in warning for warning in report["warnings"])


class TestDecisionScreen:
    def test_strong_evidence_is_buy(self):
        report = portfolio_view.build_report(
            [_row()],
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
            max_position_pct=100,
        )
        decision = report["holdings"][0]["decision"]
        assert decision["action"] == "BUY"
        assert decision["score"] >= 3
        assert decision["data_points"] >= 2

    def test_missing_data_defaults_to_hold(self):
        report = portfolio_view.build_report(
            [_row()],
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: {"ok": False},
            fx_fetcher=_fx,
            max_position_pct=100,
        )
        decision = report["holdings"][0]["decision"]
        assert decision["action"] == "HOLD"
        assert "fewer than two" in " ".join(decision["reasons"])

    def test_demanding_valuation_and_negative_growth_is_sell(self):
        bad = _facts(
            forward_pe=70, peg=4, earnings_growth_pct=-10,
            profit_margin_pct=-2, two_hundred_day_average=200,
        )
        report = portfolio_view.build_report(
            [_row()],
            quote_fetcher=lambda _: _quote(last=150, prev=155),
            fundamentals_fetcher=lambda _: bad,
            fx_fetcher=_fx,
            max_position_pct=100,
        )
        assert report["holdings"][0]["decision"]["action"] == "SELL"

    def test_deployment_plan_uses_exact_cash_amount(self):
        candidate = config._portfolio_entries([
            {"symbol": "AVGO", "sector": "Technology", "region": "United States"}
        ], watch_only=True)[0]
        report = portfolio_view.build_report(
            [], [candidate],
            deployable_cash=500,
            max_position_pct=100,
            quote_fetcher=lambda _: _quote("AVGO", 300, 295),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
        )
        assert report["deployment_plan"] == [{
            "symbol": "AVGO", "name": "AVGO", "amount": 500.0,
            "score": 5, "new_position": True,
        }]


class TestRenderingAndDelivery:
    def test_view_contains_the_complete_contract(self):
        report = portfolio_view.build_report(
            [_row()],
            base_currency="USD",
            deployable_cash=100,
            max_position_pct=100,
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
        )
        text = portfolio_view.format_view(report)
        for expected in (
            "Portfolio snapshot", "Total profit to date", "P/E T/F", "PEG",
            "Exposure and diversification", "Risks and data gaps",
            "Market context and catalysts", "Actions and cash deployment",
            "BUY", "HOLD", "SELL", "Research candidates", "no IBKR login",
        ):
            assert expected in text

    @pytest.mark.asyncio
    async def test_delivery_does_not_archive_private_view(self, monkeypatch):
        report = portfolio_view.build_report(
            [_row()],
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
            max_position_pct=100,
        )
        monkeypatch.setattr(portfolio_view, "build_configured_report", lambda: report)
        sender = MagicMock()
        sender.send = AsyncMock()
        with patch("market_brief.brief.archive_brief") as archive:
            text = await portfolio_view.run_and_notify(sender=sender)
        assert "IBKR DAILY VIEW" in text
        sender.send.assert_awaited_once()
        archive.assert_not_called()


class TestPublicDataParsing:
    def test_fundamentals_fetch_is_mocked(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"quoteResponse": {"result": [{
            "trailingPE": 30,
            "forwardPE": 24,
            "epsTrailingTwelveMonths": 5,
            "epsForward": 6,
            "fiftyDayAverage": 145,
            "twoHundredDayAverage": 130,
            "fiftyTwoWeekHigh": 160,
            "marketCap": 3000000000000,
            "regularMarketTime": 1786665600,
        }]}}
        with patch("market_brief.portfolio_view.httpx.get", return_value=response):
            facts = portfolio_view.fetch_fundamentals("AAPL")
        assert facts["ok"] is True
        assert facts["forward_pe"] == 24
        assert facts["earnings_growth_pct"] == pytest.approx(20)
        assert facts["peg"] == pytest.approx(1.2)

    def test_fx_tries_inverse_when_direct_is_missing(self):
        missing = MagicMock()
        missing.raise_for_status = MagicMock()
        missing.json.return_value = {"chart": {"result": []}}
        inverse = MagicMock()
        inverse.raise_for_status = MagicMock()
        inverse.json.return_value = {"chart": {"result": [{
            "timestamp": [1786665600],
            "indicators": {"quote": [{"close": [0.8]}]},
        }]}}
        with patch(
            "market_brief.portfolio_view.httpx.get", side_effect=[missing, inverse]
        ) as get:
            fx = portfolio_view.fetch_fx_rate("EUR", "USD")
        assert get.call_count == 2
        assert fx["rate"] == 1.25
        assert fx["source"] == "Yahoo Finance FX close"

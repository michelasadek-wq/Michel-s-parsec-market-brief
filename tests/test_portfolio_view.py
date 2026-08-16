"""Offline tests for the separate comprehensive IBKR Daily View."""

from datetime import date
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
        assert rows[0]["as_of"]
        assert rows[0]["as_of_source"] == "CSV file modified time"

    def test_activity_statement_open_positions_section(self, tmp_path):
        path = tmp_path / "activity.csv"
        path.write_text(
            "Statement,Header,Field Name,Field Value\n"
            "Statement,Data,Period,\"August 1, 2026 - August 14, 2026\"\n"
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
        assert rows[0]["as_of"] == "2026-08-14"
        assert rows[0]["as_of_source"] == "IBKR statement period"

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
        broker = [portfolio_view._normalise_broker_row({
            "Symbol": "AAPL", "Description": "Apple Inc", "Quantity": "5",
            "Average Price": "120", "Currency": "USD",
        }, "IBKR")]
        merged = portfolio_view.merge_positions([
            _row(quantity=1, asset_class="ETF")
        ], broker)
        assert merged[0]["quantity"] == 5
        assert merged[0]["sector"] == "Technology"
        assert merged[0]["factors"] == ["Quality", "Large Cap"]
        assert merged[0]["asset_class"] == "ETF"


class TestIbkrFlex:
    REPORT = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Daily Portfolio Monitoring">
  <FlexStatements count="1">
    <FlexStatement accountId="PRIVATE" fromDate="20260801" toDate="20260814">
      <OpenPositions>
        <OpenPosition accountId="PRIVATE" acctAlias="PRIVATE_ALIAS"
          assetCategory="STK" currency="USD" symbol="MSFT"
          description="Microsoft Corp" position="2" markPrice="420"
          positionValue="840" costBasisPrice="400"
          fifoPnlUnrealized="40" reportDate="20260814" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""

    def test_flex_xml_maps_camel_case_and_drops_account_fields(self):
        rows = portfolio_view.load_ibkr_flex_xml(self.REPORT)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "MSFT"
        assert rows[0]["quantity"] == 2
        assert rows[0]["average_cost"] == 400
        assert rows[0]["current_price"] == 420
        assert rows[0]["market_value"] == 840
        assert rows[0]["unrealized_pnl"] == 40
        assert rows[0]["as_of"] == "2026-08-14"
        assert rows[0]["account"] == ""
        assert "PRIVATE" not in repr(rows)

    def test_flex_requires_open_positions_section(self):
        with pytest.raises(portfolio_view.IBKRFlexError, match="Open Positions"):
            portfolio_view.load_ibkr_flex_xml(
                "<FlexQueryResponse><FlexStatements /></FlexQueryResponse>"
            )

    def test_summary_row_prevents_double_counting_lots(self):
        report = """<FlexQueryResponse><FlexStatements><FlexStatement
          toDate="20260814"><OpenPositions>
          <OpenPosition symbol="AAPL" currency="USD" position="3"
            costBasisPrice="110" levelOfDetail="Summary" />
          <OpenPosition symbol="AAPL" currency="USD" position="2"
            costBasisPrice="100" levelOfDetail="Lot" />
          <OpenPosition symbol="AAPL" currency="USD" position="1"
            costBasisPrice="130" levelOfDetail="Lot" />
          </OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>"""
        rows = portfolio_view.load_ibkr_flex_xml(report)
        assert rows[0]["quantity"] == 3
        assert rows[0]["average_cost"] == 110

    def test_flex_keeps_only_latest_snapshot_from_multi_day_period(self):
        report = """<FlexQueryResponse><FlexStatements><FlexStatement
          fromDate="20260813" toDate="20260814"><OpenPositions>
          <OpenPosition symbol="AAPL" position="1" markPrice="100"
            positionValue="100" fifoPnlUnrealized="10"
            reportDate="20260813" levelOfDetail="Summary" />
          <OpenPosition symbol="AAPL" position="2" markPrice="110"
            positionValue="220" fifoPnlUnrealized="30"
            reportDate="20260814" levelOfDetail="Summary" />
          <OpenPosition symbol="MSFT" position="1" markPrice="400"
            positionValue="400" fifoPnlUnrealized="20"
            reportDate="20260813" levelOfDetail="Summary" />
          <OpenPosition symbol="NVDA" position="1" markPrice="180"
            positionValue="180" fifoPnlUnrealized="15"
            reportDate="20260814" levelOfDetail="Summary" />
          </OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>"""

        rows = portfolio_view.load_ibkr_flex_xml(report)

        assert [row["symbol"] for row in rows] == ["AAPL", "NVDA"]
        assert rows[0]["quantity"] == 2
        assert rows[0]["market_value"] == 220
        assert rows[0]["unrealized_pnl"] == 30
        assert {row["as_of"] for row in rows} == {"2026-08-14"}

    def test_flex_uses_statement_date_not_generic_open_date(self):
        report = """<FlexQueryResponse><FlexStatements>
          <FlexStatement toDate="20260813"><OpenPositions>
            <OpenPosition symbol="AAPL" position="1" positionValue="100"
              date="20260720" />
          </OpenPositions></FlexStatement>
          <FlexStatement toDate="20260814"><OpenPositions>
            <OpenPosition symbol="AAPL" position="2" positionValue="220"
              date="20260720" />
          </OpenPositions></FlexStatement>
        </FlexStatements></FlexQueryResponse>"""

        rows = portfolio_view.load_ibkr_flex_xml(report)

        assert len(rows) == 1
        assert rows[0]["quantity"] == 2
        assert rows[0]["market_value"] == 220
        assert rows[0]["as_of"] == "2026-08-14"
        assert rows[0]["as_of_source"] == "Flex statement date"

    def test_two_step_fetch_polls_transient_response(self):
        def response(text):
            item = MagicMock()
            item.text = text
            item.raise_for_status = MagicMock()
            return item

        send = response(
            "<FlexStatementResponse><Status>Success</Status>"
            "<ReferenceCode>123456</ReferenceCode>"
            "<url>https://ndcdyn.interactivebrokers.com/AccountManagement/"
            "FlexWebService/GetStatement</url></FlexStatementResponse>"
        )
        pending = response(
            "<FlexStatementResponse><Status>Fail</Status>"
            "<ErrorCode>1019</ErrorCode>"
            "<ErrorMessage>Statement generation in progress.</ErrorMessage>"
            "</FlexStatementResponse>"
        )
        final = response(self.REPORT)
        client = MagicMock()
        client.get.side_effect = [send, pending, final]
        sleeper = MagicMock()

        result = portfolio_view.fetch_ibkr_flex_xml(
            "secret-token", "secret-query", client=client, sleep=sleeper
        )

        assert result == self.REPORT
        assert client.get.call_count == 3
        sleeper.assert_called_once_with(5)

    def test_fetch_ignores_response_url_and_uses_pinned_endpoint(self):
        def response(text):
            item = MagicMock()
            item.text = text
            item.raise_for_status = MagicMock()
            return item

        send = response(
            "<FlexStatementResponse><Status>Success</Status>"
            "<ReferenceCode>123456</ReferenceCode>"
            "<url>https://example.invalid/untrusted-report</url>"
            "</FlexStatementResponse>"
        )
        final = response(self.REPORT)
        client = MagicMock()
        client.get.side_effect = [send, final]

        result = portfolio_view.fetch_ibkr_flex_xml(
            "secret-token", "secret-query", client=client
        )

        assert result == self.REPORT
        retrieval = client.get.call_args_list[1]
        assert retrieval.args[0] == portfolio_view._IBKR_FLEX_REPORT_URL
        assert retrieval.kwargs["params"] == {
            "t": "secret-token", "q": "123456", "v": "3"
        }

    def test_fetch_error_redacts_secrets(self):
        failed = MagicMock()
        failed.text = (
            "<FlexStatementResponse><Status>Fail</Status>"
            "<ErrorCode>1015</ErrorCode>"
            "<ErrorMessage>bad TOKEN-VALUE for QUERY-VALUE</ErrorMessage>"
            "</FlexStatementResponse>"
        )
        failed.raise_for_status = MagicMock()
        client = MagicMock()
        client.get.return_value = failed
        with pytest.raises(portfolio_view.IBKRFlexError) as error:
            portfolio_view.fetch_ibkr_flex_xml(
                "TOKEN-VALUE", "QUERY-VALUE", client=client
            )
        assert "TOKEN-VALUE" not in str(error.value)
        assert "QUERY-VALUE" not in str(error.value)

    def test_live_flex_config_rows_are_metadata_only(self, monkeypatch):
        monkeypatch.setattr(config, "PORTFOLIO_VIEW_IBKR_FLEX_ENABLED", True)
        monkeypatch.setattr(config, "PORTFOLIO_VIEW_POSITIONS", [
            _row(symbol="AAPL"),
            _row(symbol="MSFT", sector="Technology", quantity=None),
        ])
        monkeypatch.setenv("IBKR_FLEX_TOKEN", "token")
        monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "query")
        monkeypatch.setattr(
            portfolio_view, "fetch_ibkr_flex_xml", lambda *_: self.REPORT
        )
        rows = portfolio_view.configured_positions()
        assert [row["symbol"] for row in rows] == ["MSFT"]
        assert rows[0]["sector"] == "Technology"


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
        assert report["summary"]["account_value"] is None
        assert report["summary"]["account_value_complete"] is False
        assert report["holdings"][0]["decision"]["action"] == "HOLD"
        assert "Account value: unavailable" in portfolio_view.format_view(report)
        assert any("FX conversion" in warning for warning in report["warnings"])

    def test_stale_snapshot_blocks_recommendations(self, monkeypatch):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 15))
        report = portfolio_view.build_report(
            [_row(as_of="2026-08-01")],
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
            max_position_pct=100,
            max_sector_pct=100,
            max_snapshot_age_days=3,
        )
        decision = report["holdings"][0]["decision"]
        assert decision["action"] == "HOLD"
        assert "snapshot is stale" in " ".join(decision["reasons"])
        assert any("older than 3 days" in warning for warning in report["warnings"])

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
            deployable_cash=500,
            quote_fetcher=lambda _: _quote(),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
            max_position_pct=100,
            max_sector_pct=100,
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
            max_sector_pct=100,
            quote_fetcher=lambda _: _quote("AVGO", 300, 295),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
        )
        assert report["deployment_plan"] == [{
            "symbol": "AVGO", "name": "AVGO", "amount": 500.0,
            "score": 5, "new_position": True,
        }]

    def test_overweight_sector_blocks_candidate_and_cash_plan(self):
        candidate = config._portfolio_entries([
            {"symbol": "AVGO", "sector": "Technology"}
        ], watch_only=True)[0]
        report = portfolio_view.build_report(
            [_row()], [candidate],
            deployable_cash=1000,
            max_position_pct=60,
            max_sector_pct=35,
            quote_fetcher=lambda symbol: _quote(symbol),
            fundamentals_fetcher=lambda _: _facts(),
            fx_fetcher=_fx,
        )
        decision = report["research_candidates"][0]["decision"]
        assert report["sector_exposure"]["Technology"] == 100
        assert decision["action"] == "HOLD"
        assert "sector is at or above" in " ".join(decision["reasons"])
        assert "sector is at or above" in decision["reasons"][0]
        assert report["deployment_plan"] == []


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
            "Portfolio snapshot", "Combined supplied P&L", "P/E T/F", "PEG",
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

"""Tests for the SEC EDGAR 13F watcher — filing detection, information-table
parsing, quarter-over-quarter diffing, first-run baselining, state persistence,
total failure tolerance, and the reporting-not-advice phrasing of its output.

No network: every EDGAR call is mocked.
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from market_brief import config, smart_money


# ── Fixtures ────────────────────────────────────────────────────────


BERKSHIRE = {"name": "Berkshire Hathaway", "cik": "0001067983"}


def _submissions(forms_and_dates: list[tuple[str, str, str]]) -> dict:
    """Build an EDGAR submissions payload from (form, accession, date) triples."""
    return {
        "cik": "1067983",
        "name": "Berkshire Hathaway Inc",
        "filings": {
            "recent": {
                "form": [f for f, _, _ in forms_and_dates],
                "accessionNumber": [a for _, a, _ in forms_and_dates],
                "filingDate": [d for _, _, d in forms_and_dates],
            }
        },
    }


def _info_table(rows: list[tuple[str, float]], shares: dict | None = None) -> str:
    """Build a 13F information table from (issuer, value) pairs.

    Share counts default to the value (fine for most tests); pass ``shares``
    to give an issuer a different count, e.g. to model a pure price move.
    """
    shares = shares or {}
    body = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer>"
        f"<titleOfClass>COM</titleOfClass><cusip>00000000{i}</cusip>"
        f"<value>{int(v)}</value>"
        f"<shrsOrPrnAmt><sshPrnamt>{int(shares.get(n, v))}</sshPrnamt>"
        f"<sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
        f"<investmentDiscretion>SOLE</investmentDiscretion>"
        f"</infoTable>"
        for i, (n, v) in enumerate(rows)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
        f"{body}</informationTable>"
    )


def _resp(text: str = "", payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.text = text
    r.json = MagicMock(return_value=payload if payload is not None else {})
    r.raise_for_status = MagicMock()
    return r


def _dir_index(names: list[str]) -> dict:
    return {"directory": {"item": [{"name": n} for n in names]}}


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "data" / "smart_money_seen.json"
    monkeypatch.setattr(smart_money, "_STATE_FILE", f)
    return f


@pytest.fixture
def tracker_config(monkeypatch):
    monkeypatch.setattr(config, "MARKET_BRIEF_TRACKERS", [BERKSHIRE])
    monkeypatch.setattr(config, "MARKET_BRIEF_SEC_CONTACT", "research@parsec.solutions")
    monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))


def _edgar_responses(submissions, files, table_xml):
    """Route the three EDGAR GETs (submissions, dir index, table) by URL."""
    def router(url, *args, **kwargs):
        if "data.sec.gov/submissions" in url:
            return _resp(payload=submissions)
        if url.endswith("index.json"):
            return _resp(payload=_dir_index(files))
        return _resp(text=table_xml)
    return router


# ── SEC request hygiene ─────────────────────────────────────────────


class TestSecHeaders:
    def test_user_agent_declares_a_contact(self, tracker_config):
        headers = smart_money._sec_headers()
        assert headers["User-Agent"].startswith("parsec-market-brief ")
        assert "@" in headers["User-Agent"]

    def test_contact_comes_from_config(self, monkeypatch):
        monkeypatch.setattr(config, "MARKET_BRIEF_SEC_CONTACT", "ops@example.com")
        assert "ops@example.com" in smart_money._sec_headers()["User-Agent"]

    def test_cik_is_zero_padded_to_ten_digits(self):
        assert smart_money._pad_cik("1067983") == "0001067983"
        assert smart_money._pad_cik("0001067983") == "0001067983"
        assert smart_money._pad_cik("CIK 1067983") == "0001067983"
        assert smart_money._pad_cik("") == ""


# ── latest_13f ──────────────────────────────────────────────────────


class TestLatest13F:
    def test_picks_the_newest_13f_hr(self):
        subs = _submissions([
            ("8-K", "0000000000-26-000001", "2026-08-10"),
            ("13F-HR", "0000950123-26-008843", "2026-08-12"),
            ("13F-HR", "0000950123-26-005000", "2026-05-15"),
        ])
        filing = smart_money.latest_13f(subs)
        assert filing["accession"] == "0000950123-26-008843"
        assert filing["accession_nodash"] == "000095012326008843"
        assert filing["filing_date"] == "2026-08-12"

    def test_amendments_count_as_filings(self):
        subs = _submissions([("13F-HR/A", "0000950123-26-009000", "2026-08-13")])
        assert smart_money.latest_13f(subs)["form"] == "13F-HR/A"

    def test_no_13f_returns_none(self):
        assert smart_money.latest_13f(_submissions([("8-K", "a", "2026-08-10")])) is None

    def test_empty_payload_returns_none(self):
        assert smart_money.latest_13f({}) is None


# ── information table parsing ───────────────────────────────────────


class TestParseInformationTable:
    def test_parses_and_sorts_by_value(self):
        xml = _info_table([("APPLE INC", 1000), ("COCA COLA CO", 3000)])
        positions = smart_money.parse_information_table(xml)
        assert [p["issuer"] for p in positions] == ["COCA COLA CO", "APPLE INC"]
        assert positions[0]["value"] == 3000

    def test_carries_share_counts(self):
        """Shares ride alongside value — they are what the diff runs on."""
        xml = _info_table([("APPLE INC", 1000)], shares={"APPLE INC": 250})
        positions = smart_money.parse_information_table(xml)
        assert positions[0]["shares"] == 250.0

    def test_aggregates_duplicate_issuer_rows(self):
        """One issuer spans several rows (share classes, sub-managers)."""
        xml = _info_table([("APPLE INC", 500), ("APPLE INC", 700), ("KRAFT", 100)])
        positions = smart_money.parse_information_table(xml)
        assert len(positions) == 2
        assert positions[0] == {"issuer": "APPLE INC", "value": 1200.0,
                                "shares": 1200.0}

    def test_html_or_garbage_returns_empty(self):
        assert smart_money.parse_information_table("<!DOCTYPE html><html></html>") == []
        assert smart_money.parse_information_table("not xml at all") == []

    def test_rows_without_issuer_are_skipped(self):
        xml = (
            '<?xml version="1.0"?><informationTable>'
            "<infoTable><value>100</value></infoTable>"
            "<infoTable><nameOfIssuer>REAL CO</nameOfIssuer><value>200</value></infoTable>"
            "</informationTable>"
        )
        assert [p["issuer"] for p in smart_money.parse_information_table(xml)] == ["REAL CO"]

    def test_unparseable_value_does_not_raise(self):
        xml = (
            '<?xml version="1.0"?><informationTable>'
            "<infoTable><nameOfIssuer>ODD CO</nameOfIssuer><value>n/a</value></infoTable>"
            "</informationTable>"
        )
        assert smart_money.parse_information_table(xml) == [
            {"issuer": "ODD CO", "value": 0.0, "shares": 0.0}
        ]


# ── diffing ─────────────────────────────────────────────────────────


class TestDiffPositions:
    def test_price_appreciation_alone_is_not_an_add(self):
        """The whole point of diffing shares: value up, shares flat = the
        price moved, the manager did nothing. Must not be reported."""
        prev = {"APPLE INC": {"value": 1000, "shares": 100}}
        curr = [{"issuer": "APPLE INC", "value": 1500, "shares": 100}]
        assert smart_money.diff_positions(prev, curr) == {"added": [], "exited": []}

    def test_share_count_changes_are_reported_with_their_basis(self):
        prev = {"APPLE INC": {"value": 1000, "shares": 100},
                "KRAFT": {"value": 500, "shares": 50}}
        curr = [{"issuer": "APPLE INC", "value": 900, "shares": 120},  # bought, price fell
                {"issuer": "KRAFT", "value": 600, "shares": 40}]      # sold, price rose
        moves = smart_money.diff_positions(prev, curr)

        added = {p["issuer"]: p for p in moves["added"]}
        exited = {p["issuer"]: p for p in moves["exited"]}
        assert added["APPLE INC"]["delta"] == 20
        assert added["APPLE INC"]["basis"] == "shares"
        assert exited["KRAFT"]["delta"] == 10
        assert exited["KRAFT"]["basis"] == "shares"

    def test_legacy_value_only_baseline_falls_back_to_value(self):
        """A baseline written by the previous version stores bare values —
        the diff must still work, on value, and say so via basis."""
        prev = {"APPLE INC": 1000}
        curr = [{"issuer": "APPLE INC", "value": 1500, "shares": 100}]
        moves = smart_money.diff_positions(prev, curr)
        assert moves["added"][0]["basis"] == "value"

    def test_detects_new_and_increased_positions(self):
        prev = {"APPLE INC": 1000, "KRAFT": 500}
        curr = [{"issuer": "APPLE INC", "value": 1500},
                {"issuer": "NEW CO", "value": 800},
                {"issuer": "KRAFT", "value": 500}]
        moves = smart_money.diff_positions(prev, curr)
        added = {p["issuer"]: p for p in moves["added"]}
        assert added["NEW CO"]["is_new"] is True
        assert added["APPLE INC"]["is_new"] is False
        assert added["APPLE INC"]["delta"] == 500
        assert "KRAFT" not in added  # unchanged

    def test_detects_exits_and_reductions(self):
        prev = {"APPLE INC": 1000, "GONE CO": 700}
        curr = [{"issuer": "APPLE INC", "value": 400}]
        moves = smart_money.diff_positions(prev, curr)
        exited = {p["issuer"]: p for p in moves["exited"]}
        assert exited["GONE CO"]["is_gone"] is True
        assert exited["APPLE INC"]["is_gone"] is False
        assert exited["APPLE INC"]["delta"] == 600

    def test_no_baseline_yields_no_moves(self):
        curr = [{"issuer": "APPLE INC", "value": 1000}]
        assert smart_money.diff_positions({}, curr) == {"added": [], "exited": []}

    def test_moves_are_capped_and_biggest_first(self):
        prev = {f"CO{i}": 100 for i in range(10)}
        curr = [{"issuer": f"CO{i}", "value": 100 + i * 10} for i in range(10)]
        moves = smart_money.diff_positions(prev, curr)
        assert len(moves["added"]) == smart_money._MOVES_N
        deltas = [p["delta"] for p in moves["added"]]
        assert deltas == sorted(deltas, reverse=True)


# ── collect_events ──────────────────────────────────────────────────


class TestCollectEvents:
    def test_reports_a_recent_new_filing_on_first_run(self, state_file, tracker_config):
        subs = _submissions([("13F-HR", "0000950123-26-008843", "2026-08-12")])
        router = _edgar_responses(subs, ["primary_doc.xml", "infotable.xml"],
                                  _info_table([("APPLE INC", 5000), ("KRAFT", 100)]))

        with patch("httpx.get", side_effect=router):
            events = smart_money.collect_events()

        assert len(events) == 1
        assert events[0]["name"] == "Berkshire Hathaway"
        assert events[0]["top"][0]["issuer"] == "APPLE INC"
        assert "0000950123-26-008843-index.htm" in events[0]["url"]

    def test_first_run_with_an_old_filing_only_baselines(self, state_file, tracker_config):
        """A filing already months old is not "new" — record it, announce nothing."""
        subs = _submissions([("13F-HR", "0000950123-26-005000", "2026-05-15")])
        router = _edgar_responses(subs, ["infotable.xml"], _info_table([("APPLE INC", 5000)]))

        with patch("httpx.get", side_effect=router):
            events = smart_money.collect_events()

        assert events == []
        saved = json.loads(state_file.read_text(encoding="utf-8"))["trackers"]
        assert saved["0001067983"]["accession"] == "0000950123-26-005000"

    def test_same_filing_is_not_reported_twice(self, state_file, tracker_config):
        subs = _submissions([("13F-HR", "0000950123-26-008843", "2026-08-12")])
        router = _edgar_responses(subs, ["infotable.xml"], _info_table([("APPLE INC", 5000)]))

        with patch("httpx.get", side_effect=router):
            first = smart_money.collect_events()
            second = smart_money.collect_events()

        assert len(first) == 1
        assert second == []

    def test_second_filing_diffs_against_the_stored_quarter(self, state_file, tracker_config):
        q1 = _submissions([("13F-HR", "0000950123-26-005000", "2026-08-12")])
        r1 = _edgar_responses(q1, ["infotable.xml"],
                              _info_table([("APPLE INC", 1000), ("GONE CO", 700)]))
        with patch("httpx.get", side_effect=r1):
            smart_money.collect_events()

        q2 = _submissions([("13F-HR", "0000950123-26-008843", "2026-08-13")])
        r2 = _edgar_responses(q2, ["infotable.xml"],
                              _info_table([("APPLE INC", 2500), ("NEW CO", 900)]))
        with patch("httpx.get", side_effect=r2):
            events = smart_money.collect_events()

        assert len(events) == 1
        added = {p["issuer"] for p in events[0]["added"]}
        exited = {p["issuer"] for p in events[0]["exited"]}
        assert "NEW CO" in added and "APPLE INC" in added
        assert "GONE CO" in exited

    def test_unparseable_table_still_reports_the_filing(self, state_file, tracker_config):
        """Degrades to "a new filing exists, here is the link" — never nothing."""
        subs = _submissions([("13F-HR", "0000950123-26-008843", "2026-08-12")])
        router = _edgar_responses(subs, ["infotable.xml"], "<!DOCTYPE html><html></html>")

        with patch("httpx.get", side_effect=router):
            events = smart_money.collect_events()

        assert len(events) == 1
        assert events[0]["top"] == []
        assert events[0]["url"]

    def test_dead_sec_endpoint_returns_no_events(self, state_file, tracker_config):
        with patch("httpx.get", side_effect=RuntimeError("SEC unreachable")):
            assert smart_money.collect_events() == []

    def test_no_trackers_configured_is_a_noop(self, state_file, monkeypatch):
        monkeypatch.setattr(config, "MARKET_BRIEF_TRACKERS", [])
        with patch("httpx.get", side_effect=AssertionError("must not fetch")) as _:
            assert smart_money.collect_events() == []

    def test_one_broken_tracker_does_not_stop_the_others(self, state_file, monkeypatch):
        monkeypatch.setattr(config, "today", lambda: date(2026, 8, 13))
        monkeypatch.setattr(config, "MARKET_BRIEF_TRACKERS", [
            {"name": "Broken", "cik": "0000000001"},
            BERKSHIRE,
        ])
        subs = _submissions([("13F-HR", "0000950123-26-008843", "2026-08-12")])
        table = _info_table([("APPLE INC", 5000)])

        def router(url, *args, **kwargs):
            if "CIK0000000001" in url:
                raise RuntimeError("boom")
            if "data.sec.gov/submissions" in url:
                return _resp(payload=subs)
            if url.endswith("index.json"):
                return _resp(payload=_dir_index(["infotable.xml"]))
            return _resp(text=table)

        with patch("httpx.get", side_effect=router):
            events = smart_money.collect_events()

        assert [e["name"] for e in events] == ["Berkshire Hathaway"]

    def test_cover_page_xml_is_never_used_as_the_table(self, state_file, tracker_config):
        """primary_doc.xml is the cover page, not the holdings."""
        subs = _submissions([("13F-HR", "0000950123-26-008843", "2026-08-12")])
        seen_urls = []

        def router(url, *args, **kwargs):
            seen_urls.append(url)
            if "data.sec.gov/submissions" in url:
                return _resp(payload=subs)
            if url.endswith("index.json"):
                return _resp(payload=_dir_index(["primary_doc.xml", "form13fInfoTable.xml"]))
            return _resp(text=_info_table([("APPLE INC", 5000)]))

        with patch("httpx.get", side_effect=router):
            smart_money.collect_events()

        assert any(u.endswith("form13fInfoTable.xml") for u in seen_urls)
        assert not any(u.endswith("primary_doc.xml") for u in seen_urls)


# ── output phrasing ─────────────────────────────────────────────────


class TestFormatEvents:
    @pytest.fixture
    def event(self):
        return {
            "name": "Berkshire Hathaway",
            "form": "13F-HR",
            "filing_date": "2026-08-12",
            "url": "https://www.sec.gov/Archives/edgar/data/1067983/x-index.htm",
            "top": [{"issuer": "APPLE INC", "value": 5000},
                    {"issuer": "COCA COLA CO", "value": 3000}],
            "added": [{"issuer": "NEW CO", "delta": 900, "is_new": True}],
            "exited": [{"issuer": "GONE CO", "delta": 700, "is_gone": True}],
        }

    def test_empty_events_render_nothing(self):
        assert smart_money.format_events([]) == ""

    def test_renders_as_disclosed_facts(self, event):
        text = smart_money.format_events([event])
        assert "filed 13F-HR on 2026-08-12" in text
        assert "Largest disclosed positions" in text
        assert "newly disclosed" in text
        assert "no longer listed" in text
        assert event["url"] in text

    def test_verbs_state_which_measure_changed(self, event):
        """A share-count change is a position change; a value-only change may
        be nothing but the price moving — the wording must keep them apart."""
        event["added"] = [
            {"issuer": "MORE CO", "delta": 10, "is_new": False, "basis": "shares"},
            {"issuer": "VAGUE CO", "delta": 100, "is_new": False, "basis": "value"},
        ]
        event["exited"] = [
            {"issuer": "LESS CO", "delta": 10, "is_gone": False, "basis": "shares"},
        ]
        text = smart_money.format_events([event])
        assert "MORE CO: reported a higher share count" in text
        assert "VAGUE CO: reported a larger value (share count unavailable)" in text
        assert "LESS CO: reported a lower share count" in text

    def test_never_uses_imperative_or_advice_language(self, event):
        text = smart_money.format_events([event]).lower()
        for banned in (
            "buy", "sell", "hold ", "add to", "trim", "follow", "copy",
            "should", "recommend", "opportunity", "bullish", "bearish",
            "target", "consider",
        ):
            assert banned not in text, f"advice-flavoured word in output: {banned!r}"

    def test_unparseable_filing_says_so_without_inventing(self, event):
        event["top"] = []
        event["added"] = []
        event["exited"] = []
        text = smart_money.format_events([event])
        assert "not parseable" in text
        assert event["url"] in text

"""CLI safety checks for enabled flags and configured input paths."""

from market_brief import __main__ as cli
from market_brief import config


def test_disabled_view_is_not_run(monkeypatch, capsys):
    monkeypatch.setattr(config, "PORTFOLIO_VIEW_ENABLED", False)
    result = cli.main(["--view"])
    assert result == 2
    assert "portfolio_view.enabled" in capsys.readouterr().err


def test_missing_configured_csv_is_an_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "PORTFOLIO_VIEW_ENABLED", True)
    monkeypatch.setattr(config, "PORTFOLIO_VIEW_POSITIONS", [])
    monkeypatch.setattr(config, "PORTFOLIO_VIEW_CANDIDATES", [])
    monkeypatch.setattr(config, "PORTFOLIO_VIEW_IBKR_CSV", tmp_path / "missing.csv")
    result = cli.main(["--view"])
    assert result == 2
    error = capsys.readouterr().err
    assert "does not exist" in error
    assert "missing.csv" in error

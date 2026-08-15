"""CLI for the market brief and the separate IBKR Daily View.

One run, one or two reports, printed to the console. There is no daemon here on
purpose: scheduling belongs to cron, systemd timers, or the deployment layer.
"""

import argparse
import asyncio
import logging
import sys

from . import brief, config


def _configure_logging(verbose: bool):
    # Log messages carry em-dashes and quoted feed titles, and a Windows
    # console defaults to cp1252 — without this, a log line about a failure
    # becomes its own UnicodeEncodeError. stdout is handled in ConsoleSender.
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # already wrapped or not reconfigurable (captured/piped stderr)

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m market_brief",
        description="U.S.-first global market monitor plus an optional, "
                    "local-first IBKR portfolio view.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true",
        help="Run the monitoring brief once (default).",
    )
    mode.add_argument(
        "--view", action="store_true",
        help="Run the comprehensive IBKR Daily View only.",
    )
    mode.add_argument(
        "--all", action="store_true",
        help="Run the market brief and the IBKR Daily View.",
    )
    parser.add_argument(
        "--no-compose", action="store_true",
        help="Skip the narrative composer and print the static digest.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every feed, quote, and fallback to stderr.",
    )
    return parser.parse_args(argv)


async def _run(args) -> int:
    run_brief = args.once or args.all or not (args.view or args.all)
    if run_brief and config.MARKET_BRIEF_WATCHLIST:
        runner = None
        if not args.no_compose:
            from . import claude
            runner = claude.get_runner()
        text = await brief.run_scan_and_notify(claude_runner=runner)
        if not text:
            print("Nothing to report in the market brief today.")

    if args.view or args.all:
        from . import portfolio_view
        await portfolio_view.run_and_notify()
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    run_brief = args.once or args.all or not (args.view or args.all)
    run_view = args.view or args.all
    has_view_input = bool(
        config.PORTFOLIO_VIEW_POSITIONS
        or config.PORTFOLIO_VIEW_CANDIDATES
        or config.PORTFOLIO_VIEW_IBKR_CSV
    )
    if run_brief and not config.MARKET_BRIEF_WATCHLIST and not run_view:
        print(
            "Watchlist is empty — nothing to monitor.\n"
            "Copy config.example.yaml to config.yaml and add the instruments "
            "you want watched.",
            file=sys.stderr,
        )
        return 2
    if run_view and not has_view_input:
        print(
            "Portfolio View has no input — set portfolio_view.ibkr_csv or add "
            "portfolio_view.positions in config.yaml.",
            file=sys.stderr,
        )
        return 2

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

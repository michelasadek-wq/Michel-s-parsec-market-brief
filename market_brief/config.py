"""Configuration — a single YAML file, read once at import.

Everything the pipeline needs is a module-level constant here, so the rest of
the package never touches the filesystem to answer "what is on the watchlist".

Resolution order for the config file:
  1. ``$MARKET_BRIEF_CONFIG`` (absolute or relative path)
  2. ``./config.yaml`` next to the package root
  3. ``./config.example.yaml`` — so a fresh clone runs before it is configured

A malformed or missing section degrades to a safe default and logs; it never
raises at import time. An empty watchlist is the natural no-op: nothing to
price, nothing to match, an empty brief.
"""

import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

#: Repo root — one level up from this package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Where the seen-set, the 13F tracker state, and the brief archive live.
DATA_DIR: Path = Path(
    os.environ.get("MARKET_BRIEF_DATA_DIR") or (PROJECT_ROOT / "data")
).resolve()


def _resolve_config_path() -> Path | None:
    env = os.environ.get("MARKET_BRIEF_CONFIG")
    if env:
        path = Path(env).expanduser()
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        if path.exists():
            return path
        logger.warning("MARKET_BRIEF_CONFIG=%s does not exist", env)
    for name in ("config.yaml", "config.example.yaml"):
        candidate = PROJECT_ROOT / name
        if candidate.exists():
            if name.endswith(".example.yaml"):
                logger.warning(
                    "No config.yaml found — falling back to config.example.yaml. "
                    "Copy it to config.yaml and edit before relying on the output."
                )
            return candidate
    return None


CONFIG_PATH: Path | None = _resolve_config_path()


def _load_yaml(path: Path | None) -> dict:
    if not path:
        logger.warning("No config file found; running on built-in defaults")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception("Could not read %s; running on built-in defaults", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s does not contain a YAML mapping; ignoring it", path)
        return {}
    return data


_cfg = _load_yaml(CONFIG_PATH)


# ── helpers ─────────────────────────────────────────────────────────


def _parse_hhmm(value, default: str = "08:00") -> str:
    """Coerce a config time to a valid 'HH:MM' string; fall back + log on bad input.

    YAML parses an unquoted ``08:00`` as the int 480 (8*60 sexagesimal), which
    then crashes consumers expecting a string. This degrades a bad value to the
    field's default instead of crashing whatever schedules the run.
    """
    s = str(value).strip()
    if ":" not in s:
        logger.warning(f"Invalid HH:MM config value {value!r}; using {default!r}")
        return default
    return s


def _parse_schedule_times(raw, defaults: list[str]) -> list[str]:
    """Normalise ``schedule_times`` to a list of 'HH:MM' strings.

    Accepts a list or a single value (string or the sexagesimal-int YAML trap,
    per _parse_hhmm). Invalid entries degrade to nothing rather than crashing;
    an empty result falls back to the defaults so the schedule never silently
    becomes "never".
    """
    if raw is None:
        return list(defaults)
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out = []
    for value in raw:
        s = str(value).strip()
        if ":" in s:
            out.append(s)
        else:
            logger.warning(f"Invalid schedule_times entry {value!r}; skipping it")
    return out or list(defaults)


def _trigger_list(raw, label: str) -> list[str]:
    """Normalise a configured keyword list: strings, stripped, lower-cased.

    A single string is accepted as a one-item list — the yaml is written by
    hand and ``macro_keywords: CBE`` is the obvious mistake to survive.
    """
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        out = [t.strip().casefold() for t in raw if isinstance(t, str) and t.strip()]
    if raw and not out:
        # Silently returning [] would switch the feature off and look exactly
        # like not configuring it. Say so, or the next person edits the yaml,
        # sees no change, and goes looking in the wrong file.
        logger.warning("%s ignored — expected a list of strings, got %r", label, raw)
    return out


def _watchlist_entries(raw, label: str = "market_brief.watchlist") -> list[dict]:
    """Normalise the watchlist: a list of {symbol, name, ...} dicts.

    Two kinds of entry are valid:
      * priced — has a `symbol`, gets a quote and can be flagged as a mover;
      * news-only — has a `name` (and usually aliases) but no tradeable symbol
        we trust: a wrong symbol is worse than no symbol, it prints a confident
        number for a different instrument. `news_only: true` also forces this
        for an entry that HAS a symbol.
    An entry with neither symbol nor name means nothing at all and is dropped
    with a warning rather than crashing the scan later.

    Aliases are kept as written — matching is case-insensitive at compare
    time, so nothing here lower-cases them.
    """
    out: list[dict] = []
    if not isinstance(raw, (list, tuple)):
        if raw:
            logger.warning("%s ignored — expected a list of dicts, got %r", label, raw)
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning("%s: skipping non-dict entry %r", label, entry)
            continue
        symbol = str(entry.get("symbol", "")).strip()
        name = str(entry.get("name", "")).strip()
        if not symbol and not name:
            logger.warning("%s: skipping entry with no symbol and no name: %r", label, entry)
            continue
        aliases = entry.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        # No symbol → nothing to price, so it is news-only by construction.
        news_only = bool(entry.get("news_only", False)) or not symbol
        out.append({
            "symbol": symbol,
            "name": name or symbol,
            "aliases": [str(a).strip() for a in aliases if str(a).strip()],
            "market": str(entry.get("market", "")).strip(),
            "watch_only": bool(entry.get("watch_only", False)),
            "news_only": news_only,
        })
    return out


def _tracker_entries(raw, label: str = "market_brief.trackers") -> list[dict]:
    """Normalise the 13F tracker list: {name, cik} dicts with a padded CIK.

    EDGAR's submissions endpoint only answers to the zero-padded 10-digit CIK,
    and the yaml is hand-written — so the padding happens here, once, instead
    of being a trap for whoever adds the next manager.
    """
    out: list[dict] = []
    if not isinstance(raw, (list, tuple)):
        if raw:
            logger.warning("%s ignored — expected a list of dicts, got %r", label, raw)
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning("%s: skipping non-dict entry %r", label, entry)
            continue
        cik = re.sub(r"\D", "", str(entry.get("cik", "")))
        if not cik:
            logger.warning("%s: skipping tracker without a CIK: %r", label, entry)
            continue
        out.append({
            "name": str(entry.get("name", "")).strip() or f"CIK {cik}",
            "cik": cik.zfill(10),
        })
    return out


# ── time ────────────────────────────────────────────────────────────

#: All dates in this app — the seen-set TTL, the 13F staleness window, the
#: brief's own date — are resolved in this timezone (the reader's local time,
#: which is also what schedule_times is written in), not the server's. A UTC
#: host would otherwise roll the date at the wrong hour and dedupe against
#: "tomorrow".
_DEFAULT_TIMEZONE = "Asia/Dubai"
TIMEZONE: str = str(_cfg.get("timezone", _DEFAULT_TIMEZONE)).strip() or _DEFAULT_TIMEZONE


def now():
    """Timezone-aware ``datetime.now()`` in the configured market timezone.

    Degrades rather than raises, in two steps. A bad timezone *name* is a
    config error and falls back to the default. A missing IANA database is an
    environment problem — zoneinfo ships no bundled copy, so a Windows host or
    a slim container without the `tzdata` package has none — and falls back to
    system-local time with a loud log. Both are wrong-but-running, which is
    what a monitor should do; a crash here would take the whole brief down
    over a clock.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    for name in (TIMEZONE, _DEFAULT_TIMEZONE):
        try:
            return datetime.now(ZoneInfo(name))
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            logger.error(
                "Timezone %r unavailable (is the `tzdata` package installed?)", name
            )
    return datetime.now().astimezone()


def today():
    """``date.today()`` in the configured market timezone."""
    return now().date()


# ── market brief ────────────────────────────────────────────────────

# Default OFF with an EMPTY watchlist: this reports one person's holdings, so
# an unconfigured deployment must do nothing rather than guess. An empty
# watchlist is also the safe no-op if the section is malformed.
_market_cfg = _cfg.get("market_brief", {})
if not isinstance(_market_cfg, dict):
    logger.warning("market_brief section is not a mapping; ignoring it")
    _market_cfg = {}

MARKET_BRIEF_ENABLED: bool = bool(_market_cfg.get("enabled", False))

#: The times of day (in TIMEZONE) a brief is meant to run. Nothing in this
#: package schedules anything — cron or a systemd timer does — but the config
#: is the single place the cadence is written down.
_DEFAULT_SCHEDULE_TIMES = ["10:00", "18:00", "23:00"]
MARKET_BRIEF_SCHEDULE_TIMES: list[str] = _parse_schedule_times(
    _market_cfg.get("schedule_times",
                    _market_cfg.get("schedule_time")),  # older configs, singular
    _DEFAULT_SCHEDULE_TIMES,
)
#: Backward-compatible singular view: the first scheduled time.
MARKET_BRIEF_SCHEDULE_TIME: str = MARKET_BRIEF_SCHEDULE_TIMES[0]

MARKET_BRIEF_WATCHLIST: list[dict] = _watchlist_entries(_market_cfg.get("watchlist", []))
MARKET_BRIEF_MACRO_KEYWORDS: list[str] = _trigger_list(
    _market_cfg.get("macro_keywords", []), "market_brief.macro_keywords"
)
MARKET_BRIEF_MAX_ITEMS: int = int(_market_cfg.get("max_items", 25))

#: Market-wide radar (day gainers + trending tickers). Data only — the compose
#: prompt forbids presenting any radar name as a pick. Defaults on; costs a
#: couple of Yahoo requests per run.
MARKET_BRIEF_RADAR_ENABLED: bool = bool(_market_cfg.get("radar_enabled", True))
MARKET_BRIEF_RADAR_COUNT: int = int(_market_cfg.get("radar_count", 5))

# Institutional 13F trackers (SEC EDGAR). Quarterly cadence, so this is nearly
# always a no-op. SEC blocks requests without a contact address in the
# User-Agent, so one is always sent.
MARKET_BRIEF_TRACKERS: list[dict] = _tracker_entries(_market_cfg.get("trackers", []))
MARKET_BRIEF_SEC_CONTACT: str = str(
    _market_cfg.get("sec_contact", "market-brief@example.com")
).strip() or "market-brief@example.com"

#: Model used by the optional narrative composer. Only read when the `claude`
#: CLI is on PATH; without it the static digest is the whole output.
COMPOSE_MODEL: str = str(
    _market_cfg.get("compose_model", "claude-sonnet-4-6")
).strip() or "claude-sonnet-4-6"


# ── delivery ────────────────────────────────────────────────────────

_delivery_cfg = _cfg.get("delivery", {})
if not isinstance(_delivery_cfg, dict):
    logger.warning("delivery section is not a mapping; ignoring it")
    _delivery_cfg = {}

#: "console" (default) or "whatsapp". See senders.py for the contract.
DELIVERY_BACKEND: str = str(_delivery_cfg.get("backend", "console")).strip() or "console"
DELIVERY_OPTIONS: dict = {
    k: v for k, v in _delivery_cfg.items() if k != "backend"
}

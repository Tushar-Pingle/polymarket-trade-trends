"""Command-line entry point.

One CLI drives every workflow:

* ``discover`` — list candidate wallets for a niche (inspection only).
* ``rank``     — (re)build the in-house leaderboard JSON. Run weekly.
* ``watch``    — run the live consensus watcher (``--once`` or ``--loop``).
* ``backtest`` — replay history to prove which alerts would have fired.
* ``report``   — post the weekly cross-niche comparison to Discord.

Every command shares config loading, logging setup, and client construction.
The destructive-by-omission flags are intentional: ``rank`` and ``report``
support ``--dry-run`` and ``watch``/``backtest`` default to safe behaviour
(single cycle / dry-run rendering) so nothing surprising happens by accident.

Examples
--------
    python -m pmwatch.cli rank --all
    python -m pmwatch.cli watch --niche politics --loop
    python -m pmwatch.cli watch --all --loop
    python -m pmwatch.cli backtest --niche crypto --since 14d --dry-run
    python -m pmwatch.cli report
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import threading
from datetime import UTC, datetime, timedelta

from . import backtest as backtest_mod
from .client import PolymarketClient, RateLimiter
from .config import Config, ConfigError, load_config
from .discord import DiscordNotifier
from .discover import discover_candidates
from .leaderboard import rank_all, rank_niche
from .ledger import Ledger
from .logging_conf import configure_logging, get_logger
from .store import Store
from .watch import Watcher

log = get_logger("pmwatch.cli")


# --------------------------------------------------------------------------- #
# Shared context object — built once per invocation from config.
# --------------------------------------------------------------------------- #
class App:
    """Bundles the configured collaborators so commands stay terse."""

    def __init__(self, cfg: Config, *, dry_run: bool) -> None:
        self.cfg = cfg
        # One shared rate limiter so concurrent niche workers respect one budget.
        limiter = RateLimiter(cfg.api.rate_limit_per_sec)
        self.client = PolymarketClient(cfg.api, rate_limiter=limiter)
        self.ledger = Ledger(cfg.storage.data_dir)
        self.notifier = DiscordNotifier(cfg, dry_run=dry_run)

    def store(self) -> Store:
        """Open the runtime store (watch only). Separate so read-only commands
        don't create a SQLite file unnecessarily."""
        return Store(self.cfg.storage.sqlite_path)


def _niche_keys(cfg: Config, args: argparse.Namespace) -> list[str]:
    """Resolve --niche / --all into a list of niche keys, validating names."""
    if getattr(args, "all", False):
        return list(cfg.niches)
    if not args.niche:
        raise SystemExit("Specify --niche <key> or --all")
    cfg.niche(args.niche)  # validates, raises ConfigError for typos
    return [args.niche]


def _parse_since(value: str) -> datetime:
    """Parse a lookback as either ISO date/time or a relative '<N>d' / '<N>h'."""
    rel = re.fullmatch(r"(\d+)([dh])", value.strip())
    if rel:
        amount, unit = int(rel.group(1)), rel.group(2)
        delta = timedelta(days=amount) if unit == "d" else timedelta(hours=amount)
        return datetime.now(UTC) - delta
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Command implementations.
# --------------------------------------------------------------------------- #
def cmd_discover(app: App, args: argparse.Namespace) -> int:
    for key in _niche_keys(app.cfg, args):
        candidates = discover_candidates(app.client, app.cfg.niche(key), app.cfg.discovery)
        log.info("Discovered %d candidates for %s", len(candidates), key)
        for addr in candidates:
            print(addr)  # noqa: T201 — intentional machine-readable stdout for piping
    return 0


def cmd_rank(app: App, args: argparse.Namespace) -> int:
    persist = not args.dry_run
    if getattr(args, "all", False):
        rank_all(app.client, app.ledger, app.cfg, persist=persist)
    else:
        for key in _niche_keys(app.cfg, args):
            rank_niche(app.client, app.cfg.niche(key), app.ledger, app.cfg, persist=persist)
    if args.dry_run:
        log.info("Dry run — leaderboard JSON was NOT written")
    return 0


def cmd_watch(app: App, args: argparse.Namespace) -> int:
    keys = _niche_keys(app.cfg, args)
    store = app.store()
    watcher = Watcher(app.cfg, app.client, store, app.ledger, app.notifier)

    if not args.loop:
        # Single cycle — handy for testing and for cron-style invocation.
        total = sum(watcher.poll_niche_once(k) for k in keys)
        log.info("Single cycle complete; %d alert(s) emitted", total)
        store.close()
        return 0

    # Long-running mode with graceful shutdown on SIGINT/SIGTERM.
    stop = threading.Event()
    _install_signal_handlers(stop)
    try:
        if len(keys) == 1:
            watcher.run_loop(keys[0], stop)
        else:
            watcher.run_all(keys, stop)
    finally:
        store.close()
    return 0


def cmd_backtest(app: App, args: argparse.Namespace) -> int:
    since = _parse_since(args.since)
    until = _parse_since(args.until) if args.until else None
    keys = _niche_keys(app.cfg, args)
    for key in keys:
        alerts = backtest_mod.backtest_niche(
            app.client,
            app.cfg,
            app.ledger,
            key,
            since=since,
            until=until,
            condition_id=args.market,
        )
        print(f"\n=== Backtest: {key} (since {since.date()}) ===")  # noqa: T201
        print(backtest_mod.summarize_alerts(alerts))  # noqa: T201
        if args.send:
            # Re-deliver the would-be alerts to Discord for a visual check.
            for alert in alerts:
                app.notifier.send_alert(alert)
    return 0


def cmd_report(app: App, args: argparse.Namespace) -> int:
    boards = {key: app.ledger.load(key) for key in app.cfg.niches}
    app.notifier.send_report(boards)
    return 0


# --------------------------------------------------------------------------- #
# Argparse wiring.
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pmwatch", description="Polymarket leader consensus watcher (read-only).")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Render to logs instead of posting / persisting")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_niche_selectors(p: argparse.ArgumentParser) -> None:
        p.add_argument("--niche", help="Niche key (see config.yaml)")
        p.add_argument("--all", action="store_true", help="Apply to every configured niche")

    p_discover = sub.add_parser("discover", help="List candidate wallets for a niche")
    add_niche_selectors(p_discover)
    p_discover.set_defaults(func=cmd_discover)

    p_rank = sub.add_parser("rank", help="Rebuild the in-house leaderboard (run weekly)")
    add_niche_selectors(p_rank)
    p_rank.set_defaults(func=cmd_rank)

    p_watch = sub.add_parser("watch", help="Run the live consensus watcher")
    add_niche_selectors(p_watch)
    p_watch.add_argument("--loop", action="store_true", help="Run continuously (default: one cycle)")
    p_watch.set_defaults(func=cmd_watch)

    p_back = sub.add_parser("backtest", help="Replay history to show alerts that would have fired")
    add_niche_selectors(p_back)
    p_back.add_argument("--since", required=True, help="Window start: ISO date or relative like 14d / 48h")
    p_back.add_argument("--until", help="Window end (ISO). Defaults to now.")
    p_back.add_argument("--market", help="Restrict to a single market condition id")
    p_back.add_argument("--send", action="store_true", help="Also post the would-be alerts to Discord")
    p_back.set_defaults(func=cmd_backtest)

    p_report = sub.add_parser("report", help="Post the weekly cross-niche comparison")
    p_report.set_defaults(func=cmd_report)

    return parser


def _install_signal_handlers(stop: threading.Event) -> None:
    """Translate SIGINT/SIGTERM into a clean stop of the watcher loops."""

    def _handler(signum: int, _frame: object) -> None:
        log.info("Received signal %s — shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)  # noqa: T201
        return 2

    configure_logging(cfg.logging.level, as_json=cfg.logging.json)
    app = App(cfg, dry_run=args.dry_run)

    try:
        return int(args.func(app, args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # top-level guard: report and exit non-zero
        log.exception("Command failed")
        try:
            app.notifier.send_error(f"`{args.command}` failed: {exc}")
        except Exception:  # noqa: BLE001 — never let error reporting mask the original
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

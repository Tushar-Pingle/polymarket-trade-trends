"""The live watcher — per-niche continuous polling loop.

Each niche is watched independently so that one slow or busy niche never delays
another (you asked for four parallel processes). In production each niche runs
as its own ``pmwatch@<niche>`` systemd service calling ``cli watch --niche X
--loop``; for local use ``watch --all --loop`` runs them as threads in one
process. Either way the design guarantees **no missed bets between cycles**:

* trades are deduplicated in SQLite by a stable per-fill key, so polling
  overlap or a restart never double-alerts and never skips a fill;
* every new fill is fed through the rolling window, so a consensus that forms
  across several short cycles is still detected; and
* on a wallet's *first* poll we seed its history silently (cold start) to avoid
  alerting on bets that happened before we started watching.

The loop is read-only and resilient: a failure polling one wallet is logged
(and surfaced to Discord as a health alert) without killing the loop.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

from .client import PolymarketClient
from .config import Config
from .consensus import AlertEngine
from .discord import DiscordNotifier
from .ledger import Leaderboard, Ledger
from .logging_conf import get_logger
from .models import Market
from .store import Store

log = get_logger(__name__)


class PriceResolver:
    """Resolves an outcome's current market price, with a short TTL cache.

    Used to show "edge left" on consensus alerts. Cached because many trades in
    one cycle reference the same market, and the price doesn't move meaningfully
    within a few seconds. Returns ``None`` on any failure so alerts degrade
    gracefully rather than breaking.
    """

    def __init__(self, client: PolymarketClient, *, ttl_seconds: float = 30.0) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Market | None]] = {}

    def __call__(self, condition_id: str, outcome_index: int) -> float | None:
        now = time.monotonic()
        cached = self._cache.get(condition_id)
        if cached is None or (now - cached[0]) > self._ttl:
            try:
                market = self._client.get_market(condition_id)
            except Exception as exc:  # price is best-effort; never break an alert
                log.debug("Price lookup failed for %s: %s", condition_id, exc)
                market = None
            self._cache[condition_id] = (now, market)
            cached = self._cache[condition_id]
        market = cached[1]
        return market.price_of(outcome_index) if market else None


class Watcher:
    """Owns the polling loop for one or more niches."""

    def __init__(
        self,
        cfg: Config,
        client: PolymarketClient,
        store: Store,
        ledger: Ledger,
        notifier: DiscordNotifier,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._store = store
        self._ledger = ledger
        self._notifier = notifier
        self._engine = AlertEngine(cfg, store)
        self._prices = PriceResolver(client)

    # ------------------------------------------------------------------ #
    # One polling cycle for a single niche.
    # ------------------------------------------------------------------ #
    def poll_niche_once(self, niche_key: str) -> int:
        """Poll every watched wallet for a niche once. Returns alerts emitted."""
        self._cfg.niche(niche_key)  # validate the niche key early
        board: Leaderboard = self._ledger.load(niche_key)
        wallets = board.watched_wallets()
        if not wallets:
            log.warning(
                "No wallets on the board yet — run `rank` first",
                extra={"extra_fields": {"niche": niche_key}},
            )
            return 0

        emitted = 0
        for wallet in wallets:
            try:
                emitted += self._poll_wallet(niche_key, wallet, board)
            except Exception as exc:
                # Isolate per-wallet failures so the rest of the niche keeps working.
                log.warning(
                    "Wallet poll failed",
                    extra={"extra_fields": {"niche": niche_key, "wallet": wallet, "error": str(exc)}},
                )
        return emitted

    def _poll_wallet(self, niche_key: str, wallet: str, board: Leaderboard) -> int:
        """Fetch a wallet's recent trades and emit alerts for genuinely new ones."""
        # Cold start: if we've never seen this wallet, seed silently (no alerts).
        cold_start = not self._store.has_seen_wallet(niche_key, wallet)

        trades = self._client.get_trades(wallet, limit=self._cfg.poll.trades_limit)
        # Process oldest-first so the rolling window / consensus build in order.
        trades.sort(key=lambda t: t.timestamp)

        emitted = 0
        for trade in trades:
            # Always mark seen (returns False if already known) to advance dedup.
            is_new = self._store.mark_seen(niche_key, trade)
            if not is_new:
                continue

            # Dust filter — record as seen above, but don't act on tiny trades.
            if trade.notional_usd < self._cfg.alerts.min_trade_size_usd:
                continue

            if cold_start:
                # Seed the window so consensus has context, but stay silent.
                self._store.record_window_trade(niche_key, trade)
                continue

            for alert in self._engine.evaluate(niche_key, board, trade, self._prices):
                self._notifier.send_alert(alert)
                emitted += 1
        return emitted

    # ------------------------------------------------------------------ #
    # Long-running loops.
    # ------------------------------------------------------------------ #
    def run_loop(self, niche_key: str, stop: threading.Event) -> None:
        """Continuously poll one niche until ``stop`` is set."""
        log.info("Watcher started", extra={"extra_fields": {"niche": niche_key}})
        last_prune = time.monotonic()
        while not stop.is_set():
            cycle_start = time.monotonic()
            try:
                emitted = self.poll_niche_once(niche_key)
                if emitted:
                    log.info("Cycle emitted alerts", extra={"extra_fields": {"niche": niche_key, "alerts": emitted}})
            except Exception as exc:
                log.exception("Polling cycle failed")
                self._notifier.send_error(f"[{niche_key}] polling cycle failed: {exc}")

            # Periodically prune state older than the consensus window (+1 day slack).
            if time.monotonic() - last_prune > 3600:
                cutoff = int(
                    (datetime.now(UTC) - timedelta(hours=self._cfg.alerts.consensus.window_hours + 24)).timestamp()
                )
                self._store.prune(older_than_ts=cutoff)
                last_prune = time.monotonic()

            # Sleep the remainder of the interval, but wake promptly on shutdown.
            elapsed = time.monotonic() - cycle_start
            stop.wait(max(0.0, self._cfg.poll.interval_seconds - elapsed))
        log.info("Watcher stopped", extra={"extra_fields": {"niche": niche_key}})

    def run_all(self, niche_keys: list[str], stop: threading.Event) -> None:
        """Run several niches concurrently as threads in this process."""
        threads = [
            threading.Thread(target=self.run_loop, args=(key, stop), name=f"watch-{key}", daemon=True)
            for key in niche_keys
        ]
        for t in threads:
            t.start()
        # Block until shutdown is requested, then let threads observe the event.
        try:
            while not stop.is_set():
                stop.wait(1.0)
        finally:
            for t in threads:
                t.join(timeout=self._cfg.poll.interval_seconds + 5)

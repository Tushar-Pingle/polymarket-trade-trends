"""Historical replay — proves which alerts the system *would* have fired.

This is the verification centrepiece you asked for: take a real, recent (often
already-resolved) window of activity and run it through the **exact same**
:class:`~pmwatch.consensus.AlertEngine` the live watcher uses, so you can
confirm end-to-end that:

* a single-leader alert would have fired when each tracked wallet bet,
* a consensus alert would have fired when enough of them converged,
* the rank / trust / edge comparison on those alerts was computed correctly.

To stay perfectly faithful to production behaviour while not touching live
runtime state, the replay runs against a throwaway **in-memory** SQLite store.
Alerts are returned (and, by default, rendered in dry-run mode rather than
posted), so a backtest never spams your Discord.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .client import PolymarketClient
from .config import Config
from .consensus import Alert, AlertEngine
from .ledger import Ledger
from .logging_conf import get_logger
from .models import Trade
from .store import Store
from .watch import PriceResolver

log = get_logger(__name__)


def backtest_niche(
    client: PolymarketClient,
    cfg: Config,
    ledger: Ledger,
    niche_key: str,
    *,
    since: datetime,
    until: datetime | None = None,
    condition_id: str | None = None,
    include_bench: bool = True,
) -> list[Alert]:
    """Replay a niche's tracked wallets over a time window.

    Parameters
    ----------
    since / until:
        Replay window (UTC). ``until`` defaults to now.
    condition_id:
        Optionally restrict the replay to a single market — ideal for checking a
        specific recently-closed market end to end.
    include_bench:
        Include benched wallets too (recommended: a closed market's participants
        may since have dropped off the active board).
    """
    until = until or datetime.now(UTC)
    cfg.niche(niche_key)  # validate the niche key early
    board = ledger.load(niche_key)

    wallets = list(board.watched_wallets())
    if include_bench:
        wallets += [s.wallet for s in board.bench]
    wallets = list(dict.fromkeys(wallets))

    if not wallets:
        log.warning("No wallets to backtest — run `rank` first for %s", niche_key)
        return []

    start_ts, end_ts = int(since.timestamp()), int(until.timestamp())

    # Pull each wallet's historical TRADE activity in the window.
    trades: list[Trade] = []
    for wallet in wallets:
        try:
            activity = client.get_all_activity(wallet, start=start_ts, end=end_ts, activity_type="TRADE")
        except Exception as exc:
            log.warning("Activity fetch failed for %s: %s", wallet, exc)
            continue
        for trade in activity:
            if condition_id and trade.condition_id != condition_id:
                continue
            trades.append(trade)

    # Replay strictly in chronological order so windows/consensus form as they
    # would have live. Ties broken by tx hash for determinism.
    trades.sort(key=lambda t: (t.timestamp, t.tx_hash))

    log.info(
        "Replaying trades",
        extra={"extra_fields": {"niche": niche_key, "wallets": len(wallets), "trades": len(trades)}},
    )

    # Fresh in-memory store => no interference with live runtime state.
    store = Store(":memory:")
    engine = AlertEngine(cfg, store)
    prices = PriceResolver(client)

    alerts: list[Alert] = []
    try:
        for trade in trades:
            if trade.notional_usd < cfg.alerts.min_trade_size_usd:
                store.mark_seen(niche_key, trade)
                continue
            if not store.mark_seen(niche_key, trade):
                continue
            # Use each trade's own timestamp as "now" so the rolling window is
            # evaluated exactly as it would have been at that moment.
            alerts.extend(engine.evaluate(niche_key, board, trade, prices, now=trade.timestamp))
    finally:
        store.close()

    return alerts


def summarize_alerts(alerts: list[Alert]) -> str:
    """Render a compact text summary of replayed alerts for the console."""
    if not alerts:
        return "No alerts would have fired in this window."
    lines = [f"{len(alerts)} alert(s) would have fired:\n"]
    for a in alerts:
        when = max((w.timestamp for w in a.wallets), default=None)
        stamp = when.strftime("%Y-%m-%d %H:%M") if when else "?"
        who = ", ".join(f"{w.alias}(#{w.rank})" if w.rank else w.alias for w in a.wallets[:5])
        more = f" +{len(a.wallets) - 5}" if len(a.wallets) > 5 else ""
        lines.append(
            f"  [{stamp}] {a.kind.upper():9} {a.side.value} {a.outcome} — {a.title[:60]}\n"
            f"            wallets: {who}{more}\n"
            f"            {a.note}"
        )
    return "\n".join(lines)

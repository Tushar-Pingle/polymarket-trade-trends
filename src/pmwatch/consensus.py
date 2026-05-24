"""The alert engine: turns observed trades into tiered, color-coded alerts.

Two tiers, exactly as specified:

* **Single-leader alert** — fired on *every* new bet by a tracked leader. Crucially
  it carries the wallet's **current leaderboard rank**, so a glance tells you
  whether to act: a bet from the #1 wallet (warning colour) is far more
  actionable than one from #10 (muted colour), where you'd rather wait for
  confirmation.
* **Consensus alert** — fired when ``min_wallets`` distinct leaders land on the
  **same market + outcome + side** inside the rolling window. Stronger colour,
  and it escalates (re-fires) as more leaders pile in. With ``alert_on_sell`` it
  also fires on coordinated SELLs — the "leaders are dumping, ride the exit"
  signal.

The engine is deliberately free of network and Discord concerns: it consumes
trades + a leaderboard snapshot + a price resolver callable, and returns
:class:`Alert` objects. :mod:`pmwatch.watch` wires it to live data and
:mod:`pmwatch.discord` renders the alerts. This separation is what lets
:mod:`pmwatch.backtest` replay history through the *exact same* logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Config
from .ledger import Leaderboard
from .logging_conf import get_logger
from .models import Side, Trade, WalletScore
from .store import Store, event_key

log = get_logger(__name__)

# A consensus this wide is treated as maximum conviction (reddest colour).
_MAX_CONVICTION_WALLETS = 5

# Resolves the current market price of an outcome for the "edge left" display.
# Returns None when unavailable (e.g. offline backtest); alerts degrade gracefully.
PriceResolver = Callable[[str, int], "float | None"]


@dataclass(frozen=True)
class WalletRef:
    """One wallet's involvement in an alert, with the context you judge it by."""

    address: str
    alias: str
    rank: int | None  # leaderboard rank within the niche (None if benched/unknown)
    trust: float | None  # 0..1; low => history of pre-resolution dumping
    entry_price: float
    size: float
    timestamp: datetime


@dataclass(frozen=True)
class Alert:
    """A rendered-ready alert. :mod:`pmwatch.discord` knows how to post it."""

    kind: str  # "single" | "consensus" | "exit"
    niche: str
    niche_display: str
    title: str
    market_url: str
    outcome: str
    side: Side
    color: int
    current_price: float | None
    wallets: list[WalletRef]
    note: str = ""
    flags: list[str] = field(default_factory=list)

    @property
    def total_size_usd(self) -> float:
        return sum(w.entry_price * w.size for w in self.wallets)


class AlertEngine:
    """Stateful (via :class:`Store`) detector that emits single + consensus alerts."""

    def __init__(self, cfg: Config, store: Store) -> None:
        self._cfg = cfg
        self._store = store

    # ------------------------------------------------------------------ #
    # Public entry point: feed one freshly-seen trade, get back 0..2 alerts.
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        niche_key: str,
        board: Leaderboard,
        trade: Trade,
        price_resolver: PriceResolver,
        *,
        now: datetime | None = None,
    ) -> list[Alert]:
        """Process one new trade and return any alerts it triggers.

        Caller contract: ``trade`` must already be confirmed new (deduped) and
        above the size floor. This method records the trade into the rolling
        window and then checks for consensus.
        """
        now = now or datetime.now(UTC)
        niche_display = self._cfg.niches[niche_key].display_name if niche_key in self._cfg.niches else niche_key
        lookup = _score_lookup(board)
        alerts: list[Alert] = []

        # --- Tier 1: single-leader alert (every tracked bet) ----------------
        if self._cfg.alerts.single.enabled:
            alerts.append(self._single_alert(niche_key, niche_display, board, lookup, trade))

        # Persist into the rolling window so consensus can see it.
        self._store.record_window_trade(niche_key, trade)

        # --- Tier 2: consensus / exit alert ---------------------------------
        consensus = self._consensus_alert(niche_key, niche_display, board, lookup, trade, price_resolver, now)
        if consensus is not None:
            alerts.append(consensus)

        return alerts

    # ------------------------------------------------------------------ #
    # Tier 1
    # ------------------------------------------------------------------ #
    def _single_alert(
        self,
        niche_key: str,
        niche_display: str,
        board: Leaderboard,
        lookup: dict[str, WalletScore],
        trade: Trade,
    ) -> Alert:
        rank = board.rank_of(trade.wallet)
        score = lookup.get(trade.wallet)
        trust = score.trust if score else None

        # Colour by how high the actor ranks: top-N => act now, else watch/wait.
        high = rank is not None and rank <= self._cfg.alerts.single.high_rank_threshold
        color = self._cfg.alerts.colors.warning if high else self._cfg.alerts.colors.muted

        note = self._single_note(rank)
        flags = _trust_flags(trust)

        ref = WalletRef(
            address=trade.wallet,
            alias=self._cfg.alias_for(trade.wallet),
            rank=rank,
            trust=trust,
            entry_price=trade.price,
            size=trade.size,
            timestamp=trade.timestamp,
        )
        return Alert(
            kind="single",
            niche=niche_key,
            niche_display=niche_display,
            title=trade.title or "(market)",
            market_url=trade.market_url,
            outcome=trade.outcome,
            side=trade.side,
            color=color,
            current_price=trade.price,  # single alerts use the fill price as reference
            wallets=[ref],
            note=note,
            flags=flags,
        )

    def _single_note(self, rank: int | None) -> str:
        """Human guidance keyed off the actor's rank."""
        board_size = self._cfg.leaderboard.board_size
        if rank is None:
            return "Benched/unranked wallet — informational only."
        if rank <= self._cfg.alerts.single.high_rank_threshold:
            return f"Top leader (#{rank} of {board_size}) — high chance others follow."
        return f"Lower-ranked leader (#{rank} of {board_size}) — consider waiting for confirmation."

    # ------------------------------------------------------------------ #
    # Tier 2
    # ------------------------------------------------------------------ #
    def _consensus_alert(
        self,
        niche_key: str,
        niche_display: str,
        board: Leaderboard,
        lookup: dict[str, WalletScore],
        trade: Trade,
        price_resolver: PriceResolver,
        now: datetime,
    ) -> Alert | None:
        ac = self._cfg.alerts.consensus

        # SELL consensus is the "leaders are dumping" exit signal — only if enabled.
        if trade.side is Side.SELL and not ac.alert_on_sell:
            return None

        window_start = int((now - timedelta(hours=ac.window_hours)).timestamp())
        rows = self._store.wallets_on(
            niche_key, trade.condition_id, trade.outcome_index, trade.side, since_ts=window_start
        )
        count = len(rows)
        if count < ac.min_wallets:
            return None

        # Escalation/dedup: only (re)fire when the distinct-wallet count has grown
        # by at least escalation_step since the last alert for this opportunity.
        key = event_key(niche_key, trade.condition_id, trade.outcome_index, trade.side)
        last = self._store.get_fired_count(key)
        if last and count < last + ac.escalation_step:
            return None
        self._store.upsert_fired(key, count)

        refs: list[WalletRef] = []
        for row in rows:
            addr = row["wallet"]
            sc = lookup.get(addr)
            refs.append(
                WalletRef(
                    address=addr,
                    alias=self._cfg.alias_for(addr),
                    rank=board.rank_of(addr),
                    trust=sc.trust if sc else None,
                    entry_price=float(row["price"]),
                    size=float(row["size"]),
                    timestamp=datetime.fromtimestamp(int(row["ts"]), tz=UTC),
                )
            )

        is_exit = trade.side is Side.SELL
        # Colour: red for exits or wide consensus, orange otherwise.
        if is_exit or count >= _MAX_CONVICTION_WALLETS:
            color = self._cfg.alerts.colors.max
        else:
            color = self._cfg.alerts.colors.strong

        current_price = price_resolver(trade.condition_id, trade.outcome_index)
        note = self._consensus_note(count, is_exit)
        # Aggregate trust flag: warn if several involved wallets are chronic dumpers.
        low_trust = [r for r in refs if r.trust is not None and r.trust < 0.5]
        flags = []
        if low_trust:
            flags.append(f"{len(low_trust)} of {count} involved wallets have a dump history — weight accordingly.")

        return Alert(
            kind="exit" if is_exit else "consensus",
            niche=niche_key,
            niche_display=niche_display,
            title=trade.title or "(market)",
            market_url=trade.market_url,
            outcome=trade.outcome,
            side=trade.side,
            color=color,
            current_price=current_price,
            wallets=refs,
            note=note,
            flags=flags,
        )

    def _consensus_note(self, count: int, is_exit: bool) -> str:
        if is_exit:
            return f"{count} leaders are SELLING this — possible coordinated exit before resolution."
        return f"{count} leaders converged on this bet — strongest signal to consider mirroring."


def _score_lookup(board: Leaderboard) -> dict[str, WalletScore]:
    """Address -> WalletScore over both board and bench, for trust/rank lookups."""
    lookup: dict[str, WalletScore] = {}
    for score in board.board + board.bench:
        lookup[score.wallet] = score
    return lookup


def _trust_flags(trust: float | None) -> list[str]:
    """Render a warning flag for low-trust (chronic dumper) wallets."""
    if trust is not None and trust < 0.5:
        return [f"⚠️ Low trust ({trust:.2f}) — history of pre-resolution dumps."]
    return []

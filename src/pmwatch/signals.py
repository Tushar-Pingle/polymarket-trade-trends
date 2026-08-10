"""Behavioural signal detection — specifically the "pre-resolution dump".

The exit-liquidity risk you flagged: a well-known wallet buys an outcome, lets
copy-traders pile in and push the price up, then **sells the bulk of the
position before the market resolves** — profiting from the move regardless of
the eventual outcome, with the copycats left holding. We detect that pattern
from a wallet's trade history and use it two ways:

1. **Protection** — a wallet that does this repeatedly gets a lower *trust*
   score, which both tags its live alerts and penalises it on the leaderboard.
2. **Exploitation** — knowing the pattern lets the watcher surface a SELL-side
   "leaders are dumping" consensus alert so you can ride and exit too (handled
   in :mod:`pmwatch.consensus`, using the trust context computed here).

Detection heuristic
-------------------
For each ``(market, outcome)`` a wallet traded, we net its BUY and SELL share
volume. If it SOLD at least ``dump_fraction`` of what it BOUGHT *and* the sells
happened before the market resolved, that round-trip is counted as a dump.
Holding to resolution instead shows up as a redeem/expiry, not a sell, so it is
correctly *not* counted. This is a deliberately conservative proxy: it can miss
exotic structures, but it won't falsely punish a buy-and-hold winner.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Side, Trade


@dataclass(frozen=True)
class DumpEvent:
    """One detected pre-resolution dump on a single market+outcome."""

    condition_id: str
    outcome_index: int
    bought_shares: float
    sold_shares: float
    avg_buy_price: float
    avg_sell_price: float
    first_buy_ts: float
    last_sell_ts: float

    @property
    def sold_fraction(self) -> float:
        """Fraction of bought shares that were sold back out pre-resolution."""
        return self.sold_shares / self.bought_shares if self.bought_shares > 0 else 0.0

    @property
    def realised_edge(self) -> float:
        """Per-share profit captured by the round-trip (sell price - buy price)."""
        return self.avg_sell_price - self.avg_buy_price


def detect_dumps(
    trades: list[Trade],
    *,
    dump_fraction: float = 0.5,
    neg_risk_condition_ids: Iterable[str] = (),
    conversions: Iterable[Trade] = (),
    conversion_window_seconds: float = 3600.0,
) -> list[DumpEvent]:
    """Find pre-resolution dumps in a wallet's trade history.

    Parameters
    ----------
    trades:
        All trade-type activity for one wallet (any order).
    dump_fraction:
        Minimum sold/bought ratio for a round-trip to count as a dump.
    neg_risk_condition_ids:
        Condition ids of markets that belong to **neg-risk** events. In a
        neg-risk event, selling NO on one outcome is economically a conversion
        into YES on the others (via the Neg Risk Adapter), so a SELL there is not
        necessarily a real exit. Only markets in this set are eligible for the
        conversion-based exclusion below.
    conversions:
        The wallet's ``CONVERSION``-type activity (parsed as :class:`Trade`).
        Used to tell a genuine neg-risk conversion apart from a real dump.
    conversion_window_seconds:
        How close (in time) a CONVERSION must be to a market's last SELL for the
        round-trip on that market to be treated as a conversion rather than a dump.

    Returns
    -------
    A list of :class:`DumpEvent`, one per offending market+outcome.
    """
    neg_risk = {cid for cid in neg_risk_condition_ids if cid}
    # Index conversion timestamps by the market they touched, for fast lookup.
    conv_ts_by_cid: dict[str, list[float]] = defaultdict(list)
    for conv in conversions:
        if conv.condition_id:
            conv_ts_by_cid[conv.condition_id].append(conv.timestamp.timestamp())

    # Group fills by the specific outcome token within a market.
    groups: dict[tuple[str, int], list[Trade]] = defaultdict(list)
    for trade in trades:
        groups[(trade.condition_id, trade.outcome_index)].append(trade)

    dumps: list[DumpEvent] = []
    for (condition_id, outcome_index), fills in groups.items():
        buys = [f for f in fills if f.side is Side.BUY]
        sells = [f for f in fills if f.side is Side.SELL]
        if not buys or not sells:
            continue  # need a round-trip (bought then sold) to be a dump

        bought = sum(f.size for f in buys)
        sold = sum(f.size for f in sells)
        if bought <= 0 or sold / bought < dump_fraction:
            continue  # didn't sell out enough of the position

        first_buy = min(f.timestamp.timestamp() for f in buys)
        last_sell = max(f.timestamp.timestamp() for f in sells)
        if last_sell <= first_buy:
            continue  # sells must come after the initial build

        # Neg-risk exclusion: if this is a neg-risk market and the wallet did a
        # CONVERSION on it close to the sell, the SELL is a conversion, not a dump.
        if condition_id in neg_risk and _has_conversion_near(
            conv_ts_by_cid.get(condition_id, ()), last_sell, conversion_window_seconds
        ):
            continue

        dumps.append(
            DumpEvent(
                condition_id=condition_id,
                outcome_index=outcome_index,
                bought_shares=bought,
                sold_shares=sold,
                avg_buy_price=_vwap(buys),
                avg_sell_price=_vwap(sells),
                first_buy_ts=first_buy,
                last_sell_ts=last_sell,
            )
        )
    return dumps


def _has_conversion_near(timestamps: Iterable[float], target_ts: float, window_seconds: float) -> bool:
    """True if any conversion timestamp is within ``window_seconds`` of ``target_ts``."""
    return any(abs(ts - target_ts) <= window_seconds for ts in timestamps)


def trust_score(dump_count: int, resolved_count: int) -> float:
    """Map dump frequency to a trust score in ``[0, 1]`` (1 == fully trusted).

    Trust falls as the share of a wallet's resolved markets that were dumps
    rises. With no resolved history we stay optimistic (1.0) rather than
    punishing wallets we simply have no data on.
    """
    if resolved_count <= 0:
        return 1.0
    dump_ratio = min(dump_count / resolved_count, 1.0)
    # Linear erosion: a wallet that dumps in half its markets lands at 0.5 trust.
    return round(max(0.0, 1.0 - dump_ratio), 4)


def _vwap(fills: list[Trade]) -> float:
    """Volume-weighted average price of a set of fills (0.0 if no volume)."""
    total_size = sum(f.size for f in fills)
    if total_size <= 0:
        return 0.0
    return sum(f.price * f.size for f in fills) / total_size

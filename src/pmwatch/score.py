"""Per-wallet performance scoring from resolved-market history.

This is the analytical core that decides *who is worth following*. For a given
wallet we pull its positions and trade activity and compute a single composite
:class:`~pmwatch.models.WalletScore` per niche, combining:

* **outcome quality** — wins vs. losses on *resolved* markets (a resolved
  position is one Polymarket marks ``redeemable``),
* **profitability** — net ROI across those resolved positions,
* **integrity** — a penalty for detected pre-resolution dumps (see
  :mod:`pmwatch.signals`), which also lowers the wallet's trust score, and
* **recency** — every contribution is exponentially **time-decayed** so a
  wallet's *recent* behaviour dominates its score. A sharp trader who has gone
  cold will drift down the board; a reformed dumper can climb back.

The weights live in ``config.yaml`` under ``leaderboard.scoring`` so the scoring
philosophy can be tuned without code changes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .client import PolymarketClient
from .config import LeaderboardConfig
from .logging_conf import get_logger
from .models import Position, Trade, WalletScore
from .signals import detect_dumps, trust_score

log = get_logger(__name__)


def decay_weight(event_time: datetime | None, now: datetime, half_life_days: float) -> float:
    """Exponential recency weight in ``(0, 1]``.

    A contribution exactly ``half_life_days`` old is worth 0.5; twice that, 0.25.
    Missing timestamps default to full weight (we don't penalise unknown timing).
    """
    if event_time is None or half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - event_time).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def score_from_data(
    *,
    wallet: str,
    niche: str,
    positions: list[Position],
    activity: list[Trade],
    cfg: LeaderboardConfig,
    now: datetime | None = None,
    conversions: list[Trade] | None = None,
    neg_risk_condition_ids: set[str] | None = None,
) -> WalletScore:
    """Compute a wallet's score from already-fetched data (pure / testable).

    Kept free of any network I/O so it can be unit-tested with fixtures; the
    network-bound :func:`score_wallet` wraps it.

    ``conversions`` and ``neg_risk_condition_ids`` let the dump detector exclude
    genuine neg-risk conversions (which look like SELLs) from the dump penalty.
    """
    now = now or datetime.now(UTC)
    s = cfg.scoring

    # Only resolved markets inform win/loss and ROI — open positions are unrealised.
    resolved = [p for p in positions if p.redeemable]

    wins = 0
    losses = 0
    weighted_points = 0.0
    total_invested = 0.0
    total_pnl = 0.0

    for pos in resolved:
        w = decay_weight(pos.end_date, now, cfg.decay_half_life_days)
        if pos.cash_pnl > 0:
            wins += 1
            weighted_points += s.win_points * w
        else:
            losses += 1
            weighted_points += s.loss_points * w  # loss_points is negative
        total_invested += abs(pos.initial_value)
        total_pnl += pos.cash_pnl

    # Net ROI across resolved positions, guarded against divide-by-zero.
    roi = (total_pnl / total_invested) if total_invested > 0 else 0.0

    # Integrity penalty: each detected dump subtracts points and erodes trust.
    # Neg-risk conversions (passed in) are excluded so they aren't mistaken for dumps.
    dumps = detect_dumps(
        activity,
        neg_risk_condition_ids=neg_risk_condition_ids or (),
        conversions=conversions or (),
    )
    dump_count = len(dumps)
    weighted_points += s.dump_penalty * dump_count

    composite = weighted_points + s.roi_weight * roi
    trust = trust_score(dump_count, resolved_count=len(resolved))

    return WalletScore(
        wallet=wallet.lower(),
        niche=niche,
        score=composite,
        trust=trust,
        wins=wins,
        losses=losses,
        dumps=dump_count,
        roi=roi,
        resolved_count=len(resolved),
        updated_at=now,
    )


def score_wallet(
    client: PolymarketClient,
    *,
    wallet: str,
    niche: str,
    cfg: LeaderboardConfig,
    activity_lookback_days: int = 180,
    now: datetime | None = None,
) -> WalletScore:
    """Fetch a wallet's data and compute its niche score.

    Network-bound convenience wrapper around :func:`score_from_data`.
    ``activity_lookback_days`` bounds the dump-detection history we pull.
    """
    now = now or datetime.now(UTC)
    start = int((now.timestamp()) - activity_lookback_days * 86400)
    end = int(now.timestamp())

    positions = client.get_positions(wallet)
    activity = client.get_activity(wallet, start=start, end=end)
    # Neg-risk conversions look like SELLs; fetch them so dumps aren't over-counted.
    conversions = client.get_activity(wallet, start=start, end=end, activity_type="CONVERSION")
    neg_risk_condition_ids = _neg_risk_markets(client, conversions)

    return score_from_data(
        wallet=wallet,
        niche=niche,
        positions=positions,
        activity=activity,
        cfg=cfg,
        now=now,
        conversions=conversions,
        neg_risk_condition_ids=neg_risk_condition_ids,
    )


def _neg_risk_markets(client: PolymarketClient, conversions: list[Trade]) -> set[str]:
    """Of the markets a wallet converted on, which are actually neg-risk.

    We only look up markets that appear in CONVERSION activity (a small set), so
    this adds at most a handful of metadata calls per wallet rather than one per
    traded market.
    """
    neg_risk: set[str] = set()
    for condition_id in {c.condition_id for c in conversions if c.condition_id}:
        try:
            market = client.get_market(condition_id)
        except Exception as exc:  # metadata is best-effort; never abort scoring
            log.debug("Neg-risk lookup failed for %s: %s", condition_id, exc)
            continue
        if market and market.neg_risk:
            neg_risk.add(condition_id)
    return neg_risk

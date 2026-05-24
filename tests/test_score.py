"""Tests for per-wallet scoring math (pure, fixture-driven)."""

from __future__ import annotations

from datetime import timedelta

from conftest import EPOCH, make_position, make_trade
from pmwatch.models import Side
from pmwatch.score import decay_weight, score_from_data


def test_wins_losses_and_roi(cfg) -> None:
    positions = [
        make_position(wallet="0xa", condition_id="m1", cash_pnl=50.0, initial_value=100.0),
        make_position(wallet="0xa", condition_id="m2", cash_pnl=-30.0, initial_value=100.0),
    ]
    score = score_from_data(
        wallet="0xa", niche="politics", positions=positions, activity=[], cfg=cfg.leaderboard, now=EPOCH
    )
    assert score.wins == 1
    assert score.losses == 1
    assert score.resolved_count == 2
    # ROI = net pnl (20) / invested (200) = 0.1
    assert round(score.roi, 4) == 0.1
    assert score.trust == 1.0  # no dumps


def test_open_positions_excluded_from_scoring(cfg) -> None:
    positions = [
        make_position(wallet="0xb", cash_pnl=999.0, redeemable=False),  # unresolved => ignored
    ]
    score = score_from_data(
        wallet="0xb", niche="crypto", positions=positions, activity=[], cfg=cfg.leaderboard, now=EPOCH
    )
    assert score.resolved_count == 0
    assert score.wins == 0 and score.losses == 0


def test_dump_penalty_and_trust(cfg) -> None:
    positions = [make_position(wallet="0xc", condition_id="m1", cash_pnl=10.0)]
    # A round-trip dump in this wallet's activity.
    activity = [
        make_trade(wallet="0xc", condition_id="m9", side=Side.BUY, size=100, price=0.4, minute=0),
        make_trade(wallet="0xc", condition_id="m9", side=Side.SELL, size=90, price=0.6, minute=90),
    ]
    score = score_from_data(
        wallet="0xc", niche="politics", positions=positions, activity=activity, cfg=cfg.leaderboard, now=EPOCH
    )
    assert score.dumps == 1
    assert score.trust < 1.0
    # Score should reflect the dump penalty (negative contribution applied).
    assert score.score < cfg.leaderboard.scoring.win_points


def test_decay_weight_halves_at_half_life(cfg) -> None:
    half = cfg.leaderboard.decay_half_life_days
    now = EPOCH
    assert decay_weight(now, now, half) == 1.0
    aged = now - timedelta(days=half)
    assert round(decay_weight(aged, now, half), 4) == 0.5
    older = now - timedelta(days=2 * half)
    assert round(decay_weight(older, now, half), 4) == 0.25

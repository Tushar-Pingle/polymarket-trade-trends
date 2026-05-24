"""Tests for the historical replay harness.

These confirm the backtest runs trades through the *same* alert engine and would
surface a consensus that really happened — the end-to-end proof described in the
plan.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import EPOCH, FakeClient, make_score, make_trade
from pmwatch.backtest import backtest_niche, summarize_alerts
from pmwatch.ledger import Leaderboard, Ledger
from pmwatch.models import Side


def _seed_board(ledger: Ledger) -> Leaderboard:
    """Put three wallets on the politics board and persist it."""
    scores = [make_score(f"0xw{i}", "politics", score=30 - i) for i in range(1, 4)]
    board = Leaderboard(niche="politics", board=scores, bench=[], updated_at=EPOCH)
    ledger.save(board, write_history=False)
    return board


def test_backtest_replays_consensus(cfg, tmp_path) -> None:
    ledger = Ledger(tmp_path)
    _seed_board(ledger)

    # Three board wallets all bought YES on the same market within minutes.
    activity = {
        "0xw1": [make_trade(wallet="0xw1", condition_id="mX", side=Side.BUY, minute=0)],
        "0xw2": [make_trade(wallet="0xw2", condition_id="mX", side=Side.BUY, minute=5)],
        "0xw3": [make_trade(wallet="0xw3", condition_id="mX", side=Side.BUY, minute=9)],
    }
    client = FakeClient(activity=activity)

    alerts = backtest_niche(
        client,
        cfg,
        ledger,
        "politics",
        since=EPOCH - timedelta(days=1),
        until=EPOCH + timedelta(days=1),
    )

    assert any(a.kind == "consensus" for a in alerts)
    assert any(a.kind == "single" for a in alerts)
    # The summary is human-readable and mentions the consensus.
    assert "CONSENSUS" in summarize_alerts(alerts).upper()


def test_backtest_market_filter(cfg, tmp_path) -> None:
    ledger = Ledger(tmp_path)
    _seed_board(ledger)
    activity = {
        "0xw1": [make_trade(wallet="0xw1", condition_id="mX", minute=0)],
        "0xw2": [make_trade(wallet="0xw2", condition_id="mY", minute=1)],  # different market
        "0xw3": [make_trade(wallet="0xw3", condition_id="mX", minute=2)],
    }
    client = FakeClient(activity=activity)

    alerts = backtest_niche(
        client,
        cfg,
        ledger,
        "politics",
        since=EPOCH - timedelta(days=1),
        condition_id="mX",
    )
    # Only the two mX trades replay; mY is filtered out, so no 3-wallet consensus.
    assert all(a.title for a in alerts)
    assert all(w.address in {"0xw1", "0xw3"} for a in alerts for w in a.wallets)


def test_backtest_empty_window_is_quiet(cfg, tmp_path) -> None:
    ledger = Ledger(tmp_path)
    _seed_board(ledger)
    client = FakeClient(activity={})  # no trades at all
    alerts = backtest_niche(client, cfg, ledger, "politics", since=EPOCH - timedelta(days=1))
    assert alerts == []
    assert "No alerts" in summarize_alerts(alerts)

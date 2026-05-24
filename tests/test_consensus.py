"""Tests for the tiered alert engine: single, consensus, escalation, exit, window."""

from __future__ import annotations

from conftest import EPOCH, make_score, make_trade
from pmwatch.consensus import AlertEngine
from pmwatch.ledger import Leaderboard
from pmwatch.models import Side
from pmwatch.store import Store

# A fixed price resolver so "edge" rendering is deterministic and offline.
PRICE = lambda _cid, _idx: 0.55  # noqa: E731


def _board() -> Leaderboard:
    """Five-wallet board, w1 highest-ranked .. w5 lowest."""
    scores = [make_score(f"0xw{i}", "politics", score=60 - 10 * i) for i in range(1, 6)]
    return Leaderboard(niche="politics", board=scores, bench=[], updated_at=EPOCH)


def _engine() -> tuple[AlertEngine, Store, object]:
    from conftest import PROJECT_ROOT
    from pmwatch.config import load_config

    cfg = load_config(PROJECT_ROOT / "config.yaml", load_env=False)
    store = Store(":memory:")
    return AlertEngine(cfg, store), store, cfg


def test_single_alert_fires_on_every_bet_with_rank() -> None:
    engine, store, _ = _engine()
    board = _board()
    trade = make_trade(wallet="0xw1", minute=0)
    alerts = engine.evaluate("politics", board, trade, PRICE, now=trade.timestamp)
    single = [a for a in alerts if a.kind == "single"]
    assert len(single) == 1
    assert single[0].wallets[0].rank == 1
    assert "Top leader" in single[0].note


def test_single_alert_color_tiers() -> None:
    engine, store, cfg = _engine()
    board = _board()
    # Rank 1 (<= high_rank_threshold) => warning colour.
    a1 = engine.evaluate("politics", board, make_trade(wallet="0xw1", minute=0), PRICE, now=EPOCH)[0]
    # Rank 5 (> threshold) => muted colour.
    a5 = engine.evaluate("politics", board, make_trade(wallet="0xw5", minute=1), PRICE, now=EPOCH)[0]
    assert a1.color == cfg.alerts.colors.warning
    assert a5.color == cfg.alerts.colors.muted


def test_unranked_wallet_is_muted_and_informational() -> None:
    engine, _, cfg = _engine()
    board = _board()
    trade = make_trade(wallet="0xstranger", minute=0)
    alert = engine.evaluate("politics", board, trade, PRICE, now=trade.timestamp)[0]
    assert alert.wallets[0].rank is None
    assert alert.color == cfg.alerts.colors.muted
    assert "Benched/unranked" in alert.note


def test_consensus_fires_at_threshold() -> None:
    engine, _, _ = _engine()
    board = _board()
    kinds: list[list[str]] = []
    for i in range(1, 4):  # three distinct wallets, same market+outcome+BUY
        trade = make_trade(wallet=f"0xw{i}", minute=i)
        alerts = engine.evaluate("politics", board, trade, PRICE, now=trade.timestamp)
        kinds.append([a.kind for a in alerts])

    # First two: single only. Third: single + consensus.
    assert kinds[0] == ["single"]
    assert kinds[1] == ["single"]
    assert "consensus" in kinds[2]


def test_consensus_escalation_step() -> None:
    engine, _, _ = _engine()  # min_wallets=3, escalation_step=2 in config
    board = _board()
    fired_consensus_at: list[int] = []
    for i in range(1, 6):  # w1..w5 all converge
        trade = make_trade(wallet=f"0xw{i}", minute=i)
        alerts = engine.evaluate("politics", board, trade, PRICE, now=trade.timestamp)
        if any(a.kind == "consensus" for a in alerts):
            fired_consensus_at.append(i)
    # Fires at 3 (first reach), then not at 4, then again at 5 (3 + step).
    assert fired_consensus_at == [3, 5]


def test_consensus_window_expiry() -> None:
    engine, _, cfg = _engine()
    board = _board()
    window_minutes = cfg.alerts.consensus.window_hours * 60

    engine.evaluate("politics", board, make_trade(wallet="0xw1", minute=0), PRICE, now=EPOCH)
    t2 = make_trade(wallet="0xw2", minute=10)
    engine.evaluate("politics", board, t2, PRICE, now=t2.timestamp)
    # Third bet lands well outside the window from the first two.
    t3 = make_trade(wallet="0xw3", minute=window_minutes + 100)
    alerts = engine.evaluate("politics", board, t3, PRICE, now=t3.timestamp)
    assert all(a.kind != "consensus" for a in alerts)


def test_sell_consensus_is_exit_alert() -> None:
    engine, _, cfg = _engine()
    board = _board()
    last_alerts = []
    for i in range(1, 4):
        trade = make_trade(wallet=f"0xw{i}", side=Side.SELL, minute=i)
        last_alerts = engine.evaluate("politics", board, trade, PRICE, now=trade.timestamp)
    exit_alerts = [a for a in last_alerts if a.kind == "exit"]
    assert len(exit_alerts) == 1
    assert exit_alerts[0].color == cfg.alerts.colors.max
    assert "SELLING" in exit_alerts[0].note

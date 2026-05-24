"""Tests for the weekly ranker, including bench / revival transitions."""

from __future__ import annotations

from dataclasses import replace

from conftest import EPOCH, FakeClient, make_market, make_position
from pmwatch.leaderboard import rank_niche
from pmwatch.ledger import Ledger


def _small_board_cfg(cfg):
    """Shrink board/bench so promotions are easy to assert with few wallets."""
    lb = replace(cfg.leaderboard, board_size=2, bench_size=5)
    return replace(cfg, leaderboard=lb)


def _client(positions: dict[str, list]) -> FakeClient:
    market = make_market("m1", tags=["Politics"])
    return FakeClient(
        positions=positions,
        markets=[market],
        holders={"m1": ["0xw1", "0xw2", "0xw3", "0xw4"]},
    )


def test_ranking_orders_by_score(cfg, tmp_path) -> None:
    cfg = _small_board_cfg(cfg)
    ledger = Ledger(tmp_path)
    positions = {
        "0xw1": [make_position(wallet="0xw1", cash_pnl=100.0)],  # best
        "0xw2": [make_position(wallet="0xw2", cash_pnl=20.0)],
        "0xw3": [make_position(wallet="0xw3", cash_pnl=-50.0)],  # worst
        "0xw4": [make_position(wallet="0xw4", cash_pnl=5.0)],
    }
    board = rank_niche(_client(positions), cfg.niche("politics"), ledger, cfg, now=EPOCH)

    assert [s.wallet for s in board.board] == ["0xw1", "0xw2"]
    # The rest are retained on the bench (not discarded).
    assert {s.wallet for s in board.bench} == {"0xw3", "0xw4"}


def test_benched_wallet_can_be_revived(cfg, tmp_path) -> None:
    cfg = _small_board_cfg(cfg)
    ledger = Ledger(tmp_path)

    # Week 1: w1 strong, w3 weak -> w3 benched.
    week1 = {
        "0xw1": [make_position(wallet="0xw1", cash_pnl=100.0)],
        "0xw2": [make_position(wallet="0xw2", cash_pnl=20.0)],
        "0xw3": [make_position(wallet="0xw3", cash_pnl=-50.0)],
        "0xw4": [make_position(wallet="0xw4", cash_pnl=5.0)],
    }
    board1 = rank_niche(_client(week1), cfg.niche("politics"), ledger, cfg, now=EPOCH)
    assert board1.rank_of("0xw3") is None  # w3 starts on the bench
    assert board1.rank_of("0xw1") == 1

    # Week 2: w3's trend flips strongly positive, w1 collapses.
    week2 = {
        "0xw1": [make_position(wallet="0xw1", cash_pnl=-80.0)],  # now losing
        "0xw2": [make_position(wallet="0xw2", cash_pnl=20.0)],
        "0xw3": [make_position(wallet="0xw3", cash_pnl=200.0)],  # now the best
        "0xw4": [make_position(wallet="0xw4", cash_pnl=5.0)],
    }
    board2 = rank_niche(_client(week2), cfg.niche("politics"), ledger, cfg, now=EPOCH)

    assert board2.rank_of("0xw3") == 1  # revived onto the board, top spot
    assert board2.rank_of("0xw1") is None  # demoted to the bench
    assert any(s.wallet == "0xw1" for s in board2.bench)  # but retained


def test_ranking_persists_json(cfg, tmp_path) -> None:
    cfg = _small_board_cfg(cfg)
    ledger = Ledger(tmp_path)
    positions = {"0xw1": [make_position(wallet="0xw1", cash_pnl=100.0)]}
    rank_niche(_client(positions), cfg.niche("politics"), ledger, cfg, now=EPOCH)

    # The board file exists and reloads with the same top wallet.
    reloaded = ledger.load("politics")
    assert reloaded.board[0].wallet == "0xw1"
    assert (tmp_path / "leaderboard" / "politics.json").is_file()

"""Tests for pre-resolution dump detection and trust scoring."""

from __future__ import annotations

from conftest import make_trade
from pmwatch.models import Side
from pmwatch.signals import detect_dumps, trust_score


def test_detects_round_trip_dump() -> None:
    # Wallet buys 100 then sells 80 of the same outcome later => a dump (80%).
    trades = [
        make_trade(wallet="0xdumper", side=Side.BUY, size=100, price=0.40, minute=0),
        make_trade(wallet="0xdumper", side=Side.SELL, size=80, price=0.60, minute=120),
    ]
    dumps = detect_dumps(trades, dump_fraction=0.5)
    assert len(dumps) == 1
    event = dumps[0]
    assert event.sold_fraction == 0.8
    # Captured edge is the difference between sell and buy VWAP.
    assert round(event.realised_edge, 2) == 0.20


def test_buy_and_hold_is_not_a_dump() -> None:
    # Only buys (held to resolution) => no sell leg => not a dump.
    trades = [
        make_trade(wallet="0xholder", side=Side.BUY, size=100, price=0.40, minute=0),
        make_trade(wallet="0xholder", side=Side.BUY, size=50, price=0.45, minute=30),
    ]
    assert detect_dumps(trades) == []


def test_small_partial_sell_below_threshold_ignored() -> None:
    # Selling only 20% of the position is below the 0.5 dump threshold.
    trades = [
        make_trade(wallet="0xw", side=Side.BUY, size=100, price=0.40, minute=0),
        make_trade(wallet="0xw", side=Side.SELL, size=20, price=0.50, minute=60),
    ]
    assert detect_dumps(trades, dump_fraction=0.5) == []


def test_trust_score_erodes_with_dump_ratio() -> None:
    assert trust_score(0, 10) == 1.0  # clean record => full trust
    assert trust_score(5, 10) == 0.5  # dumps in half the markets
    assert trust_score(10, 10) == 0.0  # dumps everywhere => no trust
    assert trust_score(3, 0) == 1.0  # no resolved history => stay optimistic


# --------------------------------------------------------------------------- #
# Neg-risk conversion exclusion
# --------------------------------------------------------------------------- #
def _neg_risk_round_trip() -> list:
    """A bought-then-sold round-trip on a neg-risk market (would look like a dump)."""
    return [
        make_trade(wallet="0xw", condition_id="0xnr", side=Side.BUY, size=100, price=0.40, minute=0),
        make_trade(wallet="0xw", condition_id="0xnr", side=Side.SELL, size=90, price=0.60, minute=120),
    ]


def test_negrisk_sell_with_matching_conversion_is_not_a_dump() -> None:
    trades = _neg_risk_round_trip()
    # A CONVERSION on the same market, right around the sell.
    conversions = [make_trade(wallet="0xw", condition_id="0xnr", minute=121)]
    dumps = detect_dumps(trades, neg_risk_condition_ids={"0xnr"}, conversions=conversions)
    assert dumps == []  # the sell was a neg-risk conversion, not an exit


def test_negrisk_sell_without_conversion_still_counts() -> None:
    trades = _neg_risk_round_trip()
    dumps = detect_dumps(trades, neg_risk_condition_ids={"0xnr"}, conversions=[])
    assert len(dumps) == 1  # neg-risk, but no conversion => a real dump


def test_non_negrisk_sell_with_conversion_still_counts() -> None:
    trades = _neg_risk_round_trip()
    conversions = [make_trade(wallet="0xw", condition_id="0xnr", minute=121)]
    # Market is NOT in the neg-risk set, so the conversion must be ignored.
    dumps = detect_dumps(trades, neg_risk_condition_ids=set(), conversions=conversions)
    assert len(dumps) == 1


def test_negrisk_conversion_outside_window_still_counts() -> None:
    trades = _neg_risk_round_trip()  # last sell at minute 120 (7200s)
    # Conversion far from the sell (minute 0) => outside the 1h window => still a dump.
    conversions = [make_trade(wallet="0xw", condition_id="0xnr", minute=0)]
    dumps = detect_dumps(
        trades, neg_risk_condition_ids={"0xnr"}, conversions=conversions, conversion_window_seconds=3600.0
    )
    assert len(dumps) == 1

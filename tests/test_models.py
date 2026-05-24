"""Tests for parsing raw Polymarket API payloads into domain models.

These guard against real-world API quirks — notably that `/positions` returns
`endDate` as an ISO-8601 string while `/trades` returns `timestamp` as Unix
seconds. A regression here previously caused every wallet to fail scoring.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pmwatch.models import Position, Side, Trade


def test_trade_parses_unix_timestamp() -> None:
    raw = {
        "proxyWallet": "0xABC",
        "conditionId": "0xm",
        "asset": "0xm-0",
        "side": "BUY",
        "outcome": "Yes",
        "outcomeIndex": 0,
        "size": 100,
        "price": 0.6,
        "timestamp": 1730808000,  # Unix seconds, as the Data API returns
        "transactionHash": "0xtx",
    }
    trade = Trade.from_api(raw)
    assert trade.wallet == "0xabc"  # normalised lower-case
    assert trade.side is Side.BUY
    assert trade.timestamp == datetime.fromtimestamp(1730808000, tz=UTC)


def test_position_parses_iso_end_date() -> None:
    # The Data API returns endDate as an ISO-8601 string, NOT Unix seconds.
    raw = {
        "proxyWallet": "0xABC",
        "conditionId": "0xm",
        "asset": "0xm-0",
        "outcome": "Yes",
        "size": 100,
        "avgPrice": 0.5,
        "curPrice": 1.0,
        "initialValue": 50.0,
        "currentValue": 100.0,
        "cashPnl": 50.0,
        "percentPnl": 1.0,
        "redeemable": True,
        "endDate": "2024-11-05T12:00:00Z",
    }
    pos = Position.from_api(raw)  # must not raise
    assert pos.redeemable is True
    assert pos.end_date is not None
    assert pos.end_date.year == 2024 and pos.end_date.month == 11
    assert pos.end_date.tzinfo is not None  # timezone-aware


def test_position_tolerates_missing_or_bad_end_date() -> None:
    base = {"proxyWallet": "0x1", "conditionId": "0xm", "cashPnl": 1.0}
    assert Position.from_api(base).end_date is None  # missing
    assert Position.from_api({**base, "endDate": ""}).end_date is None  # empty
    assert Position.from_api({**base, "endDate": "not-a-date"}).end_date is None  # garbage, no raise


def test_position_parses_numeric_string_end_date() -> None:
    pos = Position.from_api({"proxyWallet": "0x1", "conditionId": "0xm", "endDate": "1730808000"})
    assert pos.end_date == datetime.fromtimestamp(1730808000, tz=UTC)

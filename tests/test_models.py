"""Tests for parsing raw Polymarket API payloads into domain models.

These guard against real-world API quirks — notably that `/positions` returns
`endDate` as an ISO-8601 string while `/trades` returns `timestamp` as Unix
seconds. A regression here previously caused every wallet to fail scoring.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pmwatch.models import Event, Market, Position, Side, Trade


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


# --------------------------------------------------------------------------- #
# Neg-risk + Wave-2 fields
# --------------------------------------------------------------------------- #
def test_market_parses_negrisk_and_microstructure_fields() -> None:
    raw = {
        "conditionId": "0xm",
        "question": "Q?",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.6", "0.4"],
        "negRisk": True,
        "enableNegRisk": True,
        "lastTradePrice": 0.61,
        "bestBid": 0.60,
        "bestAsk": 0.62,
        "spread": 0.02,
        "oneDayPriceChange": -0.03,
        "liquidityClob": 12345.6,
        "volume24hr": 7890.1,
    }
    m = Market.from_api(raw)
    assert m.neg_risk is True and m.enable_neg_risk is True
    assert m.last_trade_price == 0.61
    assert m.best_bid == 0.60 and m.best_ask == 0.62
    assert m.spread == 0.02
    assert m.one_day_price_change == -0.03
    assert m.liquidity_clob == 12345.6
    assert m.volume_24hr == 7890.1


def test_market_fields_default_when_missing() -> None:
    m = Market.from_api({"conditionId": "0xm", "question": "Q?"})
    assert m.neg_risk is False and m.enable_neg_risk is False
    assert m.last_trade_price == 0.0
    assert m.best_bid == 0.0 and m.best_ask == 0.0
    assert m.spread == 0.0
    assert m.one_day_price_change == 0.0
    assert m.liquidity_clob == 0.0
    assert m.volume_24hr == 0.0


def test_event_parses_negrisk_and_liquidity_fields() -> None:
    raw = {
        "id": "e1",
        "title": "An event",
        "slug": "an-event",
        "volume": 1000.0,
        "tags": [{"label": "Politics", "slug": "politics"}],
        "markets": [{"conditionId": "0xm"}],
        "negRisk": True,
        "enableNegRisk": True,
        "openInterest": 5000.0,
        "liquidity": 2000.0,
        "liquidityClob": 1500.0,
        "volume24hr": 300.0,
        "volume1wk": 900.0,
        "competitive": 0.87,
        "commentCount": 42,
    }
    e = Event.from_api(raw)
    assert e.neg_risk is True and e.enable_neg_risk is True
    assert e.open_interest == 5000.0
    assert e.liquidity == 2000.0 and e.liquidity_clob == 1500.0
    assert e.volume_24hr == 300.0 and e.volume_1wk == 900.0
    assert e.competitive == 0.87
    assert e.comment_count == 42
    # Existing parsing still works.
    assert e.tags == ["politics", "politics"]  # label + slug, lower-cased
    assert e.market_condition_ids == ["0xm"]


def test_event_fields_default_when_missing() -> None:
    e = Event.from_api({"id": "e1", "title": "x", "markets": [], "tags": []})
    assert e.neg_risk is False and e.enable_neg_risk is False
    assert e.open_interest == 0.0
    assert e.liquidity == 0.0 and e.liquidity_clob == 0.0
    assert e.volume_24hr == 0.0 and e.volume_1wk == 0.0
    assert e.competitive == 0.0
    assert e.comment_count == 0

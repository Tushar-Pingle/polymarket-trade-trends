"""Shared pytest fixtures and lightweight fakes.

Two things live here:

* small **factory helpers** to build :class:`Trade` / :class:`Position` /
  :class:`WalletScore` objects tersely in tests, and
* a **FakeClient** that satisfies the slice of :class:`PolymarketClient` the
  scoring / ranking / backtest code calls, so those paths can be exercised
  deterministically without any network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pmwatch.config import Config, load_config
from pmwatch.models import Event, Market, Position, Side, Trade, WalletScore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def cfg() -> Config:
    """The real repository config, loaded without touching the environment."""
    return load_config(PROJECT_ROOT / "config.yaml", load_env=False)


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
def make_trade(
    *,
    wallet: str,
    condition_id: str = "0xmarket",
    outcome_index: int = 0,
    side: Side = Side.BUY,
    size: float = 100.0,
    price: float = 0.5,
    minute: int = 0,
    tx: str | None = None,
    title: str = "Will it happen?",
    outcome: str = "Yes",
) -> Trade:
    """Build a Trade with sensible defaults; ``minute`` offsets the timestamp."""
    return Trade(
        wallet=wallet.lower(),
        condition_id=condition_id,
        asset=f"{condition_id}-{outcome_index}",
        side=side,
        outcome=outcome,
        outcome_index=outcome_index,
        size=size,
        price=price,
        timestamp=EPOCH + timedelta(minutes=minute),
        tx_hash=tx or f"0x{wallet}{minute}{side.value}",
        title=title,
        slug="will-it-happen",
    )


def make_position(
    *,
    wallet: str,
    condition_id: str = "0xmarket",
    cash_pnl: float = 10.0,
    initial_value: float = 100.0,
    redeemable: bool = True,
    days_old: float = 0.0,
) -> Position:
    """Build a (by default resolved/redeemable) Position."""
    end = EPOCH - timedelta(days=days_old)
    return Position(
        wallet=wallet.lower(),
        condition_id=condition_id,
        asset=f"{condition_id}-0",
        outcome="Yes",
        size=100.0,
        avg_price=0.5,
        cur_price=1.0 if cash_pnl > 0 else 0.0,
        initial_value=initial_value,
        current_value=initial_value + cash_pnl,
        cash_pnl=cash_pnl,
        percent_pnl=cash_pnl / initial_value if initial_value else 0.0,
        redeemable=redeemable,
        title="Resolved market",
        end_date=end,
    )


def make_score(wallet: str, niche: str, score: float, *, trust: float = 1.0) -> WalletScore:
    return WalletScore(
        wallet=wallet.lower(),
        niche=niche,
        score=score,
        trust=trust,
        wins=1,
        losses=0,
        dumps=0,
        roi=0.1,
        resolved_count=1,
        updated_at=EPOCH,
    )


# --------------------------------------------------------------------------- #
# FakeClient
# --------------------------------------------------------------------------- #
class FakeClient:
    """In-memory stand-in for PolymarketClient used by scoring/ranking tests."""

    def __init__(
        self,
        *,
        positions: dict[str, list[Position]] | None = None,
        activity: dict[str, list[Trade]] | None = None,
        markets: list[Market] | None = None,
        holders: dict[str, list[str]] | None = None,
        events: list[Event] | None = None,
        conversions: dict[str, list[Trade]] | None = None,
    ) -> None:
        self._positions = positions or {}
        self._activity = activity or {}
        self._markets = markets or []
        self._holders = holders or {}
        self._events = events or []
        self._conversions = conversions or {}

    def get_positions(self, wallet: str, *, limit: int = 500, offset: int = 0) -> list[Position]:
        return list(self._positions.get(wallet.lower(), []))

    def get_all_positions(self, wallet: str, *, page_size: int = 500, hard_cap: int = 5000) -> list[Position]:
        return self.get_positions(wallet)

    def get_activity(
        self, wallet: str, *, start=None, end=None, limit=500, offset=0, activity_type="TRADE"
    ) -> list[Trade]:
        source = self._conversions if activity_type == "CONVERSION" else self._activity
        return list(source.get(wallet.lower(), []))

    def get_all_activity(
        self, wallet: str, *, start=None, end=None, activity_type="TRADE", page_size: int = 500, hard_cap: int = 5000
    ) -> list[Trade]:
        return self.get_activity(wallet, activity_type=activity_type)

    def get_top_markets(self, *, limit: int = 100, closed: bool = False) -> list[Market]:
        return list(self._markets)

    def get_holders(self, condition_id: str, *, limit: int = 50) -> list[str]:
        return list(self._holders.get(condition_id, []))

    def get_events(self, *, limit=100, offset=0, closed=False, order="volume", ascending=False) -> list[Event]:
        # Mimic Gamma's offset pagination over the configured event list.
        return list(self._events[offset : offset + limit])

    def get_market(self, condition_id: str) -> Market | None:
        for m in self._markets:
            if m.condition_id == condition_id:
                return m
        return None


def make_market(condition_id: str, *, tags: list[str], price: float = 0.5) -> Market:
    return Market(
        condition_id=condition_id,
        question=f"Market {condition_id}",
        slug=condition_id,
        outcomes=["Yes", "No"],
        outcome_prices=[price, 1 - price],
        volume=1_000_000.0,
        closed=False,
        tags=tags,
    )


def make_event(
    event_id: str,
    *,
    tags: list[str],
    condition_ids: list[str],
    volume: float = 1_000_000.0,
    title: str = "",
) -> Event:
    """Build an Event with lower-cased tags (as the real parser produces)."""
    return Event(
        event_id=event_id,
        title=title,
        slug=event_id,
        volume=volume,
        tags=[t.lower() for t in tags],
        market_condition_ids=condition_ids,
    )

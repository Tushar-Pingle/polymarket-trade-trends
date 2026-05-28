"""Domain models.

These frozen dataclasses are the *internal* representation the rest of the code
works with. Raw API JSON (which uses Polymarket's field names and occasionally
changes) is parsed into these types at the edge — in :mod:`pmwatch.client` and
the ``from_api`` constructors below — so the rest of the codebase never touches
loosely-typed dicts. That keeps the business logic readable and type-checked.

Conventions
-----------
* Money / sizes are floats in USD or share counts as Polymarket reports them.
* Prices are probabilities in the open interval (0, 1): 0.62 == 62 cents == 62%.
* Timestamps are timezone-aware UTC :class:`datetime` objects internally; the
  APIs hand us Unix seconds, which we convert immediately on ingest.
* ``Side`` is normalised to the strings ``"BUY"`` / ``"SELL"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def _to_utc(value: float | int | str | None) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime, tolerating both formats
    Polymarket uses: Unix epoch seconds (int/float/numeric-string, as ``/trades``
    and ``/activity`` return) AND ISO-8601 strings (as ``/positions`` returns for
    ``endDate``). Anything unparseable yields ``None`` rather than raising, so a
    single odd row can never abort scoring of an entire wallet."""
    if value is None or value == "":
        return None
    # Native numeric -> Unix epoch seconds.
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    # Numeric string (e.g. "1730808000") -> Unix epoch seconds.
    try:
        return datetime.fromtimestamp(float(text), tz=UTC)
    except ValueError:
        pass
    # Otherwise treat as ISO-8601 (tolerate a trailing "Z").
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _norm_addr(address: str | None) -> str:
    """Lower-case a wallet address for stable comparisons / dict keys."""
    return (address or "").lower()


class Side(StrEnum):
    """Trade direction, normalised. A ``StrEnum`` so it serialises cleanly to its value."""

    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def parse(cls, raw: str) -> Side:
        return cls.BUY if str(raw).upper() == "BUY" else cls.SELL


@dataclass(frozen=True)
class Trade:
    """A single fill by a wallet, as returned by ``GET /trades`` / ``/activity``.

    The Data API conveniently embeds the market ``title`` and ``outcome`` on each
    trade, so a trade is self-describing for alerting without a second lookup.
    """

    wallet: str
    condition_id: str  # identifies the market
    asset: str  # the specific outcome token id
    side: Side
    outcome: str  # human outcome label, e.g. "Yes" / "No"
    outcome_index: int
    size: float  # number of shares
    price: float  # fill price in (0, 1)
    timestamp: datetime
    tx_hash: str
    title: str = ""  # market question/title (may be blank on some rows)
    slug: str = ""  # market slug, used to build a polymarket.com link

    @property
    def notional_usd(self) -> float:
        """Approximate USD notional of the fill (shares * price)."""
        return self.size * self.price

    @property
    def market_url(self) -> str:
        """Best-effort public URL for the market."""
        return f"https://polymarket.com/event/{self.slug}" if self.slug else "https://polymarket.com"

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Trade:
        """Build a :class:`Trade` from one Data-API trade/activity record."""
        return cls(
            wallet=_norm_addr(raw.get("proxyWallet")),
            condition_id=str(raw.get("conditionId", "")),
            asset=str(raw.get("asset", "")),
            side=Side.parse(raw.get("side", "BUY")),
            outcome=str(raw.get("outcome", "")),
            outcome_index=int(raw.get("outcomeIndex", 0) or 0),
            size=float(raw.get("size", 0) or 0),
            price=float(raw.get("price", 0) or 0),
            timestamp=_to_utc(raw.get("timestamp")) or datetime.now(UTC),
            tx_hash=str(raw.get("transactionHash", "")),
            title=str(raw.get("title", "")),
            slug=str(raw.get("slug", "")),
        )


@dataclass(frozen=True)
class Position:
    """A wallet's position in one market, from ``GET /positions``.

    The ``cash_pnl`` / ``percent_pnl`` fields are what the leaderboard scores on.
    ``redeemable`` is True once a market has resolved and the position can be
    claimed — our proxy for "this market is resolved" when scoring.
    """

    wallet: str
    condition_id: str
    asset: str
    outcome: str
    size: float
    avg_price: float
    cur_price: float
    initial_value: float
    current_value: float
    cash_pnl: float  # realised+unrealised PnL in USD
    percent_pnl: float  # PnL as a fraction (0.25 == +25%)
    redeemable: bool  # True => market resolved
    title: str = ""
    end_date: datetime | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Position:
        return cls(
            wallet=_norm_addr(raw.get("proxyWallet")),
            condition_id=str(raw.get("conditionId", "")),
            asset=str(raw.get("asset", "")),
            outcome=str(raw.get("outcome", "")),
            size=float(raw.get("size", 0) or 0),
            avg_price=float(raw.get("avgPrice", 0) or 0),
            cur_price=float(raw.get("curPrice", 0) or 0),
            initial_value=float(raw.get("initialValue", 0) or 0),
            current_value=float(raw.get("currentValue", 0) or 0),
            cash_pnl=float(raw.get("cashPnl", 0) or 0),
            percent_pnl=float(raw.get("percentPnl", 0) or 0),
            redeemable=bool(raw.get("redeemable", False)),
            title=str(raw.get("title", "")),
            end_date=_to_utc(raw.get("endDate")),
        )


@dataclass(frozen=True)
class Market:
    """Market metadata from the Gamma API.

    ``outcome_prices`` is aligned by index with ``outcomes`` so the current
    market price of a given outcome can be looked up for the "edge left"
    comparison shown in alerts.
    """

    condition_id: str
    question: str
    slug: str
    outcomes: list[str]
    outcome_prices: list[float]
    volume: float
    closed: bool
    tags: list[str] = field(default_factory=list)
    end_date: datetime | None = None
    # Neg-risk: in these markets a SELL of NO can be a conversion rather than an
    # exit, which the dump detector must account for (see pmwatch.signals).
    neg_risk: bool = False
    enable_neg_risk: bool = False
    # Microstructure fields captured for Wave 2 (not yet used by alert logic).
    last_trade_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    one_day_price_change: float = 0.0
    liquidity_clob: float = 0.0
    volume_24hr: float = 0.0

    def price_of(self, outcome_index: int) -> float | None:
        """Current market price for an outcome index, or ``None`` if unknown."""
        if 0 <= outcome_index < len(self.outcome_prices):
            return self.outcome_prices[outcome_index]
        return None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Market:
        # Gamma returns outcomes/outcomePrices as JSON-encoded strings on some
        # endpoints and as native arrays on others; tolerate both.
        outcomes = _as_list(raw.get("outcomes"))
        prices = [float(p) for p in _as_list(raw.get("outcomePrices"))]
        tags_raw = raw.get("tags") or raw.get("category") or []
        if isinstance(tags_raw, str):
            tags = [tags_raw]
        else:
            tags = [str(t.get("label", t)) if isinstance(t, dict) else str(t) for t in tags_raw]
        return cls(
            condition_id=str(raw.get("conditionId", "")),
            question=str(raw.get("question", raw.get("title", ""))),
            slug=str(raw.get("slug", "")),
            outcomes=[str(o) for o in outcomes],
            outcome_prices=prices,
            volume=float(raw.get("volume", 0) or 0),
            closed=bool(raw.get("closed", False)),
            tags=tags,
            end_date=_parse_iso(raw.get("endDate")),
            neg_risk=bool(raw.get("negRisk", False)),
            enable_neg_risk=bool(raw.get("enableNegRisk", False)),
            last_trade_price=float(raw.get("lastTradePrice", 0) or 0),
            best_bid=float(raw.get("bestBid", 0) or 0),
            best_ask=float(raw.get("bestAsk", 0) or 0),
            spread=float(raw.get("spread", 0) or 0),
            one_day_price_change=float(raw.get("oneDayPriceChange", 0) or 0),
            liquidity_clob=float(raw.get("liquidityClob", 0) or 0),
            volume_24hr=float(raw.get("volume24hr", 0) or 0),
        )


@dataclass(frozen=True)
class Event:
    """A Gamma *event* — the category-bearing object that groups related markets.

    Crucially, the category **tags** (e.g. "Politics", "Crypto", "Soccer") live on
    the event, not on the individual market (a market's top-level ``tags`` come
    back empty from ``/markets``). Each event also bundles its markets, from which
    we take ``conditionId`` for the holders lookup. ``tags`` here is a flat,
    lower-cased list of every tag label *and* slug, so niche matching can do a
    simple case-insensitive substring test.
    """

    event_id: str
    title: str
    slug: str
    volume: float
    tags: list[str]  # lower-cased tag labels + slugs
    market_condition_ids: list[str]
    neg_risk: bool = False
    enable_neg_risk: bool = False
    # Liquidity / interest fields captured for Wave 2 (not yet used by alert logic).
    open_interest: float = 0.0
    liquidity: float = 0.0
    liquidity_clob: float = 0.0
    volume_24hr: float = 0.0
    volume_1wk: float = 0.0
    competitive: float = 0.0
    comment_count: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Event:
        # Flatten every tag's label and slug into one lower-cased term list.
        tag_terms: list[str] = []
        for tag in raw.get("tags") or []:
            if isinstance(tag, dict):
                for key in ("label", "slug"):
                    value = tag.get(key)
                    if value:
                        tag_terms.append(str(value).lower())

        # Collect condition ids of this event's markets (skip blanks).
        condition_ids: list[str] = []
        for market in raw.get("markets") or []:
            if isinstance(market, dict):
                cid = market.get("conditionId")
                if cid:
                    condition_ids.append(str(cid))

        return cls(
            event_id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            slug=str(raw.get("slug", "")),
            volume=float(raw.get("volume", 0) or 0),
            tags=tag_terms,
            market_condition_ids=condition_ids,
            neg_risk=bool(raw.get("negRisk", False)),
            enable_neg_risk=bool(raw.get("enableNegRisk", False)),
            open_interest=float(raw.get("openInterest", 0) or 0),
            liquidity=float(raw.get("liquidity", 0) or 0),
            liquidity_clob=float(raw.get("liquidityClob", 0) or 0),
            volume_24hr=float(raw.get("volume24hr", 0) or 0),
            volume_1wk=float(raw.get("volume1wk", 0) or 0),
            competitive=float(raw.get("competitive", 0) or 0),
            comment_count=int(raw.get("commentCount", 0) or 0),
        )


@dataclass(frozen=True)
class WalletScore:
    """A wallet's computed standing within one niche (output of the ranker)."""

    wallet: str
    niche: str
    score: float  # composite, decay-weighted points
    trust: float  # 0..1; lowered by pre-resolution dumping
    wins: int
    losses: int
    dumps: int
    roi: float  # net ROI fraction across resolved positions
    resolved_count: int  # sample size — how many resolved markets scored
    updated_at: datetime

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return self.wins / decided if decided else 0.0

    def to_json(self) -> dict[str, Any]:
        """Serialise for the committed leaderboard JSON (stable key order)."""
        return {
            "wallet": self.wallet,
            "niche": self.niche,
            "score": round(self.score, 4),
            "trust": round(self.trust, 4),
            "wins": self.wins,
            "losses": self.losses,
            "dumps": self.dumps,
            "roi": round(self.roi, 4),
            "win_rate": round(self.win_rate, 4),
            "resolved_count": self.resolved_count,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> WalletScore:
        return cls(
            wallet=str(raw["wallet"]),
            niche=str(raw["niche"]),
            score=float(raw["score"]),
            trust=float(raw["trust"]),
            wins=int(raw["wins"]),
            losses=int(raw["losses"]),
            dumps=int(raw["dumps"]),
            roi=float(raw["roi"]),
            resolved_count=int(raw["resolved_count"]),
            updated_at=_parse_iso(raw["updated_at"]) or datetime.now(UTC),
        )


def _as_list(value: Any) -> list[Any]:
    """Coerce Gamma's sometimes-JSON-string arrays into Python lists."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [value]
    return [value]


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (Gamma) into aware UTC, tolerating ``Z``."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None

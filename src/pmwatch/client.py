"""HTTP client for the Polymarket public APIs (Data API + Gamma API).

Everything this project needs is available without authentication:

* **Data API** (``data-api.polymarket.com``) — per-wallet trades, positions and
  activity, plus per-market holders. This is the behavioural data we watch and
  score.
* **Gamma API** (``gamma-api.polymarket.com``) — market/event metadata: titles,
  tags, current outcome prices, resolution dates. Used to bucket markets into
  niches during discovery and to compute the "edge left" (current price) shown
  in alerts.

Design notes
------------
* A **token-bucket rate limiter** is shared across every caller in the process
  so that four niche workers polling concurrently never exceed the configured
  requests/second. It is thread-safe because the watcher may run niches in
  threads.
* Transient failures (network errors, HTTP 429/5xx) are retried with capped
  **exponential backoff**. ``Retry-After`` is honoured when present.
* Responses are parsed into the typed models from :mod:`pmwatch.models` at this
  boundary so callers never deal with raw JSON.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from .config import ApiConfig
from .logging_conf import get_logger
from .models import Event, Market, Position, Trade

log = get_logger(__name__)


class RateLimiter:
    """A simple thread-safe token-bucket limiter.

    Tokens refill continuously at ``rate`` per second up to a small burst
    capacity. :meth:`acquire` blocks until a token is available. Sharing one
    instance across all workers caps the *aggregate* request rate, which is what
    the upstream API actually limits on.
    """

    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        self._rate = max(rate_per_sec, 0.1)
        self._capacity = burst if burst is not None else max(self._rate, 1.0)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request token is available, then consume it."""
        with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                # Refill proportionally to elapsed time, capped at capacity.
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Not enough; sleep just long enough for one token to accrue.
                deficit = 1.0 - self._tokens
                time.sleep(deficit / self._rate)


class PolymarketClient:
    """Thin, typed wrapper over the Polymarket Data + Gamma REST APIs."""

    def __init__(self, api: ApiConfig, *, rate_limiter: RateLimiter | None = None) -> None:
        self._api = api
        # Allow an externally-supplied limiter so multiple clients can share one;
        # otherwise create a private one from config.
        self._limiter = rate_limiter or RateLimiter(api.rate_limit_per_sec)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "pmwatch/0.1 (+read-only)"})

    # ------------------------------------------------------------------ #
    # Low-level request with shared rate limiting + exponential backoff.
    # ------------------------------------------------------------------ #
    def _get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """Perform a rate-limited GET with retries, returning parsed JSON."""
        url = f"{base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(self._api.max_retries + 1):
            self._limiter.acquire()
            try:
                resp = self._session.get(url, params=params, timeout=self._api.timeout_seconds)
            except requests.RequestException as exc:
                # Network-level failure (DNS, timeout, reset). Retryable.
                last_exc = exc
                self._sleep_backoff(attempt, reason=str(exc))
                continue

            # Rate limited or transient server error -> back off and retry.
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = _retry_after_seconds(resp)
                last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
                self._sleep_backoff(attempt, reason=f"HTTP {resp.status_code}", override=retry_after)
                continue

            if resp.status_code >= 400:
                # Client error (bad params, unknown wallet). Not retryable.
                raise RuntimeError(f"HTTP {resp.status_code} for {url}: {resp.text[:200]}")

            try:
                return resp.json()
            except ValueError as exc:
                raise RuntimeError(f"Non-JSON response from {url}: {resp.text[:200]}") from exc

        raise RuntimeError(f"GET {url} failed after {self._api.max_retries + 1} attempts") from last_exc

    def _sleep_backoff(self, attempt: int, *, reason: str, override: float | None = None) -> None:
        """Sleep with exponential backoff (2,4,8,16…s), or honour Retry-After."""
        delay = override if override is not None else min(2.0 * (2**attempt), 30.0)
        log.warning(
            "Retrying after backoff", extra={"extra_fields": {"reason": reason, "delay_s": delay, "attempt": attempt}}
        )
        time.sleep(delay)

    # ------------------------------------------------------------------ #
    # Data API — behavioural data we watch and score.
    # ------------------------------------------------------------------ #
    def get_trades(self, wallet: str, *, limit: int = 100, side: str | None = None) -> list[Trade]:
        """Recent trades for a wallet, newest first.

        ``GET /trades?user=<wallet>&limit=<n>``. Optionally filter to one side.
        """
        params: dict[str, Any] = {"user": wallet, "limit": limit}
        if side:
            params["side"] = side
        raw = self._get(self._api.data_base_url, "/trades", params)
        return [Trade.from_api(row) for row in _ensure_list(raw)]

    def get_positions(self, wallet: str, *, limit: int = 500) -> list[Position]:
        """All positions for a wallet (open + redeemable), used for scoring.

        ``GET /positions?user=<wallet>&limit=<n>``.
        """
        params = {"user": wallet, "limit": limit, "sortBy": "CASHPNL"}
        raw = self._get(self._api.data_base_url, "/positions", params)
        return [Position.from_api(row) for row in _ensure_list(raw)]

    def get_activity(
        self,
        wallet: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 500,
        activity_type: str = "TRADE",
    ) -> list[Trade]:
        """Time-bounded on-chain activity for a wallet (used for backtest/scoring).

        ``GET /activity?user=<wallet>&type=TRADE&start=<unix>&end=<unix>``.
        Trade-type activity maps onto the same :class:`Trade` model.
        """
        params: dict[str, Any] = {"user": wallet, "limit": limit, "type": activity_type}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        raw = self._get(self._api.data_base_url, "/activity", params)
        return [Trade.from_api(row) for row in _ensure_list(raw)]

    def get_holders(self, condition_id: str, *, limit: int = 50) -> list[str]:
        """Top holder wallet addresses for a market (candidate discovery).

        ``GET /holders?market=<conditionId>&limit=<n>``. The response groups
        holders by outcome token; we flatten to a de-duplicated address list.
        """
        params = {"market": condition_id, "limit": limit}
        raw = self._get(self._api.data_base_url, "/holders", params)
        addresses: list[str] = []
        for group in _ensure_list(raw):
            for holder in group.get("holders", []) if isinstance(group, dict) else []:
                addr = (holder.get("proxyWallet") or "").lower()
                if addr:
                    addresses.append(addr)
        # Preserve order but de-duplicate.
        return list(dict.fromkeys(addresses))

    # ------------------------------------------------------------------ #
    # Gamma API — market metadata.
    # ------------------------------------------------------------------ #
    def get_top_markets(self, *, limit: int = 100, closed: bool = False) -> list[Market]:
        """Highest-volume markets, used as the seed for niche discovery.

        ``GET /markets?closed=<bool>&order=volume&ascending=false&limit=<n>``.
        """
        params = {
            "closed": str(closed).lower(),
            "order": "volume",
            "ascending": "false",
            "limit": limit,
        }
        raw = self._get(self._api.gamma_base_url, "/markets", params)
        return [Market.from_api(row) for row in _ensure_list(raw)]

    def get_market(self, condition_id: str) -> Market | None:
        """Fetch a single market by condition id (for current-price lookups).

        ``GET /markets?condition_ids=<conditionId>``. Returns ``None`` if the
        market is not found.
        """
        params = {"condition_ids": condition_id}
        raw = self._get(self._api.gamma_base_url, "/markets", params)
        rows = _ensure_list(raw)
        return Market.from_api(rows[0]) if rows else None

    def get_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        closed: bool = False,
        order: str = "volume",
        ascending: bool = False,
    ) -> list[Event]:
        """A page of events (the category-bearing objects), used for discovery.

        ``GET /events?closed=&order=&ascending=&limit=&offset=``. Events carry the
        real category **tags** (a market's own top-level tags come back empty) and
        bundle their markets, so niche discovery is driven from here. Gamma caps
        ``limit`` at 100, so callers paginate via ``offset``. We pass ``order`` for
        convenience but also sort by volume locally, so discovery is correct even
        if the server-side sort is ignored.
        """
        params = {
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
            "limit": limit,
            "offset": offset,
        }
        raw = self._get(self._api.gamma_base_url, "/events", params)
        return [Event.from_api(row) for row in _ensure_list(raw)]


def _ensure_list(raw: Any) -> list[Any]:
    """Normalise an API payload to a list.

    Some endpoints return a bare list, others wrap rows under ``data``. This
    keeps callers from caring which.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), list):
            return raw["data"]
        return [raw]
    return []


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse a ``Retry-After`` header (seconds form) if the server sent one."""
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None

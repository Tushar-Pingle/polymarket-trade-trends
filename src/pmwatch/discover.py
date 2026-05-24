"""Candidate-wallet discovery per niche.

Before we can score and rank leaders, we need a pool of plausible candidates for
each niche. Polymarket has no "who's good at Politics" endpoint, so we bootstrap
it from public data — but the *right* way, learned from inspecting the live API:

* A market's own top-level ``tags`` come back empty from ``/markets``. The real
  category tags live on the **event** that groups related markets. So we iterate
  ``/events`` (which carry ``tags`` and bundle their markets).
* Gamma caps a page at 100 and its server-side volume sort is unreliable, so we
  **paginate via ``offset``** and **sort by volume locally**.
* For each event whose tags match the niche, we take its markets' condition ids,
  keep the highest-volume ones, and pull each market's **top holders** — the
  wallets with the largest positions are exactly the active, high-conviction
  players we want to evaluate.

These candidates then flow into :mod:`pmwatch.score` and :mod:`pmwatch.leaderboard`,
which decide which of them actually earn a spot on the board.
"""

from __future__ import annotations

from .client import PolymarketClient
from .config import DiscoveryConfig, Niche
from .logging_conf import get_logger
from .models import Event

log = get_logger(__name__)


def event_matches_niche(event: Event, niche: Niche) -> bool:
    """True if an event's tags (or title) match any of the niche's tags.

    Matching is case-insensitive and substring-based against the event's flattened
    tag labels/slugs, with the event title as a fallback. So a niche tag of
    "Politics" matches an event tagged "US Politics", and "Crypto" matches a
    "crypto" slug, etc.
    """
    haystack = " ".join(event.tags) + " " + event.title.lower()
    return any(tag.lower() in haystack for tag in niche.gamma_tags)


def discover_candidates(
    client: PolymarketClient,
    niche: Niche,
    cfg: DiscoveryConfig,
    *,
    event_pages: int | None = None,
) -> list[str]:
    """Return de-duplicated candidate wallet addresses for a niche.

    Parameters
    ----------
    event_pages:
        How many 100-event pages to scan before filtering to the niche. Defaults
        to ``cfg.event_pages``. More pages find more niche markets at the cost of
        more Gamma calls.
    """
    pages = event_pages if event_pages is not None else cfg.event_pages

    # Page through active events, keeping those that belong to this niche.
    matched: list[Event] = []
    for page in range(pages):
        events = client.get_events(limit=100, offset=page * 100, closed=False)
        if not events:
            break  # ran past the end of the event list
        matched.extend(e for e in events if event_matches_niche(e, niche))

    # Highest-volume events first (local sort; don't trust server-side ordering).
    matched.sort(key=lambda e: e.volume, reverse=True)

    # Flatten to a de-duplicated, volume-prioritised list of market condition ids.
    condition_ids: list[str] = []
    for event in matched:
        condition_ids.extend(event.market_condition_ids)
    condition_ids = list(dict.fromkeys(condition_ids))[: cfg.markets_per_niche]

    log.info(
        "Discovered niche markets",
        extra={"extra_fields": {"niche": niche.key, "events": len(matched), "markets": len(condition_ids)}},
    )

    # The top holders of those markets are our candidate leaders.
    candidates: list[str] = []
    for condition_id in condition_ids:
        candidates.extend(client.get_holders(condition_id, limit=cfg.holders_per_market))

    unique = list(dict.fromkeys(candidates))
    log.info(
        "Collected candidate wallets",
        extra={"extra_fields": {"niche": niche.key, "candidates": len(unique)}},
    )
    return unique

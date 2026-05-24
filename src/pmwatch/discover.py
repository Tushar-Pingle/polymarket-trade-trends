"""Candidate-wallet discovery per niche.

Before we can score and rank leaders, we need a pool of plausible candidates for
each niche. Polymarket has no "who's good at Politics" endpoint, so we bootstrap
it from public data:

1. Pull the highest-volume markets from the Gamma API.
2. Keep the ones whose tags/category match the niche (e.g. "Politics").
3. For each, ask the Data API for that market's **top holders** — the wallets
   with the largest positions are exactly the active, high-conviction players we
   want to evaluate.
4. De-duplicate the union of holders into a candidate list.

These candidates then flow into :mod:`pmwatch.score` and :mod:`pmwatch.leaderboard`,
which decide which of them actually earn a spot on the board.
"""

from __future__ import annotations

from .client import PolymarketClient
from .config import DiscoveryConfig, Niche
from .logging_conf import get_logger
from .models import Market

log = get_logger(__name__)


def market_matches_niche(market: Market, niche: Niche) -> bool:
    """True if a market's tags/category match any of the niche's tags.

    Matching is case-insensitive and substring-based so "US Politics" matches a
    "Politics" niche tag and vice-versa. The question text is also checked as a
    fallback because Gamma tag coverage is uneven across markets.
    """
    haystay = " ".join(market.tags).lower() + " " + market.question.lower()
    return any(tag.lower() in haystay for tag in niche.gamma_tags)


def discover_candidates(
    client: PolymarketClient,
    niche: Niche,
    cfg: DiscoveryConfig,
    *,
    market_pool: int = 200,
) -> list[str]:
    """Return de-duplicated candidate wallet addresses for a niche.

    Parameters
    ----------
    market_pool:
        How many top-volume markets to scan before filtering to the niche. A
        larger pool finds more niche markets at the cost of more Gamma calls.
    """
    # Scan a broad pool of high-volume markets, then keep the niche's.
    top_markets = client.get_top_markets(limit=market_pool, closed=False)
    niche_markets = [m for m in top_markets if market_matches_niche(m, niche)]
    niche_markets = niche_markets[: cfg.markets_per_niche]

    log.info(
        "Discovered niche markets",
        extra={"extra_fields": {"niche": niche.key, "matched": len(niche_markets)}},
    )

    candidates: list[str] = []
    for market in niche_markets:
        if not market.condition_id:
            continue
        holders = client.get_holders(market.condition_id, limit=cfg.holders_per_market)
        candidates.extend(holders)

    # Preserve discovery order while de-duplicating.
    unique = list(dict.fromkeys(candidates))
    log.info(
        "Collected candidate wallets",
        extra={"extra_fields": {"niche": niche.key, "candidates": len(unique)}},
    )
    return unique

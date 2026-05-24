"""The weekly ranking engine — builds each niche's in-house leaderboard.

This ties discovery + scoring together into the board/bench model you asked for:

* Every wallet we know about for a niche (current board, current bench, and
  freshly-discovered candidates) is **re-scored from scratch each week** using
  decay-weighted history. Re-scoring everyone is what makes the board *living*.
* Wallets are sorted by composite score. The top ``board_size`` form **the
  board** (the wallets the live watcher follows); the next ``bench_size`` are
  **retained on the bench**.
* Because benched wallets are re-scored every week, a wallet that was knocked
  off for losses or dumping can **climb back onto the board** if its recent
  trend recovers and it overtakes the current bottom-of-board — and a former
  top wallet that goes cold is **demoted to the bench**. The transitions fall
  straight out of "re-score everyone, then re-sort".

The ranker is intended to run weekly (cron or CI). It is read-only against
Polymarket; its only writes are the JSON leaderboard files via
:mod:`pmwatch.ledger`, which you then commit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .client import PolymarketClient
from .config import Config, Niche
from .discover import discover_candidates
from .ledger import Leaderboard, Ledger
from .logging_conf import get_logger
from .models import WalletScore
from .score import score_wallet

log = get_logger(__name__)


def _known_wallets(existing: Leaderboard) -> list[str]:
    """Addresses already tracked (board + bench) so they keep being re-scored."""
    return [s.wallet for s in existing.board] + [s.wallet for s in existing.bench]


def rank_niche(
    client: PolymarketClient,
    niche: Niche,
    ledger: Ledger,
    cfg: Config,
    *,
    max_wallets: int = 80,
    now: datetime | None = None,
    persist: bool = True,
) -> Leaderboard:
    """Re-score and re-rank one niche, returning (and optionally saving) the board.

    Parameters
    ----------
    max_wallets:
        Hard cap on how many distinct wallets we score in a single run, to bound
        API usage. Already-tracked wallets are prioritised so the bench is always
        refreshed; remaining budget goes to new candidates.
    persist:
        When True, write the JSON board + weekly snapshot via the ledger.
    """
    now = now or datetime.now(UTC)
    existing = ledger.load(niche.key)

    # Always keep re-scoring wallets we already track (this powers revival), then
    # top up with newly discovered candidates until we hit the budget.
    tracked = _known_wallets(existing)
    discovered = discover_candidates(client, niche, cfg.discovery)
    ordered = list(dict.fromkeys(tracked + discovered))[:max_wallets]

    log.info(
        "Ranking niche",
        extra={"extra_fields": {"niche": niche.key, "to_score": len(ordered), "tracked": len(tracked)}},
    )

    scores: list[WalletScore] = []
    for wallet in ordered:
        try:
            scores.append(score_wallet(client, wallet=wallet, niche=niche.key, cfg=cfg.leaderboard, now=now))
        except Exception as exc:  # one bad wallet must not abort the whole run
            log.warning("Failed to score wallet", extra={"extra_fields": {"wallet": wallet, "error": str(exc)}})

    # Highest composite score first; this single sort drives promotion/demotion.
    scores.sort(key=lambda s: s.score, reverse=True)

    board = scores[: cfg.leaderboard.board_size]
    bench = scores[cfg.leaderboard.board_size : cfg.leaderboard.board_size + cfg.leaderboard.bench_size]

    result = Leaderboard(niche=niche.key, board=board, bench=bench, updated_at=now)
    _log_transitions(existing, result)

    if persist:
        ledger.save(result)
    return result


def _log_transitions(old: Leaderboard, new: Leaderboard) -> None:
    """Log who joined and who was benched, for an at-a-glance weekly diff."""
    old_board = {s.wallet for s in old.board}
    new_board = {s.wallet for s in new.board}
    promoted = new_board - old_board
    demoted = old_board - new_board
    if promoted:
        log.info("Promoted to board", extra={"extra_fields": {"niche": new.niche, "wallets": sorted(promoted)}})
    if demoted:
        log.info("Demoted to bench", extra={"extra_fields": {"niche": new.niche, "wallets": sorted(demoted)}})


def rank_all(client: PolymarketClient, ledger: Ledger, cfg: Config, *, persist: bool = True) -> dict[str, Leaderboard]:
    """Rank every configured niche; returns niche-key -> resulting board."""
    results: dict[str, Leaderboard] = {}
    for key, niche in cfg.niches.items():
        results[key] = rank_niche(client, niche, ledger, cfg, persist=persist)
    return results

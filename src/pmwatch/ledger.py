"""Durable, version-controlled leaderboard storage (JSON in git).

Unlike the ephemeral SQLite runtime state, the leaderboard is the project's
long-lived, human-meaningful output and is deliberately stored as pretty-printed
JSON under ``data/`` so that:

* it is **diff-able** — each weekly re-rank produces a reviewable git diff
  showing exactly who moved up, who got benched, and who was revived;
* it is **portable** — the watcher on your server reads the same files, and you
  could serve them statically or load them elsewhere without a database; and
* it carries its own **audit trail** — a weekly snapshot is written under
  ``data/history/<niche>/<week>.json`` so trends (and the revival logic) have
  real history to work from.

File layout
-----------
``data/leaderboard/<niche>.json``::

    {
      "niche": "politics",
      "updated_at": "2026-05-24T00:00:00+00:00",
      "board":  [ <WalletScore json>, ... up to board_size ],
      "bench":  [ <WalletScore json>, ... up to bench_size ]
    }

``data/history/<niche>/<ISO-week>.json`` — same shape, one per weekly run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .logging_conf import get_logger
from .models import WalletScore

log = get_logger(__name__)


@dataclass
class Leaderboard:
    """In-memory view of one niche's board + bench."""

    niche: str
    board: list[WalletScore]  # actively-followed top wallets (rank 1..board_size)
    bench: list[WalletScore]  # retained beyond the board; eligible to climb back
    updated_at: datetime

    def rank_of(self, wallet: str) -> int | None:
        """1-based rank of a wallet on the board, or ``None`` if not on it."""
        addr = wallet.lower()
        for i, score in enumerate(self.board, start=1):
            if score.wallet == addr:
                return i
        return None

    def board_size(self) -> int:
        return len(self.board)

    def watched_wallets(self) -> list[str]:
        """Addresses the live watcher should follow for this niche."""
        return [s.wallet for s in self.board]


class Ledger:
    """Reads and writes the JSON leaderboard files under ``data_dir``."""

    def __init__(self, data_dir: str | Path) -> None:
        self._root = Path(data_dir)
        self._board_dir = self._root / "leaderboard"
        self._history_dir = self._root / "history"

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #
    def _board_path(self, niche: str) -> Path:
        return self._board_dir / f"{niche}.json"

    def _history_path(self, niche: str, week_id: str) -> Path:
        return self._history_dir / niche / f"{week_id}.json"

    # ------------------------------------------------------------------ #
    # Load
    # ------------------------------------------------------------------ #
    def load(self, niche: str) -> Leaderboard:
        """Load a niche's leaderboard, returning an empty one if none exists yet."""
        path = self._board_path(niche)
        if not path.is_file():
            return Leaderboard(niche=niche, board=[], bench=[], updated_at=datetime.now(UTC))
        raw = json.loads(path.read_text())
        return Leaderboard(
            niche=niche,
            board=[WalletScore.from_json(r) for r in raw.get("board", [])],
            bench=[WalletScore.from_json(r) for r in raw.get("bench", [])],
            updated_at=_parse_iso(raw.get("updated_at")),
        )

    def watched_wallets(self, niche: str) -> list[str]:
        """Convenience: the board addresses for a niche (used by the watcher)."""
        return self.load(niche).watched_wallets()

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #
    def save(self, board: Leaderboard, *, write_history: bool = True) -> None:
        """Persist the current board+bench and (optionally) a weekly snapshot."""
        self._board_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "niche": board.niche,
            "updated_at": board.updated_at.isoformat(),
            "board": [s.to_json() for s in board.board],
            "bench": [s.to_json() for s in board.bench],
        }
        # Pretty-print with sorted keys so diffs are stable and reviewable.
        self._board_path(board.niche).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        log.info(
            "Saved leaderboard",
            extra={"extra_fields": {"niche": board.niche, "board": len(board.board), "bench": len(board.bench)}},
        )

        if write_history:
            week_id = board.updated_at.strftime("%G-W%V")  # ISO year-week, e.g. 2026-W21
            hist_path = self._history_path(board.niche, week_id)
            hist_path.parent.mkdir(parents=True, exist_ok=True)
            hist_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _parse_iso(value: object) -> datetime:
    """Parse an ISO timestamp from stored JSON, defaulting to now() on failure."""
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)

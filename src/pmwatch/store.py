"""SQLite-backed *ephemeral runtime state* for the live watcher.

This store holds only machine-local, regenerable state and is therefore NOT
committed to git (see ``.gitignore``). It exists to make the watcher:

* **idempotent** — a trade already turned into an alert is never alerted twice,
  even across process restarts (``seen_trades``);
* **windowed** — recent trades are persisted (``window_trades``) so the
  consensus detector can evaluate "N distinct leaders within the last H hours"
  across many short polling cycles and across restarts; and
* **non-spammy** — fired consensus events are remembered (``fired_events``) so
  we escalate rather than repeat.

The durable, human-meaningful analytics (the leaderboard itself) live in JSON
under ``data/`` via :mod:`pmwatch.ledger`, not here.

All methods are safe to call from the per-niche worker threads: each call opens
its work against a single connection guarded by a lock. The volumes involved
(tens of wallets, hundreds of trades) make this more than fast enough.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .logging_conf import get_logger
from .models import Side, Trade

log = get_logger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_trades (
    trade_key  TEXT PRIMARY KEY,   -- tx_hash:asset:side, globally unique per fill
    niche      TEXT NOT NULL,
    wallet     TEXT NOT NULL,
    ts         INTEGER NOT NULL    -- unix seconds, for pruning
);

CREATE TABLE IF NOT EXISTS window_trades (
    trade_key      TEXT PRIMARY KEY,
    niche          TEXT NOT NULL,
    wallet         TEXT NOT NULL,
    condition_id   TEXT NOT NULL,
    outcome_index  INTEGER NOT NULL,
    side           TEXT NOT NULL,
    price          REAL NOT NULL,
    size           REAL NOT NULL,
    title          TEXT NOT NULL,
    slug           TEXT NOT NULL,
    ts             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_window_lookup
    ON window_trades (niche, condition_id, outcome_index, side, ts);

CREATE TABLE IF NOT EXISTS fired_events (
    event_key       TEXT PRIMARY KEY,  -- niche:condition:outcome:side
    wallet_count    INTEGER NOT NULL,  -- how many distinct leaders at last alert
    last_fired_ts   INTEGER NOT NULL
);
"""


def _trade_key(trade: Trade) -> str:
    """Stable unique key for a single fill (one wallet, one tx, one token, one side)."""
    return f"{trade.tx_hash}:{trade.asset}:{trade.side.value}"


def event_key(niche: str, condition_id: str, outcome_index: int, side: Side) -> str:
    """Stable key identifying a consensus opportunity (market+outcome+direction)."""
    return f"{niche}:{condition_id}:{outcome_index}:{side.value}"


class Store:
    """Connection-managing wrapper around the runtime SQLite database."""

    def __init__(self, sqlite_path: str | Path) -> None:
        self._path = Path(sqlite_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because worker threads share this instance;
        # a single lock serialises access (writes are tiny and infrequent).
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # Dedup: has this exact fill already been alerted on?
    # ------------------------------------------------------------------ #
    def mark_seen(self, niche: str, trade: Trade) -> bool:
        """Record a trade as seen. Returns True iff it was newly inserted.

        The boolean return is the dedup primitive: the watcher only emits a
        single-leader alert when ``mark_seen`` reports the trade is new.
        """
        key = _trade_key(trade)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen_trades (trade_key, niche, wallet, ts) VALUES (?, ?, ?, ?)",
                (key, niche, trade.wallet, int(trade.timestamp.timestamp())),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def has_seen_wallet(self, niche: str, wallet: str) -> bool:
        """True if we've ever recorded a trade for this wallet in this niche.

        Used for **cold-start seeding**: the very first time we poll a wallet we
        record its existing trades silently (no alerts) so a fresh deployment
        doesn't fire a burst of alerts about historical bets.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_trades WHERE niche = ? AND wallet = ? LIMIT 1",
                (niche, wallet.lower()),
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------ #
    # Rolling window of trades used by the consensus detector.
    # ------------------------------------------------------------------ #
    def record_window_trade(self, niche: str, trade: Trade) -> None:
        """Persist a trade into the rolling window (idempotent on trade_key)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO window_trades
                    (trade_key, niche, wallet, condition_id, outcome_index, side,
                     price, size, title, slug, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _trade_key(trade),
                    niche,
                    trade.wallet,
                    trade.condition_id,
                    trade.outcome_index,
                    trade.side.value,
                    trade.price,
                    trade.size,
                    trade.title,
                    trade.slug,
                    int(trade.timestamp.timestamp()),
                ),
            )
            self._conn.commit()

    def wallets_on(
        self,
        niche: str,
        condition_id: str,
        outcome_index: int,
        side: Side,
        *,
        since_ts: int,
    ) -> list[sqlite3.Row]:
        """Distinct wallets (with their earliest fill) on a market+outcome+side
        within the window. One row per wallet — the earliest trade for that
        wallet — so consensus counts each leader once and reports their entry.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT wallet,
                       MIN(ts)        AS ts,
                       price,
                       size,
                       title,
                       slug
                FROM window_trades
                WHERE niche = ? AND condition_id = ? AND outcome_index = ?
                      AND side = ? AND ts >= ?
                GROUP BY wallet
                ORDER BY ts ASC
                """,
                (niche, condition_id, outcome_index, side.value, since_ts),
            )
            return cur.fetchall()

    # ------------------------------------------------------------------ #
    # Consensus-event dedup / escalation bookkeeping.
    # ------------------------------------------------------------------ #
    def get_fired_count(self, key: str) -> int:
        """Distinct-wallet count at which this event was last alerted (0 if never)."""
        with self._lock:
            row = self._conn.execute("SELECT wallet_count FROM fired_events WHERE event_key = ?", (key,)).fetchone()
            return int(row["wallet_count"]) if row else 0

    def upsert_fired(self, key: str, wallet_count: int) -> None:
        """Record that we alerted ``key`` at ``wallet_count`` distinct wallets."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO fired_events (event_key, wallet_count, last_fired_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    wallet_count = excluded.wallet_count,
                    last_fired_ts = excluded.last_fired_ts
                """,
                (key, wallet_count, int(datetime.now(UTC).timestamp())),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Housekeeping.
    # ------------------------------------------------------------------ #
    def prune(self, *, older_than_ts: int) -> None:
        """Delete window/seen rows older than the cutoff to bound DB growth.

        ``fired_events`` is intentionally NOT pruned aggressively here; it is
        tiny and keeping it avoids re-firing an event that drops out of the
        window and comes back.
        """
        with self._lock:
            self._conn.execute("DELETE FROM window_trades WHERE ts < ?", (older_than_ts,))
            self._conn.execute("DELETE FROM seen_trades WHERE ts < ?", (older_than_ts,))
            self._conn.commit()

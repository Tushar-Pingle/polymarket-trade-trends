"""Tests for the HTTP client's rate limiting and pagination helpers.

Network is never touched here — we exercise the limiter directly and the
pagination loops via a monkeypatched page-fetcher.
"""

from __future__ import annotations

import threading
import time

from conftest import PROJECT_ROOT, make_position
from pmwatch.client import FileLockRateLimiter, PolymarketClient
from pmwatch.config import load_config


def _client() -> PolymarketClient:
    cfg = load_config(PROJECT_ROOT / "config.yaml", load_env=False)
    return PolymarketClient(cfg.api)


def test_file_lock_limiter_caps_combined_throughput(tmp_path) -> None:
    """Two threads sharing one file-lock bucket must not exceed the configured rate.

    With rate R and burst capacity C, N acquisitions can complete no faster than
    (N - C) / R seconds. We assert the elapsed time respects that floor, which is
    exactly the property the old per-process in-memory limiter violated.
    """
    rate = 50.0
    capacity = 1.0  # tiny burst so it's strictly rate-limited from the start
    state = tmp_path / "ratelimit.json"
    limiter = FileLockRateLimiter(state, rate, burst=capacity)

    per_thread = 20
    total = per_thread * 2

    def worker() -> None:
        for _ in range(per_thread):
            limiter.acquire()

    start = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    min_expected = (total - capacity) / rate
    # Combined throughput stayed under the cap (it took at least the floor time).
    assert elapsed >= min_expected * 0.9, f"too fast: {elapsed:.3f}s < floor {min_expected:.3f}s"
    observed_rate = total / elapsed
    assert observed_rate <= rate * 1.5, f"observed {observed_rate:.1f} rps exceeds cap {rate}"


def test_file_lock_limiter_resets_when_stale(tmp_path) -> None:
    """A state file older than stale_seconds resets the bucket to full (no starvation)."""
    state = tmp_path / "rl.json"
    # Pre-seed an exhausted bucket far in the past.
    state.write_text('{"tokens": 0.0, "updated": 1.0}')
    limiter = FileLockRateLimiter(state, rate_per_sec=1.0, burst=5.0, stale_seconds=60.0)
    # Should return immediately (reset to full capacity) rather than blocking ~1s.
    start = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - start < 0.5


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #
def test_get_all_positions_assembles_pages_and_stops_on_short(monkeypatch) -> None:
    client = _client()
    # 1200 positions across pages of 500 -> 500, 500, 200 (short page ends it).
    all_pos = [make_position(wallet="0xw", condition_id=f"m{i}") for i in range(1200)]

    def fake_get_positions(wallet, *, limit=500, offset=0):
        return all_pos[offset : offset + limit]

    monkeypatch.setattr(client, "get_positions", fake_get_positions)
    result = client.get_all_positions("0xw", page_size=500)
    assert len(result) == 1200


def test_get_all_positions_respects_hard_cap(monkeypatch, caplog) -> None:
    client = _client()

    # An "infinite" source: every page is full, so pagination only stops at the cap.
    def fake_get_positions(wallet, *, limit=500, offset=0):
        return [make_position(wallet="0xw", condition_id=f"m{offset}-{i}") for i in range(limit)]

    monkeypatch.setattr(client, "get_positions", fake_get_positions)
    import logging

    with caplog.at_level(logging.WARNING):
        result = client.get_all_positions("0xw", page_size=500, hard_cap=2000)
    assert len(result) == 2000  # capped
    assert any("hard cap" in r.message.lower() for r in caplog.records)


def test_get_all_activity_stops_at_empty_page(monkeypatch) -> None:
    client = _client()
    from conftest import make_trade

    pages = {0: [make_trade(wallet="0xw", minute=i) for i in range(500)], 500: []}  # second page empty

    def fake_get_activity(wallet, *, start=None, end=None, limit=500, offset=0, activity_type="TRADE"):
        return pages.get(offset, [])

    monkeypatch.setattr(client, "get_activity", fake_get_activity)
    result = client.get_all_activity("0xw", page_size=500)
    assert len(result) == 500

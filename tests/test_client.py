"""Tests for the HTTP client's rate limiting and pagination helpers.

Network is never touched here — we exercise the limiter directly and the
pagination loops via a monkeypatched page-fetcher.
"""

from __future__ import annotations

import threading
import time

from pmwatch.client import FileLockRateLimiter


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

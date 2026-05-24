"""pmwatch — Polymarket leader-wallet consensus watcher and alerting system.

This package is intentionally **read-only**: it consumes Polymarket's public
Data and Gamma APIs, builds an in-house per-niche leaderboard from on-chain
history, watches the leaders for new bets, and posts Discord alerts. It never
holds a wallet key and never places a trade.

Public sub-modules
-------------------
config        Strictly-validated configuration loaded from ``config.yaml``.
client        HTTP client for the Polymarket Data + Gamma APIs.
models        Frozen dataclasses describing trades, positions, markets, scores.
store         SQLite-backed *ephemeral* runtime state (dedup, consensus windows).
ledger        JSON-backed *durable* leaderboard + weekly history (committed to git).
discover      Candidate-wallet discovery per niche.
score         Per-wallet performance metrics from resolved-market history.
signals       Pre-resolution "dump" detection feeding trust scores.
leaderboard   Weekly scoring engine with bench / revival logic.
consensus     Rolling-window alert engine (single + consensus tiers).
watch         Per-niche continuous watcher loop.
backtest      Historical replay that proves which alerts *would* have fired.
discord        Discord embed formatting + delivery.
cli           Command-line entry point tying it all together.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]

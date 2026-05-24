# data/

This directory holds the **durable, version-controlled** analytics produced by the
weekly ranker (`pmwatch rank`):

- `leaderboard/<niche>.json` — the current board (top wallets followed live) plus the
  retained bench, with each wallet's score, trust, win-rate and sample size.
- `history/<niche>/<ISO-week>.json` — a weekly snapshot, giving the board a
  reviewable trend history in git (this is what powers bench/revival decisions).

These files are committed on purpose. The ephemeral runtime state (dedup,
consensus windows) lives in SQLite under `var/` and is gitignored.

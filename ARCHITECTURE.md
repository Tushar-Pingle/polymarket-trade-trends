# Architecture

This document explains how `pmwatch` is put together and why. It complements the
module-level docstrings (every module in `src/pmwatch/` opens with a "why this
exists" comment).

## Design principles

1. **Read-only, always.** The system only ever performs HTTP `GET`s against
   Polymarket and `POST`s to a Discord webhook. There is no signing key, no
   order placement, no funds at risk — by construction, not by policy.
2. **Pure core, I/O at the edges.** Business logic (scoring, dump detection, the
   alert engine) is free of network calls and takes plain data in / data out.
   Network and Discord live in dedicated modules. This is what makes the logic
   unit-testable and lets the **backtest reuse the exact same alert engine** as
   the live watcher.
3. **Two storage tiers.** Durable, human-meaningful analytics (the leaderboard)
   are versioned JSON in git; ephemeral runtime state (dedup, consensus windows)
   is local SQLite. Each is suited to its job and neither pollutes the other.
4. **Fail loud at the boundary, resilient in the loop.** Config is validated at
   startup; a bad wallet or a flaky request during a watch cycle is logged (and
   surfaced to Discord) without killing the loop.

## Data flow

### Weekly: building the board (`rank`)

```
discover.py            score.py                     leaderboard.py        ledger.py
──────────             ────────                     ─────────────         ────────
top markets   ┐
(Gamma) ──────┤
top holders   ├─▶ candidate wallets ─▶ per-wallet WalletScore ─▶ sort, take ─▶ data/leaderboard
(Data API) ───┘     (+ existing board/bench)   (PnL, ROI,         board+bench    /<niche>.json
                                                 decay, dumps)                   + weekly snapshot
```

* `discover.discover_candidates` pulls high-volume markets from Gamma, filters to
  the niche by tag, and unions the **top holders** of each as candidates.
* `score.score_from_data` turns a wallet's positions + activity into a composite,
  decay-weighted `WalletScore` (wins/losses, ROI, dump penalty, trust).
* `leaderboard.rank_niche` re-scores **everyone we already track plus the new
  candidates**, sorts, and splits into **board** (followed) and **bench**
  (retained). Because the bench is re-scored every week, demotion and **revival**
  fall straight out of the single re-sort.
* `ledger` writes pretty-printed JSON (stable key order → clean git diffs) plus a
  dated weekly snapshot under `data/history/`.

### Continuous: watching the leaders (`watch`)

```
watch.py (one loop per niche)                         consensus.py            discord.py
─────────────────────────────                         ────────────            ──────────
for each board wallet:
  get_trades (Data API)
  → dedup via store.mark_seen ─┐
                               ├─▶ AlertEngine.evaluate ─▶ [Alert...] ─▶ DiscordNotifier
  → record into rolling window ┘     single + consensus
     (store.window_trades)            + escalation/dedup
```

* Trades are processed **oldest-first** and **deduped** by a stable per-fill key,
  so overlap or a restart never double-alerts or skips a fill.
* On a wallet's **first** poll, its existing trades are seeded silently
  (cold-start) so a fresh deploy doesn't replay history as alerts.
* The **rolling window** of recent trades lives in SQLite, so a consensus that
  forms across many short polling cycles (or across a restart) is still detected.
* The `AlertEngine` is the only place alert semantics live; the watcher and the
  backtest both call it.

## The alert engine (`consensus.py`)

For each new, deduped, above-floor trade:

* **Single alert** (every bet): looks up the wallet's rank on the niche board and
  its trust score; colours by rank tier; emits an `Alert(kind="single")`.
* **Consensus alert**: counts *distinct* board wallets on the same
  `(market, outcome, side)` within `window_hours`. At/above `min_wallets` it
  emits `Alert(kind="consensus")` (or `"exit"` for SELLs). A `fired_events` table
  dedups it and only re-fires once `escalation_step` more leaders join.

`Alert` is a plain dataclass; rendering lives entirely in `discord.py`.

## The four-worker model

Each niche is watched by its **own process** (`pmwatch@<niche>` systemd
instance), so a slow or busy niche never delays another and a crash is isolated
and auto-restarted. All workers share a single **token-bucket rate limiter**
(`client.RateLimiter`) so the aggregate request rate to Polymarket stays within
budget regardless of how many niches run. For local/single-box use,
`watch --all --loop` runs the same per-niche loops as threads sharing one limiter.

## Storage tiers

| | Durable analytics | Runtime state |
| --- | --- | --- |
| Where | `data/*.json` (git) | `var/pmwatch.sqlite` (gitignored) |
| Holds | board, bench, scores, weekly history | seen-trade dedup, rolling window, fired events |
| Written by | `rank` (weekly) | `watch` (continuously) |
| Why | reviewable, portable, audit trail | fast, local, regenerable |

## Module map

| Module | Responsibility |
| --- | --- |
| `config.py` | Load + strictly validate `config.yaml` into typed dataclasses |
| `client.py` | Polymarket Data/Gamma HTTP client; shared rate limiter; backoff |
| `models.py` | Frozen domain types (`Trade`, `Position`, `Market`, `WalletScore`) |
| `discover.py` | Candidate-wallet discovery per niche |
| `score.py` | Per-wallet scoring from resolved-market history (pure + wrapper) |
| `signals.py` | Pre-resolution dump detection → trust score |
| `leaderboard.py` | Weekly ranker; board/bench; promotion/revival |
| `ledger.py` | Durable JSON leaderboard + weekly snapshots |
| `store.py` | SQLite runtime state (dedup, window, fired events) |
| `consensus.py` | Alert engine (single + consensus tiers) |
| `watch.py` | Per-niche continuous watcher loop + price resolver |
| `backtest.py` | Historical replay through the same engine |
| `discord.py` | Embed rendering + webhook delivery |
| `cli.py` | Command-line entry point |
| `logging_conf.py` | Structured logging setup |

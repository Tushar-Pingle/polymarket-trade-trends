# pmwatch — Polymarket leader-wallet consensus watcher

Follow smart Polymarket traders without staring at screens. `pmwatch` builds an
**in-house leaderboard** of the best wallets in each market niche, **watches**
those leaders continuously, and fires **color-coded Discord alerts** the moment
they place bets — a single-leader alert on *every* tracked bet, and a stronger
**consensus alert** when several leaders pile into the same market. You then
place the bet yourself.

It is **strictly read-only**: it consumes Polymarket's public APIs and posts to
Discord. It holds no wallet keys and places no trades. Nothing here can move
your money.

---

## A 60-second Polymarket primer

Polymarket is a **prediction market**. Each market is a yes/no question. You buy
`YES` or `NO` shares priced between 1¢ and 99¢ — the price *is* the crowd's
implied probability (60¢ ≈ 60% likely). On resolution the winning side pays
**$1/share** and the losing side pays **$0**. You can also **sell before
resolution** at the current price.

Because it runs on a public blockchain (Polygon), **every wallet's trades are
public** — which is exactly what makes leader-watching and copy-style alerting
possible.

Two realities this tool is built around:

1. **Copy lag.** By the time we observe a leader's bet, the price may have moved.
   Every alert shows the leader's entry price vs. the current price ("edge
   left") so you can judge whether it's still worth mirroring.
2. **The exit-liquidity trap.** A known whale can buy, let copycats pump the
   price, then dump before resolution and profit either way. `pmwatch` computes
   a **trust score** that flags chronic pre-resolution dumpers (so you weight
   their signals down) *and* can surface a "leaders are dumping" exit alert so
   you can ride the exit too.

> Polymarket is geo-restricted (it notably blocks US persons) and has its own
> terms of service. This tool only reads public data and notifies you; whether
> and how you trade is your responsibility.

---

## How it works

```
                    ┌──────────────────────────────────────────┐
   weekly           │  rank:  discover → score → bench/revive   │  writes versioned
   (cron / CI) ───▶ │         → data/leaderboard/<niche>.json    │  JSON (git)
                    └──────────────────────────────────────────┘
                                      │  board = wallets to follow
                                      ▼
   continuous       ┌──────────────────────────────────────────┐
   (4 systemd ────▶ │  watch:  poll trades → dedup → consensus   │  posts color-coded
    workers)        │          engine → Discord alerts           │  Discord embeds
                    └──────────────────────────────────────────┘
```

* **`rank`** (weekly) — discovers candidate wallets per niche (top holders of the
  niche's highest-volume markets), scores each from resolved-market history
  (wins/losses, ROI, recency decay, dump penalty), and writes a ranked **board**
  (followed) + **bench** (retained, can climb back) to `data/leaderboard/`.
* **`watch`** (always-on, one process per niche) — polls each board wallet's
  recent trades, dedups them, and runs them through the **alert engine**:
  * **single-leader alert** on every new bet, tagged with the wallet's
    leaderboard rank and colored by it (top ranks = act, low ranks = wait);
  * **consensus alert** when ≥ N distinct leaders hit the same market+outcome
    inside a rolling window, escalating as more pile in;
  * **exit alert** when leaders coordinate a SELL.
* **`backtest`** — replays real historical activity through the *same* engine to
  prove which alerts would have fired (your end-to-end sanity check).
* **`report`** — posts a weekly side-by-side comparison of the niches so you can
  pick where to lean.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and
[RUNBOOK.md](RUNBOOK.md) for day-to-day operations.

---

## Quick start

```bash
# 1. Install (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # drop [dev] for a runtime-only install

# 2. Configure Discord
cp .env.example .env             # then paste your Discord webhook URL into .env

# 3. Build the leaderboards (hits Polymarket's public API; commit the result)
python -m pmwatch.cli rank --all

# 4. See what would have happened recently (no Discord spam — prints to console)
python -m pmwatch.cli backtest --niche politics --since 7d

# 5. Run the live watcher for one niche (Ctrl-C to stop)
python -m pmwatch.cli watch --niche politics --loop

#    ...or all niches as threads in one process:
python -m pmwatch.cli watch --all --loop
```

---

## Commands

| Command | What it does | Typical use |
| --- | --- | --- |
| `rank --all` / `--niche X` | Rebuild the in-house leaderboard JSON | weekly (cron/CI) |
| `watch --niche X --loop` | Continuous live watcher for one niche | one systemd service per niche |
| `watch --all --loop` | All niches as threads in one process | single-box / local use |
| `watch --niche X` | One polling cycle then exit | cron-style / testing |
| `backtest --niche X --since 14d [--market <id>]` | Replay history; print alerts that would have fired | verification |
| `report` | Post the weekly cross-niche comparison to Discord | weekly |
| `discover --niche X` | List candidate wallets (inspection) | debugging |

Global flags: `--config <path>` (default `config.yaml`) and `--dry-run`
(render to logs instead of posting / persisting).

---

## Configuration

Everything tunable lives in [`config.yaml`](config.yaml) and is **validated at
startup** (a typo fails fast with a clear message). Highlights:

* `niches` — the baskets to track and the Gamma tags that bucket markets into
  them (ships with Politics, Crypto, Sports, Weather).
* `alerts.single` — single-leader alerting + the rank threshold that flips an
  alert from "wait" (muted) to "act" (warning) colour.
* `alerts.consensus` — `min_wallets` (default 3), `window_hours` (24),
  `escalation_step`, and whether coordinated SELLs alert.
* `leaderboard.scoring` — point weights for wins, losses, dumps and ROI, plus
  the recency `decay_half_life_days`.
* `poll` — how often each watcher polls and how many trades it pulls.

The only secret is the Discord webhook URL, read from `.env` (never committed).

---

## Reading an alert

* **Title** — `👤/🔥/🚪` (single / consensus / exit) + `🟢 BUY` or `🔴 SELL`, the
  outcome, and the market question.
* **Colour** — grey = single low-rank (watch), gold = single high-rank (act),
  orange = consensus, red = strong consensus or exit.
* **Price / edge** — leaders' average entry vs. the current price, so you can see
  how much room is left.
* **Per-wallet fields** — each involved leader's alias, **leaderboard rank**, entry
  price, size, and **trust** (low trust = history of pre-resolution dumps).
* **Footer** — niche + total notional the leaders put in.

---

## Deployment (your server)

Four independent watcher processes (one per niche) via the templated systemd
unit, plus a weekly `rank` cron. Full steps in [RUNBOOK.md](RUNBOOK.md); in short:

```bash
sudo cp systemd/pmwatch@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pmwatch@politics pmwatch@crypto pmwatch@sports pmwatch@weather

# weekly re-rank (crontab -e):
15 6 * * 1  /opt/polymarket-trade-trends/scripts/run_rank.sh >> /var/log/pmwatch-rank.log 2>&1
```

---

## Development

```bash
pip install -e ".[dev]"
ruff check src tests      # lint
black --check src tests   # format
mypy                      # types
pytest                    # tests (fully offline — fakes + fixtures)
```

CI runs all four on every push (`.github/workflows/ci.yml`).

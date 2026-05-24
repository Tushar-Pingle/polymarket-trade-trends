# Runbook

Operational guide for running `pmwatch` on a server. Everything here is
read-only against Polymarket; the only outbound write is the Discord webhook.

## Prerequisites

* Python 3.11+
* A Discord **Incoming Webhook** URL (Server Settings → Integrations → Webhooks)
* Outbound network access to `*.polymarket.com` and `discord.com`

## First-time setup

```bash
git clone <your-fork> /opt/polymarket-trade-trends
cd /opt/polymarket-trade-trends
python -m venv .venv && source .venv/bin/activate
pip install -e .                       # runtime only

cp .env.example .env
# edit .env: set DISCORD_WEBHOOK_URL=...

# Sanity check Discord wiring without touching Polymarket:
python -m pmwatch.cli --dry-run report     # prints the embed it WOULD send
python -m pmwatch.cli report               # actually posts (boards may be empty)
```

## Build the leaderboards (required before watching)

```bash
python -m pmwatch.cli rank --all
git add data/ && git commit -m "leaderboard: initial rank"
```

The watcher follows whatever is on each niche's **board**, so `rank` must run at
least once first. Re-run weekly (see below).

## Run the live watchers (one process per niche)

Install the templated systemd unit and enable one instance per niche:

```bash
sudo cp systemd/pmwatch@.service /etc/systemd/system/
# Edit User= and the two paths in the unit to match your deployment.
sudo systemctl daemon-reload
sudo systemctl enable --now pmwatch@politics pmwatch@crypto pmwatch@sports pmwatch@weather
```

Inspect / follow:

```bash
systemctl status pmwatch@politics
journalctl -u pmwatch@crypto -f
```

> Prefer one box, one process? `python -m pmwatch.cli watch --all --loop` runs
> every niche as threads under a single shared rate limiter.

## Schedule the weekly re-rank

`scripts/run_rank.sh` re-ranks all niches, posts the weekly report, and commits
the refreshed `data/` JSON. Add to crontab (`crontab -e`):

```cron
15 6 * * 1  /opt/polymarket-trade-trends/scripts/run_rank.sh >> /var/log/pmwatch-rank.log 2>&1
```

(Uncomment the `git push` line in the script if you want the commit pushed.)
Alternatively, enable `.github/workflows/weekly-rank.yml` to run it in CI.

## Verify it works end-to-end (backtest)

Pick a recently-resolved market and confirm the system *would* have alerted on it:

```bash
# Find candidates / a market id you care about, then:
python -m pmwatch.cli backtest --niche politics --since 30d
python -m pmwatch.cli backtest --niche crypto --since 30d --market <conditionId>
```

The output lists every alert (single / consensus / exit) that would have fired,
with timestamps, the wallets involved (and their ranks), and the guidance note —
proving the live path without spamming Discord. Add `--send` to also post them.

## Routine operations

| Task | Command |
| --- | --- |
| Restart a niche watcher | `sudo systemctl restart pmwatch@crypto` |
| Stop everything | `sudo systemctl stop 'pmwatch@*'` |
| Tail logs | `journalctl -u pmwatch@politics -f` |
| Force a re-rank now | `python -m pmwatch.cli rank --all` |
| Add a wallet alias | edit `wallet_aliases` in `config.yaml`, restart watcher |
| Change consensus threshold | edit `alerts.consensus.min_wallets`, restart |
| Switch logs to JSON | set `logging.json: true` in `config.yaml` |

## Troubleshooting

* **"No wallets on the board yet — run `rank` first"** — the niche's
  `data/leaderboard/<niche>.json` is empty/missing. Run `rank`.
* **No alerts ever** — on first start each wallet is cold-start-seeded silently;
  alerts begin on the *next* new bet. Confirm with a `backtest` over recent days.
* **`HTTP 403 ... Host not in allowlist`** — your network egress is restricted;
  allow `*.polymarket.com`. (This is also why the test suite is fully offline.)
* **Discord 429s** — the notifier honours `Retry-After`; sustained 429s mean too
  many alerts — raise `alerts.min_trade_size_usd` or `consensus.min_wallets`.
* **Config error on startup** — the message names the exact `config.yaml` key;
  fix and restart.
* **SQLite grows** — the watcher auto-prunes window/seen rows older than the
  consensus window (+1 day) every hour; the DB stays small.

## What's intentionally NOT here

* No private keys, no trading, no auto-execution. `pmwatch` notifies; you decide
  and place bets yourself.

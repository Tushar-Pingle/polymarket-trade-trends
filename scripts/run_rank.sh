#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Weekly ranker wrapper: rebuilds every niche's leaderboard JSON and commits the
# result so the board history is versioned. Intended for a weekly cron entry,
# e.g. (crontab):
#   15 6 * * 1  /path/to/repo/scripts/run_rank.sh >> /var/log/pmwatch-rank.log 2>&1
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

# 1) Recompute all leaderboards (writes data/leaderboard/*.json + weekly snapshot).
python -m pmwatch.cli rank --all

# 2) Post the weekly comparison to Discord.
python -m pmwatch.cli report || true

# 3) Commit the refreshed leaderboard so the trend history is preserved in git.
#    (Only commits if something actually changed.)
if [[ -n "$(git status --porcelain data/)" ]]; then
  git add data/
  git commit -m "chore(leaderboard): weekly re-rank $(date -u +%G-W%V)"
  # Pushing is left to the operator's environment/credentials; uncomment if desired:
  # git push origin "$(git rev-parse --abbrev-ref HEAD)"
fi

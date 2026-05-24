#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Wrapper that activates the project virtualenv and runs the live watcher for a
# single niche in continuous mode. Used by the systemd unit (systemd/pmwatch@.service)
# and usable standalone, e.g.:  ./scripts/run_watch.sh politics
# ---------------------------------------------------------------------------
set -euo pipefail

NICHE="${1:?usage: run_watch.sh <niche-key>}"

# Resolve the project root from this script's location so it works under systemd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# Activate the venv if present (created via: python -m venv .venv && pip install -e .).
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

exec python -m pmwatch.cli watch --niche "${NICHE}" --loop

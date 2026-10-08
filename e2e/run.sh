#!/usr/bin/env bash
# Run seer's end-to-end scenarios against the code in this checkout.
#
#   e2e/run.sh                                  all scenarios, provider from your config
#   e2e/run.sh -p claude-cli -m sonnet --repeat 3
#   e2e/run.sh --only brave -v                  one feature, events as they happen
#   e2e/run.sh --only brave-big-log -vv         one scenario, its terminal live
#   e2e/run.sh --list
#
# Arguments go to e2e/run.py (see --help). Results: e2e/results/<label>/<timestamp>/.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# The seer under test is .venv/bin/seer: make sure it matches the code here.
if command -v uv >/dev/null 2>&1; then
  uv sync --quiet
  # uv sync checks package metadata, not scripts: restore a deleted entry point.
  [ -x .venv/bin/seer ] || uv sync --quiet --reinstall-package seer-ai
elif [ ! -x .venv/bin/seer ]; then
  echo "No .venv/bin/seer and no uv to create it — install uv, or run: python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

exec .venv/bin/python e2e/run.py "$@"

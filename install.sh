#!/usr/bin/env bash
# seer installer bootstrapper
#
# Usage:
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh)"
#
#   Silent / Non-interactive:
#   curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh | bash -s -- --all
#
#   Specific version (tag or branch):
#   curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh | bash -s -- --version v1.1.0

set -euo pipefail

# Parse --version from args; everything else is passed through to setup.sh
SEER_VERSION=""
PASSTHROUGH_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      SEER_VERSION="${2:-}"
      shift 2
      ;;
    --version=*)
      SEER_VERSION="${1#--version=}"
      shift
      ;;
    *)
      PASSTHROUGH_ARGS+=("$1")
      shift
      ;;
  esac
done

# If running locally from the repository, delegate directly to the local setup.sh
if [ -f "$(dirname "$0")/shell/setup.sh" ]; then
    exec "$(dirname "$0")/shell/setup.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"
fi

REPO_BASE="https://raw.githubusercontent.com/zeddius1983/seer"
if [ -n "$SEER_VERSION" ]; then
  REPO_URL="$REPO_BASE/$SEER_VERSION"
else
  REPO_URL="${REPO_URL:-$REPO_BASE/main}"
fi

TMP_DIR="$(mktemp -d)"

# Cleanup on exit
trap 'rm -rf "$TMP_DIR"' EXIT

echo "==> Downloading SEER Installer..."
mkdir -p "$TMP_DIR/shell"

curl -sSLo "$TMP_DIR/shell/setup.sh" "$REPO_URL/shell/setup.sh"
chmod +x "$TMP_DIR/shell/setup.sh"

REPO_URL="$REPO_URL" "$TMP_DIR/shell/setup.sh" "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

API_URL="${API_URL:-http://localhost:8000}"
API_USER="${API_USER:-admin}"
API_PASS="${API_PASS:-admin}"
BOT_NAME="${BOT_NAME:-}"
BOT_PREFIX="${BOT_PREFIX:-hl-testnet-pmm-btc}"

ARGS=(
  --repo-root "${REPO_ROOT}"
  --api-url "${API_URL}"
  --api-user "${API_USER}"
  --api-pass "${API_PASS}"
  --bot-prefix "${BOT_PREFIX}"
)

if [ -n "${BOT_NAME}" ]; then
  ARGS+=(--bot-name "${BOT_NAME}")
fi

python3 "${SCRIPT_DIR}/monitor_pmm_bot.py" "${ARGS[@]}" "$@"

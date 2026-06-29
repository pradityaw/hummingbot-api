#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
API_USER="${API_USER:-admin}"
API_PASS="${API_PASS:-admin}"
BOT_NAME="${BOT_NAME:-hl-testnet-pmm-btc-20260618-081557}"
DOCKER_BIN="${DOCKER_BIN:-docker}"

if [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin/docker"
fi

curl_api() {
  curl -sS -u "${API_USER}:${API_PASS}" "$@"
}

echo "API root"
curl_api "${API_URL}/"
echo

echo "Containers"
"${DOCKER_BIN}" ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' \
  | grep -E '^(NAMES|hummingbot-api|hummingbot-broker|hummingbot-postgres|'"${BOT_NAME}"')'
echo

echo "Bot health"
curl_api "${API_URL}/bot-orchestration/${BOT_NAME}/health"
echo

echo "Bot active orders"
curl_api "${API_URL}/bot-orchestration/${BOT_NAME}/orders?active_only=true&limit=10&event_limit=10"
echo

echo "Recent bot logs"
"${DOCKER_BIN}" logs --tail 80 "${BOT_NAME}"

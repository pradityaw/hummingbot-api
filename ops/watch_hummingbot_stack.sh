#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
API_URL="${API_URL:-http://localhost:8000}"
API_USER="${API_USER:-admin}"
API_PASS="${API_PASS:-admin}"
HYPERLIQUID_INFO_URL="${HYPERLIQUID_INFO_URL:-https://api.hyperliquid-testnet.xyz/info}"
AUTO_START_DOCKER="${AUTO_START_DOCKER:-false}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
REPAIR=false
JSON=false
NO_FAIL=false

if [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  DOCKER_BIN="/Applications/Docker.app/Contents/Resources/bin/docker"
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repair)
      REPAIR=true
      ;;
    --json)
      JSON=true
      ;;
    --no-fail)
      NO_FAIL=true
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

COMPOSE_FILES=(-f docker-compose.yml)
if [ -f "$REPO_ROOT/docker-compose.hyperliquid-fix.yml" ]; then
  COMPOSE_FILES+=(-f docker-compose.hyperliquid-fix.yml)
fi

status="ok"
actions=()
checks=()

add_check() {
  checks+=("$1=$2")
  if [ "$2" != "ok" ]; then
    status="degraded"
  fi
}

repair_compose() {
  if [ "$REPAIR" = true ]; then
    (
      cd "$REPO_ROOT"
      "$DOCKER_BIN" compose "${COMPOSE_FILES[@]}" up -d hummingbot-api emqx postgres
    )
    actions+=("compose_up_core_services")
  fi
}

if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  add_check "docker" "down"
  if [ "$REPAIR" = true ] && [ "$AUTO_START_DOCKER" = true ] && command -v open >/dev/null 2>&1; then
    open -ga Docker || true
    actions+=("open_docker_desktop")
  fi
  if [ "$JSON" = true ]; then
    printf '{"status":"degraded","checks":{"docker":"down"},"actions":['
    if [ "${#actions[@]}" -gt 0 ]; then
      first=true
      for action in "${actions[@]}"; do
        if [ "$first" = true ]; then first=false; else printf ','; fi
        printf '"%s"' "$action"
      done
    fi
    printf '],"message":"Docker is not reachable. Start Docker Desktop before repair can run."}\n'
  else
    echo "status=degraded"
    echo "docker=down"
    if [ "${#actions[@]}" -gt 0 ]; then
      printf 'actions=%s\n' "$(IFS=,; echo "${actions[*]}")"
    else
      echo "actions=none"
    fi
    echo "message=Docker is not reachable. Start Docker Desktop before repair can run."
  fi
  if [ "$NO_FAIL" = true ]; then
    exit 0
  fi
  exit 1
fi
add_check "docker" "ok"

for container in hummingbot-postgres hummingbot-broker hummingbot-api; do
  if "$DOCKER_BIN" inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -qx true; then
    add_check "$container" "ok"
  else
    add_check "$container" "down"
  fi
done

if ! curl -fsS --max-time 5 "$API_URL/" >/dev/null 2>&1; then
  add_check "api_root" "down"
  repair_compose
else
  add_check "api_root" "ok"
fi

if ! curl -fsS --max-time 8 -u "${API_USER}:${API_PASS}" "$API_URL/bot-orchestration/mqtt" >/dev/null 2>&1; then
  add_check "api_mqtt_route" "down"
  repair_compose
else
  add_check "api_mqtt_route" "ok"
fi

if ! curl -fsS --max-time 8 \
  -H 'Content-Type: application/json' \
  -d '{"type":"meta"}' \
  "$HYPERLIQUID_INFO_URL" >/dev/null 2>&1; then
  add_check "hyperliquid_testnet_info" "down"
else
  add_check "hyperliquid_testnet_info" "ok"
fi

if [ "$JSON" = true ]; then
  printf '{"status":"%s","checks":{' "$status"
  first=true
  for check in "${checks[@]}"; do
    key="${check%%=*}"
    value="${check#*=}"
    if [ "$first" = true ]; then first=false; else printf ','; fi
    printf '"%s":"%s"' "$key" "$value"
  done
  printf '},"actions":['
  if [ "${#actions[@]}" -gt 0 ]; then
    first=true
    for action in "${actions[@]}"; do
      if [ "$first" = true ]; then first=false; else printf ','; fi
      printf '"%s"' "$action"
    done
  fi
  printf ']}\n'
else
  echo "status=$status"
  for check in "${checks[@]}"; do
    echo "$check"
  done
  if [ "${#actions[@]}" -gt 0 ]; then
    printf 'actions=%s\n' "$(IFS=,; echo "${actions[*]}")"
  else
    echo "actions=none"
  fi
fi

if [ "$status" = "ok" ]; then
  exit 0
fi
if [ "$NO_FAIL" = true ]; then
  exit 0
fi
exit 1

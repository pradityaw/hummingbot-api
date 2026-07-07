#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-root@168.144.111.10}"
REMOTE_REPO="${REMOTE_REPO:-/root/hummingbot-api}"
LOG_FILE="${LOG_FILE:-/Users/dubski/hummingbot-api/ops/resume-ready-notify.log}"
STATE_FILE="${STATE_FILE:-/Users/dubski/hummingbot-api/ops/resume-ready-notify.state}"
POLL_SECONDS="${POLL_SECONDS:-30}"

mkdir -p "$(dirname "$LOG_FILE")"

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

notify_ready() {
  local message="$1"
  /usr/bin/osascript -e "display notification \"$message\" with title \"Hyperliquid Canary Ready\" subtitle \"VPS resume gate flipped\"" >/dev/null 2>&1 || true
}

echo "$(timestamp) watcher_started host=${REMOTE_HOST}" >> "$LOG_FILE"

while true; do
  output="$(
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE_HOST" \
      "cd '$REMOTE_REPO' && ops/check_hyperliquid_resume_ready.sh" 2>&1 || true
  )"

  echo "$(timestamp) poll_result $(printf '%q' "$output")" >> "$LOG_FILE"

  # Anchored match: plain 'RESUME_READY' also matches the RESUME_NOT_READY line.
  if printf '%s\n' "$output" | grep -qx 'RESUME_READY'; then
    printf '%s\n' "$(timestamp) ready" > "$STATE_FILE"
    notify_ready "Passive probes and watchdog are clean. Safe to start the canary."
    echo "$(timestamp) watcher_finished ready" >> "$LOG_FILE"
    exit 0
  fi

  sleep "$POLL_SECONDS"
done

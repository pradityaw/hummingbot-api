#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
API_URL="${API_URL:-http://127.0.0.1:8000}"
API_USER="${API_USER:-admin}"
API_PASS="${API_PASS:-admin}"
BOT_NAME="${BOT_NAME:-hl-testnet-pmm-20260628-185328}"
BOT_PREFIX="${BOT_PREFIX:-hl-testnet-pmm-btc}"
STATE_DIR="${STATE_DIR:-/var/lib/hummingbot-hl-watchdog}"
STATUS_DIR="${STATUS_DIR:-$REPO_ROOT/ops/watchdog-status}"
WINDOW_SECONDS="${WINDOW_SECONDS:-1800}"
DISCONNECT_THRESHOLD="${DISCONNECT_THRESHOLD:-3}"
HARD_DISCONNECT_THRESHOLD="${HARD_DISCONNECT_THRESHOLD:-3}"
BENIGN_DISCONNECT_THRESHOLD="${BENIGN_DISCONNECT_THRESHOLD:-12}"
EXCHANGE_5XX_THRESHOLD="${EXCHANGE_5XX_THRESHOLD:-3}"
OPEN_ORDER_FAILED_THRESHOLD="${OPEN_ORDER_FAILED_THRESHOLD:-10}"
QUOTE_GAP_SECONDS="${QUOTE_GAP_SECONDS:-300}"
STUCK_RECOVERING_SECONDS="${STUCK_RECOVERING_SECONDS:-1800}"
STALLED_RUNTIME_SECONDS="${STALLED_RUNTIME_SECONDS:-180}"
STARTUP_GRACE_SECONDS="${STARTUP_GRACE_SECONDS:-180}"
HYPERLIQUID_INFO_URL="${HYPERLIQUID_INFO_URL:-https://api.hyperliquid-testnet.xyz/info}"
HEALTH_JSON_PATH="${HEALTH_JSON_PATH:-}"
ORDERS_JSON_PATH="${ORDERS_JSON_PATH:-}"
CONNECTIVITY_JSON_PATH="${CONNECTIVITY_JSON_PATH:-}"
LOG_PATH_OVERRIDE="${LOG_PATH_OVERRIDE:-}"
HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE="${HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE:-}"
NOW_EPOCH_OVERRIDE="${NOW_EPOCH_OVERRIDE:-}"
SKIP_STATUS_ARCHIVE="${SKIP_STATUS_ARCHIVE:-false}"
DRY_RUN=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

mkdir -p "$STATE_DIR" "$STATUS_DIR"
chmod 700 "$STATE_DIR"

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'
}

array_to_json() {
  python3 -c 'import json,sys; print(json.dumps([item for item in sys.argv[1:] if item]))' "$@"
}

join_or_none() {
  if [ "$#" -eq 0 ]; then
    printf 'none'
  else
    printf '%s' "$1"
    shift
    for item in "$@"; do
      printf ' %s' "$item"
    done
  fi
}

json_get() {
  local file_path="$1"
  local expr="$2"
  # Expressions are hard-coded by this script; do not pass user input here.
  python3 - "$file_path" "$expr" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expr = sys.argv[2]
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    payload = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)
namespace = {"payload": payload}
try:
    value = eval(expr, {"__builtins__": {}}, namespace)
except Exception:
    value = ""
if isinstance(value, bool):
    print(str(value).lower())
elif value is None:
    print("")
else:
    print(value)
PY
}

api_get() {
  local path="$1"
  if [ "$path" = "/bot-orchestration/${bot_name}/health" ] && [ -n "$HEALTH_JSON_PATH" ]; then
    cat "$HEALTH_JSON_PATH"
    return 0
  fi
  if [ "$path" = "/bot-orchestration/${bot_name}/orders?active_only=true&limit=20&event_limit=20" ] && [ -n "$ORDERS_JSON_PATH" ]; then
    cat "$ORDERS_JSON_PATH"
    return 0
  fi
  if [ "$path" = "/bot-orchestration/${bot_name}/connectivity" ] && [ -n "$CONNECTIVITY_JSON_PATH" ]; then
    cat "$CONNECTIVITY_JSON_PATH"
    return 0
  fi
  curl -fsS -m 12 -u "${API_USER}:${API_PASS}" "${API_URL%/}${path}"
}

api_post_json() {
  local path="$1"
  local body="$2"
  curl -fsS -m 20 -u "${API_USER}:${API_PASS}" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "${API_URL%/}${path}"
}

resolve_bot_name() {
  if [ -n "$BOT_NAME" ]; then
    printf '%s\n' "$BOT_NAME"
    return 0
  fi
  api_get "/bot-orchestration/status" | python3 -c 'import json, sys
prefix = sys.argv[1]
data = json.load(sys.stdin).get("data", {})
candidates = sorted(name for name in data if name.startswith(prefix))
print(candidates[-1] if candidates else "")' "$BOT_PREFIX"
}

if [ -n "$NOW_EPOCH_OVERRIDE" ]; then
  now_epoch="$NOW_EPOCH_OVERRIDE"
else
  now_epoch="$(date -u +%s)"
fi

bot_name="$(resolve_bot_name)"
if [ -z "$bot_name" ]; then
  echo "No bot name could be resolved" >&2
  exit 1
fi

health_json="$(api_get "/bot-orchestration/${bot_name}/health" || true)"
orders_json="$(api_get "/bot-orchestration/${bot_name}/orders?active_only=true&limit=20&event_limit=20" || true)"
connectivity_json="$(api_get "/bot-orchestration/${bot_name}/connectivity" || true)"
api_ok=true
if [ -z "$health_json" ] || [ -z "$orders_json" ]; then
  api_ok=false
fi

runtime_connectivity_ok=false
runtime_bot_name=""
runtime_state="UNKNOWN"
runtime_reason="runtime_connectivity_unavailable"
runtime_readiness_reason="runtime_connectivity_unavailable"
runtime_watchdog_reason="runtime_connectivity_unavailable"
runtime_reconciliation_result="unknown"
runtime_quoting_enabled=false
runtime_orders_unknown=false
runtime_public_ws_status="unknown"
runtime_private_ws_status="unknown"
runtime_rest_health="unknown"
runtime_last_order_book_update_timestamp=""
runtime_last_user_stream_update_timestamp=""
runtime_reconnect_attempt_count=0
runtime_reconnect_duration=""
runtime_ws_close_code=""
runtime_ws_close_reason=""
runtime_open_order_count_during_disconnect=0
if [ -n "$connectivity_json" ]; then
  runtime_payload_file="$(mktemp)"
  printf '%s' "$connectivity_json" > "$runtime_payload_file"
  runtime_bot_name="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("bot_name", payload.get("bot_name", ""))')"
  runtime_state="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("current_state", "UNKNOWN")')"
  runtime_reason="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("reason", "runtime_connectivity_unavailable")')"
  runtime_readiness_reason="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("readiness_reason", "runtime_connectivity_unavailable")')"
  runtime_watchdog_reason="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("watchdog_reason", "runtime_connectivity_unavailable")')"
  runtime_reconciliation_result="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("reconciliation_result", "unknown")')"
  runtime_quoting_enabled="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("quoting_enabled", False)')"
  runtime_orders_unknown="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("orders_unknown", False)')"
  runtime_public_ws_status="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("public_ws_status", "unknown")')"
  runtime_private_ws_status="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("private_user_ws_status", "unknown")')"
  runtime_rest_health="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("rest_health", "unknown")')"
  runtime_last_order_book_update_timestamp="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("last_order_book_update_timestamp", "")')"
  runtime_last_user_stream_update_timestamp="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("last_user_stream_update_timestamp", "")')"
  runtime_reconnect_attempt_count="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("reconnect_attempt_count", 0)')"
  runtime_reconnect_duration="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("reconnect_duration", "")')"
  runtime_ws_close_code="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("ws_close_code", "")')"
  runtime_ws_close_reason="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("ws_close_reason", "")')"
  runtime_open_order_count_during_disconnect="$(json_get "$runtime_payload_file" 'payload.get("data", {}).get("connectivity", payload.get("connectivity", {})).get("open_order_count_during_disconnect", 0)')"
  rm -f "$runtime_payload_file"
  case "$runtime_state" in
    HEALTHY|DEGRADED_TRANSIENT|DEGRADED_UNSAFE|HARD_DISCONNECTED|RECOVERING)
      runtime_connectivity_ok=true
      ;;
  esac
  if [ -n "$runtime_bot_name" ] && [ "$runtime_bot_name" != "$bot_name" ]; then
    runtime_connectivity_ok=false
    runtime_reason="runtime_bot_name_mismatch"
    runtime_readiness_reason="runtime_bot_name_mismatch"
    runtime_watchdog_reason="runtime_bot_name_mismatch"
  fi
fi

active_order_count=0
recent_order_create_count=0
latest_order_event_epoch=0
bot_status="unknown"
recently_active=false
if [ "$api_ok" = true ]; then
  active_order_count="$(printf '%s' "$orders_json" | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("data", {}).get("active_order_count", 0))
except Exception:
    print(0)')"
  bot_status="$(printf '%s' "$health_json" | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("data", {}).get("bot_status", "unknown"))
except Exception:
    print("unknown")')"
  recently_active="$(printf '%s' "$health_json" | python3 -c 'import json, sys
try:
    print(str(json.load(sys.stdin).get("data", {}).get("recently_active", False)).lower())
except Exception:
    print("false")')"
fi

activation_file="$STATE_DIR/activation_epoch"
last_status_file="$STATE_DIR/last_bot_status"
last_bot_file="$STATE_DIR/last_bot_name"
quote_gap_file="$STATE_DIR/quote_gap_started_epoch"
stuck_recovering_file="$STATE_DIR/stuck_recovering_started_epoch"
stop_state_file="$STATE_DIR/last_stop_state.json"

previous_bot_status=""
previous_bot_name=""
if [ -f "$last_status_file" ]; then
  previous_bot_status="$(cat "$last_status_file")"
fi
if [ -f "$last_bot_file" ]; then
  previous_bot_name="$(cat "$last_bot_file")"
fi

activation_epoch=""
if [ -f "$activation_file" ]; then
  activation_epoch="$(cat "$activation_file")"
fi
if [ -z "$activation_epoch" ] || [ "$previous_bot_name" != "$bot_name" ] || { [ "$bot_status" = "running" ] && [ "$previous_bot_status" != "running" ]; }; then
  activation_epoch="$now_epoch"
  printf '%s\n' "$activation_epoch" > "$activation_file"
  rm -f "$quote_gap_file" "$stuck_recovering_file" "$stop_state_file"
fi
printf '%s\n' "$bot_status" > "$last_status_file"
printf '%s\n' "$bot_name" > "$last_bot_file"

window_start=$((now_epoch - WINDOW_SECONDS))
if [ "$window_start" -lt "$activation_epoch" ]; then
  window_start="$activation_epoch"
fi

if [ "$api_ok" = true ]; then
  recent_order_create_count="$(printf '%s' "$orders_json" | python3 -c 'import json, sys
window_start = float(sys.argv[1])
try:
    events = json.load(sys.stdin).get("data", {}).get("recent_order_events", [])
except Exception:
    events = []
count = 0
for event in events:
    if float(event.get("timestamp") or 0) < window_start:
        continue
    if event.get("event_name") in {"BuyOrderCreatedEvent", "SellOrderCreatedEvent"}:
        count += 1
print(count)' "$window_start")"
  latest_order_event_epoch="$(printf '%s' "$orders_json" | python3 -c 'import json, sys
try:
    events = json.load(sys.stdin).get("data", {}).get("recent_order_events", [])
except Exception:
    events = []
latest = 0.0
for event in events:
    latest = max(latest, float(event.get("timestamp") or 0))
print(int(latest))')"
fi

if [ -n "$LOG_PATH_OVERRIDE" ]; then
  log_path="$LOG_PATH_OVERRIDE"
else
  log_path="$REPO_ROOT/bots/instances/$bot_name/logs/logs_${bot_name}.log"
fi

metrics_json="$(
  python3 - "$log_path" "$window_start" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sys

path = Path(sys.argv[1])
window_start = float(sys.argv[2])
close_code_re = re.compile(r"Close code = (\d+)")
status_5xx_re = re.compile(r"http status is 50\d")

metrics = {
    "benign_disconnect_count": 0,
    "hard_disconnect_count": 0,
    "disconnect_count": 0,
    "exchange_5xx_count": 0,
    "open_order_failed_count": 0,
    "resubscribe_count": 0,
    "last_disconnect_codes": [],
    "latest_log_epoch": 0,
}

resubscribe_markers = (
    "subscribed to private order and trades changes channels",
    "subscribed to public order book, trade, and funding info channels",
)
hard_markers = (
    "wssserverhandshakeerror",
    "unexpected error while listening to user stream",
    "unexpected error occurred when listening to order book streams",
    "networkstatus.not_connected",
)

if path.exists():
    for line in path.open("r", encoding="utf-8", errors="replace"):
        try:
            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if ts < window_start:
            continue

        lower = line.lower()
        metrics["latest_log_epoch"] = max(metrics["latest_log_epoch"], int(ts))
        if "the websocket connection was closed" in lower or "ws connection was closed unexpectedly" in lower:
            match = close_code_re.search(line)
            code = match.group(1) if match else "unknown"
            metrics["last_disconnect_codes"].append(code)
            if code == "1000":
                metrics["benign_disconnect_count"] += 1
            else:
                metrics["hard_disconnect_count"] += 1
        if any(marker in lower for marker in hard_markers):
            metrics["hard_disconnect_count"] += 1
        if any(marker in lower for marker in resubscribe_markers):
            metrics["resubscribe_count"] += 1
        if status_5xx_re.search(lower) or "504 gateway timeout" in lower:
            metrics["exchange_5xx_count"] += 1
        if "open order failed" in lower:
            metrics["open_order_failed_count"] += 1

metrics["disconnect_count"] = metrics["hard_disconnect_count"]
metrics["last_disconnect_codes"] = metrics["last_disconnect_codes"][-10:]
print(json.dumps(metrics))
PY
)"

benign_disconnect_count="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["benign_disconnect_count"])')"
hard_disconnect_count="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hard_disconnect_count"])')"
disconnect_count="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["disconnect_count"])')"
exchange_5xx_count="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["exchange_5xx_count"])')"
open_order_failed_count="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["open_order_failed_count"])')"
resubscribe_count="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["resubscribe_count"])')"
last_disconnect_codes_json="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["last_disconnect_codes"]))')"
latest_log_epoch="$(printf '%s' "$metrics_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["latest_log_epoch"])')"

if [ -n "$HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE" ]; then
  hl_code="$HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE"
else
  hl_code="$(curl -sS -o /dev/null -w '%{http_code}' -m 10 \
    -H 'Content-Type: application/json' \
    -d '{"type":"meta"}' \
    "$HYPERLIQUID_INFO_URL" 2>/dev/null || printf '000')"
fi

quote_gap_seconds=0
latest_log_age_seconds=0
latest_order_event_age_seconds=0
if [ "$latest_log_epoch" -gt 0 ]; then
  latest_log_age_seconds=$((now_epoch - latest_log_epoch))
fi
if [ "$latest_order_event_epoch" -gt 0 ]; then
  latest_order_event_age_seconds=$((now_epoch - latest_order_event_epoch))
fi
if [ "$api_ok" = true ] && [ "$bot_status" = "running" ] && [ "$recently_active" = "true" ] && [ "$active_order_count" -lt 2 ] && [ "$recent_order_create_count" -gt 0 ]; then
  if [ ! -f "$quote_gap_file" ]; then
    printf '%s\n' "$now_epoch" > "$quote_gap_file"
  fi
  quote_gap_started="$(cat "$quote_gap_file")"
  quote_gap_seconds=$((now_epoch - quote_gap_started))
else
  rm -f "$quote_gap_file"
fi

stuck_recovering_seconds=0
if [ "$runtime_connectivity_ok" = true ] && [ "$runtime_state" = "RECOVERING" ]; then
  if [ ! -f "$stuck_recovering_file" ]; then
    printf '%s\n' "$now_epoch" > "$stuck_recovering_file"
  fi
  stuck_recovering_started="$(cat "$stuck_recovering_file")"
  stuck_recovering_seconds=$((now_epoch - stuck_recovering_started))
else
  rm -f "$stuck_recovering_file"
fi

hard_reasons=()
degraded_reasons=()
within_startup_grace=false
if [ $((now_epoch - activation_epoch)) -lt "$STARTUP_GRACE_SECONDS" ]; then
  within_startup_grace=true
fi

if [ "$runtime_connectivity_ok" = true ]; then
  case "$runtime_state" in
    DEGRADED_UNSAFE|HARD_DISCONNECTED)
      hard_reasons+=("runtime_${runtime_state}:${runtime_watchdog_reason}")
      ;;
    RECOVERING)
      degraded_reasons+=("runtime_recovering:${runtime_watchdog_reason}")
      if [ "$stuck_recovering_seconds" -ge "$STUCK_RECOVERING_SECONDS" ]; then
        hard_reasons+=("stuck_recovering:${runtime_watchdog_reason}")
      fi
      ;;
    DEGRADED_TRANSIENT)
      degraded_reasons+=("runtime_degraded_transient:${runtime_watchdog_reason}")
      ;;
  esac
else
  if [ "$hard_disconnect_count" -ge "$HARD_DISCONNECT_THRESHOLD" ] || [ "$disconnect_count" -ge "$DISCONNECT_THRESHOLD" ]; then
    hard_reasons+=("hard_disconnect_instability")
  fi
  if [ "$exchange_5xx_count" -ge "$EXCHANGE_5XX_THRESHOLD" ]; then
    hard_reasons+=("exchange_5xx_instability")
  fi
  if [ "$open_order_failed_count" -ge "$OPEN_ORDER_FAILED_THRESHOLD" ]; then
    hard_reasons+=("open_order_failure_spike")
  fi
  if [ "$quote_gap_seconds" -ge "$QUOTE_GAP_SECONDS" ]; then
    hard_reasons+=("quote_gap")
  fi
  if [ "$hl_code" != "200" ]; then
    hard_reasons+=("hyperliquid_info_probe_${hl_code}")
  fi
  if [ "$bot_status" = "running" ] && [ "$active_order_count" -eq 0 ] && [ "$within_startup_grace" != true ] && [ "$latest_log_age_seconds" -ge "$STALLED_RUNTIME_SECONDS" ] && [ "$latest_order_event_age_seconds" -ge "$STALLED_RUNTIME_SECONDS" ]; then
    hard_reasons+=("stalled_runtime")
  fi
  if [ "$benign_disconnect_count" -ge "$BENIGN_DISCONNECT_THRESHOLD" ]; then
    degraded_reasons+=("benign_disconnect_churn")
  fi
  if [ "$hard_disconnect_count" -gt 0 ] && [ "$hard_disconnect_count" -lt "$HARD_DISCONNECT_THRESHOLD" ]; then
    degraded_reasons+=("hard_disconnect_warning")
  fi
  if [ "$exchange_5xx_count" -gt 0 ] && [ "$exchange_5xx_count" -lt "$EXCHANGE_5XX_THRESHOLD" ]; then
    degraded_reasons+=("exchange_5xx_warning")
  fi
  if [ "$open_order_failed_count" -gt 0 ] && [ "$open_order_failed_count" -lt "$OPEN_ORDER_FAILED_THRESHOLD" ]; then
    degraded_reasons+=("open_order_failure_warning")
  fi
  if [ "$quote_gap_seconds" -gt 0 ] && [ "$quote_gap_seconds" -lt "$QUOTE_GAP_SECONDS" ]; then
    degraded_reasons+=("quote_gap_warning")
  fi
  if [ "$bot_status" = "running" ] && [ "$active_order_count" -eq 0 ] && [ "$within_startup_grace" != true ] && { [ "$latest_log_age_seconds" -gt 0 ] || [ "$latest_order_event_age_seconds" -gt 0 ]; }; then
    degraded_reasons+=("runtime_idle_warning")
  fi

  impairment_present=false
  if [ "$quote_gap_seconds" -gt 0 ] || [ "$exchange_5xx_count" -gt 0 ] || [ "$open_order_failed_count" -gt 0 ] || { [ "$active_order_count" -lt 2 ] && [ "$recent_order_create_count" -gt 0 ]; }; then
    impairment_present=true
  fi
  if [ "$benign_disconnect_count" -ge "$BENIGN_DISCONNECT_THRESHOLD" ] && [ "$impairment_present" = true ]; then
    hard_reasons+=("benign_disconnects_with_impairment")
  fi
fi

reasons_json="$(array_to_json "${hard_reasons[@]+"${hard_reasons[@]}"}")"
degraded_reasons_json="$(array_to_json "${degraded_reasons[@]+"${degraded_reasons[@]}"}")"

trading_exposure_active=false
if [ "$bot_status" = "running" ] && { [ "$active_order_count" -gt 0 ] || [ "$recent_order_create_count" -gt 0 ] || [ "$recently_active" = "true" ]; }; then
  trading_exposure_active=true
fi

run_id="${bot_name}:${activation_epoch}"
stop_suppressed_reason=""
action="none"
stop_response=""
should_stop=false
if [ "${#hard_reasons[@]}" -gt 0 ]; then
  should_stop=true
fi

if [ "$should_stop" = true ]; then
  if [ "$bot_status" != "running" ]; then
    stop_suppressed_reason="bot_not_running"
  elif [ "$trading_exposure_active" != true ]; then
    stop_suppressed_reason="no_trading_exposure"
  elif [ -f "$stop_state_file" ] && [ "$(json_get "$stop_state_file" 'payload.get("run_id", "")')" = "$run_id" ]; then
    stop_suppressed_reason="already_stopped_this_run"
  elif [ "$DRY_RUN" = true ]; then
    action="would_stop_bot"
  else
    action="stop_bot"
    stop_response="$(api_post_json "/bot-orchestration/stop-bot" "{\"bot_name\":\"$bot_name\",\"skip_order_cancellation\":false,\"async_backend\":false}" 2>&1 || true)"
    cat > "$stop_state_file" <<EOF
{"run_id":"$run_id","bot_name":"$bot_name","activation_epoch":$activation_epoch,"last_stop_epoch":$now_epoch}
EOF
  fi
fi

stop_response_json="$(printf '%s' "$stop_response" | json_escape)"
runtime_reason_json="$(printf '%s' "$runtime_reason" | json_escape)"
runtime_readiness_reason_json="$(printf '%s' "$runtime_readiness_reason" | json_escape)"
runtime_watchdog_reason_json="$(printf '%s' "$runtime_watchdog_reason" | json_escape)"
runtime_ws_close_reason_json="$(printf '%s' "$runtime_ws_close_reason" | json_escape)"
bot_name_match=true
if [ -n "$runtime_bot_name" ] && [ "$runtime_bot_name" != "$bot_name" ]; then
  bot_name_match=false
fi

status_file="$STATUS_DIR/latest.json"
tmp_status="${status_file}.tmp"
cat > "$tmp_status" <<EOF
{
  "checked_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "bot_name": "$bot_name",
  "runtime_bot_name": "$runtime_bot_name",
  "bot_name_match": $bot_name_match,
  "bot_status": "$bot_status",
  "recently_active": $recently_active,
  "active_order_count": $active_order_count,
  "recent_order_create_count": $recent_order_create_count,
  "window_seconds": $WINDOW_SECONDS,
  "window_start_epoch": $window_start,
  "activation_epoch": $activation_epoch,
  "disconnect_count": $disconnect_count,
  "benign_disconnect_count": $benign_disconnect_count,
  "hard_disconnect_count": $hard_disconnect_count,
  "last_disconnect_codes": $last_disconnect_codes_json,
  "resubscribe_count": $resubscribe_count,
  "exchange_5xx_count": $exchange_5xx_count,
  "open_order_failed_count": $open_order_failed_count,
  "latest_log_epoch": $latest_log_epoch,
  "latest_log_age_seconds": $latest_log_age_seconds,
  "latest_order_event_epoch": $latest_order_event_epoch,
  "latest_order_event_age_seconds": $latest_order_event_age_seconds,
  "quote_gap_seconds": $quote_gap_seconds,
  "stuck_recovering_seconds": $stuck_recovering_seconds,
  "hyperliquid_info_http_code": "$hl_code",
  "runtime_connectivity_available": $runtime_connectivity_ok,
  "runtime_state": "$runtime_state",
  "runtime_reason": $runtime_reason_json,
  "runtime_readiness_reason": $runtime_readiness_reason_json,
  "runtime_watchdog_reason": $runtime_watchdog_reason_json,
  "runtime_reconciliation_result": "$runtime_reconciliation_result",
  "runtime_quoting_enabled": $runtime_quoting_enabled,
  "runtime_orders_unknown": $runtime_orders_unknown,
  "runtime_public_ws_status": "$runtime_public_ws_status",
  "runtime_private_ws_status": "$runtime_private_ws_status",
  "runtime_rest_health": "$runtime_rest_health",
  "runtime_last_order_book_update_timestamp": "$runtime_last_order_book_update_timestamp",
  "runtime_last_user_stream_update_timestamp": "$runtime_last_user_stream_update_timestamp",
  "runtime_reconnect_attempt_count": $runtime_reconnect_attempt_count,
  "runtime_reconnect_duration": "$runtime_reconnect_duration",
  "runtime_ws_close_code": "$runtime_ws_close_code",
  "runtime_ws_close_reason": $runtime_ws_close_reason_json,
  "runtime_open_order_count_during_disconnect": $runtime_open_order_count_during_disconnect,
  "reasons": $reasons_json,
  "degraded_reasons": $degraded_reasons_json,
  "trading_active": $trading_exposure_active,
  "dry_run": $DRY_RUN,
  "action": "$action",
  "stop_suppressed_reason": "$(printf '%s' "$stop_suppressed_reason")",
  "stop_response": $stop_response_json
}
EOF
mv "$tmp_status" "$status_file"
if [ "$SKIP_STATUS_ARCHIVE" != "true" ]; then
  cp "$status_file" "$STATUS_DIR/$(date -u +%Y%m%dT%H%M%SZ).json"
fi

if [ "$action" = "stop_bot" ] || [ "$action" = "would_stop_bot" ]; then
  echo "bot_name=$bot_name action=$action reasons=$(join_or_none "${hard_reasons[@]+"${hard_reasons[@]}"}") active_orders=$active_order_count recent_order_creates=$recent_order_create_count"
else
  echo "bot_name=$bot_name action=none reasons=$(join_or_none "${hard_reasons[@]+"${hard_reasons[@]}"}") degraded=$(join_or_none "${degraded_reasons[@]+"${degraded_reasons[@]}"}") active_orders=$active_order_count recent_order_creates=$recent_order_create_count suppressed=${stop_suppressed_reason:-none}"
fi

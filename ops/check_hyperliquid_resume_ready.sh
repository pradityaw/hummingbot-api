#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOOKBACK_MINUTES="${LOOKBACK_MINUTES:-30}"
MIN_SUCCESSFUL_PROBES="${MIN_SUCCESSFUL_PROBES:-10}"
PROBE_DIR="${PROBE_DIR:-$REPO_ROOT/ops/hyperliquid-probes}"
WATCHDOG_STATUS="${WATCHDOG_STATUS:-$REPO_ROOT/ops/watchdog-status/latest.json}"
BOT_NAME="${BOT_NAME:-hl-testnet-pmm-20260628-185328}"

python3 - "$PROBE_DIR" "$WATCHDOG_STATUS" "$LOOKBACK_MINUTES" "$MIN_SUCCESSFUL_PROBES" "$BOT_NAME" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

probe_dir = Path(sys.argv[1])
watchdog_path = Path(sys.argv[2])
lookback_minutes = int(sys.argv[3])
min_successful_probes = int(sys.argv[4])
bot_name = sys.argv[5]
window_start = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

def parse_ts(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def watchdog_bot_name(payload):
    if not isinstance(payload, dict):
        return None
    return payload.get("bot_name")

def runtime_bot_name(payload):
    if not isinstance(payload, dict):
        return None
    return payload.get("runtime_bot_name")

def probe_bot_name(payload):
    if not isinstance(payload, dict):
        return None
    return payload.get("bot_name")

def probe_watchdog_bot_name(payload):
    if not isinstance(payload, dict):
        return None
    ready = payload.get("ready", {})
    if isinstance(ready, dict) and ready.get("watchdog_bot_name"):
        return ready.get("watchdog_bot_name")
    watchdog = payload.get("watchdog", {})
    if isinstance(watchdog, dict):
        return watchdog_bot_name(watchdog)
    return None

def probe_runtime_bot_name(payload):
    if not isinstance(payload, dict):
        return None
    ready = payload.get("ready", {})
    if isinstance(ready, dict) and ready.get("runtime_bot_name"):
        return ready.get("runtime_bot_name")
    watchdog = payload.get("watchdog", {})
    if isinstance(watchdog, dict):
        return runtime_bot_name(watchdog)
    return None

def mismatch(source, actual):
    if actual and actual != bot_name:
        return f"{source}_bot_name_mismatch expected={bot_name} actual={actual}"
    return None

probe_files = sorted(probe_dir.glob("*.json"))
recent = []
probe_mismatches = []
for path in probe_files:
    try:
        payload = json.loads(path.read_text())
    except Exception:
        continue
    try:
        ts = parse_ts(payload["checked_at"])
    except Exception:
        continue
    if ts >= window_start:
        probe_mismatch = [
            item
            for item in (
                mismatch(f"probe:{path.name}", probe_bot_name(payload)),
                mismatch(f"probe_watchdog:{path.name}", probe_watchdog_bot_name(payload)),
                mismatch(f"probe_runtime:{path.name}", probe_runtime_bot_name(payload)),
            )
            if item
        ]
        if probe_mismatch:
            probe_mismatches.extend(probe_mismatch)
        else:
            recent.append(payload)

ready_probes = [p for p in recent if p.get("ready", {}).get("ready_now")]
watchdog_ok = False
watchdog = None
runtime_ready = None
runtime_reason = None
watchdog_mismatches = []
if watchdog_path.exists():
    watchdog = json.loads(watchdog_path.read_text())
    watchdog_mismatches = [
        item
        for item in (
            mismatch("watchdog", watchdog_bot_name(watchdog)),
            mismatch("runtime", runtime_bot_name(watchdog)),
        )
        if item
    ]
    if watchdog_mismatches:
        watchdog_ok = False
        runtime_ready = False
        runtime_reason = ",".join(watchdog_mismatches)
    elif watchdog.get("runtime_connectivity_available"):
        runtime_state = watchdog.get("runtime_state")
        reconciliation_result = watchdog.get("runtime_reconciliation_result")
        orders_unknown = bool(watchdog.get("runtime_orders_unknown"))
        unsafe = runtime_state in {"DEGRADED_UNSAFE", "HARD_DISCONNECTED"}
        reconciliation_incomplete = runtime_state == "RECOVERING" or reconciliation_result in {"required", "running", "failed"} or orders_unknown
        runtime_ready = not unsafe and not reconciliation_incomplete
        runtime_reason = "ok" if runtime_ready else watchdog.get("runtime_readiness_reason") or watchdog.get("runtime_watchdog_reason") or runtime_state
        watchdog_ok = runtime_ready
    else:
        watchdog_ok = watchdog.get("action") == "none" and not watchdog.get("reasons") and watchdog.get("active_order_count") == 0

bot_name_mismatches = watchdog_mismatches + probe_mismatches
not_ready_reason = None
if bot_name_mismatches:
    resume_ready = False
    not_ready_reason = ",".join(bot_name_mismatches)
else:
    resume_ready = runtime_ready if runtime_ready is not None else len(ready_probes) >= min_successful_probes and watchdog_ok
    if not resume_ready:
        not_ready_reason = runtime_reason or "insufficient_matching_ready_probes_or_watchdog_not_clean"

result = {
    "bot_name": bot_name,
    "lookback_minutes": lookback_minutes,
    "recent_probe_count": len(recent),
    "ready_probe_count": len(ready_probes),
    "min_successful_probes": min_successful_probes,
    "watchdog_ok": watchdog_ok,
    "runtime_ready": runtime_ready,
    "runtime_reason": runtime_reason,
    "bot_name_mismatches": bot_name_mismatches,
    "not_ready_reason": not_ready_reason,
    "resume_ready": resume_ready,
    "latest_probe_at": recent[-1]["checked_at"] if recent else None,
    "latest_watchdog": watchdog,
}
print(json.dumps(result, indent=2, sort_keys=True))
if result["resume_ready"]:
    print("RESUME_READY")
    sys.exit(0)
print(f"RESUME_NOT_READY reason={not_ready_reason}")
sys.exit(1)
PY

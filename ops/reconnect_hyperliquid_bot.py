#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


UNSAFE_RUNTIME_STATES = {"DEGRADED_UNSAFE", "HARD_DISCONNECTED"}
INCOMPLETE_RECONCILIATION_RESULTS = {"required", "running", "failed"}


def read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def pick_bot_name(
    explicit_bot_name: Optional[str],
    probe: Optional[dict[str, Any]],
    watchdog: Optional[dict[str, Any]],
) -> Optional[str]:
    for value in (
        explicit_bot_name,
        probe.get("bot_name") if probe else None,
        watchdog.get("bot_name") if watchdog else None,
        watchdog.get("runtime_bot_name") if watchdog else None,
    ):
        if value:
            return str(value)
    return None


def runtime_ready(watchdog: Optional[dict[str, Any]]) -> Optional[bool]:
    if not watchdog or not watchdog.get("runtime_connectivity_available"):
        return None
    runtime_state = watchdog.get("runtime_state")
    reconciliation_result = watchdog.get("runtime_reconciliation_result")
    orders_unknown = bool(watchdog.get("runtime_orders_unknown"))
    unsafe = runtime_state in UNSAFE_RUNTIME_STATES
    reconciliation_incomplete = (
        runtime_state == "RECOVERING"
        or reconciliation_result in INCOMPLETE_RECONCILIATION_RESULTS
        or orders_unknown
    )
    return not unsafe and not reconciliation_incomplete


def bot_name_matches(bot_name: Optional[str], probe: Optional[dict[str, Any]], watchdog: Optional[dict[str, Any]]) -> bool:
    if not bot_name:
        return False
    candidates = [
        probe.get("bot_name") if probe else None,
        watchdog.get("bot_name") if watchdog else None,
        watchdog.get("runtime_bot_name") if watchdog else None,
    ]
    return all(not candidate or str(candidate) == bot_name for candidate in candidates)


def active_order_count(watchdog: Optional[dict[str, Any]]) -> int:
    if not watchdog:
        return 0
    try:
        return int(watchdog.get("active_order_count") or 0)
    except (TypeError, ValueError):
        return 0


def build_reconnect_decision(
    probe: Optional[dict[str, Any]],
    watchdog: Optional[dict[str, Any]],
    bot_name: Optional[str],
) -> dict[str, Any]:
    reasons = []
    ready = probe.get("ready", {}) if probe else {}
    dns = probe.get("dns", {}) if probe else {}
    resolved_ips = dns.get("resolved_ips") or dns.get("ipv4") or []
    rest_ok = bool(ready.get("rest_ok"))
    ws_ok = bool(ready.get("ws_ok"))
    dns_ok = bool(ready.get("dns_ok", resolved_ips)) and bool(resolved_ips)
    probe_ready = bool(ready.get("ready_now"))
    runtime_gate = runtime_ready(watchdog)
    orders_open = active_order_count(watchdog)
    names_match = bot_name_matches(bot_name, probe, watchdog)

    for reason, ok in (
        ("missing_bot_name", bool(bot_name)),
        ("bot_name_mismatch", names_match),
        ("probe_not_ready", probe_ready),
        ("rest_probe_failed", rest_ok),
        ("ws_probe_failed", ws_ok),
        ("dns_resolution_failed", dns_ok),
        ("active_orders_present", orders_open == 0),
    ):
        if not ok:
            reasons.append(reason)

    if runtime_gate is False:
        reasons.append("runtime_reconciliation_or_safety_gate_failed")

    restart_allowed = not reasons
    return {
        "bot_name": bot_name,
        "restart_allowed": restart_allowed,
        "dry_run_safe_default": True,
        "will_place_orders": False,
        "will_set_quoting_enabled": False,
        "reasons": reasons,
        "gates": {
            "probe_ready": probe_ready,
            "rest_ok": rest_ok,
            "ws_ok": ws_ok,
            "dns_ok": dns_ok,
            "runtime_ready": runtime_gate,
            "active_order_count": orders_open,
            "bot_name_match": names_match,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run Hyperliquid bot reconnect diagnostic. It never places orders or "
            "enables quoting; --execute-restart only runs a targeted docker restart "
            "after readiness, DNS, reconciliation, and active-order gates pass."
        )
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--probe-status", default=None)
    parser.add_argument("--watchdog-status", default=None)
    parser.add_argument("--bot-name", default=None)
    parser.add_argument("--execute-restart", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    probe_path = Path(args.probe_status) if args.probe_status else repo_root / "ops" / "hyperliquid-probes" / "latest.json"
    watchdog_path = Path(args.watchdog_status) if args.watchdog_status else repo_root / "ops" / "watchdog-status" / "latest.json"
    probe = read_json(probe_path)
    watchdog = read_json(watchdog_path)
    bot_name = pick_bot_name(args.bot_name, probe, watchdog)
    decision = build_reconnect_decision(probe, watchdog, bot_name)
    decision["probe_status"] = str(probe_path)
    decision["watchdog_status"] = str(watchdog_path)
    decision["execute_restart_requested"] = args.execute_restart

    if args.execute_restart:
        if not decision["restart_allowed"]:
            print(json.dumps(decision, indent=2, sort_keys=True))
            print("Restart blocked by reconnect gates.", file=sys.stderr)
            return 1
        subprocess.run(["docker", "restart", str(bot_name)], check=True)
        decision["restart_executed"] = True
    else:
        decision["restart_executed"] = False

    if args.json or args.execute_restart:
        print(json.dumps(decision, indent=2, sort_keys=True))
    else:
        status = "allowed" if decision["restart_allowed"] else "blocked"
        reasons = ", ".join(decision["reasons"]) if decision["reasons"] else "none"
        print(f"restart_{status} bot={decision['bot_name']} reasons={reasons}")
        print("dry_run=true orders=false quoting_enabled_unchanged=true")
    return 0 if decision["restart_allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

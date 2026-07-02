#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import random
import socket
import ssl
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp


DEFAULT_BOT_NAME = "hl-testnet-pmm-20260702-085126"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_host(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addrs = sorted({info[4][0] for info in infos})
    return addrs


def hostname_from_url(url: str) -> str:
    return urlparse(url).hostname or "api.hyperliquid-testnet.xyz"


def retry_sleep_seconds(base_sleep: float, jitter: float, jitter_fn: Callable[[float, float], float] = random.uniform) -> float:
    jitter_value = jitter_fn(0, jitter) if jitter > 0 else 0
    return max(0, base_sleep + jitter_value)


def run_rest_probe(
    url: str,
    attempts: int,
    timeout: float,
    resolved_ips: Optional[list[str]] = None,
    retry_sleep: float = 1.0,
    retry_jitter: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> list[dict[str, Any]]:
    payload = b'{"type":"meta"}'
    results: list[dict[str, Any]] = []
    ips = list(resolved_ips or [])
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        req = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status = None
        body_excerpt = ""
        error = None
        try:
            with urlopen(req, timeout=timeout) as response:
                status = response.status
                body_excerpt = response.read(200).decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover - operational script
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        results.append(
            {
                "attempt": attempt,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "ok": status == 200 and error is None,
                "body_excerpt": body_excerpt,
                "error": error,
                "resolved_ips": ips,
            }
        )
        if attempt < attempts:
            sleep_fn(retry_sleep_seconds(retry_sleep, retry_jitter, jitter_fn))
    return results


async def run_ws_probe(url: str, timeout: float, resolved_ips: Optional[list[str]] = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "connect_elapsed_ms": None,
        "subscription_response_excerpt": "",
        "error": None,
        "ssl_protocol": None,
        "resolved_ips": list(resolved_ips or []),
    }
    started = time.perf_counter()
    ssl_ctx = ssl.create_default_context()
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.ws_connect(url, heartbeat=10, receive_timeout=timeout, ssl=ssl_ctx) as ws:
                result["connect_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
                transport = ws._response.connection.transport if ws._response.connection else None
                if transport is not None:
                    ssl_object = transport.get_extra_info("ssl_object")
                    if ssl_object is not None:
                        result["ssl_protocol"] = ssl_object.version()
                await ws.send_json(
                    {
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": "BTC"},
                    }
                )
                msg = await ws.receive(timeout=timeout)
                if msg.type.name == "TEXT":
                    result["subscription_response_excerpt"] = msg.data[:300]
                else:
                    result["subscription_response_excerpt"] = str(msg.data)[:300]
                result["ok"] = msg.type.name in {"TEXT", "BINARY"}
    except Exception as exc:  # pragma: no cover - operational script
        result["connect_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def read_watchdog_status(repo_root: Path) -> Optional[dict[str, Any]]:
    status_path = repo_root / "ops" / "watchdog-status" / "latest.json"
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text())
    except json.JSONDecodeError:
        return None


def read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def dns_ips_from_payload(payload: Optional[dict[str, Any]]) -> list[str]:
    if not payload:
        return []
    dns = payload.get("dns", {})
    raw_ips = dns.get("resolved_ips") or dns.get("ipv4") or []
    return sorted(str(ip) for ip in raw_ips if ip)


def load_dns_baseline(repo_root: Path) -> Optional[dict[str, Any]]:
    probe_dir = repo_root / "ops" / "hyperliquid-probes"
    latest_path = probe_dir / "latest.json"
    latest = read_json(latest_path)
    latest_ips = dns_ips_from_payload(latest)
    if latest_ips:
        return {
            "source": str(latest_path),
            "checked_at": latest.get("checked_at") if latest else None,
            "resolved_ips": latest_ips,
        }

    for path in sorted(probe_dir.glob("*.json"), reverse=True):
        if path.name == "latest.json":
            continue
        payload = read_json(path)
        ips = dns_ips_from_payload(payload)
        if ips and payload and payload.get("ready", {}).get("ready_now"):
            return {
                "source": str(path),
                "checked_at": payload.get("checked_at"),
                "resolved_ips": ips,
            }
    return None


def build_dns_drift(current_ips: list[str], baseline: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not baseline:
        return {
            "baseline_available": False,
            "changed": False,
            "previous_resolved_ips": [],
            "added": [],
            "removed": [],
        }
    previous_ips = sorted(str(ip) for ip in baseline.get("resolved_ips", []) if ip)
    current_set = set(current_ips)
    previous_set = set(previous_ips)
    return {
        "baseline_available": True,
        "baseline_source": baseline.get("source"),
        "baseline_checked_at": baseline.get("checked_at"),
        "changed": current_set != previous_set,
        "previous_resolved_ips": previous_ips,
        "added": sorted(current_set - previous_set),
        "removed": sorted(previous_set - current_set),
    }


def watchdog_bot_name(watchdog: Optional[dict[str, Any]]) -> Optional[str]:
    if not watchdog:
        return None
    value = watchdog.get("bot_name")
    return str(value) if value else None


def runtime_bot_name(watchdog: Optional[dict[str, Any]]) -> Optional[str]:
    if not watchdog:
        return None
    value = watchdog.get("runtime_bot_name")
    return str(value) if value else None


def write_status(repo_root: Path, payload: dict[str, Any]) -> Path:
    out_dir = repo_root / "ops" / "hyperliquid-probes"
    out_dir.mkdir(parents=True, exist_ok=True)
    latest_path = out_dir / "latest.json"
    stamp_path = out_dir / f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
    latest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    stamp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return latest_path


def _probe_timestamp(path: Path) -> tuple[datetime, Path]:
    try:
        payload = json.loads(path.read_text())
        checked_at = payload.get("checked_at")
        if checked_at:
            return datetime.fromisoformat(str(checked_at).replace("Z", "+00:00")), path
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc), path


def prune_probe_dir(
    probe_dir: Path,
    max_age_seconds: Optional[int],
    max_count: Optional[int],
    now: Optional[datetime] = None,
) -> list[str]:
    if max_age_seconds is None and max_count is None:
        return []
    now = now or utc_now()
    candidates = [
        path
        for path in probe_dir.glob("*.json")
        if path.name != "latest.json" and path.is_file()
    ]
    by_timestamp = sorted((_probe_timestamp(path) for path in candidates), key=lambda item: item[0], reverse=True)
    to_delete: set[Path] = set()
    if max_age_seconds is not None:
        cutoff = now.timestamp() - max_age_seconds
        to_delete.update(path for timestamp, path in by_timestamp if timestamp.timestamp() < cutoff)
    if max_count is not None and max_count >= 0:
        to_delete.update(path for _, path in by_timestamp[max_count:])
    deleted = []
    for path in sorted(to_delete):
        path.unlink()
        deleted.append(path.name)
    return deleted


def print_summary(payload: dict[str, Any]) -> None:
    rest = payload["rest"]
    ws = payload["ws"]
    ready = payload["ready"]
    print(
        f"ready={ready['ready_now']} rest_ok={rest['success_count']}/{rest['attempt_count']} "
        f"ws_ok={ws['ok']} watchdog_ok={ready['watchdog_ok']} reason={ready['reason']}"
    )


def build_ready_state(
    rest: dict[str, Any],
    ws: dict[str, Any],
    watchdog: Optional[dict[str, Any]],
    bot_name: Optional[str] = None,
    dns_addrs: Optional[list[str]] = None,
) -> dict[str, Any]:
    rest_ok = rest["success_count"] == rest["attempt_count"]
    ws_ok = ws["ok"]
    dns_ok = True if dns_addrs is None else bool(dns_addrs)
    expected_bot_name = bot_name or None
    watchdog_name = watchdog_bot_name(watchdog)
    runtime_name = runtime_bot_name(watchdog)
    mismatches = []
    for source, actual in (("watchdog", watchdog_name), ("runtime", runtime_name)):
        if expected_bot_name and actual and actual != expected_bot_name:
            mismatches.append(
                f"{source}_bot_name_mismatch expected={expected_bot_name} actual={actual}"
            )
    if mismatches:
        return {
            "ready_now": False,
            "rest_ok": rest_ok,
            "ws_ok": ws_ok,
            "dns_ok": dns_ok,
            "watchdog_ok": False,
            "runtime_connectivity_available": bool(watchdog and watchdog.get("runtime_connectivity_available")),
            "target_bot_name": expected_bot_name,
            "watchdog_bot_name": watchdog_name,
            "runtime_bot_name": runtime_name,
            "bot_name_match": False,
            "reason": ",".join(mismatches),
        }
    if watchdog and watchdog.get("runtime_connectivity_available"):
        runtime_state = watchdog.get("runtime_state")
        reconciliation_result = watchdog.get("runtime_reconciliation_result")
        orders_unknown = bool(watchdog.get("runtime_orders_unknown"))
        unsafe = runtime_state in {"DEGRADED_UNSAFE", "HARD_DISCONNECTED"}
        reconciliation_incomplete = runtime_state == "RECOVERING" or reconciliation_result in {"required", "running", "failed"} or orders_unknown
        runtime_ready = not unsafe and not reconciliation_incomplete
        ready_now = runtime_ready and rest_ok and ws_ok and dns_ok
        reason_parts = [
            part
            for part, ok in (
                ("rest_probe_failed", rest_ok),
                ("ws_probe_failed", ws_ok),
                ("dns_resolution_failed", dns_ok),
            )
            if not ok
        ]
        if not runtime_ready:
            reason_parts.append(
                watchdog.get("runtime_readiness_reason") or watchdog.get("runtime_watchdog_reason") or runtime_state
            )
        reason = "ok" if ready_now else ",".join(reason_parts)
        return {
            "ready_now": ready_now,
            "rest_ok": rest_ok,
            "ws_ok": ws_ok,
            "dns_ok": dns_ok,
            "watchdog_ok": not unsafe,
            "runtime_connectivity_available": True,
            "runtime_state": runtime_state,
            "runtime_reconciliation_result": reconciliation_result,
            "runtime_orders_unknown": orders_unknown,
            "target_bot_name": expected_bot_name,
            "watchdog_bot_name": watchdog_name,
            "runtime_bot_name": runtime_name,
            "bot_name_match": True,
            "reason": reason,
        }
    watchdog_ok = bool(watchdog) and watchdog.get("action") == "none" and not watchdog.get("reasons")
    ready_now = rest_ok and ws_ok and watchdog_ok and dns_ok
    reason = "ok" if ready_now else ",".join(
        part
        for part, ok in (
            ("rest_probe_failed", rest_ok),
            ("ws_probe_failed", ws_ok),
            ("watchdog_not_clean", watchdog_ok),
            ("dns_resolution_failed", dns_ok),
        )
        if not ok
    )
    return {
        "ready_now": ready_now,
        "rest_ok": rest_ok,
        "ws_ok": ws_ok,
        "dns_ok": dns_ok,
        "watchdog_ok": watchdog_ok,
        "runtime_connectivity_available": False,
        "target_bot_name": expected_bot_name,
        "watchdog_bot_name": watchdog_name,
        "runtime_bot_name": runtime_name,
        "bot_name_match": True,
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Passive Hyperliquid testnet connectivity probe")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--rest-url", default="https://api.hyperliquid-testnet.xyz/info")
    parser.add_argument("--ws-url", default="wss://api.hyperliquid-testnet.xyz/ws")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retry-sleep", type=float, default=1.0)
    parser.add_argument("--retry-jitter", type=float, default=0.25)
    parser.add_argument("--bot-name", default=os.environ.get("BOT_NAME", DEFAULT_BOT_NAME))
    parser.add_argument("--prune-probe-dir", action="store_true")
    parser.add_argument(
        "--retention-max-age-seconds",
        type=int,
        default=int(os.environ.get("PROBE_RETENTION_MAX_AGE_SECONDS", "0")) or None,
    )
    parser.add_argument(
        "--retention-max-count",
        type=int,
        default=int(os.environ.get("PROBE_RETENTION_MAX_COUNT", "0")) or None,
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    hostname = hostname_from_url(args.rest_url)
    ws_hostname = hostname_from_url(args.ws_url)
    dns_addrs = resolve_host(hostname)
    ws_dns_addrs = dns_addrs if ws_hostname == hostname else resolve_host(ws_hostname)
    dns_baseline = load_dns_baseline(repo_root)
    dns_drift = build_dns_drift(dns_addrs, dns_baseline)
    rest_results = run_rest_probe(
        args.rest_url,
        attempts=args.attempts,
        timeout=args.timeout,
        resolved_ips=dns_addrs,
        retry_sleep=args.retry_sleep,
        retry_jitter=args.retry_jitter,
    )
    ws_result = asyncio.run(run_ws_probe(args.ws_url, timeout=args.timeout, resolved_ips=ws_dns_addrs))
    watchdog = read_watchdog_status(repo_root)

    rest_summary = {
        "attempt_count": len(rest_results),
        "success_count": sum(1 for item in rest_results if item["ok"]),
        "results": rest_results,
    }
    ready = build_ready_state(rest_summary, ws_result, watchdog, bot_name=args.bot_name, dns_addrs=dns_addrs)
    payload = {
        "checked_at": iso_now(),
        "bot_name": args.bot_name,
        "dns": {
            "hostname": hostname,
            "ws_hostname": ws_hostname,
            "ipv4": dns_addrs,
            "resolved_ips": dns_addrs,
            "ws_resolved_ips": ws_dns_addrs,
            "drift": dns_drift,
        },
        "rest": rest_summary,
        "ws": ws_result,
        "watchdog": watchdog,
        "ready": ready,
    }
    latest_path = write_status(repo_root, payload)
    if args.prune_probe_dir:
        prune_probe_dir(latest_path.parent, args.retention_max_age_seconds, args.retention_max_count)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_summary(payload)
    return 0 if ready["ready_now"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

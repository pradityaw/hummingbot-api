#!/usr/bin/env python3
import argparse
import base64
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CLOSE_TYPE_NAMES = {
    1: "TIME_LIMIT",
    2: "STOP_LOSS",
    3: "TAKE_PROFIT",
    4: "EXPIRED",
    5: "EARLY_STOP",
    6: "TRAILING_STOP",
    7: "INSUFFICIENT_BALANCE",
    8: "FAILED",
    9: "COMPLETED",
    10: "POSITION_HOLD",
}

DISCONNECT_PATTERNS = (
    "websocket connection was closed",
    "ws connection was closed unexpectedly",
    "network status has changed to networkstatus.not_connected",
)


@dataclass
class MonitorConfig:
    repo_root: Path
    api_url: str
    api_user: str
    api_pass: str
    bot_name: Optional[str]
    bot_prefix: str
    uptime_threshold_hours: float
    min_fills: int
    min_cycles: int
    no_fill_timeout_hours: float
    disconnect_window_hours: float
    disconnect_threshold: int
    sl_to_tp_alert_ratio: float
    active_order_minimum: int
    json_output: bool


def api_get(config: MonitorConfig, path: str) -> dict[str, Any]:
    url = f"{config.api_url.rstrip('/')}{path}"
    credentials = f"{config.api_user}:{config.api_pass}".encode()
    auth_header = base64.b64encode(credentials).decode()
    request = Request(url, headers={"Authorization": f"Basic {auth_header}"})
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        raise RuntimeError(f"API request failed ({exc.code}) for {path}") from exc
    except URLError as exc:
        raise RuntimeError(f"API request failed for {path}: {exc.reason}") from exc


def resolve_bot_name(config: MonitorConfig) -> str:
    if config.bot_name:
        return config.bot_name
    status = api_get(config, "/bot-orchestration/status")
    bots = status.get("data", {})
    candidates = [
        name for name, data in bots.items()
        if name.startswith(config.bot_prefix) and data.get("status") == "running"
    ]
    if not candidates:
        raise RuntimeError(
            f"No running bot found with prefix '{config.bot_prefix}'. "
            "Pass --bot-name explicitly if needed."
        )
    return sorted(candidates)[-1]


def docker_started_at(bot_name: str) -> Optional[datetime]:
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.StartedAt}}",
                bot_name,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    raw_value = result.stdout.strip()
    if not raw_value:
        return None
    return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))


def sqlite_path(repo_root: Path, bot_name: str) -> Path:
    return repo_root / "bots" / "instances" / bot_name / "data" / f"{bot_name}.sqlite"


def bot_log_path(repo_root: Path, bot_name: str) -> Path:
    return repo_root / "bots" / "instances" / bot_name / "logs" / f"logs_{bot_name}.log"


def read_fill_metrics(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            select
                count(*) as fill_count,
                min(timestamp) as first_fill_ts,
                max(timestamp) as last_fill_ts
            from TradeFill
            """
        ).fetchone()
    fill_count, first_fill_ts, last_fill_ts = row
    return {
        "fill_count": fill_count or 0,
        "first_fill_timestamp": ts_to_datetime(first_fill_ts),
        "last_fill_timestamp": ts_to_datetime(last_fill_ts),
    }


def read_cycle_metrics(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select
                id,
                type,
                close_type,
                close_timestamp,
                filled_amount_quote,
                net_pnl_quote,
                net_pnl_pct,
                is_active,
                is_trading
            from Executors
            where type = 'position_executor'
            order by timestamp asc
            """
        ).fetchall()

    completed_cycles = 0
    stop_loss_count = 0
    take_profit_count = 0
    trailing_stop_count = 0
    early_stop_count = 0
    last_cycle_timestamp = None

    for row in rows:
        close_type = row["close_type"]
        filled_amount_quote = float(row["filled_amount_quote"] or 0)
        close_timestamp = ts_to_datetime(row["close_timestamp"])

        if close_timestamp is not None:
            last_cycle_timestamp = close_timestamp

        if close_type is None or filled_amount_quote <= 0:
            continue

        completed_cycles += 1
        close_name = CLOSE_TYPE_NAMES.get(int(close_type), f"UNKNOWN_{close_type}")
        if close_name == "STOP_LOSS":
            stop_loss_count += 1
        elif close_name == "TAKE_PROFIT":
            take_profit_count += 1
        elif close_name == "TRAILING_STOP":
            trailing_stop_count += 1
        elif close_name == "EARLY_STOP":
            early_stop_count += 1

    return {
        "completed_cycles": completed_cycles,
        "stop_loss_count": stop_loss_count,
        "take_profit_count": take_profit_count,
        "trailing_stop_count": trailing_stop_count,
        "early_stop_count": early_stop_count,
        "last_cycle_timestamp": last_cycle_timestamp,
    }


def count_disconnects(log_path: Path, window_hours: float) -> dict[str, Any]:
    if not log_path.exists():
        return {"disconnect_count": 0, "window_start": None}

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    disconnect_count = 0

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            timestamp = parse_log_timestamp(line)
            if timestamp is None or timestamp < window_start:
                continue
            lower_line = line.lower()
            if any(pattern in lower_line for pattern in DISCONNECT_PATTERNS):
                disconnect_count += 1

    return {"disconnect_count": disconnect_count, "window_start": window_start}


def parse_log_timestamp(line: str) -> Optional[datetime]:
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ts_to_datetime(timestamp: Optional[float]) -> Optional[datetime]:
    if timestamp is None:
        return None
    value = float(timestamp)
    if value >= 1e18:
        value /= 1e9
    elif value >= 1e15:
        value /= 1e6
    elif value >= 1e12:
        value /= 1e3
    return datetime.fromtimestamp(value, tz=timezone.utc)


def hours_since(value: Optional[datetime], now: datetime) -> Optional[float]:
    if value is None:
        return None
    return round((now - value).total_seconds() / 3600, 2)


def build_summary(config: MonitorConfig, bot_name: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    health = api_get(config, f"/bot-orchestration/{bot_name}/health")["data"]
    orders = api_get(
        config,
        f"/bot-orchestration/{bot_name}/orders?active_only=true&limit=20&event_limit=20",
    )["data"]

    db_path = sqlite_path(config.repo_root, bot_name)
    if not db_path.exists():
        raise RuntimeError(f"Bot database not found at {db_path}")

    fills = read_fill_metrics(db_path)
    cycles = read_cycle_metrics(db_path)
    disconnects = count_disconnects(bot_log_path(config.repo_root, bot_name), config.disconnect_window_hours)
    started_at = docker_started_at(bot_name)

    uptime_hours = hours_since(started_at, now)
    last_fill_age_hours = hours_since(fills["last_fill_timestamp"], now)
    no_fill_age_hours = last_fill_age_hours if last_fill_age_hours is not None else uptime_hours

    take_profit_count = cycles["take_profit_count"]
    stop_loss_count = cycles["stop_loss_count"]
    sl_tp_ratio = None
    if take_profit_count > 0:
        sl_tp_ratio = round(stop_loss_count / take_profit_count, 2)
    elif stop_loss_count > 0:
        sl_tp_ratio = float("inf")

    recommendation, status_flags = evaluate_monitor_state(
        config=config,
        uptime_hours=uptime_hours,
        fill_count=fills["fill_count"],
        completed_cycles=cycles["completed_cycles"],
        stop_loss_count=stop_loss_count,
        take_profit_count=take_profit_count,
        sl_tp_ratio=sl_tp_ratio,
        disconnect_count=disconnects["disconnect_count"],
        no_fill_age_hours=no_fill_age_hours,
        active_order_count=orders["active_order_count"],
        bot_status=health["bot_status"],
        recently_active=health["recently_active"],
    )

    return {
        "bot_name": bot_name,
        "bot_status": health["bot_status"],
        "recently_active": health["recently_active"],
        "uptime_hours": uptime_hours,
        "active_order_count": orders["active_order_count"],
        "fill_count": fills["fill_count"],
        "first_fill_timestamp": iso_or_none(fills["first_fill_timestamp"]),
        "last_fill_timestamp": iso_or_none(fills["last_fill_timestamp"]),
        "no_fill_age_hours": no_fill_age_hours,
        "completed_cycles": cycles["completed_cycles"],
        "stop_loss_count": stop_loss_count,
        "take_profit_count": take_profit_count,
        "trailing_stop_count": cycles["trailing_stop_count"],
        "early_stop_count": cycles["early_stop_count"],
        "stop_loss_to_take_profit_ratio": sl_tp_ratio,
        "disconnect_count_window": disconnects["disconnect_count"],
        "disconnect_window_hours": config.disconnect_window_hours,
        "latest_active_orders": orders["orders"][:2],
        "status_flags": status_flags,
        "recommendation": recommendation,
    }


def evaluate_monitor_state(
    *,
    config: MonitorConfig,
    uptime_hours: Optional[float],
    fill_count: int,
    completed_cycles: int,
    stop_loss_count: int,
    take_profit_count: int,
    sl_tp_ratio: Optional[float],
    disconnect_count: int,
    no_fill_age_hours: Optional[float],
    active_order_count: int,
    bot_status: str,
    recently_active: bool,
) -> tuple[str, list[str]]:
    flags: list[str] = []

    if bot_status != "running" or not recently_active:
        flags.append("bot_inactive")
        return "Bot is not actively running; restore healthy runtime before evaluating strategy behavior.", flags

    if active_order_count < config.active_order_minimum:
        flags.append("quote_gap")

    if disconnect_count >= config.disconnect_threshold:
        flags.append("disconnect_instability")

    if uptime_hours is not None and uptime_hours >= config.uptime_threshold_hours:
        flags.append("uptime_threshold_met")

    if fill_count >= config.min_fills:
        flags.append("fill_threshold_met")

    if completed_cycles >= config.min_cycles:
        flags.append("cycle_threshold_met")

    if no_fill_age_hours is not None and no_fill_age_hours >= config.no_fill_timeout_hours:
        flags.append("no_fill_timeout")

    if take_profit_count == 0 and stop_loss_count >= max(2, config.min_cycles):
        flags.append("stop_loss_dominant")
    elif sl_tp_ratio is not None and sl_tp_ratio != float("inf") and sl_tp_ratio >= config.sl_to_tp_alert_ratio:
        flags.append("stop_loss_dominant")

    if "disconnect_instability" in flags:
        return (
            f"Bot is quoting, but connector disconnects repeated {disconnect_count} times in the last "
            f"{config.disconnect_window_hours:g}h; stabilize connectivity before trusting performance reads.",
            flags,
        )

    if "fill_threshold_met" in flags and "cycle_threshold_met" in flags:
        if "stop_loss_dominant" in flags:
            return (
                f"Bot has enough completed cycles to evaluate expectancy, but stop losses are outpacing take profits "
                f"({stop_loss_count} SL vs {take_profit_count} TP); current parameters likely need adjustment.",
                flags,
            )
        return "Bot has enough completed cycles to evaluate expectancy.", flags

    if "no_fill_timeout" in flags:
        no_fill_text = f"{no_fill_age_hours:.1f}" if no_fill_age_hours is not None else "many"
        return (
            f"Bot is healthy but underfilled for {no_fill_text} hours at the current spread; consider wider or narrower quoting.",
            flags,
        )

    if "uptime_threshold_met" in flags:
        return (
            f"Bot cleared the uptime threshold and is still collecting data; fills={fill_count}, completed_cycles={completed_cycles}.",
            flags,
        )

    return (
        f"Bot is still in the observation window; uptime={uptime_hours or 0:.1f}h, fills={fill_count}, completed_cycles={completed_cycles}.",
        flags,
    )


def iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def parse_args() -> MonitorConfig:
    parser = argparse.ArgumentParser(description="Lightweight PMM bot monitor")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--api-user", default="admin")
    parser.add_argument("--api-pass", default="admin")
    parser.add_argument("--bot-name", default=None)
    parser.add_argument("--bot-prefix", default="hl-testnet-pmm-btc")
    parser.add_argument("--uptime-threshold-hours", type=float, default=24.0)
    parser.add_argument("--min-fills", type=int, default=10)
    parser.add_argument("--min-cycles", type=int, default=5)
    parser.add_argument("--no-fill-timeout-hours", type=float, default=36.0)
    parser.add_argument("--disconnect-window-hours", type=float, default=6.0)
    parser.add_argument("--disconnect-threshold", type=int, default=3)
    parser.add_argument("--sl-to-tp-alert-ratio", type=float, default=1.5)
    parser.add_argument("--active-order-minimum", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    return MonitorConfig(
        repo_root=Path(args.repo_root).resolve(),
        api_url=args.api_url,
        api_user=args.api_user,
        api_pass=args.api_pass,
        bot_name=args.bot_name,
        bot_prefix=args.bot_prefix,
        uptime_threshold_hours=args.uptime_threshold_hours,
        min_fills=args.min_fills,
        min_cycles=args.min_cycles,
        no_fill_timeout_hours=args.no_fill_timeout_hours,
        disconnect_window_hours=args.disconnect_window_hours,
        disconnect_threshold=args.disconnect_threshold,
        sl_to_tp_alert_ratio=args.sl_to_tp_alert_ratio,
        active_order_minimum=args.active_order_minimum,
        json_output=args.json,
    )


def main() -> int:
    config = parse_args()
    try:
        bot_name = resolve_bot_name(config)
        summary = build_summary(config, bot_name)
    except Exception as exc:
        print(f"Monitor failed: {exc}", file=sys.stderr)
        return 1

    if config.json_output:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print(f"Bot: {summary['bot_name']}")
    print(f"Status: {summary['bot_status']} | recently_active={summary['recently_active']}")
    print(
        f"Uptime: {summary['uptime_hours']}h | active_orders={summary['active_order_count']} | "
        f"fills={summary['fill_count']} | completed_cycles={summary['completed_cycles']}"
    )
    print(
        f"Close types: TP={summary['take_profit_count']} | SL={summary['stop_loss_count']} | "
        f"trailing={summary['trailing_stop_count']} | early_stop={summary['early_stop_count']}"
    )
    print(
        f"No-fill age: {summary['no_fill_age_hours']}h | disconnects({summary['disconnect_window_hours']}h)="
        f"{summary['disconnect_count_window']}"
    )
    print(f"Flags: {', '.join(summary['status_flags']) if summary['status_flags'] else 'none'}")
    print(f"Recommendation: {summary['recommendation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

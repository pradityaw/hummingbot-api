#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ACTIVE_ORDER_STATUSES = {
    "BuyOrderCreated",
    "SellOrderCreated",
    "OrderCreated",
    "OPEN",
    "PENDING_CREATE",
    "PENDING_CANCEL",
    "PARTIALLY_FILLED",
}

SIGNAL_PATTERNS = {
    "hyperliquid_504": "HTTP status is 504",
    "ws_closed": "websocket connection was closed",
    "network_not_connected": "networkstatus.not_connected",
    "price_fetch_error": "Error fetching last traded price",
    "cancel_failed": "Failed to cancel order",
    "mqtt_disconnected": "MQTT bridge disconnected",
}


def ts_to_dt(timestamp: Optional[float]) -> Optional[datetime]:
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


def iso(value: Optional[datetime]) -> str:
    return value.isoformat() if value else "n/a"


def parse_log_time(line: str) -> Optional[datetime]:
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def instance_paths(repo_root: Path, bot_name: str) -> tuple[Path, Path]:
    instance_dir = repo_root / "bots" / "instances" / bot_name
    db_path = instance_dir / "data" / f"{bot_name}.sqlite"
    log_path = instance_dir / "logs" / f"logs_{bot_name}.log"
    return db_path, log_path


def read_database_summary(db_path: Path) -> dict:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        orders = conn.execute(
            """
            select
                count(*) as total_orders,
                sum(case when last_status like '%Completed' then 1 else 0 end) as completed_orders,
                sum(case when last_status = 'OrderCancelled' then 1 else 0 end) as cancelled_orders,
                sum(case when last_status in ({}) then 1 else 0 end) as active_orders,
                min(creation_timestamp) as first_order_ts,
                max(last_update_timestamp) as last_order_update_ts
            from "Order"
            """.format(",".join("?" for _ in ACTIVE_ORDER_STATUSES)),
            sorted(ACTIVE_ORDER_STATUSES),
        ).fetchone()
        fills = conn.execute(
            """
            select
                count(*) as fill_count,
                min(timestamp) as first_fill_ts,
                max(timestamp) as last_fill_ts,
                sum((price / 1000000.0) * (amount / 1000000.0)) as quote_notional,
                sum(case when trade_type = 'BUY' then (price / 1000000.0) * (amount / 1000000.0) else 0 end) as buy_notional,
                sum(case when trade_type = 'SELL' then (price / 1000000.0) * (amount / 1000000.0) else 0 end) as sell_notional
            from TradeFill
            """
        ).fetchone()
        open_orders = conn.execute(
            """
            select id, last_status, creation_timestamp, last_update_timestamp, price, amount
            from "Order"
            where last_status in ({})
            order by creation_timestamp desc
            limit 10
            """.format(",".join("?" for _ in ACTIVE_ORDER_STATUSES)),
            sorted(ACTIVE_ORDER_STATUSES),
        ).fetchall()

    return {
        "orders": dict(orders),
        "fills": dict(fills),
        "open_orders": [dict(row) for row in open_orders],
    }


def read_log_summary(log_path: Path) -> dict:
    first_seen = {}
    last_seen = {}
    counts = Counter()
    order_events = Counter()
    first_log_time = None
    last_log_time = None

    event_re = re.compile(r"EVENT_LOG - (?P<payload>\\{.*\\})")
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            timestamp = parse_log_time(line)
            if timestamp:
                first_log_time = first_log_time or timestamp
                last_log_time = timestamp

            lower_line = line.lower()
            for name, pattern in SIGNAL_PATTERNS.items():
                if pattern.lower() in lower_line:
                    counts[name] += 1
                    if timestamp and not first_seen.get(name):
                        first_seen[name] = timestamp
                    if timestamp:
                        last_seen[name] = timestamp

            match = event_re.search(line)
            if match:
                try:
                    event = json.loads(match.group("payload"))
                except json.JSONDecodeError:
                    continue
                event_name = event.get("event_name")
                if event_name:
                    order_events[event_name] += 1

    return {
        "first_log_time": first_log_time,
        "last_log_time": last_log_time,
        "signal_counts": counts,
        "signal_first_seen": first_seen,
        "signal_last_seen": last_seen,
        "order_events": order_events,
    }


def render_markdown(bot_name: str, db_path: Path, log_path: Path, db: dict, logs: dict) -> str:
    orders = db["orders"]
    fills = db["fills"]
    signal_counts = logs["signal_counts"]
    first_seen = logs["signal_first_seen"]
    last_seen = logs["signal_last_seen"]

    lines = [
        f"# Hummingbot Postmortem: {bot_name}",
        "",
        "## Executive Summary",
        "",
        "- The bot successfully started, connected to Hyperliquid testnet, created/cancelled quotes, and recorded fills.",
        "- The first degradation signal was exchange-side/WebSocket instability, followed later by repeated Hyperliquid testnet 504s.",
        "- Local Docker/API is currently a separate availability concern: if Docker Desktop is down, the API and broker cannot be queried or repaired in-process.",
        "- The safe forward posture is fail closed on exchange instability, keep compose services health-checked, and run a watchdog that repairs only infrastructure services.",
        "",
        "## Timeline",
        "",
        f"- Log window: `{iso(logs['first_log_time'])}` to `{iso(logs['last_log_time'])}`",
        f"- First order: `{iso(ts_to_dt(orders['first_order_ts']))}`",
        f"- Last order DB update: `{iso(ts_to_dt(orders['last_order_update_ts']))}`",
        f"- First fill: `{iso(ts_to_dt(fills['first_fill_ts']))}`",
        f"- Last fill: `{iso(ts_to_dt(fills['last_fill_ts']))}`",
        f"- First WebSocket close: `{iso(first_seen.get('ws_closed'))}`",
        f"- First cancel failure: `{iso(first_seen.get('cancel_failed'))}`",
        f"- First Hyperliquid 504: `{iso(first_seen.get('hyperliquid_504'))}`",
        f"- Last MQTT disconnect: `{iso(last_seen.get('mqtt_disconnected'))}`",
        "",
        "## Trading Activity",
        "",
        f"- Total orders recorded: `{orders['total_orders']}`",
        f"- Completed orders: `{orders['completed_orders'] or 0}`",
        f"- Cancelled quote-refresh orders: `{orders['cancelled_orders'] or 0}`",
        f"- Active/created orders at last DB state: `{orders['active_orders'] or 0}`",
        f"- Fills recorded: `{fills['fill_count']}`",
        f"- Gross quote notional filled: `{(fills['quote_notional'] or 0):.6f}`",
        f"- Buy notional: `{(fills['buy_notional'] or 0):.6f}`",
        f"- Sell notional: `{(fills['sell_notional'] or 0):.6f}`",
        "",
        "## Failure Signals",
        "",
    ]

    for name in SIGNAL_PATTERNS:
        lines.append(
            f"- `{name}`: `{signal_counts[name]}` "
            f"(first `{iso(first_seen.get(name))}`, last `{iso(last_seen.get(name))}`)"
        )

    lines.extend([
        "",
        "## Open Orders At Last State",
        "",
    ])
    if db["open_orders"]:
        for order in db["open_orders"]:
            lines.append(
                f"- `{order['id']}` `{order['last_status']}` price `{order['price'] / 1000000.0:.2f}` "
                f"amount `{order['amount'] / 1000000.0:.8f}` updated `{iso(ts_to_dt(order['last_update_timestamp']))}`"
            )
    else:
        lines.append("- None recorded.")

    lines.extend([
        "",
        "## Root Cause",
        "",
        "Primary cause: Hyperliquid testnet connectivity degraded. The bot first recovered from a private WebSocket close, then later saw repeated REST `504 Gateway Timeout` responses from both `/info` and `/exchange`. Once price fetches and cancels were timing out, the PMM loop could no longer be trusted to maintain clean quote state.",
        "",
        "Contributing cause: the local API/broker runtime did not have enough health-gated recovery tooling. When Docker Desktop later stopped, the API was completely unreachable, which hid live bot state and forced offline reconstruction from SQLite/logs.",
        "",
        "## Prevention Plan",
        "",
        "- Use `make deploy` after this patch so the Hyperliquid override image is included automatically.",
        "- Run `ops/watch_hummingbot_stack.sh` from cron/launchd every 1-5 minutes with `--repair` for infrastructure-only recovery.",
        "- Treat repeated `hyperliquid_504`, `price_fetch_error`, or `cancel_failed` as a no-trade condition. Stop/redeploy bots only after explicit review because those actions affect orders.",
        "- Prefer the wider PMM config for the next run to reduce refresh pressure while validating exchange stability.",
        "- Keep a postmortem artifact per run with this script so order state and connectivity incidents are auditable.",
        "",
        "## Sources",
        "",
        f"- SQLite: `{db_path}`",
        f"- Log: `{log_path}`",
    ])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an offline postmortem for the Hyperliquid testnet PMM bot.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--bot-name", default="hl-testnet-pmm-btc-20260618-081557")
    parser.add_argument("--output", default=None, help="Optional markdown output path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    db_path, log_path = instance_paths(repo_root, args.bot_name)
    if not db_path.exists():
        raise SystemExit(f"Bot database not found: {db_path}")
    if not log_path.exists():
        raise SystemExit(f"Bot log not found: {log_path}")

    report = render_markdown(
        bot_name=args.bot_name,
        db_path=db_path,
        log_path=log_path,
        db=read_database_summary(db_path),
        logs=read_log_summary(log_path),
    )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(output_path)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

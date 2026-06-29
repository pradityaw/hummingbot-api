# Hummingbot Postmortem: hl-testnet-pmm-btc-20260618-081557

## Executive Summary

- The bot successfully started, connected to Hyperliquid testnet, created/cancelled quotes, and recorded fills.
- The first degradation signal was exchange-side/WebSocket instability, followed later by repeated Hyperliquid testnet 504s.
- Local Docker/API is currently a separate availability concern: if Docker Desktop is down, the API and broker cannot be queried or repaired in-process.
- The safe forward posture is fail closed on exchange instability, keep compose services health-checked, and run a watchdog that repairs only infrastructure services.

## Timeline

- Log window: `2026-06-18T08:16:00+00:00` to `2026-06-18T10:05:53+00:00`
- First order: `2026-06-18T08:16:03+00:00`
- Last order DB update: `2026-06-18T09:17:56.075000+00:00`
- First fill: `2026-06-18T09:17:35+00:00`
- Last fill: `2026-06-18T09:17:56+00:00`
- First WebSocket close: `2026-06-18T08:26:14+00:00`
- First cancel failure: `2026-06-18T08:41:59+00:00`
- First Hyperliquid 504: `2026-06-18T09:10:46+00:00`
- Last MQTT disconnect: `2026-06-18T10:05:53+00:00`

## Trading Activity

- Total orders recorded: `232`
- Completed orders: `4`
- Cancelled quote-refresh orders: `226`
- Active/created orders at last DB state: `2`
- Fills recorded: `4`
- Gross quote notional filled: `49.077380`
- Buy notional: `24.479980`
- Sell notional: `24.597400`

## Failure Signals

- `hyperliquid_504`: `223` (first `2026-06-18T09:10:46+00:00`, last `2026-06-18T10:05:38+00:00`)
- `ws_closed`: `15` (first `2026-06-18T08:26:14+00:00`, last `2026-06-18T09:24:40+00:00`)
- `network_not_connected`: `2` (first `2026-06-18T09:10:46+00:00`, last `2026-06-18T09:24:46+00:00`)
- `price_fetch_error`: `42` (first `2026-06-18T09:11:20+00:00`, last `2026-06-18T10:05:38+00:00`)
- `cancel_failed`: `65` (first `2026-06-18T08:41:59+00:00`, last `2026-06-18T10:05:46+00:00`)
- `mqtt_disconnected`: `2` (first `2026-06-18T10:05:48+00:00`, last `2026-06-18T10:05:53+00:00`)

## Open Orders At Last State

- `0x6219ecd2af776ab6be4fc296a8590f0e` `SellOrderCreated` price `65014.00` amount `0.00019000` updated `2026-06-18T09:17:56+00:00`
- `0x55e7fbfb646357de8d9d37643784443b` `BuyOrderCreated` price `64084.00` amount `0.00019000` updated `2026-06-18T09:17:53+00:00`

## Root Cause

Primary cause: Hyperliquid testnet connectivity degraded. The bot first recovered from a private WebSocket close, then later saw repeated REST `504 Gateway Timeout` responses from both `/info` and `/exchange`. Once price fetches and cancels were timing out, the PMM loop could no longer be trusted to maintain clean quote state.

Contributing cause: the local API/broker runtime did not have enough health-gated recovery tooling. When Docker Desktop later stopped, the API was completely unreachable, which hid live bot state and forced offline reconstruction from SQLite/logs.

## Prevention Plan

- Use `make deploy` after this patch so the Hyperliquid override image is included automatically.
- Run `ops/watch_hummingbot_stack.sh` from cron/launchd every 1-5 minutes with `--repair` for infrastructure-only recovery.
- Treat repeated `hyperliquid_504`, `price_fetch_error`, or `cancel_failed` as a no-trade condition. Stop/redeploy bots only after explicit review because those actions affect orders.
- Prefer the wider PMM config for the next run to reduce refresh pressure while validating exchange stability.
- Keep a postmortem artifact per run with this script so order state and connectivity incidents are auditable.

## Sources

- SQLite: `/Users/dubski/hummingbot-api/bots/instances/hl-testnet-pmm-btc-20260618-081557/data/hl-testnet-pmm-btc-20260618-081557.sqlite`
- Log: `/Users/dubski/hummingbot-api/bots/instances/hl-testnet-pmm-btc-20260618-081557/logs/logs_hl-testnet-pmm-btc-20260618-081557.log`

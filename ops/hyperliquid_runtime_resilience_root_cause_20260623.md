# Hyperliquid Runtime Resilience Root Cause

Date written: 2026-06-23
Bot: `hl-testnet-pmm-btc-20260618-081557`
Exchange: `hyperliquid_perpetual_testnet`
Trading pair: `BTC-USD`

## Command results

- `python3 ops/test_watch_hyperliquid_pmm_remote.py`: passed; output included `watchdog verification passed`.
- `python3 ops/postmortem_hyperliquid_testnet_pmm.py --bot-name hl-testnet-pmm-btc-20260618-081557`: passed and reproduced the existing incident summary.

## Exact incident timeline

- Log window: `2026-06-18T08:16:00+00:00` to `2026-06-18T10:05:53+00:00`.
- Initial connector ready path:
  - `2026-06-18 08:16:01,370` - Hyperliquid connector network status changed to `NetworkStatus.CONNECTED`.
  - `2026-06-18 08:16:01,578` - public order book/trade/funding streams subscribed.
  - `2026-06-18 08:16:01,711` - private order/trade stream subscribed.
- First order: `2026-06-18T08:16:03+00:00`.
- First public WebSocket disconnect:
  - `2026-06-18 08:26:14,559` - public order book data source logged `The websocket connection was closed`, close code `1006`, message data `1000`.
  - `2026-06-18 08:26:14,768` - public streams resubscribed.
  - `2026-06-18 08:26:26,740` and `2026-06-18 08:26:26,947` - new buy/sell quote orders were created after WS reopen, without explicit runtime reconciliation evidence.
- First private/user WebSocket disconnect:
  - `2026-06-18 08:27:25,266` - private user stream logged `The websocket connection was closed`, close code `1006`, message data `1000`.
  - `2026-06-18 08:27:25,589` - private stream resubscribed.
- First cancel failure:
  - `2026-06-18 08:41:59,229` - cancel of order `0xd999d12aca4ce72db07d9e9d3c6c21e8` failed with `TypeError: string indices must be integers, not 'str'` while handling the Hyperliquid cancel response.
  - `2026-06-18 08:42:00,128` and `2026-06-18 08:42:00,139` - new buy/sell quote orders were still created immediately after that cancel failure.
- First open-order placement failure:
  - `2026-06-18 08:44:34,775` - `Open order failed 0x65c15a5fe3aba2b7cbd463fab79146a5. Retrying 0/10`, caused by `Invalid nonce: duplicate nonce 1781772274647`.
- First hard disconnect/outage cluster:
  - `2026-06-18 09:10:45,468` - private/user WS closed, close code `1006`, message data `None`.
  - `2026-06-18 09:10:45,470` - public WS closed, close code `1006`, message data `None`.
  - `2026-06-18 09:10:45,757` - private WS reconnect failed with `aiohttp.client_exceptions.WSServerHandshakeError: 502`.
  - `2026-06-18 09:10:45,766` - public WS reconnect failed with `aiohttp.client_exceptions.WSServerHandshakeError: 502`.
  - `2026-06-18 09:10:46,565` - first recorded Hyperliquid REST `504 Gateway Timeout` on `POST https://api.hyperliquid-testnet.xyz/info`.
  - `2026-06-18 09:10:46,594` - Hyperliquid REST `504 Gateway Timeout` on `POST https://api.hyperliquid-testnet.xyz/exchange` while submitting a buy order.
  - `2026-06-18 09:10:46,761` - connector network status changed to `NetworkStatus.NOT_CONNECTED`.
- Reconnect evidence:
  - `2026-06-18 09:11:38,213` - connector network status changed back to `NetworkStatus.CONNECTED`.
  - `2026-06-18 09:11:38,336` - order book initialized for `BTC-USD`.
  - `2026-06-18 09:11:38,419` - public streams resubscribed.
  - `2026-06-18 09:11:38,655` - private streams resubscribed.
  - `2026-06-18 09:11:38,471` through `2026-06-18 09:11:38,477` - immediately after reconnect, funding/account REST calls still failed with Hyperliquid `500`, so WS resubscription did not prove safe recovery.
- Second hard disconnect/outage cluster:
  - `2026-06-18 09:24:40,098` - public WS closed, close code `1006`, message data `None`.
  - `2026-06-18 09:24:40,106` - private/user WS closed, close code `1006`, message data `None`.
  - `2026-06-18 09:24:40,387` and `2026-06-18 09:24:40,394` - public/private WS reconnect attempts failed with `502`.
  - `2026-06-18 09:24:46,715` - connector network status changed to `NetworkStatus.NOT_CONNECTED`.
- Last order DB update: `2026-06-18T09:17:56.075000+00:00`.
- Last fill: `2026-06-18T09:17:56+00:00`.
- Last Hyperliquid 504 in the postmortem signal scan: `2026-06-18T10:05:38+00:00`.
- Last cancel failure in the postmortem signal scan: `2026-06-18T10:05:46+00:00`.
- Last MQTT disconnect: `2026-06-18T10:05:53+00:00`.

## Watchdog impact

The existing watchdog correctly detects enough historical instability to stop the bot in the June 18 fixture, and the regression test confirms `exchange_5xx_instability` and `open_order_failure_spike` remain stop reasons. However, it is still primarily a log-history and passive-probe consumer. It does not receive an authoritative current runtime safety state from the bot. As a result, it can only react after repeated failures have already occurred, and it cannot independently know whether the runtime has reconciled orders, balances, positions, order book freshness, and user stream freshness.

## Readiness impact

The current resume readiness depends on accumulated passive probe success plus a clean watchdog with zero active orders. This can keep readiness false because of watchdog/log history rather than current unsafe state or incomplete reconciliation, and it does not prove that a running bot has completed runtime reconciliation. Historical disconnects alone should not permanently block readiness after the runtime has proven healthy recovery.

## Most likely root cause

The direct exchange-side trigger was Hyperliquid testnet instability: WebSocket closes, failed WS handshakes with `502`, REST `/info` and `/exchange` `504 Gateway Timeout`, REST `500`, and later repeated cancel/status/funding failures.

The bot-side resilience gap was the lack of a first-class runtime connectivity and recovery state machine. WebSocket reopen/resubscribe was treated as enough for normal strategy operation, while quote creation continued after disconnects, cancel failures, duplicate nonce failures, and REST 5xx/504 failures. The bot did not explicitly gate quote creation on current public WS freshness, private/user stream freshness, REST health, balance refresh, open-order reconciliation, position refresh, and order book freshness.

## Remaining unknowns

- Whether the currently deployed production bot container is still based on `hummingbot/hummingbot:latest` or `hummingbot/hummingbot:hyperliquid-fix`; this requires Docker inspection.
- The exact upstream Hummingbot connector version in the active image; the runtime overlay should be extracted from the actual image before patching.
- Whether Hyperliquid testnet occasionally returns non-standard cancel payloads that need connector-specific defensive parsing beyond state gating.
- Whether the duplicate nonce failure was caused by retry timing after earlier exchange failure, local nonce generation, or Hyperliquid accepting/rejecting a prior request ambiguously.
- Whether any open maker orders existed on exchange after the final local DB state; live exchange reconciliation is required before any resume decision.

# Hyperliquid Testnet Handoff

Date: 2026-06-23

## Current live state

- Hummingbot API is running live from patched image: `hummingbot/hummingbot-api:hyperliquid-fix`
- Hummingbot bot is running live from patched image: `hummingbot/hummingbot:hyperliquid-fix`
- Active running bot: `hl-testnet-pmm-btc-20260618-081557`
- Exchange connector in use: `hyperliquid_perpetual_testnet`
- Credentials profile: `master_account`
- MQTT/API currently see the bot as active and running.
- Live runtime connectivity currently reports:
  - `current_state=HEALTHY`
  - `public_ws_status=connected`
  - `private_user_ws_status=connected`
  - `rest_health=healthy`
  - `quoting_enabled=true`
- Active bot orders are currently visible again through the API.
- The pre-patch stopped bot container was preserved as:
  - `hl-testnet-pmm-btc-20260618-081557-prepatch-backup`

## What was fixed

- Hyperliquid testnet credentials were loaded successfully.
- `BOTS_PATH` in `.env` was corrected to the host repo root:
  - `BOTS_PATH=/Users/dubski/hummingbot-api`
- A local compose override was added:
  - `docker-compose.hyperliquid-fix.yml`
- Hyperliquid connector patch files were added under:
  - `docker/hyperliquid-patch/`
- The Hyperliquid connector was patched to:
  - disable HIP-3 market loading by default
  - use `split(':', 1)` in the Hyperliquid perp connector parser
- API image build flow now overlays local API source before applying the Hyperliquid patch:
  - `docker/hyperliquid-patch/Dockerfile.api`
- A production-grade runtime connectivity/recovery safety layer was added:
  - explicit states: `HEALTHY`, `DEGRADED_TRANSIENT`, `DEGRADED_UNSAFE`, `HARD_DISCONNECTED`, `RECOVERING`
  - quote gating when runtime is unsafe
  - reconciliation before quote resume
  - structured connectivity state and event log persisted under each bot instance
- Hyperliquid public/private stream and REST telemetry was added to the runtime overlay.
- API connectivity endpoints were added:
  - `GET /bot-orchestration/{bot_name}/connectivity`
  - `GET /bot-orchestration/{bot_name}/connectivity/events`
- Watchdog/probe/readiness logic now prefers current structured runtime state over stale historical disconnect history.
- During rollout, a recovery-gating bug was found and fixed:
  - pure `order_path_failure` now enters `RECOVERING` instead of getting stuck permanently in `DEGRADED_UNSAFE`

## Strategy state

- `supertrend_v1` was not viable as the first live proof because this runtime did not expose
  `hyperliquid_perpetual_testnet` as a usable candles connector.
- Working controller config now:
  - `bots/conf/controllers/hl_testnet_pmm_btc.yml`
- Running strategy is `pmm_simple` on `BTC-USD` with small size and `1x` leverage.
- A non-deployed candidate config exists for the next safer PMM trial:
  - `bots/conf/controllers/hl_testnet_pmm_btc_wide.yml`
  - Wider `0.003` bid/ask spreads and slower `45s` refresh

## Useful files

- `.hyperliquid_perpetual_testnet_credentials.json`
- `apply_hyperliquid_testnet_credentials.sh`
- `bots/conf/controllers/hl_testnet_pmm_btc.yml`
- `bots/conf/controllers/hl_testnet_pmm_btc_wide.yml`
- `bots/scripts/connectivity_resilience.py`
- `bots/scripts/v2_with_controllers.py`
- `docker-compose.hyperliquid-fix.yml`
- `docker/hyperliquid-patch/patch_hyperliquid_connector.py`
- `docker/hyperliquid-patch/Dockerfile.api`
- `docker/hyperliquid-patch/Dockerfile.bot`
- `ops/hyperliquid_runtime_resilience_root_cause_20260623.md`
- `ops/watch_hyperliquid_pmm_remote.sh`
- `ops/probe_hyperliquid_testnet.py`
- `ops/check_hyperliquid_resume_ready.sh`
- `ops/test_watch_hyperliquid_pmm_remote.py`
- `ops/monitor_hyperliquid_testnet_pmm.sh`
- `ops/monitor_pmm_bot.py`
- `bots/instances/hl-testnet-pmm-btc-20260618-081557/data/connectivity/runtime_connectivity_state.json`
- `bots/instances/hl-testnet-pmm-btc-20260618-081557/data/connectivity/runtime_connectivity_events.jsonl`

## Quick checks

- API root:
  - `curl -u admin:admin http://localhost:8000/`
- Bot status:
  - `curl -u admin:admin http://localhost:8000/bot-orchestration/status`
- MQTT/API bot visibility:
  - `curl -u admin:admin http://localhost:8000/bot-orchestration/mqtt`
- Bot health:
  - `curl -u admin:admin http://localhost:8000/bot-orchestration/hl-testnet-pmm-btc-20260618-081557/health`
- Connectivity state:
  - `curl -u admin:admin http://localhost:8000/bot-orchestration/hl-testnet-pmm-btc-20260618-081557/connectivity`
- Connectivity events:
  - `curl -u admin:admin 'http://localhost:8000/bot-orchestration/hl-testnet-pmm-btc-20260618-081557/connectivity/events?limit=20'`
- Active bot orders:
  - `curl -u admin:admin 'http://localhost:8000/bot-orchestration/hl-testnet-pmm-btc-20260618-081557/orders?active_only=true&limit=10&event_limit=10'`
- Readiness gate:
  - `ops/check_hyperliquid_resume_ready.sh`
- Full read-only PMM check:
  - `ops/check_hyperliquid_testnet_pmm.sh`
- Strategy observation monitor:
  - `BOT_NAME=hl-testnet-pmm-btc-20260618-081557 ops/monitor_hyperliquid_testnet_pmm.sh`
- Docker containers:
  - `PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" docker ps`

## Live incident update after observation

- A real live instability was captured on `2026-06-23`.
- The bot stayed visible through API/MQTT and the container remained up, but runtime state moved into sustained unsafe mode rather than falsely appearing healthy.
- Current observed runtime state during the incident:
  - `current_state=HARD_DISCONNECTED`
  - `public_ws_status=connected`
  - `private_user_ws_status=closed`
  - `rest_health=unhealthy`
  - `quoting_enabled=false`
  - `reconciliation_result=required`
- The runtime state machine correctly failed safe:
  - quotes stayed gated while unsafe
  - no quote resume happened before reconciliation completed
  - watchdog/readiness reflected current runtime state rather than stale history
- Recent order observations during the incident:
  - `active_order_count=0`
  - recent order events stopped after failure/cancel activity
  - latest failure events included `ClientConnectorDNSError` against `api.hyperliquid-testnet.xyz`
- Current live container logs showed repeated connection failures such as:
  - `Cannot connect to host api.hyperliquid-testnet.xyz:443 ssl:default [Name or service not known]`
  - repeated account/trade update failures
  - repeated user stream retry loops
  - price-fetch failures cascading from missing exchange responses
- A direct passive probe from the host also failed DNS resolution for `api.hyperliquid-testnet.xyz` during the incident window.
- This means the runtime resilience/safety layer appears to be working as intended under exchange/network failure, but the bot is not currently operational for quoting because upstream connectivity is still broken.
- One positive sign relative to the first observed unsafe period: later snapshots showed
  - `orders_unknown=false`
  - `open_order_count=0`
  - `open_order_count_during_disconnect=0`
  indicating local order uncertainty was eventually cleared, even though exchange connectivity did not recover.

## Suggested next work

- Investigate and implement a permanent connection fix for Hyperliquid testnet connectivity failures, with special focus on DNS resolution/network behavior inside the live bot container.
- Determine whether the root cause is:
  - Docker/container DNS configuration on the host
  - transient upstream Hyperliquid testnet DNS or edge instability
  - connector retry/backoff behavior that needs hardening
  - insufficient connector-side defensive handling when REST/WS/DNS failures partially recover
- Compare host vs container resolution and connectivity for `api.hyperliquid-testnet.xyz` and `wss://api.hyperliquid-testnet.xyz/ws`.
- Capture whether a durable mitigation should live in:
  - Docker compose/network config
  - container DNS settings
  - bot image/runtime startup config
  - connector source patching
  - a resilience sidecar/probe that can force cleaner reconnect behavior
- Preserve the current safety behavior:
  - keep quote gating while unsafe
  - keep reconciliation before quote resume
  - do not weaken watchdog/readiness checks just to make health look green
- If changing quote behavior, deploy a new bot from `hl_testnet_pmm_btc_wide.yml` rather than mutating the running bot.
- Decide whether to keep the patched images locally only or convert the patch into a proper source-based build flow.
- Keep the `*-prepatch-backup` container until the new runtime has been stable long enough to trust the rollout.

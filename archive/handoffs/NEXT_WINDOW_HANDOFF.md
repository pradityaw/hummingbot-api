## Hummingbot VPS Handoff

Date checked: 2026-06-19

### Current state

- Local Mac stack:
  - `hummingbot-api`, `hummingbot-broker`, and `hummingbot-postgres` are running locally.
  - No active local bot is trading.
- Droplet VPS:
  - Host: `168.144.111.10`
  - SSH works as `root@168.144.111.10`
  - Repo path: `/root/hummingbot-api`
  - Remote containers confirmed running:
    - `hummingbot-api`
    - `hummingbot-broker`
    - `hummingbot-postgres`
    - `hl-testnet-pmm-btc-vps-20260618-085408`

### Remote bot status

- Remote API root: `http://localhost:8000/`
- Bot orchestration status shows:
  - active bot: `hl-testnet-pmm-btc-vps-20260618-085408`
  - bot status: `running`
  - MQTT connected: `true`
  - active orders: `2`
  - fills recorded: `72`
  - last fill timestamp: `2026-06-19T05:47:14+00:00`

### Important caveat

- The remote PMM bot is live on testnet, but it is not stable enough to trust yet.
- Remote monitor output showed:
  - `disconnect_count_window = 70`
  - `disconnect_window_hours = 6`
  - recommendation: stabilize connectivity before trusting performance
- Recent remote logs included:
  - websocket disconnects
  - Hyperliquid testnet 500/502-style failures
  - repeated open-order retries

### Local hardening already done

- `make deploy` now automatically includes `docker-compose.hyperliquid-fix.yml`
- local compose healthchecks added
- watchdog added: `ops/watch_hummingbot_stack.sh`
- postmortem generator added: `ops/postmortem_hyperliquid_testnet_pmm.py`
- local LaunchAgent added to watch/repair infra only

### Good next step

Focus on the VPS instance, not local setup:

1. Inspect remote bot logs and monitor output in detail.
2. Quantify whether failures are exchange-side DNS / websocket / REST instability vs bot-side config.
3. Add remote-safe monitoring and fail-closed behavior without restarting trading blindly.
4. Only consider bot restart or config changes after reviewing the live VPS state.

### Additional status after hardening

- The testnet bot was paused cleanly on the VPS.
- Final remote API bot status:
  - `bot_status: stopped`
  - `active_order_count: 0`
- Public Docker port exposure on the VPS was removed:
  - API, Postgres, and EMQX ports now bind to `127.0.0.1` only
  - use SSH tunnels for access
- Remote watchdog is installed and enabled:
  - systemd timer: `hummingbot-hl-watchdog.timer`
  - status file: `/root/hummingbot-api/ops/watchdog-status/latest.json`
- Passive Hyperliquid connectivity probe is installed and enabled:
  - systemd timer: `hyperliquid-testnet-probe.timer`
  - latest probe: `/root/hummingbot-api/ops/hyperliquid-probes/latest.json`
  - readiness check: `/root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh`
  - runbook: `/root/hummingbot-api/ops/HYPERLIQUID_TESTNET_CANARY.md`

### Resume gate

- Do not resume the bot immediately after a single clean probe.
- Wait for enough clean passive probe samples to satisfy:
  - `ops/check_hyperliquid_resume_ready.sh`
  - expected result: `RESUME_READY`
- Default gate is:
  - 30 minute lookback
  - at least 10 successful passive probes
  - clean watchdog with no reasons and zero active orders

### Latest live status

- Date checked: `2026-06-19`
- The canary was resumed successfully and the bot is live again on the VPS:
  - bot: `hl-testnet-pmm-btc-vps-20260618-085408`
  - API status: `running`
  - active orders: `2`
  - watchdog status: clean
  - passive Hyperliquid probe: clean
- Current remote-only health confirms:
  - latest watchdog has `action = none`
  - latest watchdog has no `reasons`
  - latest probe has `ready_now = true`
  - REST probes are succeeding
  - WebSocket handshake probe is succeeding

### Important restart nuance

- `POST /bot-orchestration/start-bot` returned success but did not actually resume the previously stopped quickstart session inside the existing bot container.
- The effective recovery step was:
  - restart only the bot container: `docker restart hl-testnet-pmm-btc-vps-20260618-085408`
- After the targeted bot-container restart:
  - Hummingbot re-entered headless mode
  - MQTT bridge reconnected successfully
  - controller `hl_testnet_pmm_btc` loaded
  - first 2 active quote orders were created on Hyperliquid testnet

### June 22 incident note

- Incident window: `2026-06-22 07:23:50 UTC` to `2026-06-22 07:24:05 UTC`
- Root cause:
  - the watchdog initiated the stop
  - it was not a manual API/MQTT operator stop
  - `journalctl -u hummingbot-hl-watchdog.service` showed:
    - `2026-06-22 07:23:51 UTC`
    - `action=stop_bot reasons=hard_disconnect_instability active_orders=2 recent_order_creates=10`
- Bot-side confirmation:
  - bot log showed `stop command initiated`
  - then `Hummingbot stopped.`
- Follow-on failure mode:
  - later the container stayed `Up`, but the Hummingbot quickstart session had gone idle/stopped
  - the API continued to report `bot_status: running` even while `active_order_count: 0` and no fresh log lines were being written
- Hardening added after this incident:
  - the watchdog now classifies benign vs hard disconnects
  - repeated stop commands are suppressed per active run
  - stalled-runtime detection now stops a bot that is nominally `running` but has zero active orders and no fresh log/order activity for the stale threshold
- Recovery that worked:
  - `docker restart hl-testnet-pmm-btc-vps-20260618-085408`
  - verify controller `hl_testnet_pmm_btc_wide`
  - verify `active_order_count: 2`
  - verify watchdog `action: none`

### If the next window continues monitoring

- Primary checks:
  - `curl -u admin:admin http://127.0.0.1:8000/bot-orchestration/hl-testnet-pmm-btc-vps-20260618-085408/health`
  - `curl -u admin:admin "http://127.0.0.1:8000/bot-orchestration/hl-testnet-pmm-btc-vps-20260618-085408/orders?active_only=true&limit=10&event_limit=10"`
  - `cat /root/hummingbot-api/ops/watchdog-status/latest.json`
  - `cat /root/hummingbot-api/ops/hyperliquid-probes/latest.json`
- systemd timers to verify:
  - `hummingbot-hl-watchdog.timer`
  - `hyperliquid-testnet-probe.timer`
- If health says `running` but `active_order_count` stays `0` for more than 3 minutes:
  - inspect `ops/watchdog-status/latest.json` for `stalled_runtime`
  - inspect the latest bot log timestamp
  - if the quickstart session is idle/stopped inside a still-running container, restart only the bot container

### Laptop dependency note

- The trading bot and VPS watchdog/probe run autonomously on the VPS.
- The local macOS notification helper does not matter for bot uptime and can be ignored in a fresh agent window.

### Saved state for next agent window

- Date checked: `2026-06-22`
- Local repo now contains:
  - resilient watchdog update in `ops/watch_hyperliquid_pmm_remote.sh`
  - watchdog regression harness in `ops/test_watch_hyperliquid_pmm_remote.py`
  - watchdog fixtures in `ops/testdata/watchdog/`
  - updated canary and handoff notes in `ops/HYPERLIQUID_TESTNET_CANARY.md` and this file
- VPS changes already applied:
  - patched watchdog script copied to `/root/hummingbot-api/ops/watch_hyperliquid_pmm_remote.sh`
  - `hummingbot-hl-watchdog.service` and `.timer` restarted successfully
  - bot script config switched to `hl_testnet_pmm_btc_wide.yml`
  - wide PMM controller active with:
    - spread `0.003`
    - refresh `45`
    - `total_amount_quote: 25`
    - leverage `1`
- Root cause confirmed for the unexpected stop on `2026-06-22 07:23:50 UTC`:
  - watchdog initiated `stop_bot`
  - reason was `hard_disconnect_instability`
  - it was not a manual API/MQTT stop
- Additional hardening after that incident:
  - watchdog now distinguishes benign vs hard disconnects
  - repeat stop commands are suppressed per run
  - stalled-runtime detection added for `running` bots with zero active orders and stale log/order activity
- Latest validated healthy state before handoff:
  - timestamp: `2026-06-22T17:00:54Z`
  - bot: `hl-testnet-pmm-btc-vps-20260618-085408`
  - API health: `bot_status=running`
  - controller: `hl_testnet_pmm_btc_wide`
  - active orders: `2`
  - watchdog:
    - `action=none`
    - no `reasons`
    - no `degraded_reasons`
    - `latest_log_age_seconds=21`
    - `latest_order_event_age_seconds=21`
- Fast recovery rule if the next window finds:
  - `bot_status=running`
  - `active_order_count=0`
  - stale log/order timestamps
  - then restart only the bot container:
    - `docker restart hl-testnet-pmm-btc-vps-20260618-085408`
  - then verify:
    - `active_order_count=2`
    - watchdog `action=none`

### Local Mac note

- Local Docker is not required for the live VPS bot.
- If Docker Desktop keeps reopening on the Mac, the cause is the local LaunchAgent:
  - `~/Library/LaunchAgents/com.hummingbot.stack-watch.plist`
- That agent runs every 60 seconds and executes:
  - `cd /Users/dubski/hummingbot-api && /Users/dubski/hummingbot-api/ops/watch_hummingbot_stack.sh --repair --json --no-fail`
- It sets:
  - `AUTO_START_DOCKER=true`
- That script will call:
  - `open -ga Docker`
  - when local Docker is down
- Conclusion:
  - safe to close local Docker without affecting the VPS bot
  - if the user wants Docker to stay closed locally, unload only `com.hummingbot.stack-watch`
- Commands:
  - stop local watcher:
    - `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hummingbot.stack-watch.plist`
  - start local watcher again later:
    - `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hummingbot.stack-watch.plist`

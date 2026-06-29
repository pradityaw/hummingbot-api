# Hyperliquid Testnet Canary Resume

This runbook keeps the bot paused until both passive Hyperliquid probes and the
fail-closed watchdog stay clean long enough to trust a short canary.

## Passive readiness

On the VPS:

```bash
cd /root/hummingbot-api
ops/check_hyperliquid_resume_ready.sh
```

Expected result:

- `RESUME_READY`
- enough successful passive probes in the lookback window
- watchdog status clean with `active_order_count = 0`

## Resume canary

Use an SSH tunnel for the API first:

```bash
ssh -L 8000:127.0.0.1:8000 root@168.144.111.10
```

Then resume the existing stopped bot:

```bash
curl -sS -u admin:admin \
  -H 'Content-Type: application/json' \
  -d '{"bot_name":"hl-testnet-pmm-btc-vps-20260618-085408","async_backend":false}' \
  http://127.0.0.1:8000/bot-orchestration/start-bot
```

## Canary observation

During the first 30 to 60 minutes, watch:

```bash
cd /root/hummingbot-api
watch -n 30 'cat ops/watchdog-status/latest.json; echo; cat ops/hyperliquid-probes/latest.json'
```

Success criteria:

- active orders remain stable at `2`
- probe status stays `ready_now=true`
- watchdog action stays `none`
- no burst of `500/502/504`, websocket handshake errors, or `Open order failed`
- no prolonged `running + active_order_count=0` state with stale logs/order events

If the watchdog stops the bot, treat the canary as failed and inspect the latest
probe and watchdog JSON snapshots before trying again.

If the API still says `running` but the bot is not refreshing quotes:

```bash
docker logs --tail 80 hl-testnet-pmm-btc-vps-20260618-085408
docker restart hl-testnet-pmm-btc-vps-20260618-085408
```

Then confirm:

- controller is `hl_testnet_pmm_btc_wide`
- `active_order_count` returns to `2`
- watchdog returns to `action: none`

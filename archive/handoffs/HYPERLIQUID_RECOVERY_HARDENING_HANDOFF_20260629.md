# Hyperliquid Recovery Hardening — Next Window Handoff

Date: 2026-06-29

## Read this first in the new agent window

This handoff captures work completed locally on 2026-06-29 and the gated next steps for VPS rollout.

Related docs (older context still useful):
- `CURSOR_HANDOFF.md`
- `HYPERLIQUID_TESTNET_HANDOFF.md`
- `ops/hyperliquid_runtime_resilience_root_cause_20260623.md`
- `/Users/dubski/Projects/hyperliquid-condor-mm/docs/HANDOFF.md`

---

## Workspace

| Role | Path |
|------|------|
| Engineering source of truth | `/Users/dubski/hummingbot-api` |
| Umbrella ops / status | `/Users/dubski/Projects/hyperliquid-condor-mm` |
| VPS stack | `root@168.144.111.10` → `/root/hummingbot-api` |
| VPS umbrella | `/root/hyperliquid-condor-mm` |

SSH key: `~/.ssh/id_ed25519`

---

## Live VPS state at last probe (2026-06-29)

**Bot:** `hl-testnet-pmm-20260628-185328`  
**Controller:** `hl_testnet_pmm_btc_wide` (pmm_simple, BTC-USD testnet, 0.003 spread, $25, 1x)  
**Image:** `hummingbot/hummingbot:hyperliquid-fix`

| Signal | Value |
|--------|-------|
| Container | `running` (Up ~11h) |
| `current_state` | `RECOVERING` |
| `quoting_enabled` | `false` |
| `reconciliation_result` | `failed` / required loop |
| `orders_unknown` | `true` |
| `reconnect_attempt_count` | 122 |
| Reason | `order_path_failure,reconciliation_required` |

**Observed failure modes (recent logs):**
1. Hyperliquid rate-limit: `Too many cumulative requests sent (72857 > 70845)`
2. WS 1006 disconnects on order-book and user streams
3. Cancel-parse bug: successful cancel `{'status':'ok',...'success'}` raised as `OSError`

**DNS:** healthy in both host and container at probe time — not the live blocker.

**Readiness gate bug (pre-fix on VPS):** `check_hyperliquid_resume_ready.sh` printed `RESUME_READY` from a stale watchdog for a different bot (`hl-testnet-pmm-btc-vps-20260618-085408`). Local fix exists but is **not deployed to VPS yet**.

**Probe dir:** ~6777 files in `ops/hyperliquid-probes/` — opt-in pruning added locally.

---

## What was completed locally (NOT on VPS)

All four implementation streams finished. **32 tests passed, 3 skipped.** No commits, no image rebuild, no VPS changes.

### A. Connector defensive fixes
Files:
- `docker/hyperliquid-patch/overrides/.../hyperliquid_perpetual_derivative.py`
- `docker/hyperliquid-patch/overrides/.../hyperliquid_runtime_connectivity.py`
- `docker/hyperliquid-patch/overrides/.../hyperliquid_perpetual_user_stream_data_source.py`
- `docker/hyperliquid-patch/overrides/.../hyperliquid_perpetual_api_order_book_data_source.py`
- `test/test_hyperliquid_connector_defensive_fixes.py`

Changes:
- Cancel success parsing: `statuses: ["success"]` treated as success
- Bounded retry/backoff with jitter for DNS, 429/rate-limit, 5xx/504 on **safe** REST calls
- **Safety:** `type: "order"` placement is **NOT** retried after ambiguous failures (duplicate-order risk)
- WS close code/reason captured (1006 telemetry)
- Order/cancel failure type + message recorded for resilience layer

### B. Runtime resilience wiring
Files:
- `bots/scripts/connectivity_resilience.py`
- `test/test_hyperliquid_connectivity_resilience.py`

Changes:
- Wired `reconnect_required_stable_seconds` soak gate (additive to reconciliation)
- Bounded reconciliation retry/backoff with events
- Rate-limit order failures distinguished from genuine `orders_unknown`
- Quote gating unchanged: only `HEALTHY` / `DEGRADED_TRANSIENT`, never during incomplete reconciliation

### C. DNS / probe / reconnect sidecar
Files:
- `ops/probe_hyperliquid_testnet.py`
- `ops/reconnect_hyperliquid_bot.py` (new)
- `docker-compose.hyperliquid-fix.yml`
- `services/docker_service.py`
- `test/test_hyperliquid_probe_readiness.py`
- `test/test_hyperliquid_reconnect_tool.py`

Changes:
- Probe records resolved IPs, DNS drift metadata, jittered REST retries
- Documented host-network bot DNS limitation (`network_mode: host` ignores Docker `dns:`)
- Dry-run reconnect tool: `--execute-restart` only after probe + reconciliation + no-active-order gates

### D. Ops / readiness correctness
Files:
- `ops/check_hyperliquid_resume_ready.sh`
- `ops/watch_hyperliquid_pmm_remote.sh`
- `hyperliquid-condor-mm/ops/status.sh`

Changes:
- Readiness/watchdog bind to live `BOT_NAME=hl-testnet-pmm-20260628-185328`
- Fail closed on stale bot-name mismatch
- Opt-in probe pruning via `--prune-probe-dir`
- Condor `make status` surfaces connectivity state + mismatch warnings

---

## Hard constraints (unchanged)

- Do NOT weaken watchdog/readiness gates to make health look green
- Do NOT place live orders, resume, or redeploy bots without explicit confirmation
- Testnet only — no mainnet
- Deploy config changes as **NEW bot instances**, not in-place mutations
- Keep quote gating + reconciliation-before-resume

---

## Gated next steps (in order)

### Step 1 — Commit local changes
Review and commit in both repos:
- `/Users/dubski/hummingbot-api`
- `/Users/dubski/Projects/hyperliquid-condor-mm`

### Step 2 — Rebuild patched images
```bash
cd /Users/dubski/hummingbot-api
# Rebuild from docker/hyperliquid-patch/Dockerfile.bot and Dockerfile.api
# Tag: hummingbot/hummingbot:hyperliquid-fix
# Tag: hummingbot/hummingbot-api:hyperliquid-fix
```

### Step 3 — Sync to VPS (ops scripts first, then images)
```bash
# Ops scripts (can go before image rebuild for readiness fix)
rsync -av /Users/dubski/hummingbot-api/ops/ root@168.144.111.10:/root/hummingbot-api/ops/
rsync -av /Users/dubski/Projects/hyperliquid-condor-mm/ops/ root@168.144.111.10:/root/hyperliquid-condor-mm/ops/

# Restart timers after ops sync
ssh root@168.144.111.10 'systemctl restart hummingbot-hl-watchdog.timer hyperliquid-testnet-probe.timer'
```

### Step 4 — Deploy NEW bot (not in-place mutation)
Deploy fresh instance from `hl_testnet_pmm_btc_wide.yml`. Do **not** patch the stuck bot in place.

Keep `hl-testnet-pmm-20260628-185328` as reference until new instance is stable.

### Step 5 — Validate before trusting
```bash
cd /root/hyperliquid-condor-mm && make status
BOT_NAME=<new-bot> /root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh
curl -u admin:admin http://127.0.0.1:8000/bot-orchestration/<new-bot>/connectivity
curl -u admin:admin http://127.0.0.1:8000/bot-orchestration/<new-bot>/health
```

Pass criteria:
- watchdog/probe `bot_name` matches live bot (no mismatch warnings)
- readiness returns `RESUME_READY` only when runtime is actually safe
- `quoting_enabled=true` only after reconciliation succeeds + soak gate passes
- cancel-parse and rate-limit episodes recover without permanent `orders_unknown`

### Step 6 — Optional cleanup
```bash
# Opt-in probe pruning on VPS
python3 /root/hummingbot-api/ops/probe_hyperliquid_testnet.py \
  --prune-probe-dir --retention-max-count 500 --retention-max-age-seconds 604800
```

---

## Quick validation commands (VPS)

```bash
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'cd /root/hyperliquid-condor-mm && make status'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'cat /root/hummingbot-api/ops/watchdog-status/latest.json'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'cat /root/hummingbot-api/ops/hyperliquid-probes/latest.json'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 '/root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh'
```

Dry-run reconnect (safe default):
```bash
python3 /root/hummingbot-api/ops/reconnect_hyperliquid_bot.py --bot-name hl-testnet-pmm-20260628-185328
```

---

## Test command (local, before deploy)

```bash
cd /Users/dubski/hummingbot-api
python3 -m pytest test/test_hyperliquid_patch_installer.py \
  test/test_hyperliquid_connector_defensive_fixes.py \
  test/test_hyperliquid_connectivity_resilience.py \
  test/test_hyperliquid_probe_readiness.py \
  test/test_hyperliquid_reconnect_tool.py
```

Expected: 32 passed, 1+ skipped.

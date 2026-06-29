# Cloud Agent Handoff — Hyperliquid Recovery Hardening

Date: 2026-06-30  
Branch: `cursor-hyperliquid-recovery-hardening`  
Status: **Local hardening complete; VPS rollout not started**

---

## Mission

Continue the Hyperliquid testnet recovery hardening rollout so a **new** testnet bot is live-test ready with trustworthy readiness gates, patched connector/runtime behavior, and no false-green resume signals.

---

## Read first (in order)

1. `CLOUD_AGENT_HANDOFF.md` (this file)
2. `ops/HYPERLIQUID_RECOVERY_HARDENING_HANDOFF_20260629.md`
3. `CURSOR_HANDOFF.md`
4. `/Users/dubski/Projects/hyperliquid-condor-mm/docs/HANDOFF.md` (umbrella ops; open if multi-root workspace available)

---

## Two repos — why

| Repo | Path | Role |
|------|------|------|
| **hummingbot-api** | `/Users/dubski/hummingbot-api` | Engineering source of truth: API, connector patches, runtime resilience, Docker images, ops scripts, tests |
| **hyperliquid-condor-mm** | `/Users/dubski/Projects/hyperliquid-condor-mm` | Umbrella ops: PMM controller YAMLs, `make status`, config sync, Condor UI |

**Not two strategies.** One live bot on VPS uses one controller (`hl_testnet_pmm_btc_wide`). The umbrella repo only holds config variants and ops wrappers.

---

## What is already done (local)

- Connector defensive fixes (cancel parse, safe REST retry, WS telemetry)
- Runtime connectivity guard + quote gating (`connectivity_resilience.py`)
- API `/connectivity` endpoints
- Probe, watchdog, readiness, dry-run reconnect tooling
- Readiness fail-closed on stale `bot_name` mismatch (local scripts)
- **Tests:** `32 passed` (run before deploy)

```bash
cd /Users/dubski/hummingbot-api
python3 -m pytest test/test_hyperliquid_patch_installer.py \
  test/test_hyperliquid_connector_defensive_fixes.py \
  test/test_hyperliquid_connectivity_resilience.py \
  test/test_hyperliquid_probe_readiness.py \
  test/test_hyperliquid_reconnect_tool.py -q
```

---

## What is NOT done

- No VPS deploy of updated ops scripts
- No image rebuild/push to VPS
- No new bot instance deployed
- Old bot still running on pre-fix VPS scripts

---

## Live VPS state (last audit: 2026-06-29T21:34Z)

SSH: `ssh -i ~/.ssh/id_ed25519 root@168.144.111.10`

| Signal | Value |
|--------|-------|
| Bot | `hl-testnet-pmm-20260628-185328` |
| Controller | `hl_testnet_pmm_btc_wide` |
| Image | `hummingbot/hummingbot:hyperliquid-fix` (old build) |
| `current_state` | `RECOVERING` |
| `quoting_enabled` | `false` |
| `reconciliation_result` | `running` |
| `orders_unknown` | `true` |
| Active orders | ~23 |
| Reason | `order_path_failure,reconciliation_required` |

**Readiness gate bug (still on VPS):** `check_hyperliquid_resume_ready.sh` prints `RESUME_READY` while watchdog `bot_name` is stale (`hl-testnet-pmm-btc-vps-20260618-085408`). **Do not trust RESUME_READY until ops scripts are rsync'd.**

Quick audit:

```bash
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'cd /root/hyperliquid-condor-mm && make status'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 '/root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'curl -sS -u admin:admin http://127.0.0.1:8000/bot-orchestration/hl-testnet-pmm-20260628-185328/connectivity'
```

---

## Hard constraints

- **Testnet only** — no mainnet
- **Do NOT** weaken quote gating or reconciliation requirements
- **Do NOT** resume/restart/redeploy without explicit user confirmation at each gated step
- Deploy config changes as a **NEW bot instance** from `hl_testnet_pmm_btc_wide.yml` — no in-place mutation
- Stop or isolate old bot before new deploy (recommended — avoids rate-limit / duplicate orders)

---

## Gated rollout (execute in order)

### Gate 0 — Confirm VPS audit
Read-only: `make status`, readiness, `/connectivity`, watchdog/probe bot_name match.

### Gate 1 — Commit (if not already on branch)
Both repos should be on `cursor-hyperliquid-recovery-hardening`.

### Gate 2 — Rebuild images (ask user first)
```bash
cd /Users/dubski/hummingbot-api
# Rebuild from docker/hyperliquid-patch/Dockerfile.bot and Dockerfile.api
# Tags: hummingbot/hummingbot:hyperliquid-fix
#       hummingbot/hummingbot-api:hyperliquid-fix
```

### Gate 3 — Rsync ops scripts (ask user first)
```bash
rsync -av /Users/dubski/hummingbot-api/ops/ root@168.144.111.10:/root/hummingbot-api/ops/
rsync -av /Users/dubski/Projects/hyperliquid-condor-mm/ops/ root@168.144.111.10:/root/hyperliquid-condor-mm/ops/
ssh root@168.144.111.10 'systemctl restart hummingbot-hl-watchdog.timer hyperliquid-testnet-probe.timer'
```

Validate readiness now **fails closed** on stale bot_name until watchdog catches up.

### Gate 4 — Ship images to VPS (ask user first)
Load/push rebuilt images; verify container image IDs before bot deploy.

### Gate 5 — Stop old bot (ask user first)
`hl-testnet-pmm-20260628-185328` — stop only with confirmation.

### Gate 6 — Deploy NEW bot (ask user first)
Fresh instance from `configs/controllers/hl_testnet_pmm_btc_wide.yml` (sync via condor-mm `make sync-configs` on VPS).

### Gate 7 — Validate live-test readiness
```bash
cd /root/hyperliquid-condor-mm && make status
BOT_NAME=<new-bot> /root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh
curl -u admin:admin http://127.0.0.1:8000/bot-orchestration/<new-bot>/connectivity
curl -u admin:admin http://127.0.0.1:8000/bot-orchestration/<new-bot>/health
```

Pass when:
- watchdog/probe `bot_name` matches new bot
- readiness is trustworthy (no mismatch warnings)
- `quoting_enabled=true` only after reconciliation succeeds + soak gate
- no persistent `orders_unknown` or cancel-parse / rate-limit loops

---

## Cloud agent prompt (paste into Cursor Cloud)

```text
Continue Hyperliquid testnet recovery hardening rollout on branch cursor-hyperliquid-recovery-hardening.

Read CLOUD_AGENT_HANDOFF.md first, then ops/HYPERLIQUID_RECOVERY_HARDENING_HANDOFF_20260629.md.

Context:
- Local hardening is done; 32 tests pass
- VPS still on old image/scripts; live bot hl-testnet-pmm-20260628-185328 stuck RECOVERING
- Readiness gate falsely RESUME_READY due to stale watchdog bot_name

Hard constraints:
- Testnet only
- Do NOT weaken quote gating or reconciliation
- Ask before commit/build/rsync/stop/deploy/start at each gate
- Deploy NEW bot from hl_testnet_pmm_btc_wide.yml only

Start with read-only VPS audit, then proceed through gated steps with user confirmation.
```

---

## Workspace setup for Cloud Agent

**Minimum (engineering only):** Open `/Users/dubski/hummingbot-api` on branch `cursor-hyperliquid-recovery-hardening`.

**Full (ops + configs):** Open multi-root workspace `hyperliquid-condor-mm.code-workspace` in hyperliquid-condor-mm repo (includes hummingbot-api path if configured locally).

**VPS access:** Cloud agent needs SSH key `~/.ssh/id_ed25519` and network to `168.144.111.10`. Configure secrets/env in Cursor Cloud if SSH is not available by default.

---

## Push / fork note

If `origin` is upstream `hummingbot/hummingbot-api`, push the branch to **your fork** and point Cursor Cloud at that fork URL:

```bash
git remote add fork git@github.com:<YOUR_USER>/hummingbot-api.git  # if needed
git push -u fork cursor-hyperliquid-recovery-hardening
```

# Cloud Agent Handoff — Hyperliquid Recovery Hardening

Date: 2026-07-04
Branch: `cursor-hyperliquid-recovery-hardening`
Status: **VPS rollout complete; post-deadlock soak passed; local fixes #3/#6 pending commit/deploy**

---

## Mission

Maintain a trustworthy Hyperliquid testnet PMM bot with hardened recovery, readiness gates, and pre-mainnet guardrails. Live bot is soak-verified; next work is commit/rollout of remaining local fixes and Condor alerting.

---

## Read first (in order)

1. `CLOUD_AGENT_HANDOFF.md` (this file)
2. `/Users/dubski/Projects/hyperliquid-condor-mm/docs/PROJECT_STATUS.md` (day-to-day status)
3. `archive/handoffs/HYPERLIQUID_RECOVERY_HARDENING_HANDOFF_20260629.md`
4. `/Users/dubski/Projects/hyperliquid-condor-mm/docs/HANDOFF.md` (umbrella ops)

---

## Two repos — why

| Repo | Path | Role |
|------|------|------|
| **hummingbot-api** | `/Users/dubski/hummingbot-api` | Engineering source of truth: API, connector patches, runtime resilience, Docker images, ops scripts, tests |
| **hyperliquid-condor-mm** | `/Users/dubski/Projects/hyperliquid-condor-mm` | Umbrella ops: PMM controller YAMLs, `make status`, config sync, Condor UI |

**Not two strategies.** One live bot on VPS uses one controller (`hl_testnet_pmm_btc_wide`). The umbrella repo only holds config variants and ops wrappers.

---

## What is already done

- Connector defensive fixes (cancel parse, safe REST retry, WS telemetry)
- Runtime connectivity guard + quote gating (`connectivity_resilience.py`)
- API `/connectivity` endpoints
- Probe, watchdog, readiness, dry-run reconnect tooling
- Readiness fail-closed on stale `bot_name` mismatch
- Deadlock fixes (commit `5a4f11d`): order-not-found cancel handling, RECOVERING flatten, reconciliation retry, connector quote gate, drawdown/kill-switch when quoting disabled, stuck-RECOVERING watchdog escalation
- **VPS rollout:** images rebuilt; bot `hl-testnet-pmm-20260704-053658-20260704-053658` deployed and soak-verified
- **Fix #3 (local, uncommitted):** `HB_ALLOW_MAINNET=1` guardrail — `bots/scripts/v2_with_controllers.py`, `routers/bot_orchestration.py`, `bots/scripts/mainnet_guard.py`, `utils/mainnet_guard.py`, `test/test_mainnet_guard.py`
- **Fix #6 (local, uncommitted):** order-book spread sanity check — `bots/scripts/connectivity_resilience.py`, `HB_CONNECTIVITY_MAX_BOOK_SPREAD_RATIO` (default 0)
- **Tests:** 54 passed, 4 skipped (focused suite)

```bash
cd /Users/dubski/hummingbot-api
python3 -m pytest test/test_mainnet_guard.py \
  test/test_hyperliquid_connectivity_resilience.py \
  test/test_hyperliquid_patch_installer.py \
  test/test_hyperliquid_connector_defensive_fixes.py \
  test/test_hyperliquid_probe_readiness.py \
  test/test_hyperliquid_reconnect_tool.py \
  test/test_bot_orchestration_connectivity.py -q
```

---

## What is NOT done

- Local fixes #3/#6 **not committed** and **not on VPS** (live bot still on pre-fix images)
- **Condor/Telegram** not deployed — no `TELEGRAM_BOT_TOKEN` available
- Soak summary parser bug (raw samples healthy; summary generation failed locally)

---

## Live VPS state (last audit: 2026-07-04T14:09:30Z)

SSH: `ssh -i ~/.ssh/id_ed25519 root@168.144.111.10`

| Signal | Value |
|--------|-------|
| Bot | `hl-testnet-pmm-20260704-053658-20260704-053658` |
| Controller | `hl_testnet_pmm_btc_wide` |
| Image | `hummingbot/hummingbot:hyperliquid-fix` (a4604d08ac63) |
| `current_state` | `HEALTHY` |
| `quoting_enabled` | `true` |
| `active_order_count` | `2` |
| `resume_ready` | `true` |
| `orders_unknown` | `false` |
| Watchdog | `action=none`, `bot_name_match=true` |
| 5xx storm | none |

**Soak (7 samples, 12:38–14:09 UTC):** all raw samples `HEALTHY`; no degradation observed.

Quick audit:

```bash
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'cd /root/hyperliquid-condor-mm && make status'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 '/root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'curl -sS -u admin:admin http://127.0.0.1:8000/bot-orchestration/hl-testnet-pmm-20260704-053658-20260704-053658/connectivity'
```

---

## Hard constraints

- **Testnet only** — no mainnet
- **Do NOT** weaken quote gating or reconciliation requirements
- **Do NOT** resume/restart/redeploy without explicit user confirmation at each gated step
- Deploy config changes as a **NEW bot instance** from `hl_testnet_pmm_btc_wide.yml` — no in-place mutation
- Stop or isolate old bot before new deploy (recommended — avoids rate-limit / duplicate orders)

---

## Next rollout (when user confirms)

### Step 1 — Commit local fixes
Review and commit fixes #3 and #6 in `hummingbot-api`.

### Step 2 — Rebuild images (ask user first)
```bash
cd /Users/dubski/hummingbot-api
# Rebuild from docker/hyperliquid-patch/Dockerfile.bot and Dockerfile.api
# Tags: hummingbot/hummingbot:hyperliquid-fix
#       hummingbot/hummingbot-api:hyperliquid-fix
```

### Step 3 — Ship images + deploy NEW bot (ask user first)
Fresh instance from `configs/controllers/hl_testnet_pmm_btc_wide.yml`. Optionally set `HB_CONNECTIVITY_MAX_BOOK_SPREAD_RATIO` on rollout.

### Step 4 — Deploy Condor (ask user first)
`make deploy-condor` with `TELEGRAM_BOT_TOKEN` in condor `.env`.

### Step 5 — Validate
```bash
cd /root/hyperliquid-condor-mm && make status
BOT_NAME=<new-bot> /root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh
curl -u admin:admin http://127.0.0.1:8000/bot-orchestration/<new-bot>/connectivity
```

---

## Cloud agent prompt (paste into Cursor Cloud)

```text
Continue Hyperliquid testnet PMM ops on branch cursor-hyperliquid-recovery-hardening.

Read CLOUD_AGENT_HANDOFF.md and hyperliquid-condor-mm/docs/PROJECT_STATUS.md first.

Context:
- Live bot hl-testnet-pmm-20260704-053658-20260704-053658 HEALTHY; post-deadlock soak passed (7 samples, 2026-07-04)
- Local fixes #3 (HB_ALLOW_MAINNET) and #6 (spread sanity) implemented but uncommitted/not on VPS
- Condor/Telegram pending (no TELEGRAM_BOT_TOKEN)
- 54 tests pass locally

Hard constraints:
- Testnet only
- Do NOT weaken quote gating or reconciliation
- Ask before commit/build/rsync/stop/deploy/start at each gate
- Deploy NEW bot from hl_testnet_pmm_btc_wide.yml only

Start with user intent: commit/rollout fixes, Condor deploy, or read-only audit.
```

---

## Workspace setup for Cloud Agent

### Option A — Cloud Agent (recommended)

1. In Cursor, start a **Cloud Agent**
2. Point it at: `https://github.com/pradityaw/hummingbot-api`
3. Branch: `cursor-hyperliquid-recovery-hardening`
4. Paste the cloud agent prompt from above
5. Add secrets if needed:
   - SSH private key for VPS (`168.144.111.10`)
   - Hummingbot API credentials (`admin` / your password)

For umbrella configs, open a second cloud session or add multi-repo context from:
`https://github.com/pradityaw/hyperliquid-condor-mm` (same branch).

### Option B — Cursor Desktop (local)

Open branch `cursor-hyperliquid-recovery-hardening` in `/Users/dubski/hummingbot-api`, or open `hyperliquid-condor-mm.code-workspace` for both repos.

**VPS access:** Cloud agent needs SSH to `168.144.111.10` via `~/.ssh/id_ed25519`.

Branch is published on the user fork:

- **hummingbot-api:** https://github.com/pradityaw/hummingbot-api/tree/cursor-hyperliquid-recovery-hardening
- **hyperliquid-condor-mm:** https://github.com/pradityaw/hyperliquid-condor-mm/tree/cursor-hyperliquid-recovery-hardening

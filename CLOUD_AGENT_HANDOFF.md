# Cloud Agent Handoff — Hyperliquid Recovery Hardening

> **⚠️ SUPERSEDED SNAPSHOT (2026-07-07)** — This handoff is a historical record of the 2026-07-07 deploy. Live bot identity, watchdog/auto-restart state, test counts, and current blockers are superseded by `/Users/dubski/Projects/hyperliquid-condor-mm/docs/PROJECT_STATUS.md` (repo: `hyperliquid-condor-mm`, path `docs/PROJECT_STATUS.md`). Examples below referencing the Jul 7 bot (`hl-testnet-pmm-20260707-113533-20260707-113533`) are historical; the current live bot is **`hl-testnet-pmm-20260724-191456-20260724-191456`** (the "Jul 24 bot"). **Verify live identity in `PROJECT_STATUS.md` before running any command from this document.**

Date: 2026-07-07
Branch: `cursor-hyperliquid-recovery-hardening`
Status: **Fixes #3/#6 committed + live. New bot `hl-testnet-pmm-20260707-113533-20260707-113533` HEALTHY, RESUME_READY, spread guard armed (0.01)** *(superseded — see banner above)*

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
- **Fix #3 (committed `b5f3e65`):** `HB_ALLOW_MAINNET=1` guardrail — `bots/scripts/v2_with_controllers.py`, `routers/bot_orchestration.py`, `bots/scripts/mainnet_guard.py`, `utils/mainnet_guard.py`, `test/test_mainnet_guard.py`
- **Fix #6 (committed `24f1faf`):** order-book spread sanity check — `bots/scripts/connectivity_resilience.py`, `HB_CONNECTIVITY_MAX_BOOK_SPREAD_RATIO` (default 0; **armed at 0.01 on VPS**)
- **Env forwarding (committed `f7491f5`):** `HB_CONNECTIVITY_*`/`HB_ALLOW_MAINNET` env vars now propagate from the API to newly deployed bot containers (`services/docker_service.py`)
- **Ops watcher fix (committed `f5d7cd9`):** resume-ready notifier no longer false-positives on `RESUME_NOT_READY` (anchored grep)
- **VPS rollout 2026-07-07:** images rebuilt from `f7491f5` (bot 51a3b586826c, API ffba6c990688); runtime scripts synced; bot `hl-testnet-pmm-20260707-113533-20260707-113533` deployed (testnet_fresh, drawdown $10/$5) and validated `RESUME_READY`
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

- **Condor/Telegram** not deployed — no `TELEGRAM_BOT_TOKEN` available. Now the top gap: watchdog stops are silent and recovery is manual (bot was down Jul 5→Jul 7 unnoticed).
- **Auto-recovery policy** — watchdog stops the bot on testnet outages but nothing restarts it once `RESUME_READY` holds; decide whether to add a gated auto-`docker restart`.
- 24h soak of the new bot with the spread guard armed (watch for `order_book_spread_too_wide` false positives)

---

## Live VPS state (last audit: 2026-07-07T12:12Z)

SSH: `ssh -i ~/.ssh/id_ed25519 root@168.144.111.10`

| Signal | Value |
|--------|-------|
| Bot | `hl-testnet-pmm-20260707-113533-20260707-113533` |
| Controller | `hl_testnet_pmm_btc_wide` (90s refresh / 30s cooldown) |
| Image | `hummingbot/hummingbot:hyperliquid-fix` (51a3b586826c, includes fixes #3/#6) |
| Spread guard | `HB_CONNECTIVITY_MAX_BOOK_SPREAD_RATIO=0.01` (armed, in bot container env) |
| `current_state` | `HEALTHY` |
| `quoting_enabled` | `true` |
| `active_order_count` | `2` |
| `resume_ready` | `true` (15/15 probes) |
| `orders_unknown` | `false` |
| Watchdog | `action=none`, `bot_name_match=true` |
| 5xx storm | none |

**History:** prior bot `…20260704-053658…` was watchdog-stopped Jul 5 06:50 and Jul 7 06:51 UTC during Hyperliquid testnet CloudFront 504/500 outages (guard worked as designed); it is docker-stopped with `--restart=no`. Wallet audit at the time: 0 open orders, 0 positions, ~$999.91 USDC (lifetime PnL ≈ -$0.09 over 90 fills).

Quick audit:

```bash
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'cd /root/hyperliquid-condor-mm && make status'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'BOT_NAME=hl-testnet-pmm-20260707-113533-20260707-113533 /root/hummingbot-api/ops/check_hyperliquid_resume_ready.sh'
ssh -i ~/.ssh/id_ed25519 root@168.144.111.10 'curl -sS -u admin:admin http://127.0.0.1:8000/bot-orchestration/hl-testnet-pmm-20260707-113533-20260707-113533/connectivity'
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

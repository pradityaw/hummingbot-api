# Cursor Handoff

Date: 2026-06-29

## Latest handoff (start here for next window)

**`CLOUD_AGENT_HANDOFF.md`** — cloud agent + Cursor continuation (2026-06-30; branch `cursor-hyperliquid-recovery-hardening`)

**`ops/HYPERLIQUID_RECOVERY_HARDENING_HANDOFF_20260629.md`** — local hardening completed 2026-06-29; VPS rollout gated.

## Source of truth

- Repo: `/Users/dubski/hummingbot-api`
- Branch state at handoff:
  - `main`
  - ahead of `origin/main` by `1` commit
- Important: the latest Hyperliquid work includes local uncommitted changes and untracked files. If you use Cursor Cloud/Agents, commit and push first so the cloud environment sees the full state.

## What "latest work" means here

This repo contains the active Hummingbot Hyperliquid testnet resilience work, including:

- Hyperliquid connector patch overlay under `docker/hyperliquid-patch/`
- runtime connectivity/recovery logic
- API connectivity endpoints
- watchdog, readiness, and passive probe tooling
- handoff and root-cause notes for the June 2026 Hyperliquid testnet incidents

This is the repo to open in Cursor, not the separate `hyperliquid-trading-bot` project.

## Files to review first

1. `HYPERLIQUID_TESTNET_HANDOFF.md`
2. `ops/NEXT_WINDOW_HANDOFF.md`
3. `ops/hyperliquid_runtime_resilience_root_cause_20260623.md`
4. `CODEX_SETUP.md`
5. `docker-compose.hyperliquid-fix.yml`
6. `docker/hyperliquid-patch/patch_hyperliquid_connector.py`
7. `routers/bot_orchestration.py`
8. `bots/scripts/connectivity_resilience.py`
9. `test/test_hyperliquid_connectivity_resilience.py`
10. `test/test_hyperliquid_probe_readiness.py`

## Current local changes that matter

Modified:

- `.gitignore`
- `Makefile`
- `bots/scripts/v2_with_controllers.py`
- `docker-compose.yml`
- `routers/bot_orchestration.py`

Untracked or newly added work:

- `CODEX_SETUP.md`
- `HYPERLIQUID_TESTNET_HANDOFF.md`
- `apply_hyperliquid_testnet_credentials.sh`
- `bots/scripts/connectivity_resilience.py`
- `docker-compose.vps-localhost.yml`
- `docker-compose.hyperliquid-fix.yml`
- `docker/hyperliquid-patch/`
- `ops/`
- `test/test_hyperliquid_connectivity_resilience.py`
- `test/test_hyperliquid_patch_installer.py`
- `test/test_hyperliquid_probe_readiness.py`

## Cursor setup

### Cursor Desktop

Open:

- `/Users/dubski/hummingbot-api`

This gives Cursor the full local state immediately, including uncommitted Hyperliquid work.

### Cursor Cloud or Cursor Agents

Commit and push first:

```bash
cd /Users/dubski/hummingbot-api
git checkout -b cursor-hyperliquid-handoff
git add .
git commit -m "Handoff Hyperliquid testnet resilience work to Cursor"
git push -u origin cursor-hyperliquid-handoff
```

Then point Cursor Cloud at that branch.

## Prompt for Cursor

```text
You are taking over work in /Users/dubski/hummingbot-api.

This is the Hummingbot Hyperliquid testnet resilience work as of June 29, 2026. Read these files first before making changes:

1. HYPERLIQUID_TESTNET_HANDOFF.md
2. ops/NEXT_WINDOW_HANDOFF.md
3. ops/hyperliquid_runtime_resilience_root_cause_20260623.md
4. CODEX_SETUP.md
5. docker-compose.hyperliquid-fix.yml
6. docker/hyperliquid-patch/patch_hyperliquid_connector.py
7. routers/bot_orchestration.py
8. bots/scripts/connectivity_resilience.py
9. test/test_hyperliquid_connectivity_resilience.py
10. test/test_hyperliquid_probe_readiness.py

Project context:
- Main repo: hummingbot-api
- Goal: stabilize Hyperliquid testnet bot behavior and preserve fail-safe behavior during DNS, REST, and websocket instability
- Current known state: patched Hyperliquid images, runtime connectivity state machine, quote gating while unsafe, reconciliation before quote resume, watchdog/probe/readiness tooling
- Important constraint: do not weaken safety checks just to make health appear green
- Important constraint: do not place live orders or resume bots without explicit confirmation
- Important constraint: preserve testnet-first behavior

After reading, do this:
1. Summarize the current architecture and latest changes.
2. List the exact local modified/untracked files that matter to the Hyperliquid work.
3. Identify the highest-priority next engineering task.
4. Propose a small, safe implementation plan.
5. Only then start coding.

When you report back, keep the summary concise and reference concrete files.
```

## Suggested first task for Cursor

The best next task is to harden permanent recovery around Hyperliquid testnet DNS and partial reconnect behavior without weakening quote gating or reconciliation requirements.

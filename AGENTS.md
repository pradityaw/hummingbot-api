# Agent instructions — hummingbot-api

## Hyperliquid recovery work

When working on Hyperliquid testnet resilience or VPS rollout, **read `CLOUD_AGENT_HANDOFF.md` first**.

Engineering source of truth is this repo. Umbrella ops/configs live in `hyperliquid-condor-mm` (separate repo).

## Safety rules

- Testnet only unless explicitly told otherwise
- Never weaken quote gating, reconciliation, or readiness checks to appear healthy
- Never resume, restart, or deploy bots without explicit user confirmation per step
- Deploy strategy changes as **new bot instances**, not in-place edits to running bots

## Key paths

- Connector patches: `docker/hyperliquid-patch/`
- Runtime guard: `bots/scripts/connectivity_resilience.py`
- API connectivity: `routers/bot_orchestration.py`
- Ops scripts: `ops/`
- Tests: `test/test_hyperliquid_*.py`

## Before deploy

Run the focused pytest suite documented in `CLOUD_AGENT_HANDOFF.md`.

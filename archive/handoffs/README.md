# Archived handoff docs

Dated handoff snapshots (Jun 18–29, 2026) from the Hyperliquid testnet recovery
work. All of them are **superseded by `/CLOUD_AGENT_HANDOFF.md`** (2026-06-30),
which is the single current handoff. Kept here for historical context only —
bot names, container states, and VPS details in these files are stale.

- `NEXT_WINDOW_HANDOFF.md` (2026-06-19) — VPS rollout snapshot: droplet stack state,
  remote bot status, and SSH/ops runbook. References the first VPS bot generation
  `hl-testnet-pmm-btc-vps-20260618-085408`.
- `HYPERLIQUID_TESTNET_HANDOFF.md` (2026-06-23) — local Mac patched-image recovery:
  connector patch overlay, compose override, credential/BOTS_PATH fixes. References
  the local bot generation `hl-testnet-pmm-btc-20260618-081557`.
- `CURSOR_HANDOFF.md` (2026-06-29) — index/pointer doc describing repo layout, branch
  state, and which handoff to read next; no specific bot generation of its own.
- `HYPERLIQUID_RECOVERY_HARDENING_HANDOFF_20260629.md` (2026-06-29) — local hardening
  completed, VPS rollout gated. Bridges the old VPS bot
  `hl-testnet-pmm-btc-vps-20260618-085408` to the current live bot
  `hl-testnet-pmm-20260628-185328`.

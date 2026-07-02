# Archived strategy configs

Stale Hyperliquid testnet strategy configs moved out of `bots/conf/` on 2026-07-02.
These were previously untracked (ignored via `bots/conf/`); archived here for reference.
The active config is `bots/conf/controllers/hl_testnet_pmm_btc_wide.yml` (do not confuse
it with the narrow variant below).

- `hl_testnet_pmm_btc.yml` — narrow-spread (0.2%) `pmm_simple` PMM controller config,
  predecessor of the active wide-spread variant `hl_testnet_pmm_btc_wide.yml`.
- `hl_testnet_supertrend_btc.yml` — `supertrend_v1` directional-trading controller config,
  an abandoned experiment (3m candles, length 20, multiplier 4.0).
- `hl-testnet-supertrend-btc-20260618-081257.yml`,
  `hl-testnet-supertrend-btc-20260618-081425.yml`,
  `hl-testnet-supertrend-btc-20260618-081451.yml` — near-identical duplicate script
  configs (`v2_with_controllers.py` wrappers) generated on 2026-06-18 while launching
  the abandoned supertrend experiment.

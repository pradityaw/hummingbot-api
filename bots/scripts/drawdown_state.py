"""
Drawdown state persistence (F6).

Peak-to-trough drawdown tracking used to live only in process memory: every
`docker restart` (the standard recovery path, and the auto-restart path) re-armed
the full drawdown budget and resurrected drawdown-stopped controllers, so a crash
loop could bleed Nx the intended limit. This store persists the drawdown peaks,
the exited-controller list, and the daily-loss-floor bookkeeping to the same
state dir the connectivity guard uses, atomically (tmp + rename).

Schema (all Decimal values serialized as strings):
{
  "max_pnl_by_controller": {"ctrl_id": "1.23"},
  "max_global_pnl": "4.56",
  "drawdown_exited_controllers": ["ctrl_id"],
  "daily_halt_controllers": ["ctrl_id"],
  "daily_halt_date": "2026-07-28",          # UTC day the floor tripped, or null
  "daily_anchor_date": "2026-07-28",        # UTC day the anchor pnl belongs to
  "daily_anchor_pnl": "10.0"
}
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_STATE_DIR = "/home/hummingbot/data/connectivity"
STATE_DIR_ENV = "HB_CONNECTIVITY_STATE_DIR"

_DEFAULT_PAYLOAD: Dict[str, Any] = {
    "max_pnl_by_controller": {},
    "max_global_pnl": "0",
    "drawdown_exited_controllers": [],
    "daily_halt_controllers": [],
    "daily_halt_date": None,
    "daily_anchor_date": None,
    "daily_anchor_pnl": "0",
}


class DrawdownStateStore:
    def __init__(self, state_path: Optional[str] = None):
        base_dir = Path(os.environ.get(STATE_DIR_ENV, DEFAULT_STATE_DIR))
        self.state_path = Path(state_path) if state_path else base_dir / "drawdown_state.json"

    def load(self) -> Dict[str, Any]:
        """
        Return the persisted state, or defaults when absent/unreadable/corrupt.
        Fail-open by design (a lost state file must not brick the strategy), but
        callers should log loudly: losing this file re-arms the loss budget.
        """
        if not self.state_path.exists():
            return dict(_DEFAULT_PAYLOAD)
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return dict(_DEFAULT_PAYLOAD)
        if not isinstance(payload, dict):
            return dict(_DEFAULT_PAYLOAD)
        state = dict(_DEFAULT_PAYLOAD)
        for key, default in _DEFAULT_PAYLOAD.items():
            value = payload.get(key, default)
            if isinstance(default, dict) and not isinstance(value, dict):
                continue
            if isinstance(default, list) and not isinstance(value, list):
                continue
            state[key] = value
        return state

    def save(self, payload: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, self.state_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

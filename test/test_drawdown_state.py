"""
Tests for DrawdownStateStore (F6 persistence) — pure python, no stubs needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "bots" / "scripts"
for path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from drawdown_state import DrawdownStateStore  # noqa: E402


def test_round_trip(tmp_path):
    store = DrawdownStateStore(state_path=str(tmp_path / "drawdown_state.json"))
    payload = {
        "max_pnl_by_controller": {"ctrl-1": "10.5"},
        "max_global_pnl": "12.5",
        "drawdown_exited_controllers": ["ctrl-2"],
        "daily_halt_controllers": ["ctrl-1"],
        "daily_halt_date": "2026-07-28",
        "daily_anchor_date": "2026-07-28",
        "daily_anchor_pnl": "10.0",
    }
    store.save(payload)
    assert store.load() == payload
    on_disk = json.loads((tmp_path / "drawdown_state.json").read_text())
    assert on_disk == payload


def test_missing_file_returns_defaults(tmp_path):
    store = DrawdownStateStore(state_path=str(tmp_path / "nope.json"))
    state = store.load()
    assert state["max_pnl_by_controller"] == {}
    assert state["max_global_pnl"] == "0"
    assert state["drawdown_exited_controllers"] == []
    assert state["daily_halt_date"] is None


def test_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "drawdown_state.json"
    path.write_text("{not json")
    store = DrawdownStateStore(state_path=str(path))
    state = store.load()
    assert state["max_pnl_by_controller"] == {}


def test_wrong_types_fall_back_to_defaults(tmp_path):
    path = tmp_path / "drawdown_state.json"
    path.write_text(json.dumps({"max_pnl_by_controller": "oops",
                                "drawdown_exited_controllers": 42,
                                "max_global_pnl": "7.5"}))
    store = DrawdownStateStore(state_path=str(path))
    state = store.load()
    assert state["max_pnl_by_controller"] == {}
    assert state["drawdown_exited_controllers"] == []
    assert state["max_global_pnl"] == "7.5"


def test_save_is_atomic_and_creates_dirs(tmp_path):
    target = tmp_path / "nested" / "dir" / "drawdown_state.json"
    store = DrawdownStateStore(state_path=str(target))
    store.save({"max_pnl_by_controller": {}, "max_global_pnl": "0",
                "drawdown_exited_controllers": [], "daily_halt_controllers": [],
                "daily_halt_date": None, "daily_anchor_date": None, "daily_anchor_pnl": "0"})
    assert target.exists()
    leftovers = list(target.parent.glob("*.tmp"))
    assert leftovers == []

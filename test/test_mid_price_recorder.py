"""
Tests for MidPriceRecorder (bot-side mid series for markout) — pure python,
no hummingbot stubs required.
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

from mid_price_recorder import MidPriceRecorder  # noqa: E402


def _samples(mid: str = "60000.5"):
    return [{"connector": "hyperliquid_perpetual_testnet", "pair": "BTC-USD", "mid": mid}]


def test_records_one_line_per_sample(tmp_path):
    recorder = MidPriceRecorder(state_dir=str(tmp_path), interval_seconds=1)
    assert recorder.maybe_record(1_700_000_000.0, _samples()) is True
    path = tmp_path / "mids" / "mids_20231114.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "connector": "hyperliquid_perpetual_testnet",
        "mid": "60000.5",
        "pair": "BTC-USD",
        "ts": 1_700_000_000.0,
    }


def test_throttles_writes_to_interval(tmp_path):
    recorder = MidPriceRecorder(state_dir=str(tmp_path), interval_seconds=5)
    assert recorder.maybe_record(1000.0, _samples()) is True
    assert recorder.maybe_record(1002.0, _samples("60001")) is False
    assert recorder.maybe_record(1005.0, _samples("60002")) is True
    path = tmp_path / "mids" / "mids_19700101.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["mid"] for line in lines] == ["60000.5", "60002"]


def test_disabled_when_interval_zero(tmp_path):
    recorder = MidPriceRecorder(state_dir=str(tmp_path), interval_seconds=0)
    assert recorder.enabled is False
    assert recorder.maybe_record(1000.0, _samples()) is False
    assert not (tmp_path / "mids").exists()


def test_empty_samples_write_nothing(tmp_path):
    recorder = MidPriceRecorder(state_dir=str(tmp_path), interval_seconds=1)
    assert recorder.maybe_record(1000.0, []) is False
    assert not (tmp_path / "mids").exists()


def test_date_rotation_uses_utc_day(tmp_path):
    recorder = MidPriceRecorder(state_dir=str(tmp_path), interval_seconds=1)
    recorder.maybe_record(1_700_000_000.0, _samples("1"))  # 2023-11-14
    recorder.maybe_record(1_700_086_400.0, _samples("2"))  # 2023-11-15
    assert (tmp_path / "mids" / "mids_20231114.jsonl").exists()
    assert (tmp_path / "mids" / "mids_20231115.jsonl").exists()

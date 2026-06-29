import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


PROBE_PATH = Path(__file__).resolve().parents[1] / "ops" / "probe_hyperliquid_testnet.py"
CHECK_READY_PATH = Path(__file__).resolve().parents[1] / "ops" / "check_hyperliquid_resume_ready.sh"
WATCHDOG_PATH = Path(__file__).resolve().parents[1] / "ops" / "watch_hyperliquid_pmm_remote.sh"


def load_probe_module():
    sys.modules.setdefault("aiohttp", SimpleNamespace(ClientTimeout=object, ClientSession=object))
    spec = importlib.util.spec_from_file_location("probe_hyperliquid_testnet", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rest(ok=True):
    return {"attempt_count": 1, "success_count": 1 if ok else 0}


def ws(ok=True):
    return {"ok": ok}


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def test_structured_healthy_runtime_is_ready_even_with_historical_watchdog_noise():
    module = load_probe_module()
    watchdog = {
        "runtime_connectivity_available": True,
        "runtime_state": "HEALTHY",
        "runtime_reconciliation_result": "succeeded",
        "runtime_orders_unknown": False,
        "reasons": ["historical_disconnect"],
    }

    ready = module.build_ready_state(rest(), ws(), watchdog)

    assert ready["ready_now"] is True
    assert ready["reason"] == "ok"


def test_structured_unsafe_runtime_is_not_ready():
    module = load_probe_module()
    watchdog = {
        "runtime_connectivity_available": True,
        "runtime_state": "DEGRADED_UNSAFE",
        "runtime_reconciliation_result": "required",
        "runtime_orders_unknown": True,
        "runtime_readiness_reason": "rest_unavailable",
    }

    ready = module.build_ready_state(rest(), ws(), watchdog)

    assert ready["ready_now"] is False
    assert ready["reason"] == "rest_unavailable"


def test_recovering_runtime_is_not_ready_until_reconciliation_completes():
    module = load_probe_module()
    watchdog = {
        "runtime_connectivity_available": True,
        "runtime_state": "RECOVERING",
        "runtime_reconciliation_result": "running",
        "runtime_orders_unknown": False,
        "runtime_readiness_reason": "reconciliation_required",
    }

    ready = module.build_ready_state(rest(), ws(), watchdog)

    assert ready["ready_now"] is False
    assert ready["reason"] == "reconciliation_required"


def test_watchdog_bot_name_mismatch_is_not_ready():
    module = load_probe_module()
    watchdog = {
        "bot_name": "stale-bot",
        "runtime_bot_name": "stale-bot",
        "runtime_connectivity_available": True,
        "runtime_state": "HEALTHY",
        "runtime_reconciliation_result": "succeeded",
        "runtime_orders_unknown": False,
    }

    ready = module.build_ready_state(rest(), ws(), watchdog, bot_name="live-bot")

    assert ready["ready_now"] is False
    assert ready["bot_name_match"] is False
    assert "watchdog_bot_name_mismatch" in ready["reason"]
    assert "runtime_bot_name_mismatch" in ready["reason"]


def test_prune_probe_dir_keeps_latest_and_newest_count(tmp_path):
    module = load_probe_module()
    now = datetime(2026, 6, 29, 0, 0, tzinfo=timezone.utc)
    latest = tmp_path / "latest.json"
    old_probe = tmp_path / "20260628T000000Z.json"
    new_probe = tmp_path / "20260629T000000Z.json"
    write_json(latest, {"checked_at": now.isoformat().replace("+00:00", "Z")})
    write_json(old_probe, {"checked_at": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")})
    write_json(new_probe, {"checked_at": now.isoformat().replace("+00:00", "Z")})

    assert module.prune_probe_dir(tmp_path, None, None, now=now) == []
    assert old_probe.exists()

    deleted = module.prune_probe_dir(tmp_path, max_age_seconds=None, max_count=1, now=now)

    assert deleted == [old_probe.name]
    assert latest.exists()
    assert new_probe.exists()
    assert not old_probe.exists()


def test_resume_ready_rejects_stale_watchdog_bot_name(tmp_path):
    probe_dir = tmp_path / "probes"
    watchdog_path = tmp_path / "watchdog.json"
    write_json(
        watchdog_path,
        {
            "bot_name": "stale-bot",
            "runtime_bot_name": "stale-bot",
            "runtime_connectivity_available": True,
            "runtime_state": "HEALTHY",
            "runtime_reconciliation_result": "succeeded",
            "runtime_orders_unknown": False,
        },
    )
    env = os.environ.copy()
    env.update(
        {
            "BOT_NAME": "live-bot",
            "PROBE_DIR": str(probe_dir),
            "WATCHDOG_STATUS": str(watchdog_path),
            "MIN_SUCCESSFUL_PROBES": "0",
        }
    )

    result = subprocess.run(
        ["bash", str(CHECK_READY_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "RESUME_NOT_READY" in result.stdout
    assert "watchdog_bot_name_mismatch" in result.stdout
    assert "RESUME_READY" not in result.stdout


def test_watchdog_status_includes_runtime_bot_name(tmp_path):
    live_bot = "live-bot"
    health_path = tmp_path / "health.json"
    orders_path = tmp_path / "orders.json"
    connectivity_path = tmp_path / "connectivity.json"
    status_dir = tmp_path / "status"
    write_json(
        health_path,
        {"data": {"bot_name": live_bot, "bot_status": "running", "recently_active": False}},
    )
    write_json(
        orders_path,
        {"data": {"bot_name": live_bot, "active_order_count": 0, "recent_order_events": []}},
    )
    write_json(
        connectivity_path,
        {
            "data": {
                "bot_name": live_bot,
                "connectivity": {
                    "current_state": "HEALTHY",
                    "reason": "ok",
                    "readiness_reason": "ok",
                    "watchdog_reason": "ok",
                    "reconciliation_result": "succeeded",
                    "quoting_enabled": True,
                    "orders_unknown": False,
                },
            }
        },
    )
    env = os.environ.copy()
    env.update(
        {
            "BOT_NAME": live_bot,
            "HEALTH_JSON_PATH": str(health_path),
            "ORDERS_JSON_PATH": str(orders_path),
            "CONNECTIVITY_JSON_PATH": str(connectivity_path),
            "STATUS_DIR": str(status_dir),
            "STATE_DIR": str(tmp_path / "state"),
            "LOG_PATH_OVERRIDE": str(tmp_path / "missing.log"),
            "HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE": "200",
            "NOW_EPOCH_OVERRIDE": "1782691200",
            "SKIP_STATUS_ARCHIVE": "true",
        }
    )

    result = subprocess.run(
        ["bash", str(WATCHDOG_PATH), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    latest = json.loads((status_dir / "latest.json").read_text())

    assert result.returncode == 0
    assert latest["bot_name"] == live_bot
    assert latest["runtime_bot_name"] == live_bot
    assert latest["bot_name_match"] is True


def test_runtime_ready_still_requires_successful_probe_and_dns():
    module = load_probe_module()
    watchdog = {
        "runtime_connectivity_available": True,
        "runtime_state": "HEALTHY",
        "runtime_reconciliation_result": "succeeded",
        "runtime_orders_unknown": False,
    }

    ready = module.build_ready_state(rest(ok=False), ws(), watchdog, dns_addrs=[])

    assert ready["ready_now"] is False
    assert ready["dns_ok"] is False
    assert "rest_probe_failed" in ready["reason"]
    assert "dns_resolution_failed" in ready["reason"]


def test_dns_drift_records_changes_without_blocking_readiness():
    module = load_probe_module()
    watchdog = {
        "runtime_connectivity_available": True,
        "runtime_state": "HEALTHY",
        "runtime_reconciliation_result": "succeeded",
        "runtime_orders_unknown": False,
    }

    drift = module.build_dns_drift(
        ["2.2.2.2"],
        {"source": "latest.json", "checked_at": "2026-06-29T00:00:00Z", "resolved_ips": ["1.1.1.1"]},
    )
    ready = module.build_ready_state(rest(), ws(), watchdog, dns_addrs=["2.2.2.2"])

    assert drift["changed"] is True
    assert drift["added"] == ["2.2.2.2"]
    assert drift["removed"] == ["1.1.1.1"]
    assert ready["ready_now"] is True


def test_rest_probe_persists_resolved_ips_and_uses_retry_jitter(monkeypatch):
    module = load_probe_module()
    sleeps = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            return b'{"ok": true}'

    monkeypatch.setattr(module, "urlopen", lambda request, timeout: FakeResponse())

    results = module.run_rest_probe(
        "https://api.hyperliquid-testnet.xyz/info",
        attempts=3,
        timeout=1.0,
        resolved_ips=["1.1.1.1"],
        retry_sleep=1.0,
        retry_jitter=0.5,
        sleep_fn=sleeps.append,
        jitter_fn=lambda start, end: 0.2,
    )

    assert [item["resolved_ips"] for item in results] == [["1.1.1.1"], ["1.1.1.1"], ["1.1.1.1"]]
    assert sleeps == [1.2, 1.2]

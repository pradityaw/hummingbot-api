#!/usr/bin/env python3
"""
F3/F8 watchdog tests (JSON-fixture only, pytest-collected).

F3: API stop is verified (strategy stopped AND zero active orders), unverified
stops escalate to docker stop, and only verified stops suppress retries.
F8: bot-name identity drift is fail-closed (no actions, exit 1).
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "ops" / "watch_hyperliquid_pmm_remote.sh"
FIXTURES = REPO_ROOT / "ops" / "testdata" / "watchdog"
BOT = "test-bot"
NOW = 1_750_519_208


def _write_state(state_dir: Path, activation_epoch: int = NOW - 1800) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "activation_epoch").write_text(f"{activation_epoch}\n")
    (state_dir / "last_bot_name").write_text(f"{BOT}\n")
    (state_dir / "last_bot_status").write_text("running\n")


def _run_watchdog(tmp_path: Path, extra_env: dict, expect_ok: bool = True):
    state_dir = tmp_path / "state"
    status_dir = tmp_path / "status"
    status_dir.mkdir(exist_ok=True)
    _write_state(state_dir)
    env = {
        "REPO_ROOT": str(REPO_ROOT),
        "STATE_DIR": str(state_dir),
        "STATUS_DIR": str(status_dir),
        "BOT_NAME": BOT,
        "LOG_PATH_OVERRIDE": str(tmp_path / "empty.log"),
        "HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE": "200",
        "NOW_EPOCH_OVERRIDE": str(NOW),
        "SKIP_STATUS_ARCHIVE": "true",
        "STOP_VERIFY_DELAY_SECONDS": "0",
        **extra_env,
    }
    (tmp_path / "empty.log").touch()
    result = subprocess.run(
        [str(SCRIPT)], capture_output=True, text=True,
        env={**os.environ, **env},
    )
    if expect_ok:
        assert result.returncode == 0, f"watchdog failed: {result.stderr}\n{result.stdout}"
    status = json.loads((status_dir / "latest.json").read_text())
    return result, status, state_dir


def _health_json(bot_status: str) -> str:
    return json.dumps({"data": {"bot_status": bot_status, "recently_active": True}})


def _docker_stub(tmp_path: Path, succeed: bool) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    body = "#!/usr/bin/env bash\n"
    if succeed:
        body += "exit 0\n"
    else:
        body += "exit 1\n"
    docker.write_text(body)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return str(bin_dir)


def _unsafe_env(tmp_path: Path, health_lines, orders_fixture="orders_recent_create_only.json"):
    """Runtime DEGRADED_UNSAFE -> hard stop reason, bot running, exposure active."""
    seq = tmp_path / "health_sequence.jsonl"
    seq.write_text("\n".join(health_lines) + "\n")
    return {
        "CONNECTIVITY_JSON_PATH": str(FIXTURES / "connectivity_unsafe.json"),
        "HEALTH_JSON_SEQUENCE_PATH": str(seq),
        "ORDERS_JSON_PATH": str(FIXTURES / orders_fixture),
    }


# --- F3: verified stop --------------------------------------------------------

def test_stop_verified_via_api_health(tmp_path):
    # initial fetch: running; poll 1: still running; poll 2: stopped.
    env = _unsafe_env(tmp_path, [_health_json("running"), _health_json("running"), _health_json("stopped")])
    _, status, state_dir = _run_watchdog(tmp_path, env)

    assert status["action"] == "stop_bot"
    assert status["stop_verified"] is True
    assert status["stop_verification_attempts"] == 2
    assert status["stop_escalation"] == "none"
    assert (state_dir / "last_stop_state.json").exists()


def test_verified_stop_suppresses_repeat_stop_same_run(tmp_path):
    env = _unsafe_env(tmp_path, [_health_json("running"), _health_json("stopped")])
    _run_watchdog(tmp_path, env)
    # second run, same run_id (activation file kept): must be suppressed.
    env2 = _unsafe_env(tmp_path, [_health_json("running"), _health_json("stopped")])
    _, status2, _ = _run_watchdog(tmp_path, env2)
    assert status2["action"] == "none"
    assert status2["stop_suppressed_reason"] == "already_stopped_this_run"


def test_unverified_stop_escalates_to_docker_stop(tmp_path):
    env = _unsafe_env(tmp_path, [_health_json("running")] * 5)
    env["PATH"] = _docker_stub(tmp_path, succeed=True) + ":" + os.environ["PATH"]
    env["DOCKER_CONTAINER_NAME_OVERRIDE"] = BOT
    _, status, state_dir = _run_watchdog(tmp_path, env)

    assert status["action"] == "stop_bot"
    assert status["stop_verified"] is True
    assert status["stop_escalation"] == "docker_stop"
    assert (state_dir / "last_stop_state.json").exists()


def test_unverified_stop_with_failed_escalation_does_not_suppress_retry(tmp_path):
    env = _unsafe_env(tmp_path, [_health_json("running")] * 5)
    env["PATH"] = _docker_stub(tmp_path, succeed=False) + ":" + os.environ["PATH"]
    env["DOCKER_CONTAINER_NAME_OVERRIDE"] = BOT
    _, status, state_dir = _run_watchdog(tmp_path, env)

    assert status["action"] == "stop_bot_unverified"
    assert status["stop_verified"] is False
    assert status["stop_escalation"] == "failed"
    assert status["stop_suppressed_reason"] == "CRITICAL_stop_unverified"
    assert not (state_dir / "last_stop_state.json").exists()

    # Next run retries the stop instead of suppressing it.
    env2 = _unsafe_env(tmp_path, [_health_json("running"), _health_json("stopped")])
    _, status2, _ = _run_watchdog(tmp_path, env2)
    assert status2["action"] == "stop_bot"
    assert status2["stop_verified"] is True


def test_dry_run_stop_unchanged(tmp_path):
    env = _unsafe_env(tmp_path, [_health_json("running")])
    result = subprocess.run(
        [str(SCRIPT), "--dry-run"], capture_output=True, text=True,
        env={**os.environ, **{
            "REPO_ROOT": str(REPO_ROOT),
            "STATE_DIR": str(tmp_path / "state"),
            "STATUS_DIR": str(tmp_path / "status"),
            "BOT_NAME": BOT,
            "LOG_PATH_OVERRIDE": str(tmp_path / "empty.log"),
            "HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE": "200",
            "NOW_EPOCH_OVERRIDE": str(NOW),
            "SKIP_STATUS_ARCHIVE": "true",
            **env,
        }},
    )
    assert result.returncode == 0
    status = json.loads((tmp_path / "status" / "latest.json").read_text())
    assert status["action"] == "would_stop_bot"


# --- F8: identity hard-fail ---------------------------------------------------

def test_runtime_bot_name_mismatch_hard_fails(tmp_path):
    connectivity = json.loads((FIXTURES / "connectivity_unsafe.json").read_text())
    connectivity.setdefault("data", {})["bot_name"] = "some-other-bot"
    conn_path = tmp_path / "connectivity_mismatch.json"
    conn_path.write_text(json.dumps(connectivity))
    env = {
        "CONNECTIVITY_JSON_PATH": str(conn_path),
        "HEALTH_JSON_PATH": str(FIXTURES / "health_running.json"),
        "ORDERS_JSON_PATH": str(FIXTURES / "orders_active.json"),
    }
    result, status, _ = _run_watchdog(tmp_path, env, expect_ok=False)

    assert result.returncode == 1
    assert "identity_error" in result.stderr
    assert status["identity_error"].startswith("runtime_bot_name_mismatch")
    assert status["action"] == "none"


def test_bot_absent_from_api_status_hard_fails(tmp_path):
    # API up, but the configured bot is not in the status list: stale BOT_NAME.
    status_path = tmp_path / "api_status.json"
    status_path.write_text(json.dumps({"data": {"another-bot": {}}}))
    env = {
        "STATUS_JSON_PATH": str(status_path),
        # no health/orders fixtures -> api_ok false, triggers the status probe
    }
    result, status, _ = _run_watchdog(tmp_path, env, expect_ok=False)

    assert result.returncode == 1
    assert status["identity_error"].startswith("bot_not_found_on_api")
    assert status["action"] == "none"


def test_bot_present_but_health_failing_does_not_hard_fail(tmp_path):
    status_path = tmp_path / "api_status.json"
    status_path.write_text(json.dumps({"data": {BOT: {}}}))
    env = {"STATUS_JSON_PATH": str(status_path)}
    result, status, _ = _run_watchdog(tmp_path, env)

    assert result.returncode == 0
    assert status["identity_error"] == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

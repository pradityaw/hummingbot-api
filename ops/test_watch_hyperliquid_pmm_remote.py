#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "ops" / "watch_hyperliquid_pmm_remote.sh"
FIXTURES = REPO_ROOT / "ops" / "testdata" / "watchdog"
REAL_FAILURE_LOG = REPO_ROOT / "bots" / "instances" / "hl-testnet-pmm-btc-20260618-081557" / "logs" / "logs_hl-testnet-pmm-btc-20260618-081557.log"


def write_state(state_dir: Path, activation_epoch: int, bot_name: str = "test-bot", bot_status: str = "running") -> None:
    (state_dir / "activation_epoch").write_text(f"{activation_epoch}\n")
    (state_dir / "last_bot_name").write_text(f"{bot_name}\n")
    (state_dir / "last_bot_status").write_text(f"{bot_status}\n")


def run_case(
    name: str,
    *,
    now_epoch: int,
    log_path: Path,
    health_path: Path,
    orders_path: Path,
    activation_epoch: int,
    connectivity_path: Path = None,
    dry_run: bool = True,
):
    temp_root = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    state_dir = temp_root / "state"
    status_dir = temp_root / "status"
    state_dir.mkdir()
    status_dir.mkdir()
    write_state(state_dir, activation_epoch=activation_epoch)

    env = {
        "REPO_ROOT": str(REPO_ROOT),
        "STATE_DIR": str(state_dir),
        "STATUS_DIR": str(status_dir),
        "BOT_NAME": "test-bot",
        "HEALTH_JSON_PATH": str(health_path),
        "ORDERS_JSON_PATH": str(orders_path),
        "LOG_PATH_OVERRIDE": str(log_path),
        "HYPERLIQUID_INFO_HTTP_CODE_OVERRIDE": "200",
        "NOW_EPOCH_OVERRIDE": str(now_epoch),
        "SKIP_STATUS_ARCHIVE": "true",
    }
    if connectivity_path is not None:
        env["CONNECTIVITY_JSON_PATH"] = str(connectivity_path)

    cmd = [str(SCRIPT)]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **env}, check=True)
    status = json.loads((status_dir / "latest.json").read_text())
    return temp_root, result, status


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    temp_roots = []
    try:
        benign_root, benign_result, benign = run_case(
            "benign",
            now_epoch=1750519208,
            activation_epoch=1750517400,
            log_path=FIXTURES / "benign_reconnects.log",
            health_path=FIXTURES / "health_running.json",
            orders_path=FIXTURES / "orders_active.json",
        )
        temp_roots.append(benign_root)
        expect(benign["action"] == "none", "benign reconnects should not stop the bot")
        expect(benign["hard_disconnect_count"] == 0, "benign reconnects should not count as hard disconnects")
        expect(benign["benign_disconnect_count"] == 3, "expected three benign disconnects")
        expect(benign["resubscribe_count"] == 3, "expected three resubscribe events")

        hard_root, _, hard = run_case(
            "hard",
            now_epoch=1750519208,
            activation_epoch=1750517400,
            log_path=FIXTURES / "hard_disconnects.log",
            health_path=FIXTURES / "health_running.json",
            orders_path=FIXTURES / "orders_active.json",
        )
        temp_roots.append(hard_root)
        expect(hard["action"] == "would_stop_bot", "hard disconnects should trigger a stop in dry-run mode")
        expect("hard_disconnect_instability" in hard["reasons"], "expected hard disconnect reason")

        failure_root, _, failure = run_case(
            "failure",
            now_epoch=1781773860,
            activation_epoch=1781772060,
            log_path=REAL_FAILURE_LOG,
            health_path=FIXTURES / "health_running.json",
            orders_path=FIXTURES / "orders_active.json",
        )
        temp_roots.append(failure_root)
        expect(failure["action"] == "would_stop_bot", "June 18 exchange failures should still trigger stop")
        expect("exchange_5xx_instability" in failure["reasons"], "expected exchange 5xx reason")
        expect("open_order_failure_spike" in failure["reasons"], "expected open order failure reason")

        structured_healthy_root, _, structured_healthy = run_case(
            "structured-healthy",
            now_epoch=1781773860,
            activation_epoch=1781772060,
            log_path=REAL_FAILURE_LOG,
            health_path=FIXTURES / "health_running.json",
            orders_path=FIXTURES / "orders_active.json",
            connectivity_path=FIXTURES / "connectivity_healthy.json",
        )
        temp_roots.append(structured_healthy_root)
        expect(structured_healthy["action"] == "none", "healthy runtime state should clear historical log degradation")
        expect(structured_healthy["reasons"] == [], "healthy runtime state should not keep historical hard reasons")
        expect(structured_healthy["runtime_state"] == "HEALTHY", "expected structured healthy runtime state")

        structured_unsafe_root, _, structured_unsafe = run_case(
            "structured-unsafe",
            now_epoch=1781773860,
            activation_epoch=1781772060,
            log_path=FIXTURES / "benign_reconnects.log",
            health_path=FIXTURES / "health_running.json",
            orders_path=FIXTURES / "orders_active.json",
            connectivity_path=FIXTURES / "connectivity_unsafe.json",
        )
        temp_roots.append(structured_unsafe_root)
        expect(structured_unsafe["action"] == "would_stop_bot", "unsafe runtime state should trigger stop")
        expect(any(reason.startswith("runtime_HARD_DISCONNECTED") for reason in structured_unsafe["reasons"]), "expected runtime hard disconnect reason")

        stopped_root, _, stopped = run_case(
            "stopped",
            now_epoch=1781773860,
            activation_epoch=1781772060,
            log_path=REAL_FAILURE_LOG,
            health_path=FIXTURES / "health_stopped.json",
            orders_path=FIXTURES / "orders_recent_create_only.json",
        )
        temp_roots.append(stopped_root)
        expect(stopped["action"] == "none", "stopped bot should not receive repeated stop commands")
        expect(stopped["stop_suppressed_reason"] == "bot_not_running", "expected suppression while bot is stopped")

        stalled_root, _, stalled = run_case(
            "stalled",
            now_epoch=1782055208,
            activation_epoch=1782053400,
            log_path=FIXTURES / "stalled_runtime.log",
            health_path=FIXTURES / "health_running.json",
            orders_path=FIXTURES / "orders_stalled.json",
        )
        temp_roots.append(stalled_root)
        expect(stalled["action"] == "would_stop_bot", "stalled running bot should trigger a stop in dry-run mode")
        expect("stalled_runtime" in stalled["reasons"], "expected stalled runtime reason")

        print("watchdog verification passed")
        print(benign_result.stdout.strip())
        return 0
    finally:
        for root in temp_roots:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

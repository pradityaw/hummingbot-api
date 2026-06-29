import importlib.util
from pathlib import Path


RECONNECT_PATH = Path(__file__).resolve().parents[1] / "ops" / "reconnect_hyperliquid_bot.py"


def load_reconnect_module():
    spec = importlib.util.spec_from_file_location("reconnect_hyperliquid_bot", RECONNECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ready_probe(bot_name="bot"):
    return {
        "bot_name": bot_name,
        "dns": {"resolved_ips": ["1.1.1.1"]},
        "ready": {
            "ready_now": True,
            "rest_ok": True,
            "ws_ok": True,
            "dns_ok": True,
        },
    }


def healthy_watchdog(bot_name="bot", active_order_count=0):
    return {
        "bot_name": bot_name,
        "runtime_bot_name": bot_name,
        "runtime_connectivity_available": True,
        "runtime_state": "HEALTHY",
        "runtime_reconciliation_result": "succeeded",
        "runtime_orders_unknown": False,
        "active_order_count": active_order_count,
    }


def test_reconnect_dry_run_allows_restart_only_when_gates_pass():
    module = load_reconnect_module()

    decision = module.build_reconnect_decision(ready_probe(), healthy_watchdog(), "bot")

    assert decision["restart_allowed"] is True
    assert decision["dry_run_safe_default"] is True
    assert decision["will_place_orders"] is False
    assert decision["will_set_quoting_enabled"] is False


def test_reconnect_blocks_when_active_orders_are_present():
    module = load_reconnect_module()

    decision = module.build_reconnect_decision(ready_probe(), healthy_watchdog(active_order_count=2), "bot")

    assert decision["restart_allowed"] is False
    assert "active_orders_present" in decision["reasons"]


def test_reconnect_blocks_when_reconciliation_is_incomplete():
    module = load_reconnect_module()
    watchdog = healthy_watchdog()
    watchdog["runtime_state"] = "RECOVERING"
    watchdog["runtime_reconciliation_result"] = "running"

    decision = module.build_reconnect_decision(ready_probe(), watchdog, "bot")

    assert decision["restart_allowed"] is False
    assert "runtime_reconciliation_or_safety_gate_failed" in decision["reasons"]

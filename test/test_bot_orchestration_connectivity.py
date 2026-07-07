import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import routers.bot_orchestration as bot_orchestration
import services.docker_service as docker_service


def test_read_bot_connectivity_state_from_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_orchestration, "_bot_instance_dir", lambda bot_name: tmp_path / bot_name)
    state_dir = tmp_path / "bot" / "data" / "connectivity"
    state_dir.mkdir(parents=True)
    payload = {"current_state": "HEALTHY", "quoting_enabled": True, "readiness_reason": "ok"}
    (state_dir / "runtime_connectivity_state.json").write_text(json.dumps(payload))

    result = bot_orchestration._read_bot_connectivity_state("bot")

    assert result == payload


def test_read_bot_connectivity_state_from_controller_status(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_orchestration, "_bot_instance_dir", lambda bot_name: tmp_path / bot_name)
    status = {
        "performance": {
            "controller": {
                "custom_info": {
                    "runtime_connectivity": {
                        "current_state": "RECOVERING",
                        "quoting_enabled": False,
                        "readiness_reason": "reconciliation_required",
                    }
                }
            }
        }
    }

    result = bot_orchestration._read_bot_connectivity_state("bot", status)

    assert result["current_state"] == "RECOVERING"
    assert result["quoting_enabled"] is False


def test_read_bot_connectivity_events(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_orchestration, "_bot_instance_dir", lambda bot_name: tmp_path / bot_name)
    events_dir = tmp_path / "bot" / "data" / "connectivity"
    events_dir.mkdir(parents=True)
    path = events_dir / "runtime_connectivity_events.jsonl"
    path.write_text("\n".join(json.dumps({"index": index}) for index in range(5)))

    result = bot_orchestration._read_bot_connectivity_events("bot", 2)

    assert result == [{"index": 3}, {"index": 4}]


def test_bot_container_dns_servers_default(monkeypatch):
    monkeypatch.delenv("HBOT_BOT_DNS_SERVERS", raising=False)

    assert docker_service.DockerService._bot_container_dns_servers() == ["1.1.1.1", "8.8.8.8"]


def test_bot_container_dns_servers_can_be_overridden(monkeypatch):
    monkeypatch.setenv("HBOT_BOT_DNS_SERVERS", "9.9.9.9, 1.0.0.1")

    assert docker_service.DockerService._bot_container_dns_servers() == ["9.9.9.9", "1.0.0.1"]


def test_bot_container_dns_servers_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HBOT_BOT_DNS_SERVERS", "")

    assert docker_service.DockerService._bot_container_dns_servers() == []


def test_connectivity_guard_environment_forwards_hb_connectivity_vars(monkeypatch):
    monkeypatch.setenv("HB_CONNECTIVITY_MAX_BOOK_SPREAD_RATIO", "0.01")
    monkeypatch.setenv("HB_CONNECTIVITY_MAX_OPEN_ORDERS", "4")
    monkeypatch.setenv("HB_CONNECTIVITY_EMPTY", "")
    monkeypatch.delenv("HB_ALLOW_MAINNET", raising=False)

    env = docker_service.DockerService._connectivity_guard_environment()

    assert env["HB_CONNECTIVITY_MAX_BOOK_SPREAD_RATIO"] == "0.01"
    assert env["HB_CONNECTIVITY_MAX_OPEN_ORDERS"] == "4"
    assert "HB_CONNECTIVITY_EMPTY" not in env
    assert "HB_ALLOW_MAINNET" not in env


def test_connectivity_guard_environment_forwards_allow_mainnet_only_when_set(monkeypatch):
    for key in list(os.environ):
        if key.startswith("HB_CONNECTIVITY_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HB_ALLOW_MAINNET", "1")

    env = docker_service.DockerService._connectivity_guard_environment()

    assert env == {"HB_ALLOW_MAINNET": "1"}

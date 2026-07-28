"""
F2: the mainnet connector guard must cover ALL order/deployment entry points,
not just deploy-v2-controllers. Covers deploy-v2-script, MQTT start-bot, the
direct trading router, and credential addition.

The routers pull in the whole services graph (which needs the hummingbot
package, only present in containers), so these tests stub the service layer
and exercise the real router + guard code.
"""
from __future__ import annotations

import asyncio
import sys
import types
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_service_stubs() -> None:
    if "routers.bot_orchestration" in sys.modules:
        return

    class _StubEnum(Enum):
        LIMIT = 1
        LIMIT_MAKER = 2
        MARKET = 3
        OPEN = 4
        CLOSE = 5
        NIL = 6
        BUY = 7
        SELL = 8
        ONEWAY = 9
        HEDGE = 10

    hummingbot_common = types.ModuleType("hummingbot.core.data_type.common")
    hummingbot_common.OrderType = _StubEnum
    hummingbot_common.PositionAction = _StubEnum
    hummingbot_common.PositionMode = _StubEnum
    hummingbot_common.TradeType = _StubEnum
    for name in ("hummingbot", "hummingbot.core", "hummingbot.core.data_type"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["hummingbot.core.data_type.common"] = hummingbot_common

    database = types.ModuleType("database")
    database.AsyncDatabaseManager = MagicMock
    database.BotRunRepository = MagicMock
    sys.modules["database"] = database

    deps = types.ModuleType("deps")
    for getter in ("get_bot_archiver", "get_bots_orchestrator", "get_database_manager",
                   "get_docker_service", "get_accounts_service", "get_connector_service"):
        setattr(deps, getter, lambda: None)
    sys.modules["deps"] = deps

    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = []
    sys.modules["services"] = services_pkg
    bots_orchestrator = types.ModuleType("services.bots_orchestrator")
    bots_orchestrator.BotsOrchestrator = MagicMock
    sys.modules["services.bots_orchestrator"] = bots_orchestrator
    docker_service = types.ModuleType("services.docker_service")
    docker_service.DockerService = MagicMock
    sys.modules["services.docker_service"] = docker_service
    accounts_service = types.ModuleType("services.accounts_service")
    accounts_service.AccountsService = MagicMock
    sys.modules["services.accounts_service"] = accounts_service

    bot_archiver = types.ModuleType("utils.bot_archiver")
    bot_archiver.BotArchiver = MagicMock
    sys.modules["utils.bot_archiver"] = bot_archiver

    # utils.file_system imports hummingbot client config; provide a bare fs_util
    # whose methods tests monkeypatch per-case.
    file_system = types.ModuleType("utils.file_system")
    file_system.fs_util = types.SimpleNamespace(
        read_yaml_file=lambda path: (_ for _ in ()).throw(FileNotFoundError(path)),
        dump_dict_to_yaml=lambda *a, **k: None,
    )
    sys.modules["utils.file_system"] = file_system


_install_service_stubs()

from fastapi import HTTPException  # noqa: E402

import routers.accounts as accounts_router  # noqa: E402
import routers.bot_orchestration as bot_router  # noqa: E402
import routers.trading as trading_router  # noqa: E402
from models import StartBotAction, V2ScriptDeployment  # noqa: E402


@pytest.fixture(autouse=True)
def _mainnet_not_allowed(monkeypatch):
    monkeypatch.delenv("HB_ALLOW_MAINNET", raising=False)


def _yaml_stub(mapping):
    def read_yaml_file(path):
        if path not in mapping:
            raise FileNotFoundError(path)
        return mapping[path]
    return read_yaml_file


TESTNET_CONTROLLER = {"id": "c1", "controller_name": "pmm_simple",
                      "connector_name": "hyperliquid_perpetual_testnet"}
MAINNET_CONTROLLER = {"id": "c2", "controller_name": "pmm_simple",
                      "connector_name": "hyperliquid_perpetual"}


def _script_cfg(*controller_files):
    return {"script_file_name": "v2_with_controllers.py",
            "controllers_config": list(controller_files)}


# --- deploy-v2-script -------------------------------------------------------

def test_deploy_v2_script_blocks_mainnet_controller_config(monkeypatch):
    monkeypatch.setattr(bot_router.fs_util, "read_yaml_file", _yaml_stub({
        "conf/scripts/run.yml": _script_cfg("mainnet_ctrl"),
        "conf/controllers/mainnet_ctrl.yml": MAINNET_CONTROLLER,
    }))
    monkeypatch.setattr(bot_router.fs_util, "dump_dict_to_yaml", lambda *a, **k: None)
    deployment = V2ScriptDeployment(instance_name="bot", credentials_profile="testnet_fresh",
                                    script="v2_with_controllers", script_config="run")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bot_router.deploy_v2_script(deployment, MagicMock(), MagicMock()))
    assert exc_info.value.status_code == 400
    assert "hyperliquid_perpetual" in exc_info.value.detail


def test_deploy_v2_script_allows_testnet_and_deploys(monkeypatch):
    dump_calls = []
    monkeypatch.setattr(bot_router.fs_util, "read_yaml_file", _yaml_stub({
        "conf/scripts/run.yml": _script_cfg("testnet_ctrl"),
        "conf/controllers/testnet_ctrl.yml": TESTNET_CONTROLLER,
    }))
    monkeypatch.setattr(bot_router.fs_util, "dump_dict_to_yaml",
                        lambda *a, **k: dump_calls.append(a))
    docker_manager = MagicMock()
    docker_manager.create_hummingbot_instance = MagicMock(return_value={"success": True})
    db_manager = MagicMock()
    deployment = V2ScriptDeployment(instance_name="bot", credentials_profile="testnet_fresh",
                                    script="v2_with_controllers", script_config="run")

    response = asyncio.run(bot_router.deploy_v2_script(deployment, docker_manager, db_manager))

    assert response["success"] is True
    docker_manager.create_hummingbot_instance.assert_called_once()


def test_deploy_v2_script_missing_config_fails_closed(monkeypatch):
    monkeypatch.setattr(bot_router.fs_util, "read_yaml_file", _yaml_stub({}))
    deployment = V2ScriptDeployment(instance_name="bot", credentials_profile="testnet_fresh",
                                    script="v2_with_controllers", script_config="ghost")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bot_router.deploy_v2_script(deployment, MagicMock(), MagicMock()))
    assert exc_info.value.status_code == 400


# --- MQTT start-bot ----------------------------------------------------------

def test_start_bot_blocks_mainnet_script_conf(monkeypatch):
    monkeypatch.setattr(bot_router.fs_util, "read_yaml_file", _yaml_stub({
        "conf/scripts/run.yml": {"script_file_name": "x.py",
                                 "connector_name": "hyperliquid_perpetual"},
    }))
    action = StartBotAction(bot_name="bot", script="v2_with_controllers", conf="run")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bot_router.start_bot(action, MagicMock(), MagicMock()))
    assert exc_info.value.status_code == 400


def test_start_bot_without_conf_passes_through(monkeypatch):
    bots_manager = MagicMock()

    async def fake_start(bot_name, **kwargs):
        return {"success": True}

    bots_manager.start_bot = fake_start
    action = StartBotAction(bot_name="bot")

    result = asyncio.run(bot_router.start_bot(action, bots_manager, MagicMock()))
    assert result["status"] == "success"


# --- direct trading router ---------------------------------------------------

def _trade_request(connector_name):
    return types.SimpleNamespace(
        account_name="testnet_fresh", connector_name=connector_name,
        trading_pair="BTC-USD", trade_type="BUY", amount=0.0001,
        order_type="LIMIT", price=50000.0, position_action="OPEN",
    )


def test_place_trade_blocks_mainnet_connector():
    accounts_service = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(trading_router.place_trade(_trade_request("hyperliquid_perpetual"), accounts_service))
    assert exc_info.value.status_code == 400
    accounts_service.place_trade.assert_not_called()


def test_place_trade_allows_testnet_connector():
    accounts_service = MagicMock()

    async def fake_place_trade(**kwargs):
        return "0xorder"

    accounts_service.place_trade = fake_place_trade

    response = asyncio.run(trading_router.place_trade(
        _trade_request("hyperliquid_perpetual_testnet"), accounts_service))
    assert response.order_id == "0xorder"


# --- credential addition -----------------------------------------------------

def test_add_credential_blocks_mainnet_connector():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(accounts_router.add_credential("master", "hyperliquid_perpetual", {}, MagicMock()))
    assert exc_info.value.status_code == 400


def test_add_credential_allows_testnet_connector():
    accounts_service = MagicMock()

    async def fake_add(*args):
        return None

    accounts_service.add_credentials = fake_add

    result = asyncio.run(accounts_router.add_credential(
        "master", "hyperliquid_perpetual_testnet", {"k": "v"}, accounts_service))
    assert "success" in result["message"].lower()


# --- helper-level: existing guarded path still intact ------------------------

def test_validate_deployment_connector_names_still_blocks(monkeypatch):
    monkeypatch.setattr(bot_router.fs_util, "read_yaml_file", _yaml_stub({
        "conf/controllers/mainnet_ctrl.yml": MAINNET_CONTROLLER,
    }))
    with pytest.raises(HTTPException) as exc_info:
        bot_router._validate_deployment_connector_names(["mainnet_ctrl"])
    assert exc_info.value.status_code == 400

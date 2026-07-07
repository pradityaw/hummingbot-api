import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "bots" / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from mainnet_guard import (  # noqa: E402
    ALLOW_MAINNET_ENV,
    MainnetConnectorBlockedError,
    extract_connector_names_from_mapping,
    find_non_testnet_connectors,
    is_testnet_connector,
    validate_testnet_connectors,
)


def test_is_testnet_connector():
    assert is_testnet_connector("hyperliquid_perpetual_testnet") is True
    assert is_testnet_connector("hyperliquid_perpetual") is False


def test_find_non_testnet_connectors_deduplicates_and_sorts():
    blocked = find_non_testnet_connectors(
        ["hyperliquid_perpetual_testnet", "hyperliquid_perpetual", "binance", "hyperliquid_perpetual"]
    )

    assert blocked == ["binance", "hyperliquid_perpetual"]


def test_validate_testnet_connectors_blocks_mainnet_by_default(monkeypatch):
    monkeypatch.delenv(ALLOW_MAINNET_ENV, raising=False)

    with pytest.raises(MainnetConnectorBlockedError) as exc_info:
        validate_testnet_connectors(["hyperliquid_perpetual"])

    assert "hyperliquid_perpetual" in str(exc_info.value)
    assert ALLOW_MAINNET_ENV in str(exc_info.value)


def test_validate_testnet_connectors_allows_testnet(monkeypatch):
    monkeypatch.delenv(ALLOW_MAINNET_ENV, raising=False)

    validate_testnet_connectors(["hyperliquid_perpetual_testnet"])


def test_validate_testnet_connectors_allows_mainnet_with_opt_in(monkeypatch):
    monkeypatch.setenv(ALLOW_MAINNET_ENV, "1")

    validate_testnet_connectors(["hyperliquid_perpetual"])


def test_extract_connector_names_from_mapping():
    config = {
        "connector_name": "hyperliquid_perpetual_testnet",
        "candles_connector": "hyperliquid_perpetual_testnet",
        "maker_connector": "binance",
        "connector_pair_dominant": {
            "connector_name": "hyperliquid_perpetual",
            "trading_pair": "BTC-USD",
        },
        "unrelated": "value",
    }

    names = extract_connector_names_from_mapping(config)

    assert names == {
        "hyperliquid_perpetual_testnet",
        "binance",
        "hyperliquid_perpetual",
    }


def test_utils_mainnet_guard_matches_script_guard_behavior(monkeypatch):
    from utils.mainnet_guard import (
        ALLOW_MAINNET_ENV as utils_allow_env,
        MainnetConnectorBlockedError as utils_error,
        validate_testnet_connectors as utils_validate,
    )

    monkeypatch.delenv(ALLOW_MAINNET_ENV, raising=False)

    assert utils_allow_env == ALLOW_MAINNET_ENV
    with pytest.raises(utils_error):
        utils_validate(["hyperliquid_perpetual"])
    with pytest.raises(MainnetConnectorBlockedError):
        validate_testnet_connectors(["hyperliquid_perpetual"])


def test_v2_with_controllers_enforces_testnet_guard():
    source = (REPO_ROOT / "bots" / "scripts" / "v2_with_controllers.py").read_text(encoding="utf-8")

    assert "_enforce_testnet_connector_guard" in source
    assert "validate_testnet_connectors" in source
    assert "extract_connector_names_from_mapping" in source


try:
    import fastapi  # noqa: F401
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")
def test_validate_deployment_connector_names_rejects_mainnet(monkeypatch):
    import routers.bot_orchestration as bot_orchestration

    monkeypatch.delenv(ALLOW_MAINNET_ENV, raising=False)
    monkeypatch.setattr(
        bot_orchestration.fs_util,
        "read_yaml_file",
        lambda path: {
            "connector_name": "hyperliquid_perpetual",
            "trading_pair": "BTC-USD",
        },
    )

    with pytest.raises(bot_orchestration.HTTPException) as exc_info:
        bot_orchestration._validate_deployment_connector_names(["hl_mainnet_pmm_btc.yml"])

    assert exc_info.value.status_code == 400
    assert "hyperliquid_perpetual" in exc_info.value.detail


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")
def test_validate_deployment_connector_names_allows_testnet(monkeypatch):
    import routers.bot_orchestration as bot_orchestration

    monkeypatch.delenv(ALLOW_MAINNET_ENV, raising=False)
    monkeypatch.setattr(
        bot_orchestration.fs_util,
        "read_yaml_file",
        lambda path: {
            "connector_name": "hyperliquid_perpetual_testnet",
            "trading_pair": "BTC-USD",
        },
    )

    bot_orchestration._validate_deployment_connector_names(["hl_testnet_pmm_btc.yml"])


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")
def test_validate_deployment_connector_names_missing_config(monkeypatch):
    import routers.bot_orchestration as bot_orchestration

    def _missing(_path):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(bot_orchestration.fs_util, "read_yaml_file", _missing)

    with pytest.raises(bot_orchestration.HTTPException) as exc_info:
        bot_orchestration._validate_deployment_connector_names(["missing.yml"])

    assert exc_info.value.status_code == 400
    assert "not found" in exc_info.value.detail

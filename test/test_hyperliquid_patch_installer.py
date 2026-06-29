import importlib.util
from pathlib import Path


PATCH_PATH = Path(__file__).resolve().parents[1] / "docker" / "hyperliquid-patch" / "patch_hyperliquid_connector.py"


def load_patch_module():
    spec = importlib.util.spec_from_file_location("patch_hyperliquid_connector", PATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_patch_file_updates_known_hyperliquid_fragments(tmp_path):
    module = load_patch_module()
    target = tmp_path / "hyperliquid_perpetual_derivative.py"
    target.write_text(
        "enable_hip3_markets: bool = True,\n"
        "deployer, base = full_symbol.split(':')\n"
        "dex_name, coin = exchange_symbol.split(\":\")\n"
    )

    assert module.patch_file(target) is True
    content = target.read_text()
    assert "enable_hip3_markets: bool = False," in content
    assert "full_symbol.split(':', 1)" in content
    assert 'exchange_symbol.split(":", 1)' in content


def test_install_overrides_copies_runtime_overlay(tmp_path):
    module = load_patch_module()

    assert module.install_overrides(tmp_path) is True
    assert (tmp_path / "hummingbot" / "connector" / "derivative" / "hyperliquid_perpetual" / "hyperliquid_runtime_connectivity.py").exists()

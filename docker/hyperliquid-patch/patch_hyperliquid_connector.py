import shutil
from pathlib import Path


TARGET_NAME = "hyperliquid_perpetual_derivative.py"
OVERRIDES_DIR = Path(__file__).resolve().parent / "overrides"


def patch_file(path: Path) -> bool:
    original = path.read_text()
    updated = original

    updated = updated.replace(
        'enable_hip3_markets: bool = True,',
        'enable_hip3_markets: bool = False,',
    )
    updated = updated.replace(
        "deployer, base = full_symbol.split(':')",
        "deployer, base = full_symbol.split(':', 1)",
    )
    updated = updated.replace(
        'dex_name, coin = exchange_symbol.split(":")',
        'dex_name, coin = exchange_symbol.split(":", 1)',
    )

    if updated == original:
        return False

    path.write_text(updated)
    return True


def install_overrides(package_root: Path) -> bool:
    if not OVERRIDES_DIR.exists():
        return False
    installed_any = False
    for source in OVERRIDES_DIR.rglob("*"):
        if not source.is_file():
            continue
        relative_path = source.relative_to(OVERRIDES_DIR)
        destination = package_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        print(f"Installed override {destination}")
        installed_any = True
    return installed_any


def main() -> None:
    roots = [
        Path("/opt/conda/envs"),
        Path("/opt/conda/lib"),
        Path("/usr/local/lib"),
        Path("/home/hummingbot"),
    ]

    patched_any = False
    override_any = False
    package_roots = set()
    for root in roots:
        if not root.exists():
            continue
        for candidate in root.rglob(TARGET_NAME):
            candidate_str = str(candidate)
            marker = "hummingbot/connector/derivative/hyperliquid_perpetual/"
            if marker not in candidate_str:
                continue
            package_roots.add(Path(candidate_str.split(marker, 1)[0]))
            if patch_file(candidate):
                print(f"Patched {candidate}")
                patched_any = True

    for package_root in package_roots:
        override_any = install_overrides(package_root) or override_any

    if not patched_any and not override_any:
        raise SystemExit("No hyperliquid_perpetual_derivative.py files were patched or overridden.")


if __name__ == "__main__":
    main()

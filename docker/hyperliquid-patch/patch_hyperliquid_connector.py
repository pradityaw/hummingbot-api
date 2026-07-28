import shutil
from pathlib import Path


TARGET_NAME = "hyperliquid_perpetual_derivative.py"
POSITION_EXECUTOR_NAME = "position_executor.py"
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


def patch_position_executor_file(path: Path) -> bool:
    """
    Base-image PositionExecutor retry fixes.

    In the stock file, a synchronous placement exception (e.g. the strategy
    whitelist ValueError raised when markets have already been torn down)
    escapes control_task BEFORE _current_retries increments, so the executor
    retries forever: a close failure strands the position silently and an open
    failure spins at every tick. Count those exceptions toward retries, give up
    loudly at max retries (FAILED), and keep the normal event-driven path intact.
    """
    original = path.read_text()
    updated = original

    updated = updated.replace(
        "            else:\n"
        "                await self.control_close_order()\n"
        "                self._current_retries += 1\n",
        "            else:\n"
        "                try:\n"
        "                    await self.control_close_order()\n"
        "                except Exception as close_exc:\n"
        "                    self._current_retries += 1\n"
        "                    self.logger().error(\n"
        "                        f\"Close order placement failed during shutdown \"\n"
        "                        f\"({self._current_retries}/{self._max_retries}): {close_exc}. \"\n"
        "                        \"A persistent failure means the position may be stranded - \"\n"
        "                        \"close it manually on the exchange.\"\n"
        "                    )\n"
        "                    if self._current_retries > self._max_retries:\n"
        "                        self.close_type = CloseType.FAILED\n"
        "                        self.stop()\n"
        "                        return\n"
        "                else:\n"
        "                    self._current_retries += 1\n",
    )

    updated = updated.replace(
        "        if not self._open_order:\n"
        "            if self._is_within_activation_bounds(self.config.entry_price, self.config.side,\n"
        "                                                 self.config.triple_barrier_config.open_order_type):\n"
        "                self.place_open_order()\n",
        "        if not self._open_order:\n"
        "            if self._is_within_activation_bounds(self.config.entry_price, self.config.side,\n"
        "                                                 self.config.triple_barrier_config.open_order_type):\n"
        "                try:\n"
        "                    self.place_open_order()\n"
        "                except Exception as open_exc:\n"
        "                    self._current_retries += 1\n"
        "                    self.logger().error(\n"
        "                        f\"Open order placement failed \"\n"
        "                        f\"({self._current_retries}/{self._max_retries}): {open_exc}\"\n"
        "                    )\n",
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
        for candidate in root.rglob(POSITION_EXECUTOR_NAME):
            candidate_str = str(candidate)
            marker = "hummingbot/strategy_v2/executors/position_executor/"
            if marker not in candidate_str:
                continue
            package_roots.add(Path(candidate_str.split(marker, 1)[0]))
            if patch_position_executor_file(candidate):
                print(f"Patched {candidate}")
                patched_any = True

    for package_root in package_roots:
        override_any = install_overrides(package_root) or override_any

    if not patched_any and not override_any:
        raise SystemExit("No hyperliquid_perpetual_derivative.py files were patched or overridden.")


if __name__ == "__main__":
    main()

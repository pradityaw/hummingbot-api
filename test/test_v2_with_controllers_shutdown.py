"""
Regression tests for V2WithControllers stop/shutdown flatten path.

Ensures open positions are given a chance to close before market teardown, and that
close failures are surfaced at ERROR instead of being swallowed by silent retries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "bots" / "scripts"


def _install_hummingbot_stubs() -> None:
    """Install minimal hummingbot stubs so v2_with_controllers can be imported."""
    try:
        import hummingbot.strategy.strategy_v2_base  # noqa: F401
        return
    except ImportError:
        pass

    class RunnableStatus(Enum):
        NOT_STARTED = 1
        RUNNING = 2
        SHUTTING_DOWN = 3
        TERMINATED = 4

    class PositionAction(Enum):
        OPEN = "OPEN"
        CLOSE = "CLOSE"

    class StrategyV2ConfigBase:
        pass

    class StrategyV2Base:
        max_executors_close_attempts = 3

        def __init__(self, connectors=None, config=None):
            self.connectors = connectors or {}
            self.config = config
            self.controllers: Dict[str, Any] = {}
            self.controller_reports: Dict[str, Any] = {}
            self._is_stop_triggered = False
            self.executor_orchestrator = None
            self.market_data_provider = MagicMock()
            self.listen_to_executor_actions_task = None
            self.mqtt_enabled = False
            self._pub = None
            self.current_timestamp = 0

        @classmethod
        def logger(cls):
            return logging.getLogger("V2WithControllersTest")

        def filter_executors(self, executors, filter_func):
            return [executor for executor in executors if filter_func(executor)]

        def get_all_executors(self):
            executors: List[Any] = []
            orchestrator = self.executor_orchestrator
            if orchestrator is None:
                return executors
            active = getattr(orchestrator, "active_executors", {}) or {}
            for items in active.values():
                executors.extend(items)
            return executors

        def get_executors_by_controller(self, controller_id: str):
            orchestrator = self.executor_orchestrator
            if orchestrator is None:
                return []
            active = getattr(orchestrator, "active_executors", {}) or {}
            return list(active.get(controller_id, []))

        def buy(self, connector_name, trading_pair, amount, order_type, price=Decimal("NaN"), position_action=None):
            return self.buy_with_specific_market(
                (connector_name, trading_pair), amount, order_type, price, position_action=position_action
            )

        def sell(self, connector_name, trading_pair, amount, order_type, price=Decimal("NaN"), position_action=None):
            return self.sell_with_specific_market(
                (connector_name, trading_pair), amount, order_type, price, position_action=position_action
            )

        def buy_with_specific_market(self, market_pair, amount, order_type, price, position_action=None):
            raise NotImplementedError

        def sell_with_specific_market(self, market_pair, amount, order_type, price, position_action=None):
            raise NotImplementedError

        def is_perpetual(self, connector_name: str) -> bool:
            return "perpetual" in connector_name

        def get_performance_report(self, controller_id: str):
            return SimpleNamespace(global_pnl_quote=Decimal("0"))

    class StopExecutorAction:
        def __init__(self, executor_id: str, controller_id: Optional[str] = "main", keep_position: bool = False):
            self.executor_id = executor_id
            self.controller_id = controller_id
            self.keep_position = keep_position

    class CreateExecutorAction:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class MarketOrderFailureEvent:
        def __init__(self, order_id=None, error_message=None):
            self.order_id = order_id
            self.error_message = error_message

    class HummingbotApplication:
        @staticmethod
        def main_application():
            app = MagicMock()
            app.stop = MagicMock()
            return app

    class ConnectorBase:
        pass

    class PriceType(Enum):
        MidPrice = 1
        BestBid = 2
        BestAsk = 3

    modules = {
        "hummingbot": types.ModuleType("hummingbot"),
        "hummingbot.client": types.ModuleType("hummingbot.client"),
        "hummingbot.client.hummingbot_application": types.ModuleType("hummingbot.client.hummingbot_application"),
        "hummingbot.connector": types.ModuleType("hummingbot.connector"),
        "hummingbot.connector.connector_base": types.ModuleType("hummingbot.connector.connector_base"),
        "hummingbot.core": types.ModuleType("hummingbot.core"),
        "hummingbot.core.data_type": types.ModuleType("hummingbot.core.data_type"),
        "hummingbot.core.data_type.common": types.ModuleType("hummingbot.core.data_type.common"),
        "hummingbot.core.event": types.ModuleType("hummingbot.core.event"),
        "hummingbot.core.event.events": types.ModuleType("hummingbot.core.event.events"),
        "hummingbot.strategy": types.ModuleType("hummingbot.strategy"),
        "hummingbot.strategy.strategy_v2_base": types.ModuleType("hummingbot.strategy.strategy_v2_base"),
        "hummingbot.strategy_v2": types.ModuleType("hummingbot.strategy_v2"),
        "hummingbot.strategy_v2.models": types.ModuleType("hummingbot.strategy_v2.models"),
        "hummingbot.strategy_v2.models.base": types.ModuleType("hummingbot.strategy_v2.models.base"),
        "hummingbot.strategy_v2.models.executor_actions": types.ModuleType(
            "hummingbot.strategy_v2.models.executor_actions"
        ),
    }

    modules["hummingbot.client.hummingbot_application"].HummingbotApplication = HummingbotApplication
    modules["hummingbot.connector.connector_base"].ConnectorBase = ConnectorBase
    modules["hummingbot.core.data_type.common"].PriceType = PriceType
    modules["hummingbot.core.event.events"].MarketOrderFailureEvent = MarketOrderFailureEvent
    modules["hummingbot.core.event.events"].PositionAction = PositionAction
    modules["hummingbot.strategy.strategy_v2_base"].StrategyV2Base = StrategyV2Base
    modules["hummingbot.strategy.strategy_v2_base"].StrategyV2ConfigBase = StrategyV2ConfigBase
    modules["hummingbot.strategy_v2.models.base"].RunnableStatus = RunnableStatus
    modules["hummingbot.strategy_v2.models.executor_actions"].StopExecutorAction = StopExecutorAction
    modules["hummingbot.strategy_v2.models.executor_actions"].CreateExecutorAction = CreateExecutorAction

    for name, module in modules.items():
        sys.modules[name] = module

    # Expose stubs for tests.
    modules["hummingbot.strategy.strategy_v2_base"]._test_StrategyV2Base = StrategyV2Base
    modules["hummingbot.strategy_v2.models.base"]._test_RunnableStatus = RunnableStatus
    modules["hummingbot.core.event.events"]._test_PositionAction = PositionAction


_install_hummingbot_stubs()

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from v2_with_controllers import V2WithControllers, V2WithControllersConfig  # noqa: E402
from hummingbot.core.event.events import PositionAction  # noqa: E402
from hummingbot.strategy_v2.models.base import RunnableStatus  # noqa: E402
from hummingbot.strategy_v2.models.executor_actions import StopExecutorAction  # noqa: E402


class FakeExecutor:
    def __init__(
        self,
        executor_id: str = "exec-1",
        controller_id: str = "ctrl-1",
        status=RunnableStatus.RUNNING,
        is_trading: bool = True,
        open_filled_amount: Decimal = Decimal("0.00021"),
        connector_name: str = "hyperliquid_perpetual_testnet",
        trading_pair: str = "BTC-USD",
    ):
        self.id = executor_id
        self.controller_id = controller_id
        self.status = status
        self.is_trading = is_trading
        self.open_filled_amount = open_filled_amount
        self.is_closed = status == RunnableStatus.TERMINATED
        self.is_active = status in {RunnableStatus.RUNNING, RunnableStatus.NOT_STARTED}
        self.config = SimpleNamespace(connector_name=connector_name, trading_pair=trading_pair)
        self.stop_calls = 0
        self.early_stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        self.status = RunnableStatus.TERMINATED
        self.is_closed = True
        self.is_active = False
        self.is_trading = False

    def early_stop(self, keep_position: bool = False):
        self.early_stop_calls += 1
        self.status = RunnableStatus.SHUTTING_DOWN
        self.is_active = False


class FakeOrchestrator:
    def __init__(self, executors: Optional[Dict[str, List[FakeExecutor]]] = None):
        self.active_executors = executors or {}
        self.actions: List[Any] = []
        self.stop_calls: List[int] = []
        self.store_all_executors = MagicMock()
        self._teardown_started = False

    def execute_actions(self, actions):
        if self._teardown_started:
            raise AssertionError("StopExecutorAction issued after market teardown began")
        self.actions.extend(actions)
        for action in actions:
            if isinstance(action, StopExecutorAction):
                for executor in self.active_executors.get(action.controller_id, []):
                    if executor.id == action.executor_id:
                        executor.early_stop(keep_position=bool(action.keep_position))

    async def stop(self, max_attempts: int = 3):
        # Simulate base-image orchestrator stop happening while markets are still live.
        self.stop_calls.append(max_attempts)
        self._teardown_started = True


class FakeController:
    def __init__(self, controller_id: str = "ctrl-1", status=RunnableStatus.RUNNING):
        self.id = controller_id
        self.status = status
        self.config = SimpleNamespace(manual_kill_switch=False, model_dump=lambda: {})
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        self.status = RunnableStatus.TERMINATED

    def start(self):
        self.status = RunnableStatus.RUNNING

    def get_custom_info(self):
        return {}


def _build_strategy(executors: Optional[Dict[str, List[FakeExecutor]]] = None) -> V2WithControllers:
    strategy = object.__new__(V2WithControllers)
    strategy.connectors = {"hyperliquid_perpetual_testnet": MagicMock()}
    strategy.config = V2WithControllersConfig()
    strategy.controllers = {}
    strategy.controller_reports = {}
    strategy._is_stop_triggered = False
    strategy.max_pnl_by_controller = {}
    strategy.max_global_pnl = Decimal("0")
    strategy.drawdown_exited_controllers = []
    strategy.closed_executors_buffer = 30
    strategy._last_performance_report_timestamp = 0
    strategy.connectivity_guard = MagicMock()
    strategy.market_data_provider = MagicMock()
    strategy.listen_to_executor_actions_task = None
    strategy.mqtt_enabled = False
    strategy._pub = None
    strategy.max_executors_close_attempts = 3
    strategy.current_timestamp = 1000
    strategy.executor_orchestrator = FakeOrchestrator(executors or {})
    for controller_id in (executors or {}):
        strategy.controllers[controller_id] = FakeController(controller_id)
    return strategy


def test_on_stop_issues_flatten_before_teardown():
    executor = FakeExecutor(is_trading=True, open_filled_amount=Decimal("0.00021"))
    # After early_stop, leave a still-open position so stranded reporting is exercised.
    original_early_stop = executor.early_stop

    def early_stop_keep_open(keep_position: bool = False):
        original_early_stop(keep_position=keep_position)
        executor.is_trading = True
        executor.open_filled_amount = Decimal("0.00021")
        executor.is_closed = False

    executor.early_stop = early_stop_keep_open  # type: ignore[method-assign]

    strategy = _build_strategy({"ctrl-1": [executor]})
    teardown_order: List[str] = []

    original_execute = strategy.executor_orchestrator.execute_actions

    def tracked_execute(actions):
        teardown_order.append("flatten_actions")
        return original_execute(actions)

    strategy.executor_orchestrator.execute_actions = tracked_execute  # type: ignore[method-assign]

    async def tracked_stop(max_attempts=3):
        teardown_order.append("orchestrator_stop")
        await FakeOrchestrator.stop(strategy.executor_orchestrator, max_attempts)

    strategy.executor_orchestrator.stop = tracked_stop  # type: ignore[method-assign]

    original_mdp_stop = strategy.market_data_provider.stop

    def tracked_mdp_stop():
        teardown_order.append("market_teardown")
        return original_mdp_stop()

    strategy.market_data_provider.stop = tracked_mdp_stop

    asyncio.run(strategy.on_stop())

    assert teardown_order[:2] == ["flatten_actions", "orchestrator_stop"]
    assert "market_teardown" in teardown_order
    assert teardown_order.index("flatten_actions") < teardown_order.index("market_teardown")
    assert strategy._is_stop_triggered is True
    assert strategy.controllers["ctrl-1"].stop_calls == 1
    assert any(isinstance(action, StopExecutorAction) and action.keep_position is False
               for action in strategy.executor_orchestrator.actions)
    assert executor.early_stop_calls == 1
    assert strategy.executor_orchestrator.store_all_executors.called


def test_on_stop_reports_stranded_position_at_error(caplog):
    executor = FakeExecutor(
        executor_id="stranded-exec",
        is_trading=True,
        open_filled_amount=Decimal("0.00021"),
        status=RunnableStatus.SHUTTING_DOWN,
    )
    executor.is_active = False
    strategy = _build_strategy({"ctrl-1": [executor]})

    # Skip early flatten mutation; leave executor open through orchestrator.stop.
    strategy.executor_orchestrator.execute_actions = lambda actions: strategy.executor_orchestrator.actions.extend(actions)

    with caplog.at_level(logging.ERROR, logger="V2WithControllersTest"):
        asyncio.run(strategy.on_stop())

    error_messages = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("STRANDED POSITION" in message for message in error_messages)
    assert any("0.00021" in message for message in error_messages)
    assert any("stranded-exec" in message for message in error_messages)
    # No assertion that the executor was force-stopped: get_all_executors()
    # yields ExecutorInfo, which has no stop() method, so the strategy cannot
    # terminate a stranded executor from here. The loud ERROR above is the
    # actual protection, and the retry loop itself needs a base-image fix.


def test_buy_close_whitelist_failure_logs_error_loudly(caplog):
    executor = FakeExecutor(is_trading=True, open_filled_amount=Decimal("0.00021"))
    strategy = _build_strategy({"ctrl-1": [executor]})
    strategy._is_stop_triggered = True

    def fail_buy(*_args, **_kwargs):
        raise ValueError("Market object for buy order is not in the whitelisted markets set.")

    strategy.buy_with_specific_market = fail_buy  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="V2WithControllersTest"):
        with pytest.raises(ValueError, match="whitelisted markets set"):
            strategy.buy(
                "hyperliquid_perpetual_testnet",
                "BTC-USD",
                Decimal("0.00021"),
                order_type="MARKET",
                position_action=PositionAction.CLOSE,
            )

    error_messages = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("CLOSE/ORDER FAILED" in message for message in error_messages)
    assert any("whitelisted" in message for message in error_messages)
    assert any("manual" in message.lower() for message in error_messages)


def test_controller_drawdown_flattens_trading_executors():
    trading = FakeExecutor(
        executor_id="trading-exec",
        is_trading=True,
        open_filled_amount=Decimal("0.00021"),
        status=RunnableStatus.RUNNING,
    )
    quoting = FakeExecutor(
        executor_id="quoting-exec",
        is_trading=False,
        open_filled_amount=Decimal("0"),
        status=RunnableStatus.RUNNING,
    )
    strategy = _build_strategy({"ctrl-1": [trading, quoting]})
    strategy.config.max_controller_drawdown_quote = 5.0
    strategy.max_pnl_by_controller["ctrl-1"] = Decimal("10")
    strategy.get_performance_report = lambda _controller_id: SimpleNamespace(  # type: ignore[method-assign]
        global_pnl_quote=Decimal("0")
    )

    strategy.check_max_controller_drawdown()

    stopped_ids = {action.executor_id for action in strategy.executor_orchestrator.actions}
    assert stopped_ids == {"trading-exec", "quoting-exec"}
    assert all(action.keep_position is False for action in strategy.executor_orchestrator.actions)
    assert "ctrl-1" in strategy.drawdown_exited_controllers
    assert trading.early_stop_calls == 1
    assert quoting.early_stop_calls == 1


def test_on_stop_exists_and_mentions_flatten_before_teardown():
    source = (SCRIPTS_DIR / "v2_with_controllers.py").read_text(encoding="utf-8")
    assert "async def on_stop" in source
    assert "_issue_flatten_stop_actions" in source
    assert "STRANDED POSITION" in source
    assert "keep_position=False" in source


class FakeControllerWithMarket(FakeController):
    def __init__(self, controller_id: str = "ctrl-1"):
        super().__init__(controller_id)
        self.config = SimpleNamespace(
            manual_kill_switch=False,
            connector_name="hyperliquid_perpetual_testnet",
            trading_pair="BTC-USD",
            model_dump=lambda: {},
        )


def _build_mid_strategy(tmp_path) -> V2WithControllers:
    from mid_price_recorder import MidPriceRecorder

    strategy = _build_strategy({"ctrl-1": []})
    strategy.controllers["ctrl-1"] = FakeControllerWithMarket("ctrl-1")
    strategy._mid_recorder = MidPriceRecorder(state_dir=str(tmp_path), interval_seconds=1)
    strategy._last_mid_record_warning = 0.0
    snapshot = SimpleNamespace(quoting_enabled=False)
    strategy.connectivity_guard.evaluate = MagicMock(return_value=snapshot)
    strategy.connectivity_guard.apply_safety_actions = MagicMock()
    return strategy


def test_on_tick_records_mid_snapshot_even_when_gated(tmp_path):
    strategy = _build_mid_strategy(tmp_path)
    strategy.market_data_provider.get_price_by_type = MagicMock(return_value=Decimal("60123.5"))

    strategy.on_tick()

    path = tmp_path / "mids" / "mids_19700101.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["mid"] == "60123.5"
    assert payload["pair"] == "BTC-USD"
    assert payload["connector"] == "hyperliquid_perpetual_testnet"
    strategy.market_data_provider.get_price_by_type.assert_called_once()
    call_args = strategy.market_data_provider.get_price_by_type.call_args[0]
    assert call_args[0] == "hyperliquid_perpetual_testnet"
    assert call_args[1] == "BTC-USD"


def test_mid_record_failure_does_not_break_on_tick(tmp_path):
    strategy = _build_mid_strategy(tmp_path)
    strategy.market_data_provider.get_price_by_type = MagicMock(side_effect=RuntimeError("book not ready"))

    # Gated path: recorder must swallow the error and the tick must proceed to
    # safety actions without raising.
    strategy.on_tick()

    strategy.connectivity_guard.apply_safety_actions.assert_called_once()
    assert not (tmp_path / "mids").exists()

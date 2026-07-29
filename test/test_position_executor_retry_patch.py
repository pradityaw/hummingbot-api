"""
Behavioral tests for the base-image PositionExecutor retry patch.

The stock executor let synchronous placement exceptions escape control_task
before _current_retries incremented — the 2026-07-15 whitelist ValueError loop
(9.4 days silent downtime with a stranded position). These tests patch a pinned
copy of the upstream file (test/hummingbot_fixtures/position_executor_base.py)
with the production patcher, load it with stubs, and drive the failure loop.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCHER_PATH = REPO_ROOT / "docker" / "hyperliquid-patch" / "patch_hyperliquid_connector.py"
FIXTURE_PATH = REPO_ROOT / "test" / "hummingbot_fixtures" / "position_executor_base.py"


def _load_patcher():
    spec = importlib.util.spec_from_file_location("patch_hyperliquid_connector", PATCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeOrderType(Enum):
    LIMIT = 1
    LIMIT_MAKER = 2
    MARKET = 3

    def is_limit_type(self):
        return True


class FakeTradeType(Enum):
    BUY = 1
    SELL = 2


class FakePositionAction(Enum):
    NIL = 1
    OPEN = 2
    CLOSE = 3


class FakePositionMode(Enum):
    ONEWAY = 1
    HEDGE = 2


class FakePriceType(Enum):
    MidPrice = 1
    BestBid = 2
    BestAsk = 3


class FakeRunnableStatus(Enum):
    NOT_STARTED = 1
    RUNNING = 2
    SHUTTING_DOWN = 3
    TERMINATED = 4


class FakeCloseType(Enum):
    POSITION_HOLD = 1
    EXPIRED = 2
    STOP_LOSS = 3
    TAKE_PROFIT = 4
    TIME_LIMIT = 5
    TRAILING_STOP = 6
    EARLY_STOP = 7
    FAILED = 8
    INSUFFICIENT_BALANCE = 9


def _install_stubs():
    if "executor_under_test" in sys.modules:
        return

    def install(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    class StubExecutorBase:
        def __init__(self, *args, **kwargs):
            pass

        def place_order(self, connector_name, trading_pair, order_type, side, amount,
                        position_action=None, price=Decimal("NaN")):
            if side == FakeTradeType.BUY:
                return self._strategy.buy(connector_name, trading_pair, amount, order_type, price, position_action)
            return self._strategy.sell(connector_name, trading_pair, amount, order_type, price, position_action)

        def evaluate_max_retries(self):
            if self._current_retries > self._max_retries:
                self.close_type = FakeCloseType.FAILED
                self.stop()

        def stop(self):
            self._status = FakeRunnableStatus.TERMINATED

        def is_perpetual_connector(self, connector_name):
            return "perpetual" in connector_name.lower()

        def get_in_flight_order(self, connector_name, order_id):
            return None

        def get_price(self, *args, **kwargs):
            return Decimal("60000")

    install("hummingbot")
    install("hummingbot.connector")
    install("hummingbot.connector.connector_base", ConnectorBase=object)
    install("hummingbot.core")
    install("hummingbot.core.data_type")
    install(
        "hummingbot.core.data_type.common",
        OrderType=FakeOrderType,
        PositionAction=FakePositionAction,
        PositionMode=FakePositionMode,
        PriceType=FakePriceType,
        TradeType=FakeTradeType,
    )
    install("hummingbot.core.data_type.order_candidate", OrderCandidate=object, PerpetualOrderCandidate=object)
    install("hummingbot.core.event")
    install(
        "hummingbot.core.event.events",
        BuyOrderCompletedEvent=object,
        BuyOrderCreatedEvent=object,
        MarketOrderFailureEvent=object,
        OrderCancelledEvent=object,
        OrderFilledEvent=object,
        SellOrderCompletedEvent=object,
        SellOrderCreatedEvent=object,
    )
    install("hummingbot.logger", HummingbotLogger=logging.Logger)
    install("hummingbot.strategy")
    install("hummingbot.strategy.strategy_v2_base", StrategyV2Base=object)
    install("hummingbot.strategy_v2")
    install("hummingbot.strategy_v2.executors")
    install("hummingbot.strategy_v2.executors.executor_base", ExecutorBase=StubExecutorBase)
    install("hummingbot.strategy_v2.executors.position_executor")
    install("hummingbot.strategy_v2.executors.position_executor.data_types", PositionExecutorConfig=object)
    install("hummingbot.strategy_v2.models")
    install("hummingbot.strategy_v2.models.base", RunnableStatus=FakeRunnableStatus)
    install(
        "hummingbot.strategy_v2.models.executors",
        CloseType=FakeCloseType,
        TrackedOrder=SimpleNamespace,
    )


def _load_patched_executor_module(tmp_path) -> types.ModuleType:
    _install_stubs()
    patcher = _load_patcher()
    target = tmp_path / "position_executor.py"
    target.write_text(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert patcher.patch_position_executor_file(target) is True
    spec = importlib.util.spec_from_file_location("executor_under_test", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules["executor_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _make_executor(module, *, place_side_effect):
    executor = object.__new__(module.PositionExecutor)
    executor._max_retries = 3
    executor._current_retries = 0
    executor.close_type = FakeCloseType.STOP_LOSS
    executor.close_timestamp = None
    executor._status = FakeRunnableStatus.SHUTTING_DOWN
    executor._open_order = SimpleNamespace(
        order_id="open-1",
        executed_amount_base=Decimal("0.0002"),
        cum_fees_base=Decimal("0"),
        fee_asset="USDC",
        is_done=True,
        is_filled=True,
        order=None,
    )
    executor._close_order = None
    executor._take_profit_limit_order = None
    executor._failed_orders = []
    executor._held_position_orders = []
    executor.trading_rules = SimpleNamespace(min_order_size=Decimal("0.0001"), min_notional_size=Decimal("1"))
    executor.config = SimpleNamespace(
        id="exec-1",
        connector_name="hyperliquid_perpetual_testnet",
        trading_pair="BTC-USD",
        side=FakeTradeType.BUY,
        entry_price=Decimal("60000"),
        amount=Decimal("0.0002"),
        leverage=1,
        timestamp=100.0,
        activation_bounds=None,
        triple_barrier_config=SimpleNamespace(
            open_order_type=FakeOrderType.LIMIT_MAKER,
            take_profit=Decimal("0.008"),
            take_profit_order_type=FakeOrderType.LIMIT_MAKER,
            stop_loss=Decimal("0.01"),
            time_limit=900,
        ),
    )
    connector = SimpleNamespace(
        quantize_order_amount=lambda trading_pair, amount: amount,
        position_mode=FakePositionMode.ONEWAY,
    )
    executor.connectors = {"hyperliquid_perpetual_testnet": connector}

    def raising_sell(*args, **kwargs):
        raise place_side_effect

    strategy = SimpleNamespace(
        current_timestamp=123.0,
        buy=raising_sell,
        sell=raising_sell,
        cancel=lambda **kwargs: None,
    )
    executor._strategy = strategy
    return executor


def test_close_placement_failure_counts_retries_and_stops_at_max(tmp_path, caplog):
    module = _load_patched_executor_module(tmp_path)
    executor = _make_executor(
        module,
        place_side_effect=ValueError("Market object for sell order is not in the whitelisted markets set."),
    )

    with caplog.at_level(logging.ERROR):
        for _ in range(4):
            asyncio.run(executor.control_shutdown_process())

    # 3 counted retries, then the 4th crosses max_retries: FAILED + terminated.
    assert executor._current_retries == 4
    assert executor.close_type == FakeCloseType.FAILED
    assert executor._status == FakeRunnableStatus.TERMINATED
    messages = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("Close order placement failed during shutdown" in message for message in messages)
    assert any("stranded" in message for message in messages)


def test_open_placement_failure_counts_retries_and_stops_at_max(tmp_path, caplog):
    module = _load_patched_executor_module(tmp_path)
    executor = _make_executor(module, place_side_effect=OSError("Quoting disabled by runtime connectivity guard"))
    executor._open_order = None  # no open order yet: control_open_order path

    with caplog.at_level(logging.ERROR):
        for _ in range(4):
            executor.control_open_order()
            executor.evaluate_max_retries()

    assert executor._current_retries == 4
    assert executor.close_type == FakeCloseType.FAILED
    assert executor._status == FakeRunnableStatus.TERMINATED
    messages = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("Open order placement failed" in message for message in messages)


def test_patcher_is_idempotent_on_fixture(tmp_path):
    patcher = _load_patcher()
    target = tmp_path / "position_executor.py"
    target.write_text(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert patcher.patch_position_executor_file(target) is True
    assert patcher.patch_position_executor_file(target) is False


def test_unpatched_fixture_has_the_forever_retry_shape():
    # Guard the premise: if upstream ever merges an equivalent fix, revisit the patch.
    source = FIXTURE_PATH.read_text(encoding="utf-8")
    assert "await self.control_close_order()\n                self._current_retries += 1" in source

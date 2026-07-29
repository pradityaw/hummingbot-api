"""
Tests for the pmm_skewed controller (maker-only entries, inventory skew,
traded-close cooldown).

The hummingbot package only exists inside the bot container, so these tests install
faithful stubs of MarketMakingController{Base,ConfigBase} (same formulas as upstream
master) and exercise the real pmm_skewed module against them.
"""
from __future__ import annotations

import asyncio
import sys
import types
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS_DIR = REPO_ROOT / "bots" / "controllers" / "market_making"


def _install_hummingbot_stubs() -> None:
    try:
        import hummingbot.strategy_v2.controllers.market_making_controller_base  # noqa: F401
        return
    except ImportError:
        pass

    from pydantic import BaseModel, ConfigDict, Field, field_validator

    class OrderType(Enum):
        LIMIT = 1
        LIMIT_MAKER = 2
        MARKET = 3

        def is_limit_type(self) -> bool:
            return self in {OrderType.LIMIT, OrderType.LIMIT_MAKER}

    class PriceType(Enum):
        MidPrice = 1
        BestBid = 2
        BestAsk = 3

    class TradeType(Enum):
        BUY = 1
        SELL = 2

    class PositionMode(Enum):
        ONEWAY = 1
        HEDGE = 2

    class CloseType(Enum):
        POSITION_HOLD = 1
        EXPIRED = 2
        STOP_LOSS = 3
        TAKE_PROFIT = 4
        TIME_LIMIT = 5
        TRAILING_STOP = 6
        EARLY_STOP = 7
        FAILED = 8
        INSUFFICIENT_BALANCE = 9

    def parse_enum_value(enum_cls, value, field_name):
        if isinstance(value, enum_cls):
            return value
        try:
            return enum_cls[value]
        except KeyError:
            raise ValueError(f"Invalid value for {field_name}: {value}")

    def parse_comma_separated_list(value):
        if isinstance(value, str):
            return [float(item) for item in value.split(",") if item.strip()]
        return value

    class TripleBarrierConfig(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        stop_loss: Optional[Decimal] = None
        take_profit: Optional[Decimal] = None
        time_limit: Optional[int] = None
        trailing_stop: Optional[Any] = None
        open_order_type: OrderType = OrderType.LIMIT
        take_profit_order_type: OrderType = OrderType.LIMIT
        stop_loss_order_type: OrderType = OrderType.MARKET
        time_limit_order_type: OrderType = OrderType.MARKET

    class PositionExecutorConfig(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        timestamp: float
        level_id: Optional[str] = None
        connector_name: str
        trading_pair: str
        entry_price: Optional[Decimal] = None
        amount: Decimal
        triple_barrier_config: TripleBarrierConfig
        leverage: int = 1
        side: TradeType

    class MarketMakingControllerConfigBase(BaseModel):
        """Faithful stub of upstream config base (same defaults/formulas)."""
        model_config = ConfigDict(arbitrary_types_allowed=True)
        id: str
        controller_name: str = "pmm_simple"
        controller_type: str = "market_making"
        total_amount_quote: Decimal = Decimal("100")
        manual_kill_switch: bool = False
        connector_name: str = "binance_perpetual"
        trading_pair: str = "WLD-USDT"
        buy_spreads: List[float] = Field(default="0.01,0.02")
        sell_spreads: List[float] = Field(default="0.01,0.02")
        buy_amounts_pct: Optional[List[Decimal]] = None
        sell_amounts_pct: Optional[List[Decimal]] = None
        executor_refresh_time: int = 300
        cooldown_time: int = 15
        leverage: int = 20
        position_mode: PositionMode = PositionMode.HEDGE
        stop_loss: Optional[Decimal] = Decimal("0.03")
        take_profit: Optional[Decimal] = Decimal("0.02")
        time_limit: Optional[int] = 2700
        take_profit_order_type: OrderType = OrderType.LIMIT
        trailing_stop: Optional[Any] = None

        @field_validator("buy_spreads", "sell_spreads", mode="before")
        @classmethod
        def parse_spreads(cls, v):
            return parse_comma_separated_list(v)

        @field_validator("buy_amounts_pct", "sell_amounts_pct", mode="before")
        @classmethod
        def parse_amounts(cls, v):
            if v is None or v == "":
                return None
            parsed = parse_comma_separated_list(v)
            return [Decimal(str(item)) for item in parsed] if parsed else None

        @field_validator("stop_loss", "take_profit", mode="before")
        @classmethod
        def parse_barriers(cls, v):
            if v is None or v == "":
                return None
            return Decimal(str(v))

        @field_validator("take_profit_order_type", mode="before")
        @classmethod
        def parse_tp_order_type(cls, v):
            if v is None:
                return OrderType.MARKET
            if isinstance(v, str):
                v = v.replace("OrderType.", "")
            return parse_enum_value(OrderType, v, "take_profit_order_type")

        @field_validator("position_mode", mode="before")
        @classmethod
        def parse_position_mode(cls, v):
            return parse_enum_value(PositionMode, v, "position_mode")

        @property
        def triple_barrier_config(self) -> TripleBarrierConfig:
            return TripleBarrierConfig(
                stop_loss=self.stop_loss,
                take_profit=self.take_profit,
                time_limit=self.time_limit,
                trailing_stop=self.trailing_stop,
                open_order_type=OrderType.LIMIT,
                take_profit_order_type=self.take_profit_order_type,
                stop_loss_order_type=OrderType.MARKET,
                time_limit_order_type=OrderType.MARKET,
            )

        def get_spreads_and_amounts_in_quote(self, trade_type):
            buy_amounts_pct = self.buy_amounts_pct or [1 for _ in self.buy_spreads]
            sell_amounts_pct = self.sell_amounts_pct or [1 for _ in self.sell_spreads]
            total_pct = sum(buy_amounts_pct) + sum(sell_amounts_pct)
            if trade_type == TradeType.BUY:
                normalized = [Decimal(str(p)) / Decimal(str(total_pct)) for p in buy_amounts_pct]
                return self.buy_spreads, [p * self.total_amount_quote for p in normalized]
            normalized = [Decimal(str(p)) / Decimal(str(total_pct)) for p in sell_amounts_pct]
            return self.sell_spreads, [p * self.total_amount_quote for p in normalized]

    class MarketMakingControllerBase:
        """Faithful stub of upstream controller base (same formulas)."""

        def __init__(self, config, market_data_provider, actions_queue, update_interval: float = 1.0):
            self.config = config
            self.market_data_provider = market_data_provider
            self.actions_queue = actions_queue
            self.executors_info: List[Any] = []
            self.positions_held: List[Any] = []
            self.processed_data: Dict[str, Any] = {}

        def filter_executors(self, executors=None, executor_filter=None, filter_func=None):
            filtered = list(executors if executors is not None else self.executors_info)
            if filter_func:
                filtered = [e for e in filtered if filter_func(e)]
            return filtered

        def get_levels_to_execute(self) -> List[str]:
            working_levels = self.filter_executors(
                executors=self.executors_info,
                filter_func=lambda x: x.is_active or (
                    x.close_type == CloseType.STOP_LOSS
                    and self.market_data_provider.time() - x.close_timestamp < self.config.cooldown_time
                ),
            )
            working_levels_ids = [executor.custom_info["level_id"] for executor in working_levels]
            return self.get_not_active_levels_ids(working_levels_ids)

        async def update_processed_data(self):
            reference_price = self.market_data_provider.get_price_by_type(
                self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
            self.processed_data = {"reference_price": Decimal(reference_price), "spread_multiplier": Decimal("1")}

        def get_price_and_amount(self, level_id: str):
            level = self.get_level_from_level_id(level_id)
            trade_type = self.get_trade_type_from_level_id(level_id)
            spreads, amounts_quote = self.config.get_spreads_and_amounts_in_quote(trade_type)
            reference_price = Decimal(self.processed_data["reference_price"])
            spread_in_pct = Decimal(spreads[int(level)]) * Decimal(self.processed_data["spread_multiplier"])
            side_multiplier = Decimal("-1") if trade_type == TradeType.BUY else Decimal("1")
            order_price = reference_price * (1 + side_multiplier * spread_in_pct)
            return order_price, Decimal(amounts_quote[int(level)]) / order_price

        def get_level_id_from_side(self, trade_type, level: int) -> str:
            return f"{trade_type.name.lower()}_{level}"

        def get_trade_type_from_level_id(self, level_id: str):
            return TradeType.BUY if level_id.startswith("buy") else TradeType.SELL

        def get_level_from_level_id(self, level_id: str) -> int:
            return int(level_id.split("_")[1])

        def get_not_active_levels_ids(self, active_levels_ids: List[str]) -> List[str]:
            buy_ids_missing = [self.get_level_id_from_side(TradeType.BUY, level) for level in range(len(self.config.buy_spreads))
                               if self.get_level_id_from_side(TradeType.BUY, level) not in active_levels_ids]
            sell_ids_missing = [self.get_level_id_from_side(TradeType.SELL, level) for level in range(len(self.config.sell_spreads))
                                if self.get_level_id_from_side(TradeType.SELL, level) not in active_levels_ids]
            return buy_ids_missing + sell_ids_missing

        def get_current_base_position(self) -> Decimal:
            total_base_amount = Decimal("0")
            for position in self.positions_held:
                if position.connector_name == self.config.connector_name and position.trading_pair == self.config.trading_pair:
                    if position.side == TradeType.BUY:
                        total_base_amount += position.amount
                    else:
                        total_base_amount -= position.amount
            return total_base_amount

    modules = {
        "hummingbot": types.ModuleType("hummingbot"),
        "hummingbot.core": types.ModuleType("hummingbot.core"),
        "hummingbot.core.data_type": types.ModuleType("hummingbot.core.data_type"),
        "hummingbot.core.data_type.common": types.ModuleType("hummingbot.core.data_type.common"),
        "hummingbot.strategy_v2": types.ModuleType("hummingbot.strategy_v2"),
        "hummingbot.strategy_v2.controllers": types.ModuleType("hummingbot.strategy_v2.controllers"),
        "hummingbot.strategy_v2.controllers.market_making_controller_base": types.ModuleType(
            "hummingbot.strategy_v2.controllers.market_making_controller_base"),
        "hummingbot.strategy_v2.executors": types.ModuleType("hummingbot.strategy_v2.executors"),
        "hummingbot.strategy_v2.executors.position_executor": types.ModuleType(
            "hummingbot.strategy_v2.executors.position_executor"),
        "hummingbot.strategy_v2.executors.position_executor.data_types": types.ModuleType(
            "hummingbot.strategy_v2.executors.position_executor.data_types"),
        "hummingbot.strategy_v2.models": types.ModuleType("hummingbot.strategy_v2.models"),
        "hummingbot.strategy_v2.models.executors": types.ModuleType("hummingbot.strategy_v2.models.executors"),
        "hummingbot.strategy_v2.utils": types.ModuleType("hummingbot.strategy_v2.utils"),
        "hummingbot.strategy_v2.utils.common": types.ModuleType("hummingbot.strategy_v2.utils.common"),
    }
    modules["hummingbot.core.data_type.common"].OrderType = OrderType
    modules["hummingbot.core.data_type.common"].PriceType = PriceType
    modules["hummingbot.core.data_type.common"].TradeType = TradeType
    modules["hummingbot.core.data_type.common"].PositionMode = PositionMode
    modules["hummingbot.strategy_v2.controllers.market_making_controller_base"].MarketMakingControllerBase = MarketMakingControllerBase
    modules["hummingbot.strategy_v2.controllers.market_making_controller_base"].MarketMakingControllerConfigBase = MarketMakingControllerConfigBase
    modules["hummingbot.strategy_v2.executors.position_executor.data_types"].PositionExecutorConfig = PositionExecutorConfig
    modules["hummingbot.strategy_v2.executors.position_executor.data_types"].TripleBarrierConfig = TripleBarrierConfig
    modules["hummingbot.strategy_v2.models.executors"].CloseType = CloseType
    modules["hummingbot.strategy_v2.utils.common"].parse_enum_value = parse_enum_value
    modules["hummingbot.strategy_v2.utils.common"].parse_comma_separated_list = parse_comma_separated_list
    for name, module in modules.items():
        sys.modules[name] = module

    modules["hummingbot.core.data_type.common"]._test_OrderType = OrderType
    modules["hummingbot.strategy_v2.models.executors"]._test_CloseType = CloseType
    modules["hummingbot.core.data_type.common"]._test_TradeType = TradeType


_install_hummingbot_stubs()

if str(CONTROLLERS_DIR) not in sys.path:
    sys.path.insert(0, str(CONTROLLERS_DIR))

from hummingbot.core.data_type.common import OrderType, TradeType  # noqa: E402
from hummingbot.strategy_v2.models.executors import CloseType  # noqa: E402
from pmm_skewed import PMMSkewedConfig, PMMSkewedController  # noqa: E402


class FakeMarketDataProvider:
    def __init__(self, mid: Decimal = Decimal("60000"), now: float = 10_000.0):
        self._mid = mid
        self._now = now

    def time(self) -> float:
        return self._now

    def get_price_by_type(self, connector_name, trading_pair, price_type):
        return self._mid


def _make_config(**overrides) -> PMMSkewedConfig:
    params = dict(
        id="skewed-test",
        connector_name="hyperliquid_perpetual_testnet",
        trading_pair="BTC-USD",
        total_amount_quote=Decimal("25"),
        buy_spreads="0.003",
        sell_spreads="0.003",
        buy_amounts_pct="1",
        sell_amounts_pct="1",
        executor_refresh_time=90,
        cooldown_time=30,
        leverage=1,
        position_mode="ONEWAY",
        stop_loss="0.01",
        take_profit="0.008",
        time_limit=900,
        take_profit_order_type="LIMIT_MAKER",
    )
    params.update(overrides)
    config = PMMSkewedConfig.model_construct()  # placeholder, replaced below
    del config
    return PMMSkewedConfig(**params)


def _make_controller(config: Optional[PMMSkewedConfig] = None, mid: Decimal = Decimal("60000")) -> PMMSkewedController:
    config = config or _make_config()
    return PMMSkewedController(config, FakeMarketDataProvider(mid=mid), None)


def _executor_info(
    level_id: str,
    is_active: bool = False,
    close_type: Optional[CloseType] = None,
    close_timestamp: Optional[float] = None,
    filled_amount_quote: Decimal = Decimal("0"),
):
    return SimpleNamespace(
        is_active=is_active,
        close_type=close_type,
        close_timestamp=close_timestamp,
        filled_amount_quote=filled_amount_quote,
        custom_info={"level_id": level_id},
    )


def _position(side: TradeType, amount: Decimal):
    return SimpleNamespace(
        connector_name="hyperliquid_perpetual_testnet",
        trading_pair="BTC-USD",
        side=side,
        amount=amount,
    )


# --- config / order types -------------------------------------------------

def test_entries_default_to_limit_maker_and_barriers_match_config():
    config = _make_config()
    barriers = config.triple_barrier_config
    assert barriers.open_order_type == OrderType.LIMIT_MAKER
    assert barriers.take_profit_order_type == OrderType.LIMIT_MAKER
    assert barriers.stop_loss_order_type == OrderType.MARKET
    assert barriers.time_limit_order_type == OrderType.MARKET
    assert barriers.stop_loss == Decimal("0.01")
    assert barriers.take_profit == Decimal("0.008")
    assert barriers.time_limit == 900


def test_open_order_type_accepts_strings_and_rejects_market():
    config = _make_config(open_order_type="LIMIT")
    assert config.triple_barrier_config.open_order_type == OrderType.LIMIT
    with pytest.raises(ValueError):
        _make_config(open_order_type="MARKET")


def test_executor_config_carries_maker_open_type():
    controller = _make_controller()
    asyncio.run(controller.update_processed_data())
    executor_config = controller.get_executor_config("buy_0", Decimal("59820"), Decimal("0.0002"))
    assert executor_config.triple_barrier_config.open_order_type == OrderType.LIMIT_MAKER
    assert executor_config.level_id == "buy_0"
    assert executor_config.side == TradeType.BUY


def test_skew_strength_must_be_non_negative():
    with pytest.raises(ValueError):
        _make_config(inventory_skew_strength="-0.1")


# --- inventory skew --------------------------------------------------------

def test_no_inventory_no_shift():
    controller = _make_controller()
    asyncio.run(controller.update_processed_data())
    assert Decimal(controller.processed_data["reference_price"]) == Decimal("60000")


def test_long_inventory_shifts_reference_down():
    controller = _make_controller()
    # 0.0008334 BTC * 60000 = $50.004 -> over the $25 budget, clamps to ratio 1.
    controller.positions_held = [_position(TradeType.BUY, Decimal("0.0008334"))]
    asyncio.run(controller.update_processed_data())
    mid = Decimal("60000")
    skewed = Decimal(controller.processed_data["reference_price"])
    expected_shift = Decimal("0.5") * Decimal("0.003") * Decimal("1")
    assert skewed == mid * (1 - expected_shift)
    buy_price, _ = controller.get_price_and_amount("buy_0")
    sell_price, _ = controller.get_price_and_amount("sell_0")
    # Sell pulls toward mid (easier to offload), buy backs further below mid.
    assert sell_price < mid * Decimal("1.003")
    assert buy_price < mid * Decimal("0.997")
    # Sell stays above mid: still a maker quote, not a crossing dump.
    assert sell_price > mid


def test_short_inventory_shifts_reference_up():
    controller = _make_controller()
    controller.positions_held = [_position(TradeType.SELL, Decimal("0.00020835"))]  # ~$12.5 short
    asyncio.run(controller.update_processed_data())
    mid = Decimal("60000")
    skewed = Decimal(controller.processed_data["reference_price"])
    exact_ratio = Decimal("0.00020835") * mid / Decimal("25")
    expected_shift = Decimal("0.5") * Decimal("0.003") * exact_ratio
    assert abs(skewed - mid * (1 + expected_shift)) < Decimal("0.000001")
    buy_price, _ = controller.get_price_and_amount("buy_0")
    # Buy pulls up toward mid to cover the short faster.
    assert buy_price > mid * Decimal("0.997")
    assert buy_price < mid


def test_zero_strength_disables_skew():
    config = _make_config(inventory_skew_strength="0")
    controller = _make_controller(config=config)
    controller.positions_held = [_position(TradeType.BUY, Decimal("0.001"))]
    asyncio.run(controller.update_processed_data())
    assert Decimal(controller.processed_data["reference_price"]) == Decimal("60000")


def test_custom_budget_changes_ratio():
    config = _make_config(inventory_skew_budget_quote="12.5")
    controller = _make_controller(config=config)
    controller.positions_held = [_position(TradeType.BUY, Decimal("0.00020835"))]  # $12.501 -> ratio ~1 vs custom budget
    asyncio.run(controller.update_processed_data())
    skew = controller.processed_data["inventory_skew"]
    assert Decimal(skew["inventory_ratio"]) == Decimal("1")


def test_get_custom_info_exposes_skew_telemetry():
    controller = _make_controller()
    asyncio.run(controller.update_processed_data())
    info = controller.get_custom_info()
    assert info["open_order_type"].endswith("LIMIT_MAKER")
    assert "inventory_skew" in info and "reference_shift_pct" in info["inventory_skew"]


# --- traded-close cooldown -------------------------------------------------

def test_tp_close_cools_down_level_but_unfilled_refresh_does_not():
    controller = _make_controller()
    now = 10_000.0
    controller.executors_info = [
        _executor_info("buy_0", close_type=CloseType.TAKE_PROFIT, close_timestamp=now - 10, filled_amount_quote=Decimal("12.5")),
        _executor_info("sell_0", close_type=CloseType.EARLY_STOP, close_timestamp=now - 10, filled_amount_quote=Decimal("0")),
    ]
    levels = controller.get_levels_to_execute()
    assert "buy_0" not in levels  # TP close 10s ago: inside 30s cooldown
    assert "sell_0" in levels     # unfilled refresh: re-quotes immediately


def test_cooldown_expires_after_cooldown_time():
    controller = _make_controller()
    now = 10_000.0
    controller.executors_info = [
        _executor_info("buy_0", close_type=CloseType.TIME_LIMIT, close_timestamp=now - 31, filled_amount_quote=Decimal("12.5")),
    ]
    assert "buy_0" in controller.get_levels_to_execute()


def test_stop_loss_cooldown_preserved_and_failed_close_cools_down():
    controller = _make_controller()
    now = 10_000.0
    controller.executors_info = [
        _executor_info("buy_0", close_type=CloseType.STOP_LOSS, close_timestamp=now - 5, filled_amount_quote=Decimal("12.5")),
        _executor_info("sell_0", close_type=CloseType.FAILED, close_timestamp=now - 5, filled_amount_quote=Decimal("0")),
    ]
    levels = controller.get_levels_to_execute()
    assert "buy_0" not in levels
    assert "sell_0" not in levels  # post-only reject churn is throttled


def test_active_executor_blocks_level():
    controller = _make_controller()
    controller.executors_info = [_executor_info("buy_0", is_active=True)]
    levels = controller.get_levels_to_execute()
    assert "buy_0" not in levels
    assert "sell_0" in levels

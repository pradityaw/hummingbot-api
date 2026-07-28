"""
pmm_skewed — pmm_simple with post-only entries, inventory skew, and traded-close cooldown.

Why this controller exists (edge iteration, 2026-07-28):

1. Maker-only entries. Stock `pmm_simple` hardcodes `open_order_type=OrderType.LIMIT`
   in `MarketMakingControllerConfigBase.triple_barrier_config`, so entry quotes are
   Gtc and can cross the spread and pay taker. Gate A data (403 fills) showed maker
   share at 58.8% with taker fills paying 66% of the fee bill; Gate C requires >90%
   maker. This config exposes `open_order_type` (default LIMIT_MAKER) as a real field.
   With LIMIT_MAKER, the base PositionExecutor clamps the entry price to the non-
   crossing side of the book (best bid for buys / best ask for sells), so post-only
   rejects are rare races; when they do happen the executor re-prices on retry and
   this controller's traded-close cooldown throttles respawn after FAILED closes.

2. Inventory skew. `pmm_simple` quotes symmetric spreads regardless of position, so
   inventory accumulates directionally (Gate A: max long $37 quote on a $25 budget).
   This controller shifts the reference price against net inventory (Avellaneda-style
   reservation price): when long, both quotes move down so the sell fills sooner and
   the buy backs off; when short, both move up. Quoted width is preserved.

3. Cooldown after any traded close. Upstream `get_levels_to_execute` only applies
   `cooldown_time` after STOP_LOSS closes; TP/time-limit closes re-arm instantly and
   can re-quote into the same adverse move. Here every close that traded (plus FAILED
   and INSUFFICIENT_BALANCE) cools down before re-quoting. Unfilled refreshes
   (EARLY_STOP with zero fill) still re-quote immediately, preserving cadence.

Keep this file self-contained: it must load inside the bot container where only the
hummingbot package and the controllers directory are importable. Do not import from
sibling controllers.
"""
from decimal import Decimal
from typing import List, Optional

from pydantic import Field, field_validator

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import (
    PositionExecutorConfig,
    TripleBarrierConfig,
)
from hummingbot.strategy_v2.models.executors import CloseType
from hummingbot.strategy_v2.utils.common import parse_enum_value

# Close types that always earn a re-quote cooldown. Resolved by name so the file
# keeps working if the container image's CloseType predates a member.
_COOLDOWN_CLOSE_TYPE_NAMES = (
    "STOP_LOSS",
    "TAKE_PROFIT",
    "TIME_LIMIT",
    "TRAILING_STOP",
    "FAILED",
    "INSUFFICIENT_BALANCE",
)
_COOLDOWN_CLOSE_TYPES = frozenset(
    getattr(CloseType, name) for name in _COOLDOWN_CLOSE_TYPE_NAMES if hasattr(CloseType, name)
)


class PMMSkewedConfig(MarketMakingControllerConfigBase):
    controller_name: str = "pmm_skewed"
    open_order_type: OrderType = Field(
        default=OrderType.LIMIT_MAKER,
        json_schema_extra={
            "prompt": "Enter the order type for open quotes (LIMIT_MAKER/LIMIT): ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    inventory_skew_strength: Decimal = Field(
        default=Decimal("0.5"),
        json_schema_extra={
            "prompt": "Inventory skew strength as a fraction of the average spread applied at full inventory budget (e.g., 0.5). 0 disables: ",
            "prompt_on_new": True,
            "is_updatable": True,
        },
    )
    inventory_skew_budget_quote: Optional[Decimal] = Field(
        default=None,
        json_schema_extra={
            "prompt": "Quote inventory that counts as 'full' for skew purposes (blank = total_amount_quote): ",
            "prompt_on_new": False,
            "is_updatable": True,
        },
    )

    @field_validator("open_order_type", mode="before")
    @classmethod
    def validate_open_order_type(cls, v) -> OrderType:
        if v is None:
            return OrderType.LIMIT_MAKER
        if isinstance(v, str):
            v = v.replace("OrderType.", "")
        order_type = parse_enum_value(OrderType, v, "open_order_type")
        if not order_type.is_limit_type():
            raise ValueError("open_order_type must be a limit type (LIMIT or LIMIT_MAKER); MARKET entries are not market making.")
        return order_type

    @field_validator("inventory_skew_strength", mode="before")
    @classmethod
    def validate_skew_strength(cls, v) -> Decimal:
        if v is None or v == "":
            return Decimal("0")
        value = Decimal(str(v))
        if value < 0:
            raise ValueError("inventory_skew_strength must be >= 0")
        return value

    @field_validator("inventory_skew_budget_quote", mode="before")
    @classmethod
    def validate_skew_budget(cls, v) -> Optional[Decimal]:
        if v is None or v == "":
            return None
        value = Decimal(str(v))
        if value <= 0:
            raise ValueError("inventory_skew_budget_quote must be > 0")
        return value

    @property
    def triple_barrier_config(self) -> TripleBarrierConfig:
        return TripleBarrierConfig(
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            time_limit=self.time_limit,
            trailing_stop=self.trailing_stop,
            open_order_type=self.open_order_type,
            take_profit_order_type=self.take_profit_order_type,
            stop_loss_order_type=OrderType.MARKET,
            time_limit_order_type=OrderType.MARKET,
        )


class PMMSkewedController(MarketMakingControllerBase):
    def __init__(self, config: PMMSkewedConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price,
            amount=amount,
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
            side=trade_type,
        )

    async def update_processed_data(self):
        """
        Mid reference from the base class, then shifted against net inventory.
        Shift magnitude = strength * avg_spread * clamp(inventory_quote / budget, +/-1).
        Long inventory lowers the reference (sell pulls toward mid, buy backs off);
        short inventory raises it. The quoted width is untouched.
        """
        await super().update_processed_data()
        mid_price = Decimal(self.processed_data["reference_price"])
        base_position = self.get_current_base_position()
        inventory_quote = base_position * mid_price
        budget = self.config.inventory_skew_budget_quote or self.config.total_amount_quote
        budget = Decimal(str(budget))
        strength = Decimal(str(self.config.inventory_skew_strength))
        inventory_ratio = Decimal("0")
        shift = Decimal("0")
        if budget > 0 and strength > 0 and mid_price > 0:
            inventory_ratio = max(min(inventory_quote / budget, Decimal("1")), Decimal("-1"))
            spreads = [Decimal(str(s)) for s in list(self.config.buy_spreads) + list(self.config.sell_spreads)]
            avg_spread = sum(spreads) / Decimal(len(spreads)) if spreads else Decimal("0")
            shift = strength * avg_spread * inventory_ratio
        skewed_reference = mid_price * (Decimal("1") - shift)
        self.processed_data["reference_price"] = skewed_reference
        self.processed_data["inventory_skew"] = {
            "mid_price": str(mid_price),
            "base_position": str(base_position),
            "inventory_quote": str(inventory_quote),
            "inventory_ratio": str(inventory_ratio),
            "reference_shift_pct": str(shift),
            "skewed_reference_price": str(skewed_reference),
        }

    def get_levels_to_execute(self) -> List[str]:
        """
        Upstream only cools down after STOP_LOSS; this cools down after any close
        that traded (TP/time-limit/early-stop-with-fill) plus FAILED and
        INSUFFICIENT_BALANCE, so filled levels pause before re-quoting while
        unfilled refreshes re-quote immediately.
        """
        working_levels = self.filter_executors(
            executors=self.executors_info,
            filter_func=self._is_working_or_cooling_level,
        )
        working_levels_ids = [executor.custom_info["level_id"] for executor in working_levels]
        return self.get_not_active_levels_ids(working_levels_ids)

    def _is_working_or_cooling_level(self, executor_info) -> bool:
        if executor_info.is_active:
            return True
        close_type = executor_info.close_type
        if close_type is None:
            return False
        close_timestamp = executor_info.close_timestamp or 0
        filled_quote = getattr(executor_info, "filled_amount_quote", None) or Decimal("0")
        traded = Decimal(str(filled_quote)) > 0
        if close_type in _COOLDOWN_CLOSE_TYPES or traded:
            return self.market_data_provider.time() - close_timestamp < self.config.cooldown_time
        return False

    def get_custom_info(self) -> dict:
        skew = self.processed_data.get("inventory_skew", {}) if isinstance(self.processed_data, dict) else {}
        return {
            "open_order_type": str(self.config.open_order_type),
            "inventory_skew_strength": str(self.config.inventory_skew_strength),
            "inventory_skew": skew,
        }

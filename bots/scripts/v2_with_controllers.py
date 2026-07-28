import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

from hummingbot.client.hummingbot_application import HummingbotApplication
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import PriceType
from hummingbot.core.event.events import MarketOrderFailureEvent, PositionAction
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction

try:
    from scripts.connectivity_resilience import RuntimeConnectivityGuard
    from scripts.mainnet_guard import extract_connector_names_from_mapping, validate_testnet_connectors
    from scripts.mid_price_recorder import MidPriceRecorder
except ModuleNotFoundError:
    from connectivity_resilience import RuntimeConnectivityGuard
    from mainnet_guard import extract_connector_names_from_mapping, validate_testnet_connectors
    from mid_price_recorder import MidPriceRecorder


_MARKETS_WHITELIST_ERROR_MARKER = "not in the whitelisted markets set"
_S_DECIMAL_NAN = Decimal("NaN")


class V2WithControllersConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    max_global_drawdown_quote: Optional[float] = None
    max_controller_drawdown_quote: Optional[float] = None


class V2WithControllers(StrategyV2Base):
    """
    This script runs a generic strategy with cash out feature. Will also check if the controllers configs have been
    updated and apply the new settings.
    The cash out of the script can be set by the time_to_cash_out parameter in the config file. If set, the script will
    stop the controllers after the specified time has passed, and wait until the active executors finalize their
    execution.
    The controllers will also have a parameter to manually cash out. In that scenario, the main strategy will stop the
    specific controller and wait until the active executors finalize their execution. The rest of the executors will
    wait until the main strategy stops them.
    """
    performance_report_interval: int = 1

    def __init__(self, connectors: Dict[str, ConnectorBase], config: V2WithControllersConfig):
        super().__init__(connectors, config)
        self._enforce_testnet_connector_guard()
        self.config = config
        self.max_pnl_by_controller = {}
        self.max_global_pnl = Decimal("0")
        self.drawdown_exited_controllers = []
        self.closed_executors_buffer: int = 30
        self._last_performance_report_timestamp = 0
        self.connectivity_guard = RuntimeConnectivityGuard(connectors=self.connectors)
        self._mid_recorder = MidPriceRecorder()
        self._last_mid_record_warning = 0.0

    def _enforce_testnet_connector_guard(self) -> None:
        connector_names = set(self.connectors.keys())
        for controller in self.controllers.values():
            connector_names.update(
                extract_connector_names_from_mapping(controller.config.model_dump())
            )
        validate_testnet_connectors(connector_names)

    def on_tick(self):
        self._record_mid_snapshot()
        connectivity_snapshot = self.connectivity_guard.evaluate(self.current_timestamp)
        if not self._is_stop_triggered:
            self.check_manual_kill_switch()
            self.control_max_drawdown()
        if not connectivity_snapshot.quoting_enabled:
            self.connectivity_guard.apply_safety_actions(
                executors=self.get_all_executors(),
                executor_orchestrator=self.executor_orchestrator,
                stop_action_cls=StopExecutorAction,
            )
            self.send_performance_report()
            return
        super().on_tick()
        if not self._is_stop_triggered:
            self.send_performance_report()

    def _record_mid_snapshot(self):
        """
        Persist connector mid prices every tick (throttled by the recorder) so the
        analyzer can compute markout without relying on short-retention venue
        candles. Telemetry must never break the tick: any failure is logged at
        most once a minute and swallowed.
        """
        recorder = getattr(self, "_mid_recorder", None)
        if recorder is None or not recorder.enabled:
            return
        try:
            samples = []
            seen = set()
            for controller in self.controllers.values():
                config = getattr(controller, "config", None)
                connector_name = getattr(config, "connector_name", None)
                trading_pair = getattr(config, "trading_pair", None)
                if not connector_name or not trading_pair or (connector_name, trading_pair) in seen:
                    continue
                seen.add((connector_name, trading_pair))
                price = self.market_data_provider.get_price_by_type(connector_name, trading_pair, PriceType.MidPrice)
                if price is None:
                    continue
                samples.append({"connector": connector_name, "pair": trading_pair, "mid": str(price)})
            recorder.maybe_record(self.current_timestamp, samples)
        except Exception as exc:
            if self.current_timestamp - self._last_mid_record_warning >= 60:
                self._last_mid_record_warning = self.current_timestamp
                self.logger().warning(f"Mid-price snapshot failed (markout telemetry degraded): {exc}")

    def control_max_drawdown(self):
        if self.config.max_controller_drawdown_quote:
            self.check_max_controller_drawdown()
        if self.config.max_global_drawdown_quote:
            self.check_max_global_drawdown()

    def check_max_controller_drawdown(self):
        for controller_id, controller in self.controllers.items():
            if controller.status != RunnableStatus.RUNNING:
                continue
            controller_pnl = self.get_performance_report(controller_id).global_pnl_quote
            last_max_pnl = self.max_pnl_by_controller[controller_id]
            if controller_pnl > last_max_pnl:
                self.max_pnl_by_controller[controller_id] = controller_pnl
            else:
                current_drawdown = last_max_pnl - controller_pnl
                if current_drawdown > self.config.max_controller_drawdown_quote:
                    self.logger().info(
                        f"Controller {controller_id} reached max drawdown. "
                        "Stopping the controller and flattening open positions."
                    )
                    controller.stop()
                    # Flatten ALL active executors (including trading/open positions).
                    # Previously only non-trading executors were stopped, which left
                    # filled inventory riding barriers after a controller breach.
                    executors_to_stop = self.filter_executors(
                        executors=self.get_executors_by_controller(controller_id),
                        filter_func=lambda x: x.is_active or bool(getattr(x, "is_trading", False)),
                    )
                    self.executor_orchestrator.execute_actions(
                        actions=[
                            StopExecutorAction(
                                controller_id=controller_id,
                                executor_id=executor.id,
                                keep_position=False,
                            )
                            for executor in executors_to_stop
                        ]
                    )
                    self.drawdown_exited_controllers.append(controller_id)

    def check_max_global_drawdown(self):
        current_global_pnl = sum([self.get_performance_report(controller_id).global_pnl_quote for controller_id in self.controllers.keys()])
        if current_global_pnl > self.max_global_pnl:
            self.max_global_pnl = current_global_pnl
        else:
            current_global_drawdown = self.max_global_pnl - current_global_pnl
            if current_global_drawdown > self.config.max_global_drawdown_quote:
                self.drawdown_exited_controllers.extend(list(self.controllers.keys()))
                self.logger().info("Global drawdown reached. Stopping the strategy.")
                self._is_stop_triggered = True
                HummingbotApplication.main_application().stop()

    def get_controller_report(self, controller_id: str) -> dict:
        """
        Get the full report for a controller including performance and custom info.
        """
        performance_report = self.controller_reports.get(controller_id, {}).get("performance")
        custom_info = self.controllers[controller_id].get_custom_info() or {}
        custom_info["runtime_connectivity"] = self.connectivity_guard.current_state_payload()
        return {
            "performance": performance_report.dict() if performance_report else {},
            "custom_info": custom_info
        }

    def send_performance_report(self):
        if self.current_timestamp - self._last_performance_report_timestamp >= self.performance_report_interval and self._pub:
            controller_reports = {controller_id: self.get_controller_report(controller_id) for controller_id in self.controllers.keys()}
            self._pub(controller_reports)
            self._last_performance_report_timestamp = self.current_timestamp

    def check_manual_kill_switch(self):
        for controller_id, controller in self.controllers.items():
            if controller.config.manual_kill_switch and controller.status == RunnableStatus.RUNNING:
                self.logger().info(f"Manual cash out for controller {controller_id}.")
                controller.stop()
                executors_to_stop = self.get_executors_by_controller(controller_id)
                self.executor_orchestrator.execute_actions(
                    [StopExecutorAction(executor_id=executor.id,
                                        controller_id=executor.controller_id,
                                        keep_position=False) for executor in executors_to_stop])
            if not controller.config.manual_kill_switch and controller.status == RunnableStatus.TERMINATED:
                if controller_id in self.drawdown_exited_controllers:
                    continue
                self.logger().info(f"Restarting controller {controller_id}.")
                controller.start()

    def check_executors_status(self):
        active_executors = self.filter_executors(
            executors=self.get_all_executors(),
            filter_func=lambda executor: executor.status == RunnableStatus.RUNNING
        )
        if not active_executors:
            self.logger().info("All executors have finalized their execution. Stopping the strategy.")
            HummingbotApplication.main_application().stop()
        else:
            non_trading_executors = self.filter_executors(
                executors=active_executors,
                filter_func=lambda executor: not executor.is_trading
            )
            self.executor_orchestrator.execute_actions(
                [StopExecutorAction(executor_id=executor.id,
                                    controller_id=executor.controller_id,
                                    keep_position=False) for executor in non_trading_executors])

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        return []

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        return []

    def apply_initial_setting(self):
        connectors_position_mode = {}
        for controller_id, controller in self.controllers.items():
            self.max_pnl_by_controller[controller_id] = Decimal("0")
            config_dict = controller.config.model_dump()
            if "connector_name" in config_dict:
                if self.is_perpetual(config_dict["connector_name"]):
                    if "position_mode" in config_dict:
                        connectors_position_mode[config_dict["connector_name"]] = config_dict["position_mode"]
                    if "leverage" in config_dict and "trading_pair" in config_dict:
                        self.connectors[config_dict["connector_name"]].set_leverage(
                            leverage=config_dict["leverage"],
                            trading_pair=config_dict["trading_pair"])
        for connector_name, position_mode in connectors_position_mode.items():
            self.connectors[connector_name].set_position_mode(position_mode)

    def did_fail_order(self, order_failed_event: MarketOrderFailureEvent):
        """
        Handle order failure events by logging the error and stopping the strategy if necessary.
        """
        error_message = getattr(order_failed_event, "error_message", None) or ""
        if error_message and "position side" in error_message.lower():
            connectors_position_mode = {}
            for controller_id, controller in self.controllers.items():
                config_dict = controller.config.model_dump()
                if "connector_name" in config_dict:
                    if self.is_perpetual(config_dict["connector_name"]):
                        if "position_mode" in config_dict:
                            connectors_position_mode[config_dict["connector_name"]] = config_dict["position_mode"]
            for connector_name, position_mode in connectors_position_mode.items():
                self.connectors[connector_name].set_position_mode(position_mode)
            return

        # Surface close/open placement failures loudly so operators can act.
        if self._is_stop_triggered or self._looks_like_close_failure(error_message):
            self.logger().error(
                "ORDER PLACEMENT FAILED DURING SHUTDOWN/CLOSE: "
                f"order_id={getattr(order_failed_event, 'order_id', None)} "
                f"error={error_message!r}. "
                "If a position remains open on the exchange, close it manually."
            )

    async def on_stop(self):
        """
        Flatten open positions while markets are still whitelisted, then tear down.

        Base-image PositionExecutors can keep retrying close orders after markets are
        removed from the strategy whitelist, leaving inventory stranded. We issue
        flatten stops first, wait for orchestrator shutdown, then loudly report and
        force-terminate any lingering executors so they cannot retry forever after
        teardown.
        """
        self._is_stop_triggered = True
        self.logger().info("Strategy stop: stopping controllers and flattening executors before market teardown.")

        for controller_id, controller in self.controllers.items():
            try:
                controller.stop()
            except Exception:
                self.logger().exception(f"Failed to stop controller {controller_id} during strategy shutdown.")

        self._issue_flatten_stop_actions(reason="strategy_on_stop")

        listen_task = getattr(self, "listen_to_executor_actions_task", None)
        if listen_task is not None:
            listen_task.cancel()

        try:
            await self.executor_orchestrator.stop(self.max_executors_close_attempts)
        except Exception:
            self.logger().exception("Executor orchestrator stop failed during strategy shutdown.")

        stranded = self._collect_open_position_executors()
        if stranded:
            self.logger().error(
                "STRANDED POSITION(S) AFTER STRATEGY STOP: close could not complete before "
                "market teardown. Manual intervention required on the exchange. "
                f"details={self._format_executor_summaries(stranded)}"
            )
        else:
            self.logger().info("Strategy stop: no open executor positions remain after flatten attempt.")

        market_data_provider = getattr(self, "market_data_provider", None)
        if market_data_provider is not None:
            market_data_provider.stop()

        store_all = getattr(self.executor_orchestrator, "store_all_executors", None)
        if callable(store_all):
            store_all()

        if getattr(self, "mqtt_enabled", False) and getattr(self, "_pub", None):
            self._pub({controller_id: {} for controller_id in self.controllers.keys()})
            self._pub = None

    def buy(
        self,
        connector_name: str,
        trading_pair: str,
        amount: Decimal,
        order_type,
        price=_S_DECIMAL_NAN,
        position_action=PositionAction.OPEN,
    ) -> str:
        try:
            return super().buy(
                connector_name, trading_pair, amount, order_type, price, position_action=position_action
            )
        except ValueError as exc:
            self._handle_order_placement_failure(
                side="buy",
                connector_name=connector_name,
                trading_pair=trading_pair,
                amount=amount,
                position_action=position_action,
                error=exc,
            )
            raise

    def sell(
        self,
        connector_name: str,
        trading_pair: str,
        amount: Decimal,
        order_type,
        price=_S_DECIMAL_NAN,
        position_action=PositionAction.OPEN,
    ) -> str:
        try:
            return super().sell(
                connector_name, trading_pair, amount, order_type, price, position_action=position_action
            )
        except ValueError as exc:
            self._handle_order_placement_failure(
                side="sell",
                connector_name=connector_name,
                trading_pair=trading_pair,
                amount=amount,
                position_action=position_action,
                error=exc,
            )
            raise

    def _issue_flatten_stop_actions(self, reason: str) -> None:
        executors = self.filter_executors(
            executors=self.get_all_executors(),
            filter_func=lambda executor: (
                not bool(getattr(executor, "is_closed", False))
                and getattr(executor, "status", None) != RunnableStatus.TERMINATED
            ),
        )
        if not executors:
            self.logger().info(f"No active executors to flatten ({reason}).")
            return

        actions = [
            StopExecutorAction(
                executor_id=executor.id,
                controller_id=getattr(executor, "controller_id", "main"),
                keep_position=False,
            )
            for executor in executors
        ]
        self.logger().info(
            f"Issuing flatten StopExecutorAction for {len(actions)} executor(s) "
            f"before teardown ({reason}): {self._format_executor_summaries(executors)}"
        )
        self.executor_orchestrator.execute_actions(actions)

    def _collect_open_position_executors(self) -> List[Any]:
        return self.filter_executors(
            executors=self.get_all_executors(),
            filter_func=lambda executor: self._executor_has_open_position(executor),
        )

    @staticmethod
    def _executor_has_open_position(executor: Any) -> bool:
        if bool(getattr(executor, "is_closed", False)):
            return False
        if bool(getattr(executor, "is_trading", False)):
            return True
        # Requiring a non-zero fill keeps the stranded-position ERROR
        # meaningful: an unfilled executor still winding down is not a stranded
        # position, and crying wolf here trains operators to ignore the one log
        # line that matters.
        filled = V2WithControllers._executor_fill_amount(executor)
        if filled is None:
            return False
        try:
            return Decimal(str(filled)) > 0
        except (ArithmeticError, TypeError, ValueError):
            return bool(filled)

    @staticmethod
    def _executor_fill_amount(executor: Any) -> Any:
        """Fill size of an executor. get_all_executors() yields ExecutorInfo,
        which reports filled_amount_quote; PositionExecutor itself exposes
        open_filled_amount."""
        filled = getattr(executor, "filled_amount_quote", None)
        if filled is None:
            filled = getattr(executor, "open_filled_amount", None)
        return filled

    def _handle_order_placement_failure(
        self,
        side: str,
        connector_name: str,
        trading_pair: str,
        amount: Decimal,
        position_action: Any,
        error: Exception,
    ) -> None:
        action_name = getattr(position_action, "name", str(position_action))
        message = str(error)
        if _MARKETS_WHITELIST_ERROR_MARKER in message:
            self.logger().error(
                "CLOSE/ORDER FAILED: market already removed from strategy whitelist. "
                f"side={side} connector={connector_name} pair={trading_pair} amount={amount} "
                f"position_action={action_name} error={message}. "
                "Position may be stranded on the exchange — close it manually. "
                f"open_executors={self._format_executor_summaries(self._collect_open_position_executors())}"
            )
            return
        if self._is_stop_triggered or self._is_close_position_action(position_action):
            self.logger().error(
                "CLOSE/ORDER FAILED during strategy shutdown/close path. "
                f"side={side} connector={connector_name} pair={trading_pair} amount={amount} "
                f"position_action={action_name} error={message}. "
                "If a position remains open on the exchange, close it manually."
            )

    @staticmethod
    def _is_close_position_action(position_action: Any) -> bool:
        action_name = getattr(position_action, "name", str(position_action)).upper()
        return "CLOSE" in action_name

    @staticmethod
    def _looks_like_close_failure(error_message: str) -> bool:
        lowered = (error_message or "").lower()
        return _MARKETS_WHITELIST_ERROR_MARKER in lowered or "close" in lowered

    @staticmethod
    def _format_executor_summaries(executors: List[Any]) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for executor in executors:
            config = getattr(executor, "config", None)
            summaries.append(
                {
                    "executor_id": getattr(executor, "id", None),
                    "controller_id": getattr(executor, "controller_id", None),
                    "status": str(getattr(executor, "status", None)),
                    "is_trading": bool(getattr(executor, "is_trading", False)),
                    "filled_amount": str(V2WithControllers._executor_fill_amount(executor)),
                    "connector_name": getattr(config, "connector_name", None),
                    "trading_pair": getattr(config, "trading_pair", None),
                }
            )
        return summaries

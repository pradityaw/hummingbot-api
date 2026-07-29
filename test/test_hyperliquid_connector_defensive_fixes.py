import asyncio
import importlib.util
import socket
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace


OVERRIDE_DIR = (
    Path(__file__).resolve().parents[1]
    / "docker"
    / "hyperliquid-patch"
    / "overrides"
    / "hummingbot"
    / "connector"
    / "derivative"
    / "hyperliquid_perpetual"
)
DERIVATIVE_PATH = OVERRIDE_DIR / "hyperliquid_perpetual_derivative.py"
RUNTIME_PATH = OVERRIDE_DIR / "hyperliquid_runtime_connectivity.py"


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)

    def debug(self, message, *args, **kwargs):
        self.debugs.append(message)

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakePerpetualDerivativePyBase:
    async def _api_post(self, path_url, data=None, **kwargs):
        outcome = self._api_post_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _sleep(self, delay):
        self.sleep_calls.append(delay)

    def logger(self):
        return self._logger


class FakeOrderType(Enum):
    LIMIT = 1
    LIMIT_MAKER = 2
    MARKET = 3


class FakeTradeType(Enum):
    BUY = 1
    SELL = 2


class FakePositionAction(Enum):
    NIL = 1
    CLOSE = 2
    OPEN = 3


class FakePositionMode(Enum):
    ONEWAY = 1


class FakePositionSide(Enum):
    LONG = 1
    SHORT = 2


class FakeBidict(dict):
    @property
    def inverse(self):
        return {value: key for key, value in self.items()}


def install_module(name, **attrs):
    module = ModuleType(name)
    for attr_name, value in attrs.items():
        setattr(module, attr_name, value)
    sys.modules[name] = module
    return module


def install_derivative_import_stubs():
    install_module("bidict", bidict=FakeBidict)
    install_module("hummingbot")
    install_module("hummingbot.connector")
    install_module("hummingbot.connector.constants", s_decimal_NaN=object())
    install_module("hummingbot.connector.derivative")
    hyperliquid_package = install_module("hummingbot.connector.derivative.hyperliquid_perpetual")

    constants = install_module(
        "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_constants",
        ASSET_CONTEXT_TYPE="metaAndAssetCtxs",
        BROKER_ID="HBOT",
        BUILDER_SUPPORTED=False,
        CANCEL_ORDER_URL="/exchange",
        CREATE_ORDER_URL="/exchange",
        CURRENCY="USD",
        DEPTH_ENDPOINT_NAME="l2Book",
        DOMAIN="hyperliquid_perpetual",
        DEX_ASSET_CONTEXT_TYPE="allPerpMetas",
        EXCHANGE_INFO_URL="/info",
        FOUNDATION_BUILDER_ADDRESS="0x0000000000000000000000000000000000000000",
        FOUNDATION_BUILDER_FEE_TENTHS_BPS=0,
        HEARTBEAT_TIME_INTERVAL=30.0,
        MAX_BUILDER_FEE_TYPE="maxBuilderFee",
        MAX_ORDER_ID_LEN=32,
        META_INFO="meta",
        MIN_NOTIONAL_SIZE="1",
        ORDER_NOT_EXIST_MESSAGE="not found",
        ORDER_STATE={},
        PING_URL="/info",
        RATE_LIMITS=[],
        SPOT_BALANCE_ABSTRACTION_MODES=set(),
        TESTNET_DOMAIN="hyperliquid_perpetual_testnet",
        TRADES_ENDPOINT_NAME="trades",
        UNKNOWN_ORDER_MESSAGE="unknown order",
        WS_MESSAGE_TIMEOUT=30.0,
    )
    web_utils = install_module(
        "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_web_utils",
        build_api_factory=lambda **kwargs: None,
        is_exchange_information_valid=lambda exchange_info: True,
    )
    hyperliquid_package.hyperliquid_perpetual_constants = constants
    hyperliquid_package.hyperliquid_perpetual_web_utils = web_utils

    install_module(
        "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_api_order_book_data_source",
        HyperliquidPerpetualAPIOrderBookDataSource=object,
    )
    install_module(
        "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_auth",
        HyperliquidPerpetualAuth=object,
    )
    install_module(
        "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_user_stream_data_source",
        HyperliquidPerpetualUserStreamDataSource=object,
    )
    install_module("hummingbot.connector.derivative.position", Position=object)
    install_module(
        "hummingbot.connector.perpetual_derivative_py_base",
        PerpetualDerivativePyBase=FakePerpetualDerivativePyBase,
    )
    install_module("hummingbot.connector.trading_rule", TradingRule=object)
    install_module(
        "hummingbot.connector.utils",
        combine_to_hb_trading_pair=lambda base, quote: f"{base}-{quote}",
        get_new_client_order_id=lambda **kwargs: "client-order",
    )
    install_module("hummingbot.core")
    install_module("hummingbot.core.api_throttler")
    install_module("hummingbot.core.api_throttler.data_types", RateLimit=object)
    install_module("hummingbot.core.data_type")
    install_module(
        "hummingbot.core.data_type.common",
        OrderType=FakeOrderType,
        PositionAction=FakePositionAction,
        PositionMode=FakePositionMode,
        PositionSide=FakePositionSide,
        TradeType=FakeTradeType,
    )
    install_module(
        "hummingbot.core.data_type.in_flight_order",
        InFlightOrder=object,
        OrderUpdate=object,
        TradeUpdate=object,
    )
    install_module(
        "hummingbot.core.data_type.order_book_tracker_data_source",
        OrderBookTrackerDataSource=object,
    )
    install_module(
        "hummingbot.core.data_type.trade_fee",
        TokenAmount=object,
        TradeFeeBase=SimpleNamespace(new_perpetual_fee=lambda **kwargs: None),
    )
    install_module(
        "hummingbot.core.data_type.user_stream_tracker_data_source",
        UserStreamTrackerDataSource=object,
    )
    install_module(
        "hummingbot.core.utils.async_utils",
        safe_ensure_future=lambda awaitable: awaitable,
        safe_gather=asyncio.gather,
    )
    install_module("hummingbot.core.utils.estimate_fee", build_trade_fee=lambda *args, **kwargs: None)
    install_module("hummingbot.core.web_assistant")
    install_module("hummingbot.core.web_assistant.web_assistants_factory", WebAssistantsFactory=object)

    runtime_spec = importlib.util.spec_from_file_location(
        "hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_runtime_connectivity",
        RUNTIME_PATH,
    )
    runtime_module = importlib.util.module_from_spec(runtime_spec)
    sys.modules[runtime_spec.name] = runtime_module
    runtime_spec.loader.exec_module(runtime_module)
    return runtime_module


def load_derivative_module():
    runtime_module = install_derivative_import_stubs()
    spec = importlib.util.spec_from_file_location("hyperliquid_perpetual_derivative_under_test", DERIVATIVE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, runtime_module


def connector_for(module):
    connector = object.__new__(module.HyperliquidPerpetualDerivative)
    connector._hb_runtime_connectivity = {}
    connector._logger = FakeLogger()
    connector.sleep_calls = []
    return connector


def test_cancel_response_with_string_success_is_accepted():
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector.coin_to_asset = {"BTC": 0}
    connector.exchange_symbol_associated_to_pair = lambda trading_pair: asyncio.sleep(0, result="BTC")
    connector._order_tracker = SimpleNamespace(
        process_order_not_found=lambda order_id: asyncio.sleep(0),
    )

    async def api_post(**kwargs):
        return {
            "status": "ok",
            "response": {
                "type": "cancel",
                "data": {"statuses": ["success"]},
            },
        }

    connector._api_post = api_post
    tracked_order = SimpleNamespace(trading_pair="BTC-USD")

    result = asyncio.run(module.HyperliquidPerpetualDerivative._place_cancel(connector, "client-order", tracked_order))

    assert result is True
    assert connector._hb_runtime_connectivity.get("cancel_failure_count", 0) == 0


def test_cancel_order_not_found_does_not_increment_cancel_failure_count():
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector.coin_to_asset = {"BTC": 0}
    connector.exchange_symbol_associated_to_pair = lambda trading_pair: asyncio.sleep(0, result="BTC")
    connector._order_tracker = SimpleNamespace(
        process_order_not_found=lambda order_id: asyncio.sleep(0),
    )

    async def api_post(**kwargs):
        return {
            "status": "err",
            "response": {
                "type": "cancel",
                "data": {"statuses": [{"error": "Order not found"}]},
            },
        }

    connector._api_post = api_post
    tracked_order = SimpleNamespace(trading_pair="BTC-USD")

    try:
        asyncio.run(module.HyperliquidPerpetualDerivative._place_cancel(connector, "client-order", tracked_order))
    except OSError:
        pass
    else:
        raise AssertionError("Expected order-not-found cancel to raise")

    assert connector._hb_runtime_connectivity.get("cancel_failure_count", 0) == 0


def test_cancel_transport_failure_increments_cancel_failure_count():
    module, runtime_module = load_derivative_module()
    connector = connector_for(module)
    connector.coin_to_asset = {"BTC": 0}
    connector.exchange_symbol_associated_to_pair = lambda trading_pair: asyncio.sleep(0, result="BTC")
    connector._order_tracker = SimpleNamespace(
        process_order_not_found=lambda order_id: asyncio.sleep(0),
    )

    async def api_post(**kwargs):
        raise OSError("connection reset")

    connector._api_post = api_post
    tracked_order = SimpleNamespace(trading_pair="BTC-USD")

    try:
        asyncio.run(module.HyperliquidPerpetualDerivative._place_cancel(connector, "client-order", tracked_order))
    except OSError:
        pass
    else:
        raise AssertionError("Expected transport failure to raise")

    assert connector._hb_runtime_connectivity.get("cancel_failure_count", 0) == 1


def test_buy_rejected_when_runtime_quoting_disabled():
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector._hb_runtime_quoting_enabled = False

    try:
        module.HyperliquidPerpetualDerivative.buy(
            connector,
            trading_pair="BTC-USD",
            amount=module.Decimal("0.0001"),
            order_type=module.OrderType.LIMIT,
            price=module.Decimal("50000"),
        )
    except (OSError, IOError) as exc:
        assert "Quoting disabled" in str(exc)
    else:
        raise AssertionError("Expected buy to be rejected when quoting disabled")


def test_place_order_rejected_when_runtime_quoting_disabled():
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector._hb_runtime_quoting_enabled = False
    connector.exchange_symbol_associated_to_pair = lambda trading_pair: asyncio.sleep(0, result="BTC")
    connector.coin_to_asset = {"BTC": 0}

    try:
        asyncio.run(
            module.HyperliquidPerpetualDerivative._place_order(
                connector,
                order_id="0xabc",
                trading_pair="BTC-USD",
                amount=module.Decimal("0.0001"),
                trade_type=module.TradeType.BUY,
                order_type=module.OrderType.LIMIT,
                price=module.Decimal("50000"),
            )
        )
    except OSError as exc:
        assert "Quoting disabled" in str(exc)
    else:
        raise AssertionError("Expected _place_order to be rejected when quoting disabled")


def _drain_ensure_future(awaitable):
    if asyncio.iscoroutine(awaitable):
        awaitable.close()
    return None


def _gated_connector_for_orders(module, monkeypatch):
    connector = connector_for(module)
    connector._hb_runtime_quoting_enabled = False
    connector.exchange_symbol_associated_to_pair = lambda trading_pair: asyncio.sleep(0, result="BTC")
    connector.coin_to_asset = {"BTC": 0}
    connector.current_timestamp = 123.0

    async def _fake_create_order(*args, **kwargs):
        return None

    connector._create_order = _fake_create_order
    monkeypatch.setattr(module, "safe_ensure_future", _drain_ensure_future)
    return connector


async def _ok_place_order_api_post(**kwargs):
    return {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": 999}}]}},
    }


def test_close_orders_allowed_when_runtime_quoting_disabled(monkeypatch):
    module, _ = load_derivative_module()
    connector = _gated_connector_for_orders(module, monkeypatch)
    connector._api_post = _ok_place_order_api_post

    buy_order_id = module.HyperliquidPerpetualDerivative.buy(
        connector,
        trading_pair="BTC-USD",
        amount=module.Decimal("0.0001"),
        order_type=module.OrderType.LIMIT,
        price=module.Decimal("50000"),
        position_action=module.PositionAction.CLOSE,
    )
    sell_order_id = module.HyperliquidPerpetualDerivative.sell(
        connector,
        trading_pair="BTC-USD",
        amount=module.Decimal("0.0001"),
        order_type=module.OrderType.LIMIT,
        price=module.Decimal("50000"),
        position_action=module.PositionAction.CLOSE,
    )
    exchange_order_id, timestamp = asyncio.run(
        module.HyperliquidPerpetualDerivative._place_order(
            connector,
            order_id="0xabc",
            trading_pair="BTC-USD",
            amount=module.Decimal("0.0001"),
            trade_type=module.TradeType.SELL,
            order_type=module.OrderType.LIMIT,
            price=module.Decimal("50000"),
            position_action=module.PositionAction.CLOSE,
        )
    )

    assert buy_order_id.startswith("0x")
    assert sell_order_id.startswith("0x")
    assert exchange_order_id == "999"
    assert timestamp == 123.0


def test_open_orders_still_rejected_when_runtime_quoting_disabled(monkeypatch):
    module, _ = load_derivative_module()
    connector = _gated_connector_for_orders(module, monkeypatch)

    for method_name, call in (
        (
            "buy",
            lambda: module.HyperliquidPerpetualDerivative.buy(
                connector,
                trading_pair="BTC-USD",
                amount=module.Decimal("0.0001"),
                order_type=module.OrderType.LIMIT,
                price=module.Decimal("50000"),
                position_action=module.PositionAction.OPEN,
            ),
        ),
        (
            "sell",
            lambda: module.HyperliquidPerpetualDerivative.sell(
                connector,
                trading_pair="BTC-USD",
                amount=module.Decimal("0.0001"),
                order_type=module.OrderType.LIMIT,
                price=module.Decimal("50000"),
                position_action=module.PositionAction.OPEN,
            ),
        ),
        (
            "_place_order",
            lambda: asyncio.run(
                module.HyperliquidPerpetualDerivative._place_order(
                    connector,
                    order_id="0xabc",
                    trading_pair="BTC-USD",
                    amount=module.Decimal("0.0001"),
                    trade_type=module.TradeType.BUY,
                    order_type=module.OrderType.LIMIT,
                    price=module.Decimal("50000"),
                    position_action=module.PositionAction.OPEN,
                )
            ),
        ),
    ):
        try:
            call()
        except (OSError, IOError) as exc:
            assert "Quoting disabled" in str(exc), method_name
        else:
            raise AssertionError(f"Expected {method_name} OPEN to be rejected when quoting disabled")


def test_orders_without_position_action_still_rejected_when_gated(monkeypatch):
    module, _ = load_derivative_module()
    connector = _gated_connector_for_orders(module, monkeypatch)

    for method_name, call in (
        (
            "buy",
            lambda: module.HyperliquidPerpetualDerivative.buy(
                connector,
                trading_pair="BTC-USD",
                amount=module.Decimal("0.0001"),
                order_type=module.OrderType.LIMIT,
                price=module.Decimal("50000"),
            ),
        ),
        (
            "sell",
            lambda: module.HyperliquidPerpetualDerivative.sell(
                connector,
                trading_pair="BTC-USD",
                amount=module.Decimal("0.0001"),
                order_type=module.OrderType.LIMIT,
                price=module.Decimal("50000"),
            ),
        ),
        (
            "_place_order",
            lambda: asyncio.run(
                module.HyperliquidPerpetualDerivative._place_order(
                    connector,
                    order_id="0xabc",
                    trading_pair="BTC-USD",
                    amount=module.Decimal("0.0001"),
                    trade_type=module.TradeType.BUY,
                    order_type=module.OrderType.LIMIT,
                    price=module.Decimal("50000"),
                )
            ),
        ),
    ):
        try:
            call()
        except (OSError, IOError) as exc:
            assert "Quoting disabled" in str(exc), method_name
        else:
            raise AssertionError(f"Expected {method_name} without position_action to stay gated")


def test_api_post_retries_transient_read_request_and_records_attempts(monkeypatch):
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector._api_post_outcomes = [
        OSError("HTTP status 429: Too many cumulative requests sent"),
        {"status": "ok"},
    ]
    failures = []
    successes = []
    monkeypatch.setattr(module, "_API_POST_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(module.random, "uniform", lambda start, end: 0)
    monkeypatch.setattr(module, "record_rest_failure", lambda connector, path_url, exc: failures.append((path_url, exc)))
    monkeypatch.setattr(module, "record_rest_success", lambda connector, path_url: successes.append(path_url))

    result = asyncio.run(module.HyperliquidPerpetualDerivative._api_post(connector, "/info", data={"type": "meta"}))

    assert result == {"status": "ok"}
    assert len(failures) == 1
    assert successes == ["/info"]
    assert connector.sleep_calls == [module._API_POST_RETRY_BASE_DELAY]
    assert connector._logger.warnings


def test_api_post_does_not_retry_order_placement_after_ambiguous_transient_failure(monkeypatch):
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector._api_post_outcomes = [
        OSError("HTTP status 504 Gateway Timeout"),
        {"status": "ok"},
    ]
    failures = []
    successes = []
    monkeypatch.setattr(module, "_API_POST_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(module, "record_rest_failure", lambda connector, path_url, exc: failures.append((path_url, exc)))
    monkeypatch.setattr(module, "record_rest_success", lambda connector, path_url: successes.append(path_url))

    try:
        asyncio.run(module.HyperliquidPerpetualDerivative._api_post(connector, "/exchange", data={"type": "order"}))
    except OSError as exc:
        assert "Gateway Timeout" in str(exc)
    else:
        raise AssertionError("Expected ambiguous order placement failure to be re-raised without retry")

    assert len(failures) == 1
    assert successes == []
    assert connector.sleep_calls == []


def test_api_post_does_not_retry_non_transient_final_failure(monkeypatch):
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector._api_post_outcomes = [OSError("invalid cloid")]
    failures = []
    successes = []
    monkeypatch.setattr(module, "_API_POST_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(module, "record_rest_failure", lambda connector, path_url, exc: failures.append((path_url, exc)))
    monkeypatch.setattr(module, "record_rest_success", lambda connector, path_url: successes.append(path_url))

    try:
        asyncio.run(module.HyperliquidPerpetualDerivative._api_post(connector, "/exchange", data={"type": "order"}))
    except OSError as exc:
        assert "invalid cloid" in str(exc)
    else:
        raise AssertionError("Expected non-transient REST failure to be re-raised")

    assert len(failures) == 1
    assert successes == []
    assert connector.sleep_calls == []


def test_retry_classifier_includes_dns_and_gateway_failures():
    module, _ = load_derivative_module()

    assert module.HyperliquidPerpetualDerivative._is_retryable_api_post_exception(socket.gaierror("dns failed"))
    assert module.HyperliquidPerpetualDerivative._is_retryable_api_post_exception(
        OSError("HTTP status 504 Gateway Timeout")
    )
    assert not module.HyperliquidPerpetualDerivative._is_retryable_api_post_exception(OSError("invalid cloid"))


def test_ws_close_metadata_extracts_nested_code_and_reason():
    _, runtime_module = load_derivative_module()
    ws = SimpleNamespace(
        _connection=SimpleNamespace(
            _websocket=SimpleNamespace(close_code=1006, close_reason="abnormal closure")
        )
    )

    assert runtime_module.extract_ws_close_metadata(ws) == ("1006", "abnormal closure")


def test_cancel_err_with_unrelated_message_is_a_cancel_failure_not_not_found():
    module, _ = load_derivative_module()
    connector = connector_for(module)
    connector.coin_to_asset = {"BTC": 0}
    connector.exchange_symbol_associated_to_pair = lambda trading_pair: asyncio.sleep(0, result="BTC")
    not_found_calls = []
    connector._order_tracker = SimpleNamespace(
        process_order_not_found=lambda order_id: not_found_calls.append(order_id) or asyncio.sleep(0),
    )

    async def api_post(**kwargs):
        return {
            "status": "err",
            "response": {
                "type": "cancel",
                "data": {"statuses": [{"error": "Too many cumulative requests sent"}]},
            },
        }

    connector._api_post = api_post
    tracked_order = SimpleNamespace(trading_pair="BTC-USD")

    try:
        asyncio.run(module.HyperliquidPerpetualDerivative._place_cancel(connector, "client-order", tracked_order))
    except OSError:
        pass
    else:
        raise AssertionError("Expected rejected cancel to raise")

    # The live order must stay tracked (no process_order_not_found), and the
    # failure must feed the guard so quoting gates + reconciles instead.
    assert not_found_calls == []
    assert connector._hb_runtime_connectivity.get("cancel_failure_count", 0) == 1


def test_gate_is_fail_closed_by_default_when_never_stamped(monkeypatch):
    module, _ = load_derivative_module()
    connector = _gated_connector_for_orders(module, monkeypatch)
    # Delete the stamp entirely: a bot that never ran the guard must not quote.
    del connector._hb_runtime_quoting_enabled

    try:
        module.HyperliquidPerpetualDerivative.buy(
            connector,
            trading_pair="BTC-USD",
            amount=module.Decimal("0.0001"),
            order_type=module.OrderType.LIMIT,
            price=module.Decimal("50000"),
        )
    except (OSError, IOError) as exc:
        assert "Quoting disabled" in str(exc)
    else:
        raise AssertionError("Expected buy to be rejected when the gate was never stamped")


def _set_position(connector, module, amount):
    connector._perpetual_trading = SimpleNamespace(
        account_positions={
            "key": SimpleNamespace(trading_pair="BTC-USD", amount=module.Decimal(str(amount))),
        }
    )


def test_reduce_by_netting_sell_allowed_when_gated(monkeypatch):
    module, _ = load_derivative_module()
    connector = _gated_connector_for_orders(module, monkeypatch)
    connector._api_post = _ok_place_order_api_post
    _set_position(connector, module, "0.0002")

    # ONEWAY close sent as PositionAction.OPEN: sell against a 0.0002 long.
    order_id = module.HyperliquidPerpetualDerivative.sell(
        connector,
        trading_pair="BTC-USD",
        amount=module.Decimal("0.0001"),
        order_type=module.OrderType.LIMIT,
        price=module.Decimal("50000"),
        position_action=module.PositionAction.OPEN,
    )
    assert order_id.startswith("0x")


def test_oversized_or_same_direction_orders_still_gated(monkeypatch):
    module, _ = load_derivative_module()
    connector = _gated_connector_for_orders(module, monkeypatch)
    _set_position(connector, module, "0.0002")

    cases = {
        # larger than the tracked long: would flip exposure, not reduce it
        "oversized_sell": lambda: module.HyperliquidPerpetualDerivative.sell(
            connector, trading_pair="BTC-USD", amount=module.Decimal("0.0003"),
            order_type=module.OrderType.LIMIT, price=module.Decimal("50000"),
            position_action=module.PositionAction.OPEN),
        # same direction as the long: increases exposure
        "same_direction_buy": lambda: module.HyperliquidPerpetualDerivative.buy(
            connector, trading_pair="BTC-USD", amount=module.Decimal("0.0001"),
            order_type=module.OrderType.LIMIT, price=module.Decimal("50000"),
            position_action=module.PositionAction.OPEN),
    }
    for name, call in cases.items():
        try:
            call()
        except (OSError, IOError) as exc:
            assert "Quoting disabled" in str(exc), name
        else:
            raise AssertionError(f"Expected {name} to stay gated")


def test_reduce_by_netting_blocked_when_position_unknown(monkeypatch):
    module, _ = load_derivative_module()
    connector = _gated_connector_for_orders(module, monkeypatch)
    # No position cache at all -> cannot prove reduction -> fail closed.
    connector._perpetual_trading = SimpleNamespace(account_positions={})

    try:
        module.HyperliquidPerpetualDerivative.sell(
            connector, trading_pair="BTC-USD", amount=module.Decimal("0.0001"),
            order_type=module.OrderType.LIMIT, price=module.Decimal("50000"),
            position_action=module.PositionAction.OPEN)
    except (OSError, IOError) as exc:
        assert "Quoting disabled" in str(exc)
    else:
        raise AssertionError("Expected sell to stay gated with unknown position")

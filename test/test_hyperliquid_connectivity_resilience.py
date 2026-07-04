import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bots" / "scripts"))

from connectivity_resilience import ConnectivityState, ConnectivityStateStore, ConnectivityThresholds, RuntimeConnectivityGuard


class FakeTracker:
    def __init__(self, order_count=0):
        self.all_updatable_orders = {str(index): object() for index in range(order_count)}


class FakeConnector:
    name = "hyperliquid_perpetual_testnet"
    network_status = "CONNECTED"
    _trading_pairs = ["BTC-USD"]

    def __init__(self, runtime=None, order_count=0, cancel_available=True):
        self._hb_runtime_connectivity = runtime or {}
        self._order_tracker = FakeTracker(order_count)
        self.cancel_calls = 0
        if cancel_available:
            self.cancel_all = self._cancel_all

    def _cancel_all(self, timeout_seconds=10):
        self.cancel_calls += 1
        self._order_tracker.all_updatable_orders = {}


class FlakyReconcileConnector(FakeConnector):
    def __init__(self, runtime=None, order_count=0, failures_before_success=1):
        super().__init__(runtime=runtime, order_count=order_count)
        self.failures_before_success = failures_before_success
        self.balance_update_calls = 0

    async def _update_balances(self):
        self.balance_update_calls += 1
        if self.balance_update_calls <= self.failures_before_success:
            raise RuntimeError("temporary REST failure")

    async def _update_order_status(self):
        return None

    async def _update_positions(self):
        return None


class FakeExecutor:
    def __init__(self):
        self.status = "RUNNING"
        self.controller_id = "controller"
        self.id = "executor"


class FakeOrchestrator:
    def __init__(self):
        self.actions = []

    def execute_actions(self, actions):
        self.actions.extend(actions)


class FakeStopAction:
    def __init__(self, controller_id, executor_id):
        self.controller_id = controller_id
        self.executor_id = executor_id


def guard_for(
    tmp_path,
    connector,
    now=1000,
    reconnect_required_stable_seconds=10,
    reconciliation_max_retries=3,
    reconciliation_retry_backoff_seconds=5,
    rate_limit_unsafe_threshold=3,
    max_open_orders=4,
):
    return RuntimeConnectivityGuard(
        connectors={"hyperliquid_perpetual_testnet": connector},
        thresholds=ConnectivityThresholds(
            order_book_stale_seconds=45,
            user_stream_stale_seconds=90,
            hard_disconnect_seconds=20,
            reconnect_required_stable_seconds=reconnect_required_stable_seconds,
            reconciliation_max_retries=reconciliation_max_retries,
            reconciliation_retry_backoff_seconds=reconciliation_retry_backoff_seconds,
            rate_limit_unsafe_threshold=rate_limit_unsafe_threshold,
            max_open_orders=max_open_orders,
        ),
        store=ConnectivityStateStore(
            state_path=str(tmp_path / "state.json"),
            event_log_path=str(tmp_path / "events.jsonl"),
        ),
        time_fn=lambda: now,
    )


def test_public_ws_hard_disconnect_disables_quotes_and_captures_open_orders(tmp_path):
    connector = FakeConnector(
        runtime={"public_ws_status": "closed", "last_ws_close_timestamp": 970, "rest_health": "healthy"},
        order_count=2,
    )
    guard = guard_for(tmp_path, connector)

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.HARD_DISCONNECTED
    assert snapshot.quoting_enabled is False
    assert snapshot.open_order_count_during_disconnect == 2


def test_private_stream_recovery_requires_reconciliation_before_quotes(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=0,
    )
    guard = guard_for(tmp_path, connector)
    guard._last_state = ConnectivityState.HARD_DISCONNECTED
    guard.reconciliation_result = "required"

    recovering = guard.evaluate(1000)
    assert recovering.state == ConnectivityState.RECOVERING
    assert recovering.quoting_enabled is False

    guard.reconciliation_result = "succeeded"
    guard.orders_unknown = False
    soaking = guard.evaluate(1001)
    assert soaking.state == ConnectivityState.RECOVERING
    assert soaking.quoting_enabled is False
    assert soaking.reconciliation_result == "succeeded"
    assert "reconnect_stabilizing" in soaking.reason

    healthy = guard.evaluate(1011)
    assert healthy.state == ConnectivityState.HEALTHY
    assert healthy.quoting_enabled is True


def test_reconnect_soak_gate_requires_stable_window_after_reconciliation(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=0,
    )
    guard = guard_for(tmp_path, connector, reconnect_required_stable_seconds=5)
    guard._last_state = ConnectivityState.HARD_DISCONNECTED
    guard.reconciliation_result = "succeeded"
    guard.orders_unknown = False

    soaking = guard.evaluate(1000)
    assert soaking.state == ConnectivityState.RECOVERING
    assert soaking.quoting_enabled is False
    assert soaking.reconciliation_result == "succeeded"

    still_soaking = guard.evaluate(1004)
    assert still_soaking.state == ConnectivityState.RECOVERING
    assert still_soaking.quoting_enabled is False

    healthy = guard.evaluate(1005)
    assert healthy.state == ConnectivityState.HEALTHY
    assert healthy.quoting_enabled is True


def test_temporary_rest_unavailability_is_transient(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "unhealthy",
            "rest_consecutive_failures": 1,
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        }
    )
    guard = guard_for(tmp_path, connector)

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.DEGRADED_TRANSIENT
    assert snapshot.quoting_enabled is True


def test_repeated_rest_failures_are_unsafe(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "unhealthy",
            "rest_consecutive_failures": 3,
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        }
    )
    guard = guard_for(tmp_path, connector)

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.DEGRADED_UNSAFE
    assert snapshot.quoting_enabled is False


def test_reconnect_with_failed_reconciliation_does_not_resume(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=1,
    )
    guard = guard_for(tmp_path, connector)
    guard._last_state = ConnectivityState.HARD_DISCONNECTED
    guard.reconciliation_result = "failed"
    guard.orders_unknown = True

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.RECOVERING
    assert snapshot.quoting_enabled is False
    assert snapshot.readiness_reason != "ok"


def test_order_path_failure_without_transport_failure_enters_recovering(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "order_failure_count": 1,
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=0,
    )
    guard = guard_for(tmp_path, connector)

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.RECOVERING
    assert snapshot.quoting_enabled is False
    assert snapshot.reconciliation_result == "running"


def test_rate_limited_order_failure_does_not_make_orders_unknown(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "order_failure_count": 2,
            "last_order_failure_type": "rate_limit",
            "last_order_failure_message": "HTTP 429 too many requests",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=0,
    )
    guard = guard_for(tmp_path, connector, rate_limit_unsafe_threshold=3)

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.DEGRADED_TRANSIENT
    assert snapshot.orders_unknown is False
    assert snapshot.reconciliation_result == "not_started"
    assert snapshot.quoting_enabled is True
    assert "order_path_rate_limited" in snapshot.reason
    assert "order_path_rate_limited_persistent" not in snapshot.reason


def test_persistent_rate_limited_order_failures_become_unsafe(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "order_failure_count": 3,
            "last_order_failure_type": "rate_limit",
            "last_order_failure_message": "HTTP 429 too many requests",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=0,
    )
    guard = guard_for(tmp_path, connector, rate_limit_unsafe_threshold=3)

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.DEGRADED_UNSAFE
    assert snapshot.orders_unknown is True
    assert snapshot.quoting_enabled is False
    assert "order_path_rate_limited_persistent" in snapshot.reason


def test_open_order_count_over_cap_becomes_unsafe(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=5,
    )
    guard = guard_for(tmp_path, connector, max_open_orders=4)

    snapshot = guard.evaluate(1000)

    assert snapshot.state == ConnectivityState.DEGRADED_UNSAFE
    assert snapshot.orders_unknown is True
    assert snapshot.quoting_enabled is False
    assert "open_order_cap_exceeded" in snapshot.reason


def test_reconciliation_success_clears_rate_limit_failure_streak(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "order_failure_count": 3,
            "last_order_failure_type": "rate_limit",
            "last_order_failure_message": "HTTP 429 too many requests",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=0,
    )
    guard = guard_for(tmp_path, connector, rate_limit_unsafe_threshold=3, reconnect_required_stable_seconds=0)
    unsafe = guard.evaluate(1000)
    assert unsafe.state == ConnectivityState.DEGRADED_UNSAFE
    assert guard._rate_limit_failure_streak == 3

    connector._hb_runtime_connectivity["order_failure_count"] = 0
    connector._hb_runtime_connectivity.pop("last_order_failure_type", None)
    connector._hb_runtime_connectivity.pop("last_order_failure_message", None)

    async def run_reconciliation():
        await guard._reconcile()

    asyncio.run(run_reconciliation())

    assert guard._rate_limit_failure_streak == 0
    assert guard._last_seen_order_failure_count == 0
    assert guard.reconciliation_result == "succeeded"

    soaking = guard.evaluate(1001)
    assert soaking.state == ConnectivityState.HEALTHY
    assert soaking.orders_unknown is False
    assert soaking.quoting_enabled is True


def test_reconciliation_retries_after_transient_failure_with_backoff(tmp_path):
    current_time = {"now": 1000.0}
    connector = FlakyReconcileConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=0,
        failures_before_success=1,
    )
    guard = RuntimeConnectivityGuard(
        connectors={"hyperliquid_perpetual_testnet": connector},
        thresholds=ConnectivityThresholds(
            order_book_stale_seconds=45,
            user_stream_stale_seconds=90,
            hard_disconnect_seconds=20,
            reconnect_required_stable_seconds=0,
            reconciliation_max_retries=1,
            reconciliation_retry_backoff_seconds=2,
        ),
        store=ConnectivityStateStore(
            state_path=str(tmp_path / "state.json"),
            event_log_path=str(tmp_path / "events.jsonl"),
        ),
        time_fn=lambda: current_time["now"],
    )
    guard._last_state = ConnectivityState.HARD_DISCONNECTED
    guard.reconciliation_result = "required"

    async def run_recovery():
        first = guard.evaluate(current_time["now"])
        assert first.state == ConnectivityState.RECOVERING
        assert first.reconciliation_result == "running"

        await asyncio.sleep(0)
        assert guard.reconciliation_result == "failed"
        assert connector.balance_update_calls == 1

        current_time["now"] = 1001.0
        before_backoff = guard.evaluate(current_time["now"])
        await asyncio.sleep(0)
        assert before_backoff.reconciliation_result == "failed"
        assert connector.balance_update_calls == 1

        current_time["now"] = 1002.0
        retrying = guard.evaluate(current_time["now"])
        assert retrying.reconciliation_result == "running"
        await asyncio.sleep(0)
        assert connector.balance_update_calls == 2

        recovered = guard.evaluate(current_time["now"])
        assert recovered.state == ConnectivityState.HEALTHY
        assert recovered.reconciliation_result == "succeeded"
        assert recovered.quoting_enabled is True

    asyncio.run(run_recovery())

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "reconciliation_retry_scheduled" in [event["event_type"] for event in events]
    assert "reconciliation_succeeded" in [event["event_type"] for event in events]


def test_disconnect_while_quotes_exist_stops_executors_and_cancels(tmp_path):
    connector = FakeConnector(
        runtime={"private_ws_status": "closed", "last_ws_close_timestamp": 970},
        order_count=2,
    )
    guard = guard_for(tmp_path, connector)
    snapshot = guard.evaluate(1000)
    orchestrator = FakeOrchestrator()

    guard.apply_safety_actions([FakeExecutor()], orchestrator, FakeStopAction)

    assert snapshot.quoting_enabled is False
    assert len(orchestrator.actions) == 1
    assert connector.cancel_calls == 1


def test_cancel_path_unavailable_marks_orders_unknown(tmp_path):
    connector = FakeConnector(
        runtime={"private_ws_status": "closed", "last_ws_close_timestamp": 970},
        order_count=2,
        cancel_available=False,
    )
    guard = guard_for(tmp_path, connector)
    guard.evaluate(1000)

    guard.apply_safety_actions([FakeExecutor()], FakeOrchestrator(), FakeStopAction)

    assert guard.orders_unknown is True


def test_recovering_with_open_orders_triggers_safety_actions(tmp_path):
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "cancel_failure_count": 1,
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=2,
    )
    guard = guard_for(tmp_path, connector)
    snapshot = guard.evaluate(1000)
    orchestrator = FakeOrchestrator()

    guard.apply_safety_actions([FakeExecutor()], orchestrator, FakeStopAction)

    assert snapshot.state == ConnectivityState.RECOVERING
    assert snapshot.quoting_enabled is False
    assert len(orchestrator.actions) == 1
    assert connector.cancel_calls == 1


def test_reconciliation_retries_when_open_orders_remain(tmp_path):
    current_time = {"now": 1000.0}
    connector = FakeConnector(
        runtime={
            "public_ws_status": "connected",
            "private_ws_status": "connected",
            "rest_health": "healthy",
            "last_order_book_update_timestamp": 1000,
            "last_user_stream_update_timestamp": 1000,
        },
        order_count=2,
    )
    guard = RuntimeConnectivityGuard(
        connectors={"hyperliquid_perpetual_testnet": connector},
        thresholds=ConnectivityThresholds(
            order_book_stale_seconds=45,
            user_stream_stale_seconds=90,
            hard_disconnect_seconds=20,
            reconnect_required_stable_seconds=0,
            reconciliation_max_retries=1,
            reconciliation_retry_backoff_seconds=2,
        ),
        store=ConnectivityStateStore(
            state_path=str(tmp_path / "state.json"),
            event_log_path=str(tmp_path / "events.jsonl"),
        ),
        time_fn=lambda: current_time["now"],
    )
    guard._last_state = ConnectivityState.HARD_DISCONNECTED
    guard.reconciliation_result = "required"
    guard.orders_unknown = True

    async def run_reconciliation_cycle():
        first = guard.evaluate(current_time["now"])
        assert first.state == ConnectivityState.RECOVERING
        await asyncio.sleep(0)
        assert guard.reconciliation_result == "failed"
        assert guard._reconciliation_retry_exhausted is False

        current_time["now"] = 1001.0
        before_backoff = guard.evaluate(current_time["now"])
        await asyncio.sleep(0)
        assert before_backoff.reconciliation_result == "failed"
        assert guard._reconciliation_retry_exhausted is False

        current_time["now"] = 1003.0
        connector._order_tracker.all_updatable_orders = {}
        retrying = guard.evaluate(current_time["now"])
        assert retrying.reconciliation_result == "running"
        await asyncio.sleep(0)
        recovered = guard.evaluate(current_time["now"])
        assert recovered.reconciliation_result == "succeeded"
        assert guard._reconciliation_retry_exhausted is False

    asyncio.run(run_reconciliation_cycle())

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "reconciliation_retry_scheduled" in [event["event_type"] for event in events]
    assert "reconciliation_succeeded" in [event["event_type"] for event in events]

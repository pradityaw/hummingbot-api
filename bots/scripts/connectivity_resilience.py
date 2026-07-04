import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional


class ConnectivityState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED_TRANSIENT = "DEGRADED_TRANSIENT"
    DEGRADED_UNSAFE = "DEGRADED_UNSAFE"
    HARD_DISCONNECTED = "HARD_DISCONNECTED"
    RECOVERING = "RECOVERING"


@dataclass
class ConnectivityThresholds:
    order_book_stale_seconds: float = float(os.environ.get("HB_CONNECTIVITY_ORDER_BOOK_STALE_SECONDS", "45"))
    user_stream_stale_seconds: float = float(os.environ.get("HB_CONNECTIVITY_USER_STREAM_STALE_SECONDS", "90"))
    hard_disconnect_seconds: float = float(os.environ.get("HB_CONNECTIVITY_HARD_DISCONNECT_SECONDS", "20"))
    rest_failure_transient_threshold: int = int(os.environ.get("HB_CONNECTIVITY_REST_TRANSIENT_THRESHOLD", "1"))
    rest_failure_unsafe_threshold: int = int(os.environ.get("HB_CONNECTIVITY_REST_UNSAFE_THRESHOLD", "3"))
    reconnect_required_stable_seconds: float = float(os.environ.get("HB_CONNECTIVITY_RECONNECT_STABLE_SECONDS", "10"))
    cancel_retry_seconds: float = float(os.environ.get("HB_CONNECTIVITY_CANCEL_RETRY_SECONDS", "15"))
    reconciliation_max_retries: int = int(os.environ.get("HB_CONNECTIVITY_RECONCILIATION_MAX_RETRIES", "3"))
    reconciliation_retry_backoff_seconds: float = float(os.environ.get("HB_CONNECTIVITY_RECONCILIATION_RETRY_BACKOFF_SECONDS", "5"))
    rate_limit_unsafe_threshold: int = int(os.environ.get("HB_CONNECTIVITY_RATE_LIMIT_UNSAFE_THRESHOLD", "3"))
    max_open_orders: int = int(os.environ.get("HB_CONNECTIVITY_MAX_OPEN_ORDERS", "4"))


@dataclass
class RuntimeConnectivitySnapshot:
    exchange: str
    trading_pair: str
    state: ConnectivityState
    reason: str
    public_ws_status: str = "unknown"
    private_ws_status: str = "unknown"
    rest_health: str = "unknown"
    last_order_book_update_timestamp: Optional[float] = None
    last_user_stream_update_timestamp: Optional[float] = None
    reconnect_attempt_count: int = 0
    reconnect_duration: Optional[float] = None
    ws_close_code: Optional[str] = None
    ws_close_reason: Optional[str] = None
    open_order_count_during_disconnect: int = 0
    open_order_count: int = 0
    reconciliation_result: str = "not_started"
    readiness_reason: str = "initializing"
    watchdog_reason: str = "initializing"
    quoting_enabled: bool = False
    orders_unknown: bool = False
    last_order_failure_type: Optional[str] = None
    last_order_failure_message: Optional[str] = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exchange": self.exchange,
            "trading_pair": self.trading_pair,
            "current_state": self.state.value,
            "reason": self.reason,
            "public_ws_status": self.public_ws_status,
            "private_user_ws_status": self.private_ws_status,
            "rest_health": self.rest_health,
            "last_order_book_update_timestamp": self.last_order_book_update_timestamp,
            "last_user_stream_update_timestamp": self.last_user_stream_update_timestamp,
            "reconnect_attempt_count": self.reconnect_attempt_count,
            "reconnect_duration": self.reconnect_duration,
            "ws_close_code": self.ws_close_code,
            "ws_close_reason": self.ws_close_reason,
            "open_order_count_during_disconnect": self.open_order_count_during_disconnect,
            "open_order_count": self.open_order_count,
            "reconciliation_result": self.reconciliation_result,
            "readiness_reason": self.readiness_reason,
            "watchdog_reason": self.watchdog_reason,
            "quoting_enabled": self.quoting_enabled,
            "orders_unknown": self.orders_unknown,
            "last_order_failure_type": self.last_order_failure_type,
            "last_order_failure_message": self.last_order_failure_message,
            "updated_at": self.updated_at,
        }


class ConnectivityStateStore:
    def __init__(self, state_path: Optional[str] = None, event_log_path: Optional[str] = None):
        base_dir = Path(os.environ.get("HB_CONNECTIVITY_STATE_DIR", "/home/hummingbot/data/connectivity"))
        self.state_path = Path(state_path) if state_path else base_dir / "runtime_connectivity_state.json"
        self.event_log_path = Path(event_log_path) if event_log_path else base_dir / "runtime_connectivity_events.jsonl"

    def write_state(self, snapshot: RuntimeConnectivitySnapshot) -> None:
        payload = snapshot.to_dict()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.state_path)

    def write_event(self, event_type: str, snapshot: RuntimeConnectivitySnapshot, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = snapshot.to_dict()
        payload["event_type"] = event_type
        payload["event_at"] = time.time()
        if extra:
            payload.update(extra)
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _stringify(value: Any, default: str = "unknown") -> str:
    if value is None:
        return default
    return getattr(value, "name", str(value))


def _maybe_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


def _open_order_count(connector: Any) -> int:
    for attr in ("in_flight_orders", "limit_orders"):
        value = getattr(connector, attr, None)
        if value is not None:
            return _maybe_len(value)
    tracker = getattr(connector, "_order_tracker", None)
    if tracker is not None:
        for attr in ("all_updatable_orders", "active_orders", "all_fillable_orders"):
            value = getattr(tracker, attr, None)
            if value is not None:
                return _maybe_len(value)
    return 0


def _runtime_state(connector: Any) -> Dict[str, Any]:
    return dict(getattr(connector, "_hb_runtime_connectivity", {}) or {})


def _is_rate_limit_order_failure(failure_type: Optional[str], failure_message: Optional[str]) -> bool:
    text = " ".join(part.lower() for part in (failure_type or "", failure_message or "") if part)
    return any(
        marker in text
        for marker in (
            "rate_limit",
            "rate limit",
            "rate-limited",
            "throttled",
            "too many cumulative requests",
            "too many requests",
            "429",
        )
    )


async def _maybe_await(result: Any) -> Any:
    if hasattr(result, "__await__"):
        return await result
    return result


class RuntimeConnectivityGuard:
    def __init__(
        self,
        connectors: Dict[str, Any],
        trading_pairs_by_connector: Optional[Dict[str, Iterable[str]]] = None,
        thresholds: Optional[ConnectivityThresholds] = None,
        store: Optional[ConnectivityStateStore] = None,
        time_fn: Callable[[], float] = time.time,
    ):
        self.connectors = connectors
        self.trading_pairs_by_connector = trading_pairs_by_connector or {}
        self.thresholds = thresholds or ConnectivityThresholds()
        self.store = store or ConnectivityStateStore()
        self.time_fn = time_fn
        self.snapshot: Optional[RuntimeConnectivitySnapshot] = None
        self.reconciliation_result = "not_started"
        self.orders_unknown = False
        self._last_state: Optional[ConnectivityState] = None
        self._last_cancel_attempt = 0.0
        self._reconciliation_task: Optional[asyncio.Task] = None
        self._unsafe_started_at: Optional[float] = None
        self._reconnect_stable_started_at: Optional[float] = None
        self._reconnect_soak_required = False
        self._reconciliation_retry_count = 0
        self._next_reconciliation_attempt_at = 0.0
        self._reconciliation_retry_exhausted = False
        self._rate_limit_failure_streak = 0
        self._last_seen_order_failure_count = 0

    @property
    def quoting_enabled(self) -> bool:
        return bool(self.snapshot and self.snapshot.quoting_enabled)

    def current_state_payload(self) -> Dict[str, Any]:
        return self.snapshot.to_dict() if self.snapshot else {}

    def evaluate(self, current_timestamp: Optional[float] = None) -> RuntimeConnectivitySnapshot:
        now = float(current_timestamp if current_timestamp is not None else self.time_fn())
        snapshot = self._build_snapshot(now)
        previous = self.snapshot
        self.snapshot = snapshot
        for connector in self.connectors.values():
            setattr(connector, "_hb_runtime_quoting_enabled", snapshot.quoting_enabled)
        self.store.write_state(snapshot)
        if previous is None or previous.state != snapshot.state or previous.reason != snapshot.reason:
            self.store.write_event("state_change", snapshot, {"previous_state": previous.state.value if previous else None})
        self._last_state = snapshot.state
        return snapshot

    def _build_snapshot(self, now: float) -> RuntimeConnectivitySnapshot:
        connector_name, connector = next(iter(self.connectors.items())) if self.connectors else ("unknown", None)
        runtime = _runtime_state(connector) if connector is not None else {}
        trading_pair = self._trading_pair_for(connector_name, connector)
        exchange = _stringify(getattr(connector, "name", connector_name), connector_name)
        public_status = runtime.get("public_ws_status", "unknown")
        private_status = runtime.get("private_ws_status", "unknown")
        rest_health = runtime.get("rest_health", "unknown")
        last_order_book_update = runtime.get("last_order_book_update_timestamp")
        last_user_stream_update = runtime.get("last_user_stream_update_timestamp") or self._last_user_stream_time(connector)
        reconnect_count = int(runtime.get("reconnect_attempt_count") or 0)
        reconnect_duration = runtime.get("reconnect_duration")
        ws_close_code = runtime.get("ws_close_code")
        ws_close_reason = runtime.get("ws_close_reason")
        open_order_count = _open_order_count(connector) if connector is not None else 0
        rest_failures = int(runtime.get("rest_consecutive_failures") or 0)
        cancel_failures = int(runtime.get("cancel_failure_count") or 0)
        order_failures = int(runtime.get("order_failure_count") or 0)
        order_failure_type = runtime.get("last_order_failure_type")
        order_failure_message = runtime.get("last_order_failure_message")
        network_status = _stringify(getattr(connector, "network_status", None), "unknown").lower()
        state = ConnectivityState.HEALTHY
        reason_parts = []

        if "not_connected" in network_status or "not connected" in network_status:
            state = ConnectivityState.HARD_DISCONNECTED
            reason_parts.append("connector_network_not_connected")
        if public_status in {"closed", "error", "connecting_failed"} or private_status in {"closed", "error", "connecting_failed"}:
            closed_at = float(runtime.get("last_ws_close_timestamp") or now)
            if now - closed_at >= self.thresholds.hard_disconnect_seconds:
                state = ConnectivityState.HARD_DISCONNECTED
                reason_parts.append("websocket_hard_disconnected")
            elif state != ConnectivityState.HARD_DISCONNECTED:
                state = ConnectivityState.DEGRADED_UNSAFE
                reason_parts.append("websocket_disconnected")
        if last_order_book_update and now - float(last_order_book_update) > self.thresholds.order_book_stale_seconds:
            if state != ConnectivityState.HARD_DISCONNECTED:
                state = ConnectivityState.DEGRADED_UNSAFE
            reason_parts.append("order_book_stale")
        if last_user_stream_update and now - float(last_user_stream_update) > self.thresholds.user_stream_stale_seconds:
            if state != ConnectivityState.HARD_DISCONNECTED:
                state = ConnectivityState.DEGRADED_UNSAFE
            reason_parts.append("user_stream_stale")
        if rest_failures >= self.thresholds.rest_failure_unsafe_threshold:
            if state != ConnectivityState.HARD_DISCONNECTED:
                state = ConnectivityState.DEGRADED_UNSAFE
            reason_parts.append("rest_unavailable")
        elif rest_failures >= self.thresholds.rest_failure_transient_threshold and state == ConnectivityState.HEALTHY:
            state = ConnectivityState.DEGRADED_TRANSIENT
            reason_parts.append("rest_transient_failure")

        if cancel_failures > 0:
            reason_parts.append("order_path_failure")
            self.orders_unknown = True
            self._reconnect_soak_required = True
        if order_failures > 0:
            if _is_rate_limit_order_failure(order_failure_type, order_failure_message):
                self._update_rate_limit_failure_streak(order_failures)
                if self._rate_limit_failure_streak >= self.thresholds.rate_limit_unsafe_threshold:
                    if state != ConnectivityState.HARD_DISCONNECTED:
                        state = ConnectivityState.DEGRADED_UNSAFE
                    reason_parts.append("order_path_rate_limited_persistent")
                    self.orders_unknown = True
                    self._reconnect_soak_required = True
                else:
                    reason_parts.append("order_path_rate_limited")
                    if state == ConnectivityState.HEALTHY:
                        state = ConnectivityState.DEGRADED_TRANSIENT
            else:
                self._rate_limit_failure_streak = 0
                reason_parts.append("order_path_failure")
                self.orders_unknown = True
                self._reconnect_soak_required = True
        elif order_failures == 0:
            self._rate_limit_failure_streak = 0
            self._last_seen_order_failure_count = 0

        if open_order_count > self.thresholds.max_open_orders:
            if state != ConnectivityState.HARD_DISCONNECTED:
                state = ConnectivityState.DEGRADED_UNSAFE
            reason_parts.append("open_order_cap_exceeded")
            self.orders_unknown = True
            self._reconnect_soak_required = True

        telemetry_state = state
        transport_unsafe = telemetry_state in {ConnectivityState.DEGRADED_UNSAFE, ConnectivityState.HARD_DISCONNECTED}
        transport_stable = self._transport_telemetry_stable(
            telemetry_state,
            public_status,
            private_status,
            rest_health,
            last_order_book_update,
            last_user_stream_update,
            now,
        )

        previously_unsafe = self._last_state in {ConnectivityState.DEGRADED_UNSAFE, ConnectivityState.HARD_DISCONNECTED}
        if previously_unsafe:
            self._reconnect_soak_required = True
        reconciliation_required = (
            (previously_unsafe and self.reconciliation_result != "succeeded")
            or self.reconciliation_result in {"required", "failed", "running"}
            or self.orders_unknown
        )

        if transport_unsafe:
            self._unsafe_started_at = self._unsafe_started_at or now
            self._reset_reconnect_stability()
            self._reconnect_soak_required = True
            if self.reconciliation_result != "running":
                self._reset_reconciliation_retries()
            open_count_during_disconnect = int(runtime.get("open_order_count_during_disconnect") or open_order_count)
            self.reconciliation_result = "required"
        else:
            open_count_during_disconnect = int(runtime.get("open_order_count_during_disconnect") or 0)
            if reconciliation_required:
                self._reconnect_soak_required = True
                state = ConnectivityState.RECOVERING
                reason_parts.append("reconciliation_required")
                self._start_reconciliation(now)
            elif self._reconnect_soak_required and self.reconciliation_result == "succeeded" and not self.orders_unknown:
                state = ConnectivityState.RECOVERING
                reason_parts.append("reconnect_stabilizing")
            elif self.reconciliation_result in {"not_started", "succeeded"}:
                self._unsafe_started_at = None
                self._clear_reconnect_soak()

        if state == ConnectivityState.RECOVERING and self.reconciliation_result == "succeeded" and not self.orders_unknown:
            state = self._recovering_state_after_soak(now, transport_stable, open_order_count, reason_parts)
            if state == ConnectivityState.HEALTHY:
                reason_parts = [
                    part
                    for part in reason_parts
                    if part not in {"reconciliation_required", "reconnect_stabilizing", "reconnect_transport_unstable"}
                ]
                self._unsafe_started_at = None

        reason = ",".join(dict.fromkeys(reason_parts)) or "ok"
        unsafe = state in {ConnectivityState.DEGRADED_UNSAFE, ConnectivityState.HARD_DISCONNECTED}
        reconciliation_incomplete = state == ConnectivityState.RECOVERING or self.reconciliation_result in {"required", "running", "failed"} or self.orders_unknown
        quoting_enabled = state in {ConnectivityState.HEALTHY, ConnectivityState.DEGRADED_TRANSIENT} and not reconciliation_incomplete
        readiness_reason = "ok" if not unsafe and not reconciliation_incomplete else reason
        watchdog_reason = reason if state != ConnectivityState.HEALTHY else "ok"
        return RuntimeConnectivitySnapshot(
            exchange=exchange,
            trading_pair=trading_pair,
            state=state,
            reason=reason,
            public_ws_status=public_status,
            private_ws_status=private_status,
            rest_health=rest_health,
            last_order_book_update_timestamp=last_order_book_update,
            last_user_stream_update_timestamp=last_user_stream_update,
            reconnect_attempt_count=reconnect_count,
            reconnect_duration=reconnect_duration,
            ws_close_code=ws_close_code,
            ws_close_reason=ws_close_reason,
            open_order_count_during_disconnect=open_count_during_disconnect,
            open_order_count=open_order_count,
            reconciliation_result=self.reconciliation_result,
            readiness_reason=readiness_reason,
            watchdog_reason=watchdog_reason,
            quoting_enabled=quoting_enabled,
            orders_unknown=self.orders_unknown,
            last_order_failure_type=_stringify(order_failure_type, None) if order_failure_type is not None else None,
            last_order_failure_message=str(order_failure_message) if order_failure_message is not None else None,
            updated_at=now,
        )

    def _trading_pair_for(self, connector_name: str, connector: Any) -> str:
        configured = list(self.trading_pairs_by_connector.get(connector_name, []))
        if configured:
            return configured[0]
        for attr in ("trading_pairs", "_trading_pairs"):
            value = getattr(connector, attr, None)
            if value:
                try:
                    return list(value)[0]
                except Exception:
                    return str(value)
        return "unknown"

    def _last_user_stream_time(self, connector: Any) -> Optional[float]:
        tracker = getattr(connector, "_user_stream_tracker", None)
        data_source = getattr(tracker, "data_source", None) if tracker is not None else None
        for obj in (tracker, data_source):
            if obj is None:
                continue
            value = getattr(obj, "last_recv_time", None)
            if value:
                return float(value)
        return None

    def _transport_telemetry_stable(
        self,
        telemetry_state: ConnectivityState,
        public_status: str,
        private_status: str,
        rest_health: str,
        last_order_book_update: Optional[float],
        last_user_stream_update: Optional[float],
        now: float,
    ) -> bool:
        if telemetry_state != ConnectivityState.HEALTHY:
            return False
        if public_status in {"closed", "error", "connecting_failed"} or private_status in {"closed", "error", "connecting_failed"}:
            return False
        if rest_health not in {"unknown", "healthy", "ok"}:
            return False
        if last_order_book_update and now - float(last_order_book_update) > self.thresholds.order_book_stale_seconds:
            return False
        if last_user_stream_update and now - float(last_user_stream_update) > self.thresholds.user_stream_stale_seconds:
            return False
        return True

    def _recovering_state_after_soak(
        self,
        now: float,
        transport_stable: bool,
        open_order_count: int,
        reason_parts: list,
    ) -> ConnectivityState:
        if open_order_count > 0:
            self.orders_unknown = True
            self.reconciliation_result = "failed"
            self._reset_reconnect_stability()
            reason_parts.append("open_orders_after_reconciliation")
            return ConnectivityState.RECOVERING
        if not transport_stable:
            self._reset_reconnect_stability()
            reason_parts.append("reconnect_transport_unstable")
            return ConnectivityState.RECOVERING
        if self._reconnect_stable_started_at is None:
            self._reconnect_stable_started_at = now
        stable_seconds = max(0.0, float(self.thresholds.reconnect_required_stable_seconds))
        if now - self._reconnect_stable_started_at >= stable_seconds:
            self._clear_reconnect_soak()
            return ConnectivityState.HEALTHY
        reason_parts.append("reconnect_stabilizing")
        return ConnectivityState.RECOVERING

    def _update_rate_limit_failure_streak(self, order_failures: int) -> None:
        if order_failures > self._last_seen_order_failure_count:
            self._rate_limit_failure_streak += order_failures - self._last_seen_order_failure_count
        self._last_seen_order_failure_count = order_failures

    def _reset_reconnect_stability(self) -> None:
        self._reconnect_stable_started_at = None

    def _clear_reconnect_soak(self) -> None:
        self._reset_reconnect_stability()
        self._reconnect_soak_required = False

    def _reset_reconciliation_retries(self) -> None:
        self._reconciliation_retry_count = 0
        self._next_reconciliation_attempt_at = 0.0
        self._reconciliation_retry_exhausted = False

    def _start_reconciliation(self, now: float) -> None:
        if self._reconciliation_task is not None and not self._reconciliation_task.done():
            return
        if self._reconciliation_retry_exhausted:
            return
        if self.reconciliation_result == "failed" and now < self._next_reconciliation_attempt_at:
            return
        self.reconciliation_result = "running"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reconciliation_task = loop.create_task(self._reconcile())

    async def _reconcile(self) -> None:
        errors = []
        for connector_name, connector in self.connectors.items():
            for method_name in ("_update_balances", "_update_order_status", "_update_positions"):
                method = getattr(connector, method_name, None)
                if method is None:
                    continue
                try:
                    await _maybe_await(method())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors.append(f"{connector_name}.{method_name}:{type(exc).__name__}:{exc}")
        if errors:
            self.reconciliation_result = "failed"
            self._reconciliation_retry_count += 1
            retry_backoff = max(0.0, float(self.thresholds.reconciliation_retry_backoff_seconds))
            retry_delay = retry_backoff * self._reconciliation_retry_count
            retries_remaining = max(0, int(self.thresholds.reconciliation_max_retries) - self._reconciliation_retry_count + 1)
            if retries_remaining > 0:
                self._next_reconciliation_attempt_at = self.time_fn() + retry_delay
                if self.snapshot:
                    self.store.write_event(
                        "reconciliation_retry_scheduled",
                        self.snapshot,
                        {
                            "reconciliation_errors": errors,
                            "reconciliation_retry_count": self._reconciliation_retry_count,
                            "next_reconciliation_attempt_at": self._next_reconciliation_attempt_at,
                            "reconciliation_retries_remaining": retries_remaining,
                        },
                    )
                return
            self._reconciliation_retry_exhausted = True
            if self.snapshot:
                self.store.write_event(
                    "reconciliation_failed",
                    self.snapshot,
                    {
                        "reconciliation_errors": errors,
                        "reconciliation_retry_count": self._reconciliation_retry_count,
                        "reconciliation_retries_exhausted": True,
                    },
                )
            return
        remaining_open_orders = sum(_open_order_count(connector) for connector in self.connectors.values())
        if remaining_open_orders > 0:
            self.reconciliation_result = "failed"
            self.orders_unknown = True
            self._reconciliation_retry_count += 1
            retry_backoff = max(0.0, float(self.thresholds.reconciliation_retry_backoff_seconds))
            retry_delay = retry_backoff * self._reconciliation_retry_count
            self._next_reconciliation_attempt_at = self.time_fn() + retry_delay
            if self.snapshot:
                self.store.write_event(
                    "reconciliation_retry_scheduled",
                    self.snapshot,
                    {
                        "remaining_open_order_count": remaining_open_orders,
                        "reconciliation_retry_count": self._reconciliation_retry_count,
                        "next_reconciliation_attempt_at": self._next_reconciliation_attempt_at,
                        "reconciliation_retries_remaining": -1,
                    },
                )
            return
        self.reconciliation_result = "succeeded"
        self.orders_unknown = False
        self._rate_limit_failure_streak = 0
        self._last_seen_order_failure_count = 0
        self._reset_reconciliation_retries()
        for connector in self.connectors.values():
            runtime = getattr(connector, "_hb_runtime_connectivity", None)
            if isinstance(runtime, dict):
                runtime["cancel_failure_count"] = 0
                runtime["order_failure_count"] = 0
                runtime["rate_limit_failure_count"] = 0
                runtime["open_order_count_during_disconnect"] = 0
        if self.snapshot:
            self.store.write_event("reconciliation_succeeded", self.snapshot)

    def _should_apply_safety_actions(self) -> bool:
        if not self.snapshot:
            return False
        if self.snapshot.state in {ConnectivityState.DEGRADED_UNSAFE, ConnectivityState.HARD_DISCONNECTED}:
            return True
        if self.snapshot.state != ConnectivityState.RECOVERING:
            return False
        reconciliation_incomplete = self.reconciliation_result in {"required", "running", "failed"}
        return reconciliation_incomplete or self.orders_unknown or self.snapshot.orders_unknown

    def apply_safety_actions(self, executors: Iterable[Any], executor_orchestrator: Any, stop_action_cls: Any) -> None:
        if not self._should_apply_safety_actions():
            return
        now = self.time_fn()
        actions = []
        for executor in executors:
            status = _stringify(getattr(executor, "status", None), "unknown")
            if status.lower() in {"running", "not_started"}:
                actions.append(stop_action_cls(controller_id=getattr(executor, "controller_id", None), executor_id=getattr(executor, "id", None)))
        if actions and executor_orchestrator is not None:
            executor_orchestrator.execute_actions(actions)
        if now - self._last_cancel_attempt < self.thresholds.cancel_retry_seconds:
            return
        self._last_cancel_attempt = now
        for connector in self.connectors.values():
            cancel_all = getattr(connector, "cancel_all", None)
            if cancel_all is None:
                self.orders_unknown = True
                continue
            try:
                result = cancel_all(timeout_seconds=10)
                if hasattr(result, "__await__"):
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
            except Exception:
                self.orders_unknown = True

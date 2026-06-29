import time
from typing import Any, Dict, Optional, Tuple


def ensure_runtime_state(connector: Any) -> Dict[str, Any]:
    state = getattr(connector, "_hb_runtime_connectivity", None)
    if not isinstance(state, dict):
        state = {
            "public_ws_status": "unknown",
            "private_ws_status": "unknown",
            "rest_health": "unknown",
            "rest_consecutive_failures": 0,
            "reconnect_attempt_count": 0,
            "reconnect_duration": None,
            "ws_close_code": None,
            "ws_close_reason": None,
            "last_order_book_update_timestamp": None,
            "last_user_stream_update_timestamp": None,
            "last_ws_close_timestamp": None,
            "open_order_count_during_disconnect": 0,
            "cancel_failure_count": 0,
            "order_failure_count": 0,
        }
        setattr(connector, "_hb_runtime_connectivity", state)
    return state


def _open_order_count(connector: Any) -> int:
    tracker = getattr(connector, "_order_tracker", None)
    for value in (
        getattr(connector, "in_flight_orders", None),
        getattr(tracker, "all_updatable_orders", None) if tracker is not None else None,
        getattr(tracker, "all_fillable_orders", None) if tracker is not None else None,
    ):
        if value is not None:
            try:
                return len(value)
            except Exception:
                return 0
    return 0


def extract_ws_close_metadata(websocket_assistant: Any) -> Tuple[Optional[str], Optional[str]]:
    if websocket_assistant is None:
        return None, None

    candidates = [
        websocket_assistant,
        getattr(websocket_assistant, "_connection", None),
        getattr(websocket_assistant, "_websocket", None),
    ]
    for candidate in list(candidates):
        if candidate is not None:
            candidates.append(getattr(candidate, "_websocket", None))
            candidates.append(getattr(candidate, "websocket", None))

    close_code = None
    close_reason = None
    for candidate in candidates:
        if candidate is None:
            continue
        if close_code is None:
            for attribute in ("close_code", "closed_code", "code"):
                value = getattr(candidate, attribute, None)
                if value is not None:
                    close_code = str(value)
                    break
        if close_reason is None:
            for attribute in ("close_reason", "closed_reason", "reason"):
                value = getattr(candidate, attribute, None)
                if value:
                    close_reason = str(value)
                    break
        if close_code is not None and close_reason is not None:
            break
    return close_code, close_reason


def record_ws_connecting(connector: Any, stream: str) -> None:
    state = ensure_runtime_state(connector)
    state[f"{stream}_ws_status"] = "connecting"
    state[f"{stream}_connect_started_at"] = time.time()
    state["reconnect_attempt_count"] = int(state.get("reconnect_attempt_count") or 0) + 1


def record_ws_connected(connector: Any, stream: str) -> None:
    state = ensure_runtime_state(connector)
    now = time.time()
    started_at = state.get(f"{stream}_connect_started_at")
    state[f"{stream}_ws_status"] = "connected"
    if started_at:
        state["reconnect_duration"] = round(now - float(started_at), 3)


def record_ws_closed(connector: Any, stream: str, exc: Optional[BaseException] = None, code: Optional[str] = None, reason: Optional[str] = None) -> None:
    state = ensure_runtime_state(connector)
    state[f"{stream}_ws_status"] = "closed"
    state["last_ws_close_timestamp"] = time.time()
    state["ws_close_code"] = code
    state["ws_close_reason"] = reason or (f"{type(exc).__name__}: {exc}" if exc else None)
    state["open_order_count_during_disconnect"] = max(int(state.get("open_order_count_during_disconnect") or 0), _open_order_count(connector))


def record_order_book_update(connector: Any, timestamp: Optional[float] = None) -> None:
    state = ensure_runtime_state(connector)
    state["last_order_book_update_timestamp"] = float(timestamp or time.time())
    state["public_ws_status"] = "connected"


def record_user_stream_update(connector: Any, timestamp: Optional[float] = None) -> None:
    state = ensure_runtime_state(connector)
    state["last_user_stream_update_timestamp"] = float(timestamp or time.time())
    state["private_ws_status"] = "connected"


def record_rest_success(connector: Any, path_url: str) -> None:
    state = ensure_runtime_state(connector)
    state["rest_health"] = "healthy"
    state["rest_consecutive_failures"] = 0
    state["last_rest_success_timestamp"] = time.time()
    state["last_rest_path_url"] = path_url


def record_rest_failure(connector: Any, path_url: str, exc: BaseException) -> None:
    state = ensure_runtime_state(connector)
    state["rest_health"] = "unhealthy"
    state["rest_consecutive_failures"] = int(state.get("rest_consecutive_failures") or 0) + 1
    state["last_rest_failure_timestamp"] = time.time()
    state["last_rest_path_url"] = path_url
    state["last_rest_error"] = f"{type(exc).__name__}: {exc}"


def record_cancel_failure(connector: Any, failure_type: Optional[str] = None, failure_message: Optional[str] = None) -> None:
    state = ensure_runtime_state(connector)
    state["cancel_failure_count"] = int(state.get("cancel_failure_count") or 0) + 1
    state["last_cancel_failure_type"] = failure_type
    state["last_cancel_failure_message"] = failure_message
    state["open_order_count_during_disconnect"] = max(int(state.get("open_order_count_during_disconnect") or 0), _open_order_count(connector))


def record_order_failure(connector: Any, failure_type: Optional[str] = None, failure_message: Optional[str] = None) -> None:
    state = ensure_runtime_state(connector)
    state["order_failure_count"] = int(state.get("order_failure_count") or 0) + 1
    state["last_order_failure_type"] = failure_type
    state["last_order_failure_message"] = failure_message
    state["open_order_count_during_disconnect"] = max(int(state.get("open_order_count_during_disconnect") or 0), _open_order_count(connector))

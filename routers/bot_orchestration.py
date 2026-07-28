import asyncio
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from database import AsyncDatabaseManager, BotRunRepository
from deps import get_bot_archiver, get_bots_orchestrator, get_database_manager, get_docker_service
from models import StartBotAction, StopBotAction, V2ControllerDeployment, V2ScriptDeployment
from services.bots_orchestrator import BotsOrchestrator
from services.docker_service import DockerService
from utils.bot_archiver import BotArchiver
from utils.file_system import fs_util
from utils.mainnet_guard import (
    MainnetConnectorBlockedError,
    extract_connector_names_from_mapping,
    validate_testnet_connectors,
)

# Create module-specific logger
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Bot Orchestration"], prefix="/bot-orchestration")

ACTIVE_ORDER_STATUSES = {
    "BuyOrderCreated",
    "SellOrderCreated",
    "OrderCreated",
    "OPEN",
    "PENDING_CREATE",
    "PENDING_CANCEL",
    "PARTIALLY_FILLED",
}

BOT_DB_DECIMAL_SCALE = Decimal("1000000")


def _bot_instance_dir(bot_name: str) -> Path:
    return Path("bots") / "instances" / bot_name


def _bot_sqlite_path(bot_name: str) -> Path:
    return _bot_instance_dir(bot_name) / "data" / f"{bot_name}.sqlite"


def _bot_connectivity_dir(bot_name: str) -> Path:
    return _bot_instance_dir(bot_name) / "data" / "connectivity"


def _bot_connectivity_state_path(bot_name: str) -> Path:
    return _bot_connectivity_dir(bot_name) / "runtime_connectivity_state.json"


def _bot_connectivity_events_path(bot_name: str) -> Path:
    return _bot_connectivity_dir(bot_name) / "runtime_connectivity_events.jsonl"


def _read_json_path(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _connectivity_from_status(status: dict) -> Optional[dict]:
    for report in status.get("performance", {}).values():
        custom_info = report.get("custom_info", {}) if isinstance(report, dict) else {}
        connectivity = custom_info.get("runtime_connectivity")
        if connectivity:
            return connectivity
    return None


def _read_bot_connectivity_state(bot_name: str, status: Optional[dict] = None) -> dict:
    payload = _read_json_path(_bot_connectivity_state_path(bot_name))
    if payload is not None:
        return payload
    if status is not None:
        payload = _connectivity_from_status(status)
        if payload is not None:
            return payload
    return {
        "current_state": "UNKNOWN",
        "reason": "runtime_connectivity_state_unavailable",
        "readiness_reason": "runtime_connectivity_state_unavailable",
        "watchdog_reason": "runtime_connectivity_state_unavailable",
        "quoting_enabled": False,
    }


def _read_bot_connectivity_events(bot_name: str, limit: int) -> list:
    path = _bot_connectivity_events_path(bot_name)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _normalize_db_decimal(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return format((Decimal(value) / BOT_DB_DECIMAL_SCALE).normalize(), "f")


def _read_bot_orders_from_sqlite(bot_name: str, active_only: bool, limit: int) -> dict:
    sqlite_path = _bot_sqlite_path(bot_name)
    if not sqlite_path.exists():
        raise HTTPException(status_code=404, detail=f"Order database not found for bot '{bot_name}'")

    status_filter = ""
    params = []
    if active_only:
        placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        status_filter = f"where last_status in ({placeholders})"
        params.extend(sorted(ACTIVE_ORDER_STATUSES))
    params.append(limit)

    query = f"""
        select
            id,
            config_file_path,
            strategy,
            market,
            symbol,
            base_asset,
            quote_asset,
            creation_timestamp,
            order_type,
            amount,
            leverage,
            price,
            last_status,
            last_update_timestamp,
            exchange_order_id,
            position
        from "Order"
        {status_filter}
        order by creation_timestamp desc, last_update_timestamp desc
        limit ?
    """

    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        total_orders = conn.execute('select count(*) from "Order"').fetchone()[0]
        active_orders = conn.execute(
            f'select count(*) from "Order" where last_status in ({",".join("?" for _ in ACTIVE_ORDER_STATUSES)})',
            sorted(ACTIVE_ORDER_STATUSES),
        ).fetchone()[0]

    orders = []
    for row in rows:
        order = dict(row)
        order["amount_normalized"] = _normalize_db_decimal(order.get("amount"))
        order["price_normalized"] = _normalize_db_decimal(order.get("price"))
        orders.append(order)

    return {
        "sqlite_path": str(sqlite_path),
        "active_only": active_only,
        "total_orders": total_orders,
        "active_order_count": active_orders,
        "orders": orders,
    }


def _collect_controller_connector_names(controller_config_names: list[str]) -> set[str]:
    connector_names: set[str] = set()
    for controller in controller_config_names:
        config_name = controller if controller.endswith(".yml") else f"{controller}.yml"
        try:
            config = fs_util.read_yaml_file(f"conf/controllers/{config_name}")
        except FileNotFoundError:
            raise HTTPException(
                status_code=400,
                detail=f"Controller configuration '{config_name}' not found",
            )
        connector_names.update(extract_connector_names_from_mapping(config))
    return connector_names


def _validate_deployment_connector_names(controller_config_names: list[str]) -> None:
    connector_names = _collect_controller_connector_names(controller_config_names)
    try:
        validate_testnet_connectors(connector_names)
    except MainnetConnectorBlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _validate_script_config_connector_names(script_config: Optional[str]) -> None:
    """
    Mainnet guard for deploy-v2-script and MQTT start-bot: extract connector
    names from the script config (and any controller configs it references)
    and enforce the testnet-only policy. A referenced config that cannot be
    read fails closed (400) — an unverifiable config is not deployable.
    """
    if not script_config:
        return
    config_name = script_config if script_config.endswith(".yml") else f"{script_config}.yml"
    try:
        script_cfg = fs_util.read_yaml_file(f"conf/scripts/{config_name}")
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"Script configuration '{config_name}' not found",
        )
    connector_names = extract_connector_names_from_mapping(script_cfg)
    controllers = script_cfg.get("controllers_config") or []
    if isinstance(controllers, str):
        controllers = [controllers]
    connector_names.update(_collect_controller_connector_names(list(controllers)))
    try:
        validate_testnet_connectors(connector_names)
    except MainnetConnectorBlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _extract_recent_order_events(bot_name: str, limit: int) -> list:
    log_path = _bot_instance_dir(bot_name) / "logs" / f"logs_{bot_name}.log"
    if not log_path.exists():
        return []

    events = []
    with log_path.open("r", encoding="utf-8", errors="replace") as log_file:
        for line in log_file:
            marker = "EVENT_LOG - "
            if marker not in line:
                continue
            try:
                event = json.loads(line.split(marker, 1)[1])
            except json.JSONDecodeError:
                continue
            event_name = event.get("event_name", "")
            if "Order" in event_name:
                events.append(event)

    return events[-limit:]


@router.get("/status")
def get_active_bots_status(bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get the status of all active bots.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with status and data containing all active bot statuses
    """
    return {"status": "success", "data": bots_manager.get_all_bots_status()}


@router.get("/mqtt")
def get_mqtt_status(bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get MQTT connection status and discovered bots.

    Args:
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with MQTT connection status, discovered bots, and broker information
    """
    mqtt_connected = bots_manager.mqtt_manager.is_connected
    discovered_bots = bots_manager.mqtt_manager.get_discovered_bots()
    active_bots = list(bots_manager.active_bots.keys())

    # Check client state
    client_state = "connected" if bots_manager.mqtt_manager.is_connected else "disconnected"

    return {
        "status": "success",
        "data": {
            "mqtt_connected": mqtt_connected,
            "discovered_bots": discovered_bots,
            "active_bots": active_bots,
            "broker_host": bots_manager.broker_host,
            "broker_port": bots_manager.broker_port,
            "broker_username": bots_manager.broker_username,
            "client_state": client_state
        }
    }


@router.get("/controller-performance-latest")
async def get_latest_controller_performance(
    bot_name: str = None,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get the most recent performance snapshot for each bot/controller.
    Optionally filter by bot_name.
    """
    try:
        snapshots = await bots_manager.get_latest_controller_performance(bot_name=bot_name)
        return {"status": "success", "data": snapshots}
    except Exception as e:
        logger.error(f"Failed to get latest controller performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/controller-performance-history")
async def get_controller_performance_history(
    bot_name: str = None,
    controller_id: str = None,
    limit: int = Query(default=100, le=1000),
    cursor: str = None,
    start_time: str = None,
    end_time: str = None,
    interval: str = Query(default="5m", pattern="^(5m|15m|30m|1h|4h|12h|1d)$"),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get historical controller performance snapshots with pagination and interval sampling.
    """
    try:
        parsed_start = datetime.fromisoformat(start_time) if start_time else None
        parsed_end = datetime.fromisoformat(end_time) if end_time else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {e}")

    try:
        history, next_cursor, has_more = await bots_manager.get_controller_performance_history(
            bot_name=bot_name,
            controller_id=controller_id,
            limit=limit,
            cursor=cursor,
            start_time=parsed_start,
            end_time=parsed_end,
            interval=interval
        )
        return {
            "status": "success",
            "data": history,
            "pagination": {
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
                "interval": interval,
            }
        }
    except Exception as e:
        logger.error(f"Failed to get controller performance history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{bot_name}/status")
def get_bot_status(bot_name: str, bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get the status of a specific bot.

    Args:
        bot_name: Name of the bot to get status for
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with bot status information

    Raises:
        HTTPException: 404 if bot not found
    """
    response = bots_manager.get_bot_status(bot_name)
    if not response:
        raise HTTPException(status_code=404, detail="Bot not found")
    return {
        "status": "success",
        "data": response
    }


@router.get("/{bot_name}/health")
def get_bot_health(bot_name: str, bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    """
    Get a compact operational health view for one bot.
    """
    status = bots_manager.get_bot_status(bot_name)
    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Bot not found")

    orders = _read_bot_orders_from_sqlite(bot_name, active_only=True, limit=20)
    connectivity = _read_bot_connectivity_state(bot_name, status)
    return {
        "status": "success",
        "data": {
            "bot_name": bot_name,
            "bot_status": status.get("status"),
            "recently_active": status.get("recently_active", False),
            "error_log_count": len(status.get("error_logs", [])),
            "general_log_count": len(status.get("general_logs", [])),
            "controllers": list(status.get("performance", {}).keys()),
            "active_order_count": orders["active_order_count"],
            "order_database": orders["sqlite_path"],
            "source": bots_manager.active_bots.get(bot_name, {}).get("source", "unknown"),
            "connectivity": connectivity,
            "readiness_reason": connectivity.get("readiness_reason"),
            "watchdog_reason": connectivity.get("watchdog_reason"),
            "quoting_enabled": connectivity.get("quoting_enabled", False),
        },
    }


@router.get("/{bot_name}/connectivity")
def get_bot_connectivity(bot_name: str, bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)):
    status = bots_manager.get_bot_status(bot_name)
    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Bot not found")
    return {
        "status": "success",
        "data": {
            "bot_name": bot_name,
            "connectivity": _read_bot_connectivity_state(bot_name, status),
        },
    }


@router.get("/{bot_name}/connectivity/events")
def get_bot_connectivity_events(
    bot_name: str,
    limit: int = Query(default=100, ge=1, le=1000),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    status = bots_manager.get_bot_status(bot_name)
    if status.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Bot not found")
    return {
        "status": "success",
        "data": {
            "bot_name": bot_name,
            "events": _read_bot_connectivity_events(bot_name, limit),
        },
    }


@router.get("/{bot_name}/orders")
def get_bot_orders(
    bot_name: str,
    active_only: bool = True,
    limit: int = Query(default=20, ge=1, le=200),
    event_limit: int = Query(default=20, ge=0, le=200),
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
):
    """
    Inspect orders recorded by a running Hummingbot instance.

    This is bot-scoped and reads the instance recorder database, so it can see
    orders created by headless controller bots that are not owned by the API's
    account-level connector cache.
    """
    if bot_name not in bots_manager.active_bots:
        raise HTTPException(status_code=404, detail="Bot not found")

    orders = _read_bot_orders_from_sqlite(bot_name, active_only=active_only, limit=limit)
    return {
        "status": "success",
        "data": {
            "bot_name": bot_name,
            **orders,
            "recent_order_events": _extract_recent_order_events(bot_name, event_limit),
        },
    }


@router.get("/{bot_name}/history")
async def get_bot_history(
    bot_name: str,
    days: int = 0,
    verbose: bool = False,
    precision: int = None,
    timeout: float = 30.0,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator)
):
    """
    Get trading history for a bot with optional parameters.

    Args:
        bot_name: Name of the bot to get history for
        days: Number of days of history to retrieve (0 for all)
        verbose: Whether to include verbose output
        precision: Decimal precision for numerical values
        timeout: Timeout in seconds for the operation
        bots_manager: Bot orchestrator service dependency

    Returns:
        Dictionary with bot trading history
    """
    response = await bots_manager.get_bot_history(
        bot_name,
        days=days,
        verbose=verbose,
        precision=precision,
        timeout=timeout
    )
    return {"status": "success", "response": response}


@router.post("/start-bot")
async def start_bot(
    action: StartBotAction,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Start a bot with the specified configuration.

    Args:
        action: StartBotAction containing bot configuration parameters
        bots_manager: Bot orchestrator service dependency
        db_manager: Database manager dependency

    Returns:
        Dictionary with status and response from bot start operation
    """
    _validate_script_config_connector_names(action.conf)
    response = await bots_manager.start_bot(
        action.bot_name, log_level=action.log_level, script=action.script,
        conf=action.conf, async_backend=action.async_backend
    )

    # Bot run tracking simplified - only track deployment and stop times

    return {"status": "success", "response": response}


@router.post("/stop-bot")
async def stop_bot(
    action: StopBotAction,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Stop a bot with the specified configuration.

    Args:
        action: StopBotAction containing bot stop parameters
        bots_manager: Bot orchestrator service dependency
        db_manager: Database manager dependency

    Returns:
        Dictionary with status and response from bot stop operation
    """
    # Capture final status BEFORE stopping (performance data is cleared on stop)
    final_status = None
    try:
        final_status = bots_manager.get_bot_status(action.bot_name)
        logger.info(f"Captured final status for {action.bot_name} before stopping")
    except Exception as e:
        logger.warning(f"Failed to capture final status for {action.bot_name}: {e}")

    response = await bots_manager.stop_bot(
        action.bot_name, skip_order_cancellation=action.skip_order_cancellation,
        async_backend=action.async_backend
    )

    # Update bot run status to STOPPED if stop was successful
    if response.get("success"):
        try:
            async with db_manager.get_session_context() as session:
                bot_run_repo = BotRunRepository(session)
                await bot_run_repo.update_bot_run_stopped(
                    action.bot_name,
                    final_status=final_status
                )
                logger.info(f"Updated bot run status to STOPPED for {action.bot_name}")
        except Exception as e:
            logger.error(f"Failed to update bot run status: {e}")
            # Don't fail the stop operation if bot run update fails

    return {"status": "success", "response": response}


@router.get("/bot-runs")
async def get_bot_runs(
    bot_name: str = None,
    account_name: str = None,
    strategy_type: str = None,
    strategy_name: str = None,
    run_status: str = None,
    deployment_status: str = None,
    limit: int = 100,
    offset: int = 0,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Get bot runs with optional filtering.

    Args:
        bot_name: Filter by bot name
        account_name: Filter by account name
        strategy_type: Filter by strategy type (script or controller)
        strategy_name: Filter by strategy name
        run_status: Filter by run status (CREATED, RUNNING, STOPPED, ERROR)
        deployment_status: Filter by deployment status (DEPLOYED, FAILED, ARCHIVED)
        limit: Maximum number of results to return
        offset: Number of results to skip
        db_manager: Database manager dependency

    Returns:
        List of bot runs with their details
    """
    try:
        async with db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_runs = await bot_run_repo.get_bot_runs(
                bot_name=bot_name,
                account_name=account_name,
                strategy_type=strategy_type,
                strategy_name=strategy_name,
                run_status=run_status,
                deployment_status=deployment_status,
                limit=limit,
                offset=offset
            )

            # Convert bot runs to dictionaries for JSON serialization
            runs_data = []
            for run in bot_runs:
                run_dict = {
                    "id": run.id,
                    "bot_name": run.bot_name,
                    "instance_name": run.instance_name,
                    "deployed_at": run.deployed_at.isoformat() if run.deployed_at else None,
                    "stopped_at": run.stopped_at.isoformat() if run.stopped_at else None,
                    "strategy_type": run.strategy_type,
                    "strategy_name": run.strategy_name,
                    "config_name": run.config_name,
                    "account_name": run.account_name,
                    "image_version": run.image_version,
                    "deployment_status": run.deployment_status,
                    "run_status": run.run_status,
                    "deployment_config": run.deployment_config,
                    "final_status": run.final_status,
                    "error_message": run.error_message
                }
                runs_data.append(run_dict)

            return {
                "status": "success",
                "data": runs_data,
                "total": len(runs_data),
                "limit": limit,
                "offset": offset
            }
    except Exception as e:
        logger.error(f"Failed to get bot runs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bot-runs/stats")
async def get_bot_run_stats(
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Get statistics about bot runs.

    Args:
        db_manager: Database manager dependency

    Returns:
        Bot run statistics
    """
    try:
        async with db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            stats = await bot_run_repo.get_bot_run_stats()

            return {"status": "success", "data": stats}
    except Exception as e:
        logger.error(f"Failed to get bot run stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bot-runs/{bot_run_id}")
async def get_bot_run_by_id(
    bot_run_id: int,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Get a specific bot run by ID.

    Args:
        bot_run_id: ID of the bot run
        db_manager: Database manager dependency

    Returns:
        Bot run details

    Raises:
        HTTPException: 404 if bot run not found
    """
    try:
        async with db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_run = await bot_run_repo.get_bot_run_by_id(bot_run_id)

            if not bot_run:
                raise HTTPException(status_code=404, detail=f"Bot run {bot_run_id} not found")

            run_dict = {
                "id": bot_run.id,
                "bot_name": bot_run.bot_name,
                "instance_name": bot_run.instance_name,
                "deployed_at": bot_run.deployed_at.isoformat() if bot_run.deployed_at else None,
                "stopped_at": bot_run.stopped_at.isoformat() if bot_run.stopped_at else None,
                "strategy_type": bot_run.strategy_type,
                "strategy_name": bot_run.strategy_name,
                "config_name": bot_run.config_name,
                "account_name": bot_run.account_name,
                "image_version": bot_run.image_version,
                "deployment_status": bot_run.deployment_status,
                "run_status": bot_run.run_status,
                "deployment_config": bot_run.deployment_config,
                "final_status": bot_run.final_status,
                "error_message": bot_run.error_message
            }

            return {"status": "success", "data": run_dict}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get bot run {bot_run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/bot-runs/{bot_run_id}")
async def delete_bot_run(
    bot_run_id: int,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Delete a bot run record by ID.

    Args:
        bot_run_id: ID of the bot run to delete
        db_manager: Database manager dependency

    Returns:
        Confirmation of deletion

    Raises:
        HTTPException: 404 if bot run not found
    """
    try:
        async with db_manager.get_session_context() as session:
            bot_run_repo = BotRunRepository(session)
            bot_run = await bot_run_repo.delete_bot_run(bot_run_id)

            if not bot_run:
                raise HTTPException(status_code=404, detail=f"Bot run {bot_run_id} not found")

            # Also delete the archived bot folder if it exists
            archived_dir = os.path.join('bots', 'archived', bot_run.instance_name)
            archived_deleted = False
            if os.path.isdir(archived_dir):
                try:
                    import subprocess, platform
                    if platform.system() == 'Darwin':
                        # Strip macOS ACLs (Docker adds "deny delete" ACLs)
                        subprocess.run(['chmod', '-R', '-N', archived_dir], check=False)
                    shutil.rmtree(archived_dir)
                    archived_deleted = True
                    logger.info(f"Deleted archived folder: {archived_dir}")
                except Exception as e:
                    logger.warning(f"Failed to delete archived folder {archived_dir}: {e}")

            return {
                "status": "success",
                "message": f"Bot run {bot_run_id} deleted successfully",
                "bot_name": bot_run.bot_name,
                "archived_folder_deleted": archived_deleted
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete bot run {bot_run_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _background_stop_and_archive(
    bot_name: str,
    container_name: str,
    bot_name_for_orchestrator: str,
    skip_order_cancellation: bool,
    archive_locally: bool,
    s3_bucket: str,
    bots_manager: BotsOrchestrator,
    docker_manager: DockerService,
    bot_archiver: BotArchiver,
    db_manager: AsyncDatabaseManager
):
    """Background task to handle the stop and archive process"""
    try:
        logger.info(f"Starting background stop-and-archive for {bot_name}")

        # Step 1: Capture bot final status before stopping (while bot is still running)
        logger.info(f"Capturing final status for {bot_name_for_orchestrator}")
        final_status = None
        try:
            final_status = bots_manager.get_bot_status(bot_name_for_orchestrator)
            logger.info(f"Captured final status for {bot_name_for_orchestrator}: {final_status}")
        except Exception as e:
            logger.warning(f"Failed to capture final status for {bot_name_for_orchestrator}: {e}")

        # Step 2: Update bot run with stopped_at timestamp and final status before stopping
        try:
            async with db_manager.get_session_context() as session:
                bot_run_repo = BotRunRepository(session)
                await bot_run_repo.update_bot_run_stopped(
                    bot_name,
                    final_status=final_status
                )
                logger.info(f"Updated bot run with stopped_at timestamp and final status for {bot_name}")
        except Exception as e:
            logger.error(f"Failed to update bot run with stopped status: {e}")
            # Continue with stop process even if database update fails

        # Step 3: Mark the bot as stopping, and stop the bot trading process
        bots_manager.set_bot_stopping(bot_name_for_orchestrator)
        logger.info(f"Stopping bot trading process for {bot_name_for_orchestrator}")
        stop_response = await bots_manager.stop_bot(
            bot_name_for_orchestrator,
            skip_order_cancellation=skip_order_cancellation,
            async_backend=True  # Always use async for background tasks
        )

        if not stop_response or not stop_response.get("success", False):
            error_msg = stop_response.get('error', 'Unknown error') if stop_response else 'No response from bot orchestrator'
            logger.error(f"Failed to stop bot process: {error_msg}")
            return

        # Step 4: Wait for graceful shutdown (15 seconds as requested)
        logger.info(f"Waiting 15 seconds for bot {bot_name} to gracefully shutdown")
        await asyncio.sleep(15)

        # Step 5: Stop the container with monitoring
        max_retries = 10
        retry_interval = 2
        container_stopped = False

        for i in range(max_retries):
            logger.info(f"Attempting to stop container {container_name} (attempt {i+1}/{max_retries})")
            docker_manager.stop_container(container_name)

            # Check if container is already stopped
            container_status = docker_manager.get_container_status(container_name)
            if container_status.get("state", {}).get("status") == "exited":
                container_stopped = True
                logger.info(f"Container {container_name} is already stopped")
                break

            await asyncio.sleep(retry_interval)

        if not container_stopped:
            logger.error(f"Failed to stop container {container_name} after {max_retries} attempts")
            return

        # Step 6: Archive the bot data
        instance_dir = os.path.join('bots', 'instances', container_name)
        logger.info(f"Archiving bot data from {instance_dir}")

        try:
            if archive_locally:
                bot_archiver.archive_locally(container_name, instance_dir)
            else:
                bot_archiver.archive_and_upload(container_name, instance_dir, bucket_name=s3_bucket)
            logger.info(f"Successfully archived bot data for {container_name}")
        except Exception as e:
            logger.error(f"Archive failed: {str(e)}")
            # Continue with removal even if archive fails

        # Step 7: Remove the container
        logging.info(f"Removing container {container_name}")
        remove_response = docker_manager.remove_container(container_name, force=False)

        if not remove_response.get("success"):
            # If graceful remove fails, try force remove
            logging.warning("Graceful container removal failed, attempting force removal")
            remove_response = docker_manager.remove_container(container_name, force=True)

        if remove_response.get("success"):
            logging.info(f"Successfully completed stop-and-archive for bot {bot_name}")

            # Step 8: Update bot run deployment status to ARCHIVED
            try:
                async with db_manager.get_session_context() as session:
                    bot_run_repo = BotRunRepository(session)
                    await bot_run_repo.update_bot_run_archived(bot_name)
                    logger.info(f"Updated bot run deployment status to ARCHIVED for {bot_name}")
            except Exception as e:
                logger.error(f"Failed to update bot run to archived: {e}")
        else:
            logging.error(f"Failed to remove container {container_name}")

            # Update bot run with error status (but keep stopped_at timestamp from earlier)
            try:
                async with db_manager.get_session_context() as session:
                    bot_run_repo = BotRunRepository(session)
                    await bot_run_repo.update_bot_run_stopped(
                        bot_name,
                        error_message="Failed to remove container during archive process"
                    )
                    logger.info(f"Updated bot run with error status for {bot_name}")
            except Exception as e:
                logger.error(f"Failed to update bot run with error: {e}")

    except Exception as e:
        logging.error(f"Error in background stop-and-archive for {bot_name}: {str(e)}")

        # Update bot run with error status
        try:
            async with db_manager.get_session_context() as session:
                bot_run_repo = BotRunRepository(session)
                await bot_run_repo.update_bot_run_stopped(
                    bot_name,
                    error_message=str(e)
                )
                logger.info(f"Updated bot run with error status for {bot_name}")
        except Exception as db_error:
            logger.error(f"Failed to update bot run with error: {db_error}")
    finally:
        # Always clear the stopping status when the background task completes
        bots_manager.clear_bot_stopping(bot_name_for_orchestrator)
        logger.info(f"Cleared stopping status for bot {bot_name}")

        # Remove bot from active_bots and clear all MQTT data
        if bot_name_for_orchestrator in bots_manager.active_bots:
            bots_manager.mqtt_manager.clear_bot_data(bot_name_for_orchestrator)
            del bots_manager.active_bots[bot_name_for_orchestrator]
            logger.info(f"Removed bot {bot_name_for_orchestrator} from active_bots and cleared MQTT data")


@router.post("/stop-and-archive-bot/{bot_name}")
async def stop_and_archive_bot(
    bot_name: str,
    background_tasks: BackgroundTasks,
    skip_order_cancellation: bool = True,
    archive_locally: bool = True,
    s3_bucket: str = None,
    bots_manager: BotsOrchestrator = Depends(get_bots_orchestrator),
    docker_manager: DockerService = Depends(get_docker_service),
    bot_archiver: BotArchiver = Depends(get_bot_archiver),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Gracefully stop a bot and archive its data in the background.
    This initiates a background task that will:
    1. Stop the bot trading process via MQTT
    2. Wait 15 seconds for graceful shutdown
    3. Monitor and stop the Docker container
    4. Archive the bot data (locally or to S3)
    5. Remove the container

    Returns immediately with a success message while the process continues in the background.
    """
    try:
        # Step 1: Normalize bot name and container name
        # Container name is now the same as bot name (no prefix added)
        actual_bot_name = bot_name
        container_name = bot_name

        logging.info(f"Normalized bot_name: {actual_bot_name}, container_name: {container_name}")

        # Step 2: Validate bot exists in active bots
        active_bots = list(bots_manager.active_bots.keys())

        # Check if bot exists in active bots (could be stored as either format)
        bot_found = (actual_bot_name in active_bots) or (container_name in active_bots)

        if not bot_found:
            return {
                "status": "error",
                "message": (
                    f"Bot '{actual_bot_name}' not found in active bots. "
                    f"Active bots: {active_bots}. Cannot perform graceful shutdown."
                ),
                "details": {
                    "input_name": bot_name,
                    "actual_bot_name": actual_bot_name,
                    "container_name": container_name,
                    "active_bots": active_bots,
                    "reason": "Bot must be actively managed via MQTT for graceful shutdown"
                }
            }

        # Use the format that's actually stored in active bots
        bot_name_for_orchestrator = container_name if container_name in active_bots else actual_bot_name

        # Add the background task
        background_tasks.add_task(
            _background_stop_and_archive,
            bot_name=actual_bot_name,
            container_name=container_name,
            bot_name_for_orchestrator=bot_name_for_orchestrator,
            skip_order_cancellation=skip_order_cancellation,
            archive_locally=archive_locally,
            s3_bucket=s3_bucket,
            bots_manager=bots_manager,
            docker_manager=docker_manager,
            bot_archiver=bot_archiver,
            db_manager=db_manager
        )

        return {
            "status": "success",
            "message": f"Stop and archive process started for bot {actual_bot_name}",
            "details": {
                "input_name": bot_name,
                "actual_bot_name": actual_bot_name,
                "container_name": container_name,
                "process": (
                    "The bot will be gracefully stopped, archived, and removed in the background. "
                    "This process typically takes 20-30 seconds."
                )
            }
        }

    except Exception as e:
        logging.error(f"Error initiating stop_and_archive_bot for {bot_name}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deploy-v2-controllers")
async def deploy_v2_controllers(
    deployment: V2ControllerDeployment,
    docker_manager: DockerService = Depends(get_docker_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Deploy a V2 strategy with controllers by generating the script config and creating the instance.
    This endpoint simplifies the deployment process for V2 controller strategies.

    Args:
        deployment: V2ControllerDeployment configuration
        docker_manager: Docker service dependency

    Returns:
        Dictionary with deployment response and generated configuration details

    Raises:
        HTTPException: 500 if deployment fails
    """
    try:
        # Generate unique script config filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        script_config_filename = f"{deployment.instance_name}-{timestamp}.yml"
        # Use the same name with timestamp for the instance to ensure uniqueness
        unique_instance_name = f"{deployment.instance_name}-{timestamp}"

        _validate_deployment_connector_names(deployment.controllers_config)

        # Ensure controller config names have .yml extension
        controllers_with_extension = []
        for controller in deployment.controllers_config:
            if not controller.endswith('.yml'):
                controllers_with_extension.append(f"{controller}.yml")
            else:
                controllers_with_extension.append(controller)

        # Create the script config content
        # Note: candles_config and markets removed - they're optional and empty,
        # and older hummingbot versions don't expect them in the config
        script_config_content = {
            "script_file_name": "v2_with_controllers.py",
            "controllers_config": controllers_with_extension,
        }

        # Add optional drawdown parameters if provided
        if deployment.max_global_drawdown_quote is not None:
            script_config_content["max_global_drawdown_quote"] = deployment.max_global_drawdown_quote
        if deployment.max_controller_drawdown_quote is not None:
            script_config_content["max_controller_drawdown_quote"] = deployment.max_controller_drawdown_quote

        # Save the script config to the scripts directory
        scripts_dir = os.path.join("conf", "scripts")

        script_config_path = os.path.join(scripts_dir, script_config_filename)
        fs_util.dump_dict_to_yaml(script_config_path, script_config_content)

        logging.info(f"Generated script config: {script_config_filename} with content: {script_config_content}")

        # Set generated config on the deployment and deploy
        deployment.instance_name = unique_instance_name
        deployment.script_config = script_config_filename
        response = docker_manager.create_hummingbot_instance(deployment)

        if response.get("success"):
            response["script_config_generated"] = script_config_filename
            response["controllers_deployed"] = deployment.controllers_config
            response["unique_instance_name"] = unique_instance_name

            # Track bot run if deployment was successful
            try:
                async with db_manager.get_session_context() as session:
                    bot_run_repo = BotRunRepository(session)
                    await bot_run_repo.create_bot_run(
                        bot_name=unique_instance_name,
                        instance_name=unique_instance_name,
                        strategy_type="controller",
                        strategy_name="v2_with_controllers",
                        account_name=deployment.credentials_profile,
                        config_name=script_config_filename,
                        image_version=deployment.image,
                        deployment_config=deployment.dict()
                    )
                    logger.info(f"Created bot run record for controller deployment {unique_instance_name}")
            except Exception as e:
                logger.error(f"Failed to create bot run record: {e}")
                # Don't fail the deployment if bot run creation fails

        return response

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error deploying V2 controllers: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/deploy-v2-script")
async def deploy_v2_script(
    deployment: V2ScriptDeployment,
    docker_manager: DockerService = Depends(get_docker_service),
    db_manager: AsyncDatabaseManager = Depends(get_database_manager)
):
    """
    Deploy a V2 script bot with optional script configuration.
    This endpoint creates and starts a Hummingbot instance running the specified script.

    Args:
        deployment: V2ScriptDeployment configuration containing instance name, credentials,
                   optional script name and configuration
        docker_manager: Docker service dependency
        db_manager: Database manager dependency

    Returns:
        Dictionary with deployment response including instance details

    Raises:
        HTTPException: 500 if deployment fails
    """
    try:
        # Generate unique instance name with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        unique_instance_name = f"{deployment.instance_name}-{timestamp}"

        _validate_script_config_connector_names(deployment.script_config)

        # Update deployment with unique name
        deployment.instance_name = unique_instance_name

        # Create the hummingbot instance
        response = docker_manager.create_hummingbot_instance(deployment)

        if response.get("success"):
            response["unique_instance_name"] = unique_instance_name

            # Track bot run if deployment was successful
            try:
                async with db_manager.get_session_context() as session:
                    bot_run_repo = BotRunRepository(session)
                    await bot_run_repo.create_bot_run(
                        bot_name=unique_instance_name,
                        instance_name=unique_instance_name,
                        strategy_type="script",
                        strategy_name=deployment.script or "default",
                        account_name=deployment.credentials_profile,
                        config_name=deployment.script_config,
                        image_version=deployment.image,
                        deployment_config=deployment.dict()
                    )
                    logger.info(f"Created bot run record for script deployment {unique_instance_name}")
            except Exception as e:
                logger.error(f"Failed to create bot run record: {e}")
                # Don't fail the deployment if bot run creation fails

        return response

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error deploying V2 script: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect

from app import ha, xray
from app.db import Session, get_db
from app.ha import cache, core_config, outbox
from app.models.admin import Admin
from app.models.core import CoreStats
from app.utils import responses
from app.xray import XRayConfig
from config import XRAY_LOCAL_CORE_ENABLED

router = APIRouter(tags=["Core"], prefix="/api", responses={401: responses._401})


@router.websocket("/core/logs")
async def core_logs(websocket: WebSocket, db: Session = Depends(get_db)):
    token = websocket.query_params.get("token") or websocket.headers.get(
        "Authorization", ""
    ).removeprefix("Bearer ")
    admin = Admin.get_admin(token, db)
    if not admin:
        return await websocket.close(reason="Unauthorized", code=4401)

    if not admin.is_sudo:
        return await websocket.close(reason="You're not allowed", code=4403)

    if not XRAY_LOCAL_CORE_ENABLED:
        # Closed before accept so the client gets a real error instead of an
        # open socket that never produces a line.
        return await websocket.close(
            reason="Local Xray core is disabled on this panel; "
                   "use /api/node/{node_id}/logs",
            code=4404,
        )

    interval = websocket.query_params.get("interval")
    if interval:
        try:
            interval = float(interval)
        except ValueError:
            return await websocket.close(reason="Invalid interval value", code=4400)
        if interval > 10:
            return await websocket.close(
                reason="Interval must be more than 0 and at most 10 seconds", code=4400
            )

    await websocket.accept()

    cache = ""
    last_sent_ts = 0
    with xray.core.get_logs() as logs:
        while True:
            if interval and time.time() - last_sent_ts >= interval and cache:
                try:
                    await websocket.send_text(cache)
                except (WebSocketDisconnect, RuntimeError):
                    break
                cache = ""
                last_sent_ts = time.time()

            if not logs:
                try:
                    await asyncio.wait_for(websocket.receive(), timeout=0.2)
                    continue
                except asyncio.TimeoutError:
                    continue
                except (WebSocketDisconnect, RuntimeError):
                    break

            log = logs.popleft()

            if interval:
                cache += f"{log}\n"
                continue

            try:
                await websocket.send_text(log)
            except (WebSocketDisconnect, RuntimeError):
                break


@router.get("/core", response_model=CoreStats)
def get_core_stats(admin: Admin = Depends(Admin.get_current)):
    """Retrieve core statistics such as version and uptime."""
    return CoreStats(
        enabled=XRAY_LOCAL_CORE_ENABLED,
        version=xray.core.version,
        started=xray.core.started,
        logs_websocket=router.url_path_for("core_logs"),
    )


@router.post("/core/restart", responses={403: responses._403})
def restart_core(admin: Admin = Depends(Admin.check_sudo_admin)):
    """Restart the core and all connected nodes."""
    if not ha.is_leader():
        # A follower must not talk to nodes; the leader collapses this into a
        # single restart pass on its next drain.
        outbox.enqueue(outbox.OP_RESTART_ALL)
        return {}

    startup_config = xray.config.include_db_users()
    xray.core.restart(startup_config)

    for node_id, node in list(xray.nodes.items()):
        if node.connected:
            xray.operations.restart_node(node_id, startup_config)

    return {}


@router.get("/core/config", responses={403: responses._403})
def get_core_config(admin: Admin = Depends(Admin.check_sudo_admin)) -> dict:
    """Get the current core configuration."""
    config = core_config.read_raw()
    if config is None:
        config = core_config.read_file()
    return config


@router.put("/core/config", responses={403: responses._403})
def modify_core_config(
    payload: dict, admin: Admin = Depends(Admin.check_sudo_admin)
) -> dict:
    """Modify the core configuration and restart the core."""
    try:
        config = XRayConfig(payload, api_port=xray.config.api_port)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    xray.config = config
    # The inbounds of nodes with their own template live on the catalog of
    # the object that was just replaced.
    xray.node_configs.apply_catalog()
    # Stored centrally (when XRAY_JSON_DB_BACKED) so the peer panel picks the
    # same template up within one cache poll instead of drifting.
    core_config.save(payload, updated_by=admin.username)

    if ha.is_leader():
        startup_config = xray.config.include_db_users()
        xray.core.restart(startup_config)
        for node_id, node in list(xray.nodes.items()):
            if node.connected:
                xray.operations.restart_node(node_id, startup_config)
    else:
        outbox.enqueue(outbox.OP_RESTART_ALL)

    xray.hosts.update()
    cache.bump(cache.HOSTS)

    return payload

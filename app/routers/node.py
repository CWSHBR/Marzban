import asyncio
import json
import time
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, WebSocket
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

from app import ha, logger, xray
from app.db import Session, crud, get_db
from app.db.models import Node
from app.dependencies import get_dbnode, validate_dates
from app.ha import cache, core_config
from app.models.admin import Admin
from app.models.node import (
    NodeConfigResponse,
    NodeConfigSummary,
    NodeCreate,
    NodeModify,
    NodeResponse,
    NodeSettings,
    NodeStatus,
    NodesUsageResponse,
)
from app.models.proxy import ProxyHost
from app.utils import responses
# Liveness snapshot maintained by the core health check, so request handlers
# never make blocking node probes of their own.
from app.xray import node_liveness

router = APIRouter(
    tags=["Node"], prefix="/api", responses={401: responses._401, 403: responses._403}
)


def add_host_if_needed(new_node: NodeCreate, db: Session):
    """Add a host if specified in the new node settings."""
    if new_node.add_as_new_host:
        host = ProxyHost(
            remark=f"{new_node.name} ({{USERNAME}}) [{{PROTOCOL}} - {{TRANSPORT}}]",
            address=new_node.address,
        )
        for inbound_tag in xray.config.inbounds_by_tag:
            crud.add_host(db, inbound_tag, host)
        xray.hosts.update()
        cache.bump(cache.HOSTS)


def config_pending(dbnode) -> Optional[bool]:
    """Whether the node still runs an older template than the one saved.

    Compared by hash against what the leader recorded at the last (re)start,
    so a follower answers this as well as the leader does.
    """
    if not dbnode.applied_config_hash:
        return None
    return dbnode.applied_config_hash != xray.node_configs.template_for(dbnode.id).template_hash


def shared_config_raw() -> dict:
    config = core_config.read_raw()
    if config is None:
        config = core_config.read_file()
    return config


def config_summary(dbnode, template, db: Session) -> NodeConfigSummary:
    summary = xray.node_configs.describe(dbnode.id, template)
    summary["new_tags"] = [
        inbound["tag"] for inbound in summary["inbounds"]
        if inbound["origin"] == "node_only" and not crud.inbound_exists(db, inbound["tag"])
    ]
    return NodeConfigSummary(**summary)


def node_config_response(dbnode, db: Session) -> NodeConfigResponse:
    shared = shared_config_raw()
    custom = dbnode.xray_config
    if isinstance(custom, str):  # JSON comes back as text on some drivers
        custom = json.loads(custom)

    summary = None
    template = xray.node_configs.get(dbnode.id)
    if custom is not None and template is not None:
        try:
            summary = config_summary(dbnode, template, db)
        except ValueError:
            pass

    return NodeConfigResponse(
        node_id=dbnode.id,
        custom=custom is not None,
        config=custom if custom is not None else shared,
        shared_config=shared,
        updated_at=dbnode.xray_config_updated_at,
        updated_by=dbnode.xray_config_updated_by,
        pending=config_pending(dbnode),
        summary=summary,
    )


def add_hosts_for_new_tags(dbnode, tags: List[str], db: Session) -> None:
    """Point clients at the node for tags that only just came into being.

    Any new tag would otherwise get the default host at ``{SERVER_IP}``, i.e.
    the panel, where an inbound of this node's own config is not served.
    """
    for tag in tags:
        crud.create_inbound_with_host(
            db, tag,
            remark=f"{dbnode.name} ({{USERNAME}}) [{{PROTOCOL}} - {{TRANSPORT}}]",
            address=dbnode.address,
        )


def publish_node_configs() -> None:
    """Rebuild the catalog here and tell the peer panel to do the same."""
    xray.node_configs.reload()
    cache.bump(cache.CORE_CONFIG)
    xray.hosts.update()
    cache.bump(cache.HOSTS)


def node_response(dbnode) -> NodeResponse:
    """Build the API view of a node without touching the network.

    ``runtime_node.connected`` and ``.started`` are each a blocking HTTPS POST
    with a 3s timeout. Calling them per node inside a sync route meant
    ``GET /api/nodes`` could stall the threadpool for 6s x node_count. The
    health check already probes every node on a 10s interval, so its result is
    reused here instead. A follower has no runtime nodes at all and falls back
    to the status the leader recorded in the database.
    """
    response = NodeResponse.model_validate(dbnode)
    response.status_message = response.message
    response.config_pending = config_pending(dbnode)

    liveness = node_liveness.get(dbnode.id)
    if liveness is None:
        return response

    response.is_attached = liveness.get("is_attached")
    response.is_running = liveness.get("is_running")
    response.needs_restart = bool(liveness.get("needs_restart"))
    response.status_message = liveness.get("status_message") or response.message
    return response


@router.get("/node/settings", response_model=NodeSettings)
def get_node_settings(
    db: Session = Depends(get_db), admin: Admin = Depends(Admin.check_sudo_admin)
):
    """Retrieve the current node settings, including TLS certificate."""
    tls = crud.get_tls_certificate(db)
    return NodeSettings(certificate=tls.certificate)


@router.post("/node", response_model=NodeResponse, responses={409: responses._409})
def add_node(
    new_node: NodeCreate,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Add a new node to the database and optionally add it as a host."""
    try:
        dbnode = crud.create_node(db, new_node)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f'Node "{new_node.name}" already exists'
        )

    bg.add_task(xray.operations.connect_node, node_id=dbnode.id)
    bg.add_task(add_host_if_needed, new_node, db)

    logger.info(f'New node "{dbnode.name}" added')
    return dbnode


@router.get("/node/{node_id}", response_model=NodeResponse)
def get_node(
    dbnode: Node = Depends(get_dbnode),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Retrieve details of a specific node by its ID."""
    return node_response(dbnode)


@router.websocket("/node/{node_id}/logs")
async def node_logs(node_id: int, websocket: WebSocket, db: Session = Depends(get_db)):
    token = websocket.query_params.get("token") or websocket.headers.get(
        "Authorization", ""
    ).removeprefix("Bearer ")
    admin = Admin.get_admin(token, db)
    if not admin:
        return await websocket.close(reason="Unauthorized", code=4401)

    if not admin.is_sudo:
        return await websocket.close(reason="You're not allowed", code=4403)

    if not xray.nodes.get(node_id):
        return await websocket.close(reason="Node not found", code=4404)

    if not xray.nodes[node_id].connected:
        return await websocket.close(
            reason="Node control session is not attached; reconnect node before reading logs",
            code=4400
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
    node = xray.nodes[node_id]
    with node.get_logs() as logs:
        while True:
            if not node == xray.nodes[node_id]:
                break

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


@router.get("/nodes", response_model=List[NodeResponse])
def get_nodes(
    db: Session = Depends(get_db), _: Admin = Depends(Admin.check_sudo_admin)
):
    """Retrieve a list of all nodes. Accessible only to sudo admins."""
    return [node_response(dbnode) for dbnode in crud.get_nodes(db)]


@router.put("/node/{node_id}", response_model=NodeResponse)
def modify_node(
    modified_node: NodeModify,
    bg: BackgroundTasks,
    dbnode: Node = Depends(get_dbnode),
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Update a node's details. Only accessible to sudo admins."""
    updated_node = crud.update_node(db, dbnode, modified_node)
    if updated_node.status == NodeStatus.disabled:
        xray.operations.remove_node(updated_node.id, stop=True)
    else:
        xray.operations.remove_node(updated_node.id)
        bg.add_task(xray.operations.connect_node, node_id=updated_node.id)

    logger.info(f'Node "{updated_node.name}" modified')
    return node_response(updated_node)


@router.get("/node/{node_id}/config", response_model=NodeConfigResponse)
def get_node_config(
    dbnode: Node = Depends(get_dbnode),
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """The Xray config the node boots from: its own, or the shared one."""
    return node_config_response(dbnode, db)


@router.post(
    "/node/{node_id}/config/validate",
    response_model=NodeConfigSummary,
    responses={400: responses._400},
)
def validate_node_config(
    payload: dict,
    dbnode: Node = Depends(get_dbnode),
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Check a config for the node without saving it."""
    try:
        template = xray.node_configs.build(payload)
        return config_summary(dbnode, template, db)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))


@router.put(
    "/node/{node_id}/config",
    response_model=NodeConfigResponse,
    responses={400: responses._400},
)
def modify_node_config(
    payload: dict,
    restart: bool = False,
    dbnode: Node = Depends(get_dbnode),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Give the node its own Xray config.

    The config is stored and, unless `restart` is set, takes effect the next
    time the node's Xray is (re)started. Inbound tags the panel has never
    seen get a host at the node's address.
    """
    try:
        template = xray.node_configs.build(payload)
        summary = config_summary(dbnode, template, db)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    crud.set_node_xray_config(db, dbnode, payload, updated_by=admin.username)
    add_hosts_for_new_tags(dbnode, summary.new_tags, db)
    publish_node_configs()

    restart = restart and dbnode.status != NodeStatus.disabled
    if restart:
        xray.operations.restart_node(dbnode.id)

    logger.info(f'Own Xray config of node "{dbnode.name}" saved by "{admin.username}"'
                + (", restarting" if restart else ""))
    return node_config_response(dbnode, db)


@router.delete("/node/{node_id}/config", response_model=NodeConfigResponse)
def reset_node_config(
    restart: bool = False,
    dbnode: Node = Depends(get_dbnode),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Drop the node's own config so it follows the shared one again."""
    if dbnode.xray_config is not None:
        crud.set_node_xray_config(db, dbnode, None, updated_by=admin.username)
        publish_node_configs()

        if restart and dbnode.status != NodeStatus.disabled:
            xray.operations.restart_node(dbnode.id)

        logger.info(f'Node "{dbnode.name}" returned to the shared Xray config '
                    f'by "{admin.username}"')
    return node_config_response(dbnode, db)


@router.post("/node/{node_id}/restart")
def restart_node_core(
    dbnode: Node = Depends(get_dbnode),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Restart Xray on this node alone, applying its current config."""
    if dbnode.status == NodeStatus.disabled:
        raise HTTPException(status_code=409, detail="Node is disabled")
    xray.operations.restart_node(dbnode.id)
    return {"detail": "Restart task scheduled"}


@router.post("/node/{node_id}/reconnect")
def reconnect_node(
    bg: BackgroundTasks,
    dbnode: Node = Depends(get_dbnode),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Trigger a reconnection for the specified node. Only accessible to sudo admins."""
    bg.add_task(xray.operations.connect_node, node_id=dbnode.id)
    return {"detail": "Reconnection task scheduled"}


@router.delete("/node/{node_id}")
def remove_node(
    dbnode: Node = Depends(get_dbnode),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Delete a node and remove it from xray in the background."""
    had_config = dbnode.xray_config is not None
    xray.operations.remove_node(dbnode.id, stop=True)
    crud.remove_node(db, dbnode)
    if had_config:
        # Its inbounds leave the catalog with it.
        publish_node_configs()

    logger.info(f'Node "{dbnode.name}" deleted')
    return {}


@router.get("/nodes/usage", response_model=NodesUsageResponse)
def get_usage(
    db: Session = Depends(get_db),
    start: str = "",
    end: str = "",
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Retrieve usage statistics for nodes within a specified date range."""
    start, end = validate_dates(start, end)

    usages = crud.get_nodes_usage(db, start, end)

    return {"usages": usages}

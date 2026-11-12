import time
import traceback

from app import app, ha, logger, xray
from app.db import GetDB, crud
from app.ha.jobs import leader_job
from app.models.node import NodeStatus
from config import (JOB_CORE_HEALTH_CHECK_INTERVAL, NODE_PERSISTENT_MODE,
                    XRAY_LOCAL_CORE_ENABLED)
from xray_api import exc as xray_exc

# Lives on app.xray so request handlers can import it; this module is loaded by
# path and is not importable by name.
node_liveness = xray.node_liveness


def core_health_check():
    if not ha.is_leader():
        return

    config = None

    def config_for(node_id):
        """The shared snapshot, built at most once per pass.

        None for a node with a template of its own: it builds its own
        snapshot, so the shared one would be computed for nothing.
        """
        nonlocal config
        if xray.node_configs.get(node_id) is not None:
            return None
        if not config:
            config = xray.config.include_db_users()
        return config

    # main core
    if XRAY_LOCAL_CORE_ENABLED and not xray.core.started:
        if not config:
            config = xray.config.include_db_users()
        xray.core.restart(config)

    # nodes' core
    for node_id, node in list(xray.nodes.items()):
        connected = node.connected
        started = False
        if connected:
            try:
                started = node.started
                assert started
                node.api.get_sys_stats(timeout=2)
            except (ConnectionError, xray_exc.XrayError, AssertionError):
                xray.operations.restart_node(node_id, config_for(node_id))

        node_liveness[node_id] = {
            "is_running": started,
            "is_attached": connected,
            "needs_restart": getattr(node, "needs_restart", False),
            "status_message": getattr(node, "status_message", None),
            "checked_at": time.time(),
        }

        if not connected:
            xray.operations.connect_node(node_id, config_for(node_id))


def start_core():
    """Take ownership of the dataplane.

    This used to be a startup event. It is now the promotion handler: a
    follower must never mark nodes as `connecting` in the shared database, nor
    connect to them, because the node's /connect is last-writer-wins and would
    steal the session from the live leader.
    """
    # The templates are loaded at import, which a database blip (or a schema
    # not migrated yet) can make fail quietly. Booting a node from the shared
    # config when it has its own would be wrong, so read them again here.
    xray.node_configs.reload()

    if XRAY_LOCAL_CORE_ENABLED:
        logger.info("Generating Xray core config")

    start_time = time.time()
    config = xray.config.include_db_users()
    logger.info(f"Xray core config generated in {(time.time() - start_time):.2f} seconds")

    # main core
    if XRAY_LOCAL_CORE_ENABLED:
        logger.info("Starting main Xray core")
        try:
            xray.core.start(config)
        except Exception:
            traceback.print_exc()

    # nodes' core
    logger.info("Starting nodes Xray core")
    with GetDB() as db:
        dbnodes = crud.get_nodes(db=db, enabled=True)
        node_ids = [dbnode.id for dbnode in dbnodes]
        for dbnode in dbnodes:
            crud.update_node_status(db, dbnode, NodeStatus.connecting)

    # A running persistent node is attached, not restarted, so user sessions
    # survive the failover; connect_node then diffs its users against the DB.
    # A restarted node gets the full snapshot. Either way writes that landed
    # during the failover window are covered, and the outbox replay is redundant.
    for node_id in node_ids:
        xray.operations.connect_node(node_id, config)


def detach_all_nodes():
    """Let go of the dataplane without stopping it.

    Used by both demotion and shutdown. In persistent mode this detaches the
    control session and leaves Xray serving traffic, which is the whole point:
    losing the panel must not drop user connections.
    """
    if XRAY_LOCAL_CORE_ENABLED:
        logger.info("Stopping main Xray core")
        xray.core.stop()

    if NODE_PERSISTENT_MODE:
        logger.info("Detaching from nodes Xray core")
        for node_id in list(xray.nodes.keys()):
            xray.operations.detach_node(node_id)
    else:
        logger.info("Stopping nodes Xray core")
        for node in list(xray.nodes.values()):
            try:
                node.disconnect()
            except Exception:
                pass

    node_liveness.clear()


ha.on_promote(start_core)
ha.on_demote(detach_all_nodes)

leader_job(core_health_check, 'interval',
           seconds=JOB_CORE_HEALTH_CHECK_INTERVAL,
           coalesce=True, max_instances=1)


@app.on_event("shutdown")
def app_shutdown():
    detach_all_nodes()

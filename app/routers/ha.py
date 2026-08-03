from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app import ha, xray
from app.ha import members, notifications as notification_outbox, outbox
from app.ha.lease import request_stepdown
from app.models.admin import Admin
from app.models.ha import (HALeaseInfo, HAMemberInfo, HANodesInfo,
                           HAOutboxInfo, HASelfInfo, HAStatus)
from app.utils import responses
from config import (HA_ENABLED, HA_METRICS_TOKEN, HA_OUTBOX_BACKLOG_AGE,
                    HA_OUTBOX_BACKLOG_THRESHOLD, XRAY_LOCAL_CORE_ENABLED)

router = APIRouter(tags=["HA"], prefix="/api", responses={401: responses._401})

# A member whose heartbeat is older than this is not a safe handover target.
# Deliberately much tighter than HA_MEMBER_STALE_AFTER, which only controls
# when a row is considered abandoned.
HANDOVER_STALE_AFTER = 15


def _age_seconds(value) -> float:
    if not isinstance(value, datetime):
        return None
    reference = datetime.now(timezone.utc) if value.tzinfo else datetime.utcnow()
    return max(0.0, (reference - value).total_seconds())


def _outbox_info() -> HAOutboxInfo:
    stats = outbox.stats()
    age = _age_seconds(stats.get("oldest_created_at"))
    pending = stats.get("pending")
    return HAOutboxInfo(
        pending=pending,
        oldest_created_at=stats.get("oldest_created_at"),
        oldest_age_seconds=age,
        backlog_bailout=bool(
            (pending is not None and pending > HA_OUTBOX_BACKLOG_THRESHOLD)
            or (age is not None and age > HA_OUTBOX_BACKLOG_AGE)
        ),
    )


def _notifications_info() -> HAOutboxInfo:
    stats = notification_outbox.stats()
    return HAOutboxInfo(
        pending=stats.get("pending"),
        oldest_created_at=stats.get("oldest_created_at"),
        oldest_age_seconds=_age_seconds(stats.get("oldest_created_at")),
    )


def _nodes_info() -> HANodesInfo:
    from app.db import GetDB, crud

    by_status = {}
    with GetDB() as db:
        for dbnode in crud.get_nodes(db):
            key = dbnode.status.value if hasattr(dbnode.status, "value") else str(dbnode.status)
            by_status[key] = by_status.get(key, 0) + 1

    needs_restart = sum(
        1 for live in xray.node_liveness.values() if live.get("needs_restart")
    )
    return HANodesInfo(by_status=by_status, needs_restart=needs_restart)


def build_status() -> HAStatus:
    """Whole snapshot comes from the database, so it reads the same on both roles."""
    lease = members.read_lease() if HA_ENABLED else None
    member_rows = members.list_members() if HA_ENABLED else []

    me = ha.panel_id()
    member_infos = []
    for row in member_rows:
        member_infos.append(HAMemberInfo(is_self=(row["panel_id"] == me), **row))

    can_handover = False
    target = None
    blocked = None

    if not HA_ENABLED:
        blocked = "HA is disabled on this panel"
    elif lease is None or not lease.get("holder"):
        blocked = "there is no leader to hand over from"
    elif lease.get("stepdown_requested"):
        blocked = "a handover is already in progress"
    else:
        peers = [
            m for m in member_infos
            if not m.is_self
            and m.last_seen_age is not None
            and m.last_seen_age <= HANDOVER_STALE_AFTER
        ]
        if not peers:
            blocked = "no other panel is responding, there is nobody to hand over to"
        else:
            can_handover = True
            target = peers[0].panel_id

    return HAStatus(
        self_=HASelfInfo(
            panel_id=me,
            role=ha.state.role,
            epoch=ha.current_epoch(),
            ha_enabled=HA_ENABLED,
            local_core_enabled=XRAY_LOCAL_CORE_ENABLED,
            fencing_seconds_left=ha.state.seconds_until_deadline(),
            leader_for_seconds=ha.state.uptime_as_leader(),
            last_error=ha.state.last_error,
        ),
        lease=HALeaseInfo(**lease) if lease else None,
        members=member_infos,
        outbox=_outbox_info(),
        notifications=_notifications_info(),
        nodes=_nodes_info(),
        can_handover=can_handover,
        handover_target=target,
        handover_blocked_reason=blocked,
    )


@router.get("/ha/status", response_model=HAStatus)
def get_ha_status(admin: Admin = Depends(Admin.get_current)):
    """Cluster status: both panels, the lease, and the sync queues."""
    return build_status()


@router.post("/ha/stepdown", responses={403: responses._403, 409: responses._409})
def stepdown(admin: Admin = Depends(Admin.check_sudo_admin)):
    """Hand leadership to the other panel.

    Works from either panel: the request is recorded in the lease row rather
    than sent to the leader over HTTP, so it does not depend on the two panels
    being able to reach each other.
    """
    if not HA_ENABLED:
        raise HTTPException(status_code=409, detail="HA is not enabled on this panel")

    status = build_status()
    if not status.can_handover:
        raise HTTPException(
            status_code=409,
            detail=status.handover_blocked_reason or "handover is not possible right now",
        )

    if not request_stepdown():
        raise HTTPException(status_code=409, detail="there is currently no lease holder")

    return {"requested": True, "target": status.handover_target}


@router.get("/ha/metrics", include_in_schema=False)
def ha_metrics(request: Request):
    """Prometheus exposition of the same snapshot.

    Alert on "there is no leader", never on "the leader is not panel A" --
    roles float by design, so the latter fires on every healthy failover.
    """
    if not HA_METRICS_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Set HA_METRICS_TOKEN to enable the metrics endpoint",
        )

    provided = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if provided != HA_METRICS_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    status = build_status()
    lines = [
        "# HELP marzban_ha_is_leader Whether this panel currently holds the lease.",
        "# TYPE marzban_ha_is_leader gauge",
        f'marzban_ha_is_leader{{panel_id="{status.self_.panel_id}"}} '
        f'{1 if status.self_.role == ha.LEADER else 0}',
        "# HELP marzban_ha_lease_epoch Fencing token of the current lease.",
        "# TYPE marzban_ha_lease_epoch counter",
        f"marzban_ha_lease_epoch {status.lease.epoch if status.lease else 0}",
        "# HELP marzban_ha_has_leader Whether the cluster has any live leader.",
        "# TYPE marzban_ha_has_leader gauge",
        f"marzban_ha_has_leader {1 if (status.lease and status.lease.holder) else 0}",
        "# HELP marzban_ha_outbox_pending Dataplane changes waiting for the leader.",
        "# TYPE marzban_ha_outbox_pending gauge",
        f"marzban_ha_outbox_pending {status.outbox.pending or 0}",
        "# HELP marzban_ha_outbox_oldest_age_seconds Age of the oldest pending change.",
        "# TYPE marzban_ha_outbox_oldest_age_seconds gauge",
        f"marzban_ha_outbox_oldest_age_seconds {status.outbox.oldest_age_seconds or 0}",
        "# HELP marzban_ha_notifications_pending Undelivered webhook notifications.",
        "# TYPE marzban_ha_notifications_pending gauge",
        f"marzban_ha_notifications_pending {status.notifications.pending or 0}",
        "# HELP marzban_ha_member_last_seen_seconds Heartbeat age per panel.",
        "# TYPE marzban_ha_member_last_seen_seconds gauge",
    ]
    for member in status.members:
        lines.append(
            f'marzban_ha_member_last_seen_seconds{{panel_id="{member.panel_id}",'
            f'role="{member.role}"}} {member.last_seen_age or 0}'
        )

    lines.append("# HELP marzban_ha_nodes Node count by status.")
    lines.append("# TYPE marzban_ha_nodes gauge")
    for status_name, count in status.nodes.by_status.items():
        lines.append(f'marzban_ha_nodes{{status="{status_name}"}} {count}')
    lines.append("# HELP marzban_ha_nodes_needs_restart Nodes running a stale config.")
    lines.append("# TYPE marzban_ha_nodes_needs_restart gauge")
    lines.append(f"marzban_ha_nodes_needs_restart {status.nodes.needs_restart}")

    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

"""Dataplane changes made on a follower, applied by the leader.

A follower accepts the full write API but must never touch nodes -- the node's
``/connect`` is last-writer-wins, so two panels talking to one node thrash. So
the follower records an intent here and the leader applies it.

The important property is that this is **level-triggered**. Rows are hints
saying "user N may be out of date", not a command log. The drain re-reads the
user's current state and derives the action from that. Consequences:

* idempotent -- replaying a row is a no-op
* order-independent within a user -- only the final state matters
* retry-free -- a failed row is simply re-derived on the next pass

Writes made during a failover window are never lost either: promotion either
restarts a node with a full ``include_db_users()`` snapshot or, when it attaches
to a running node, diffs the node's users against the database
(``reconcile_node_users``); rows committed before that are then redundantly
re-applied.
"""

from typing import Dict, List, Optional, Set

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app import logger
from app.db import GetDB
from config import (
    HA_OUTBOX_BACKLOG_AGE,
    HA_OUTBOX_BACKLOG_THRESHOLD,
    HA_OUTBOX_BATCH_SIZE,
)

OP_ADD_USER = "add_user"
OP_UPDATE_USER = "update_user"
OP_REMOVE_USER = "remove_user"
OP_RESTART_ALL = "restart_all"

USER_OPS = {OP_ADD_USER, OP_UPDATE_USER, OP_REMOVE_USER}


def _now_expr(db) -> str:
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        return "(now() AT TIME ZONE 'utc')"
    if dialect == "mysql":
        return "UTC_TIMESTAMP(6)"
    return "datetime('now')"


def enqueue(op: str, user_id: Optional[int] = None,
            payload: Optional[dict] = None) -> None:
    """Record an intent. Never raises -- a failure here must not fail the API call."""
    import json

    try:
        with GetDB() as db:
            db.execute(
                text(
                    "INSERT INTO node_sync_outbox (created_at, op, user_id, payload, attempts) "
                    f"VALUES ({_now_expr(db)}, :op, :user_id, :payload, 0)"
                ),
                {
                    "op": op,
                    "user_id": user_id,
                    "payload": json.dumps(payload) if payload is not None else None,
                },
            )
            db.commit()
    except SQLAlchemyError as exc:
        logger.error(f"Could not enqueue {op} for user {user_id} into the HA outbox: {exc}")


def stats() -> Dict[str, Optional[int]]:
    """Pending depth and age -- the health signal for follower writes."""
    try:
        with GetDB() as db:
            row = db.execute(text(
                "SELECT COUNT(*), MIN(created_at) FROM node_sync_outbox "
                "WHERE done_at IS NULL"
            )).first()
    except SQLAlchemyError:
        return {"pending": None, "oldest_created_at": None}

    pending = int(row[0]) if row and row[0] is not None else 0
    return {"pending": pending, "oldest_created_at": row[1] if row else None}


def _select_pending(db) -> List[dict]:
    """Claim a batch.

    ``FOR UPDATE SKIP LOCKED`` means a panel that momentarily believes it is
    leader cannot double-drain the same rows. SQLite has neither, and never
    runs two panels, so it is skipped there.
    """
    suffix = "" if db.bind.dialect.name == "sqlite" else " FOR UPDATE SKIP LOCKED"
    rows = db.execute(
        text(
            "SELECT id, op, user_id, payload FROM node_sync_outbox "
            "WHERE done_at IS NULL ORDER BY id LIMIT :limit" + suffix
        ),
        {"limit": HA_OUTBOX_BATCH_SIZE},
    ).all()
    return [
        {"id": r[0], "op": r[1], "user_id": r[2], "payload": r[3]}
        for r in rows
    ]


def coalesce(rows: List[dict]) -> Dict[str, object]:
    """Collapse a batch to one action per user, plus a restart flag.

    Pure function -- this is what the unit tests exercise.
    """
    latest: Dict[int, dict] = {}
    restart_all = False
    ids: List[int] = []

    for row in rows:
        ids.append(row["id"])
        if row["op"] == OP_RESTART_ALL:
            restart_all = True
            continue
        if row["op"] not in USER_OPS or row["user_id"] is None:
            continue
        current = latest.get(row["user_id"])
        if current is None or row["id"] > current["id"]:
            latest[row["user_id"]] = row

    return {"ids": ids, "users": latest, "restart_all": restart_all}


def _backlog_is_large(db) -> bool:
    row = db.execute(text(
        "SELECT COUNT(*) FROM node_sync_outbox WHERE done_at IS NULL"
    )).first()
    pending = int(row[0]) if row and row[0] is not None else 0
    if pending > HA_OUTBOX_BACKLOG_THRESHOLD:
        return True

    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        age = ("SELECT COUNT(*) FROM node_sync_outbox WHERE done_at IS NULL "
               "AND created_at < (now() AT TIME ZONE 'utc') - make_interval(secs => :age)")
    elif dialect == "mysql":
        age = ("SELECT COUNT(*) FROM node_sync_outbox WHERE done_at IS NULL "
               "AND created_at < DATE_SUB(UTC_TIMESTAMP(6), INTERVAL :age SECOND)")
    else:
        age = ("SELECT COUNT(*) FROM node_sync_outbox WHERE done_at IS NULL "
               "AND created_at < datetime('now', '-' || :age || ' seconds')")

    stale = db.execute(text(age), {"age": HA_OUTBOX_BACKLOG_AGE}).first()
    return bool(stale and stale[0])


def _mark_done(db, ids: List[int]) -> None:
    if not ids:
        return
    stmt = text(
        f"UPDATE node_sync_outbox SET done_at = {_now_expr(db)} WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    db.execute(stmt, {"ids": list(ids)})


def _apply_user(user_id: int, op: str, payload: Optional[dict]) -> None:
    """Derive the right call from the user's *current* state, not from `op`."""
    from app.db import crud
    from app.models.user import UserStatus
    from app.xray import operations

    with GetDB() as db:
        dbuser = crud.get_user_by_id(db, user_id)

        if op == OP_REMOVE_USER or dbuser is None:
            email = (payload or {}).get("email")
            if email is None and dbuser is not None:
                email = f"{dbuser.id}.{dbuser.username}"
            if email:
                operations.remove_user_by_email(email)
            return

        if dbuser.status not in (UserStatus.active, UserStatus.on_hold):
            operations.remove_user(dbuser)
            return

        # update_user is remove-then-add per inbound, so it subsumes add_user
        # and is safe to apply repeatedly.
        operations.update_user(dbuser)


def drain() -> None:
    """Leader-only. Apply everything queued since the last pass."""
    import json

    from app import ha

    if not ha.is_leader():
        return

    try:
        with GetDB() as db:
            if _backlog_is_large(db):
                logger.warning(
                    "HA outbox backlog is large; doing one full node reload "
                    "instead of per-user updates"
                )
                _drain_via_full_reload(db)
                return

            rows = _select_pending(db)
            if not rows:
                return
            batch = coalesce(rows)
            _mark_done(db, batch["ids"])
            db.commit()
    except SQLAlchemyError as exc:
        logger.warning(f"HA outbox drain failed: {exc}")
        return

    for user_id, row in batch["users"].items():
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        try:
            _apply_user(user_id, row["op"], payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"Failed to apply outbox row for user {user_id}: {exc}")

    if batch["restart_all"]:
        _restart_all_nodes()


def _drain_via_full_reload(db) -> None:
    """One full config push is cheaper and more correct than N thousand RPCs."""
    db.execute(text(
        f"UPDATE node_sync_outbox SET done_at = {_now_expr(db)} WHERE done_at IS NULL"
    ))
    db.commit()
    _restart_all_nodes()


def _restart_all_nodes() -> None:
    from app import xray
    try:
        xray.operations.restart_all_cores()
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(f"Failed to restart nodes from the HA outbox: {exc}")


def purge_done() -> None:
    """Leader-only retention pass."""
    from app import ha
    if not ha.is_leader():
        return

    try:
        with GetDB() as db:
            dialect = db.bind.dialect.name
            if dialect == "postgresql":
                cutoff = "(now() AT TIME ZONE 'utc') - make_interval(days => 1)"
            elif dialect == "mysql":
                cutoff = "DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 DAY)"
            else:
                cutoff = "datetime('now', '-1 day')"
            db.execute(text(
                f"DELETE FROM node_sync_outbox WHERE done_at IS NOT NULL AND done_at < {cutoff}"
            ))
            db.commit()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not purge the HA outbox: {exc}")


__all__ = [
    "OP_ADD_USER",
    "OP_REMOVE_USER",
    "OP_RESTART_ALL",
    "OP_UPDATE_USER",
    "coalesce",
    "drain",
    "enqueue",
    "purge_done",
    "stats",
]

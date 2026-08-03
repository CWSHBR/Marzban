"""Durable webhook outbox.

The original queue was a process-local ``deque``, which breaks twice under A/B:
a notification raised on the follower is never delivered (only the leader runs
the sender, and it reads its *own* deque), and everything pending is lost when
the leader dies.
"""

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app import logger
from app.db import GetDB


def _now_expr(db) -> str:
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        return "(now() AT TIME ZONE 'utc')"
    if dialect == "mysql":
        return "UTC_TIMESTAMP(6)"
    return "datetime('now')"


def enqueue(action: str, payload: dict) -> None:
    try:
        with GetDB() as db:
            db.execute(
                text(
                    "INSERT INTO notification_outbox "
                    "  (action, payload, tries, send_at, created_at) "
                    f"VALUES (:action, :payload, 0, {_now_expr(db)}, {_now_expr(db)})"
                ),
                {"action": action, "payload": json.dumps(payload)},
            )
            db.commit()
    except SQLAlchemyError as exc:
        logger.error(f"Could not enqueue notification {action!r}: {exc}")


def claim(limit: int, max_tries: int) -> List[Dict[str, Any]]:
    """Take a batch of due notifications.

    ``FOR UPDATE SKIP LOCKED`` guards against two panels briefly overlapping.
    """
    try:
        with GetDB() as db:
            suffix = "" if db.bind.dialect.name == "sqlite" else " FOR UPDATE SKIP LOCKED"
            rows = db.execute(
                text(
                    "SELECT id, action, payload, tries FROM notification_outbox "
                    "WHERE sent_at IS NULL AND tries <= :max_tries "
                    f"  AND (send_at IS NULL OR send_at <= {_now_expr(db)}) "
                    "ORDER BY id LIMIT :limit" + suffix
                ),
                {"limit": limit, "max_tries": max_tries},
            ).all()
            db.commit()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not claim notifications: {exc}")
        return []

    claimed = []
    for row in rows:
        payload = row[2]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        claimed.append({"id": row[0], "action": row[1], "payload": payload, "tries": row[3]})
    return claimed


def mark_sent(ids: List[int]) -> None:
    if not ids:
        return
    try:
        with GetDB() as db:
            stmt = text(
                f"UPDATE notification_outbox SET sent_at = {_now_expr(db)} WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            db.execute(stmt, {"ids": list(ids)})
            db.commit()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not mark notifications as sent: {exc}")


def reschedule(ids: List[int], delay_seconds: int) -> None:
    """Bump the retry counter and push delivery out."""
    if not ids:
        return
    try:
        with GetDB() as db:
            dialect = db.bind.dialect.name
            if dialect == "postgresql":
                later = "((now() AT TIME ZONE 'utc') + make_interval(secs => :delay))"
            elif dialect == "mysql":
                later = "DATE_ADD(UTC_TIMESTAMP(6), INTERVAL :delay SECOND)"
            else:
                later = "datetime('now', '+' || :delay || ' seconds')"
            stmt = text(
                f"UPDATE notification_outbox SET tries = tries + 1, send_at = {later} "
                "WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            db.execute(stmt, {"ids": list(ids), "delay": delay_seconds})
            db.commit()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not reschedule notifications: {exc}")


def purge_sent(older_than_days: int = 3) -> None:
    try:
        with GetDB() as db:
            dialect = db.bind.dialect.name
            if dialect == "postgresql":
                cutoff = "(now() AT TIME ZONE 'utc') - make_interval(days => :days)"
            elif dialect == "mysql":
                cutoff = "DATE_SUB(UTC_TIMESTAMP(6), INTERVAL :days DAY)"
            else:
                cutoff = "datetime('now', '-' || :days || ' days')"
            db.execute(
                text(
                    "DELETE FROM notification_outbox "
                    f"WHERE sent_at IS NOT NULL AND sent_at < {cutoff}"
                ),
                {"days": older_than_days},
            )
            db.commit()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not purge sent notifications: {exc}")


def stats() -> Dict[str, Optional[int]]:
    try:
        with GetDB() as db:
            row = db.execute(text(
                "SELECT COUNT(*), MIN(created_at) FROM notification_outbox "
                "WHERE sent_at IS NULL"
            )).first()
    except SQLAlchemyError:
        return {"pending": None, "oldest_created_at": None}

    return {
        "pending": int(row[0]) if row and row[0] is not None else 0,
        "oldest_created_at": row[1] if row else None,
    }


__all__ = ["claim", "enqueue", "mark_sent", "purge_sent", "reschedule", "stats"]

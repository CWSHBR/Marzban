"""Reading the cluster membership view.

The lease row only knows who leads. ``ha_members`` is heartbeated by *both*
panels so the dashboard can say whether there is anyone to hand over to.
"""

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import logger
from app.db import GetDB
from config import HA_MEMBER_STALE_AFTER


def _now_expr(db) -> str:
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        return "(now() AT TIME ZONE 'utc')"
    if dialect == "mysql":
        return "UTC_TIMESTAMP(6)"
    return "datetime('now')"


def _as_datetime(value) -> Optional[datetime]:
    """Raw text() queries return TEXT on SQLite, datetimes on Postgres/MySQL."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def list_members() -> List[Dict]:
    """All known panels, with heartbeat age measured on the database clock."""
    try:
        with GetDB() as db:
            now_expr = _now_expr(db)
            rows = db.execute(text(
                "SELECT panel_id, role, version, advertise_url, local_core_enabled, "
                f"       started_at, last_seen, {now_expr} AS db_now "
                "FROM ha_members ORDER BY panel_id"
            )).all()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not read HA members: {exc}")
        return []

    members = []
    for row in rows:
        last_seen, db_now = _as_datetime(row[6]), _as_datetime(row[7])
        age = None
        if last_seen is not None and db_now is not None:
            age = max(0.0, (db_now - last_seen).total_seconds())
        members.append({
            "panel_id": row[0],
            "role": row[1],
            "version": row[2],
            "advertise_url": row[3],
            "local_core_enabled": bool(row[4]),
            "started_at": _as_datetime(row[5]),
            "last_seen": last_seen,
            "last_seen_age": age,
            "is_stale": age is None or age > HA_MEMBER_STALE_AFTER,
        })
    return members


def read_lease() -> Optional[Dict]:
    try:
        with GetDB() as db:
            now_expr = _now_expr(db)
            row = db.execute(text(
                "SELECT holder, epoch, advertise_url, acquired_at, renewed_at, "
                f"       expires_at, stepdown_requested_epoch, {now_expr} AS db_now "
                "FROM ha_leases WHERE name = 'panel'"
            )).first()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not read HA lease: {exc}")
        return None

    if row is None:
        return None

    expires_at, db_now = _as_datetime(row[5]), _as_datetime(row[7])
    expires_in = None
    if expires_at is not None and db_now is not None:
        expires_in = (expires_at - db_now).total_seconds()

    holder = row[0]
    return {
        # Report an expired lease as unheld, so the UI never shows a dead panel
        # as the current leader.
        "holder": holder if (expires_in is None or expires_in > 0) else None,
        "epoch": int(row[1]) if row[1] is not None else 0,
        "advertise_url": row[2],
        "acquired_at": _as_datetime(row[3]),
        "renewed_at": _as_datetime(row[4]),
        "expires_at": expires_at,
        "expires_in": expires_in,
        "stepdown_requested": row[6] is not None and row[6] == row[1],
    }


def purge_stale_members() -> None:
    """Drop panels that stopped heartbeating long ago.

    Without this a renamed or decommissioned panel stays in the list forever.
    """
    from app import ha
    if not ha.is_leader():
        return

    # Ten times the staleness window: long enough that a panel down for
    # maintenance is not forgotten, short enough to stay tidy.
    cutoff_seconds = HA_MEMBER_STALE_AFTER * 10
    try:
        with GetDB() as db:
            dialect = db.bind.dialect.name
            if dialect == "postgresql":
                cutoff = "(now() AT TIME ZONE 'utc') - make_interval(secs => :age)"
            elif dialect == "mysql":
                cutoff = "DATE_SUB(UTC_TIMESTAMP(6), INTERVAL :age SECOND)"
            else:
                cutoff = "datetime('now', '-' || :age || ' seconds')"
            db.execute(
                text(f"DELETE FROM ha_members WHERE last_seen < {cutoff}"),
                {"age": cutoff_seconds},
            )
            db.commit()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not purge stale HA members: {exc}")


__all__ = ["list_members", "purge_stale_members", "read_lease"]

"""Xray config template stored in the database instead of a local file.

``PUT /api/core/config`` used to write ``XRAY_JSON`` on local disk, which means
two panels silently drift apart and, after a failover, the nodes get whichever
template the surviving panel happened to have. Keeping the template in the
shared database removes that whole class of bug.

The file is still read once, to seed the row on first boot. After that it is
inert.
"""

import json
from typing import Optional

import commentjson
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import logger
from app.db import GetDB
from config import XRAY_JSON, XRAY_JSON_DB_BACKED

ROW_ID = 1


def _now_expr(db) -> str:
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        return "(now() AT TIME ZONE 'utc')"
    if dialect == "mysql":
        return "UTC_TIMESTAMP(6)"
    return "datetime('now')"


def read_file() -> dict:
    with open(XRAY_JSON, "r") as file:
        return commentjson.loads(file.read())


def read_raw() -> Optional[dict]:
    """Current template as stored, or None when the row does not exist yet."""
    if not XRAY_JSON_DB_BACKED:
        return read_file()

    with GetDB() as db:
        row = db.execute(
            text("SELECT config FROM core_configs WHERE id = :id"),
            {"id": ROW_ID},
        ).first()

    if row is None:
        return None
    value = row[0]
    # JSON columns come back decoded on most drivers and as text on some.
    return json.loads(value) if isinstance(value, str) else value


def seed_from_file() -> Optional[dict]:
    """Insert the on-disk template if no row exists yet.

    Whichever panel boots first wins; the other one's insert is a no-op. The
    migration cannot do this because it has no way to resolve ``XRAY_JSON``,
    whose path differs per host.
    """
    if not XRAY_JSON_DB_BACKED:
        return read_file()

    existing = read_raw()
    if existing is not None:
        return existing

    payload = read_file()
    try:
        with GetDB() as db:
            db.execute(
                text(
                    "INSERT INTO core_configs (id, config, version, updated_at, updated_by) "
                    f"VALUES (:id, :config, 1, {_now_expr(db)}, :by)"
                ),
                {"id": ROW_ID, "config": json.dumps(payload), "by": "seed"},
            )
            db.commit()
        logger.warning(
            f"Seeded the Xray config template into the database from {XRAY_JSON}. "
            f"Edits to that file are ignored from now on; use PUT /api/core/config."
        )
    except IntegrityError:
        # The peer panel seeded it first between our read and our insert.
        return read_raw()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not seed core config into the database: {exc}")
        return payload

    return payload


def save(payload: dict, updated_by: Optional[str] = None) -> None:
    """Persist a new template and signal peer panels to reload it."""
    if not XRAY_JSON_DB_BACKED:
        with open(XRAY_JSON, "w") as file:
            file.write(json.dumps(payload, indent=4))
        return

    with GetDB() as db:
        updated = db.execute(
            text(
                "UPDATE core_configs SET config = :config, version = version + 1, "
                f"updated_at = {_now_expr(db)}, updated_by = :by WHERE id = :id"
            ),
            {"id": ROW_ID, "config": json.dumps(payload), "by": updated_by},
        ).rowcount
        if not updated:
            db.execute(
                text(
                    "INSERT INTO core_configs (id, config, version, updated_at, updated_by) "
                    f"VALUES (:id, :config, 1, {_now_expr(db)}, :by)"
                ),
                {"id": ROW_ID, "config": json.dumps(payload), "by": updated_by},
            )
        db.commit()

    from app.ha import cache
    cache.bump(cache.CORE_CONFIG)


def load_from_db():
    """Build an ``XRayConfig`` from the stored template, or None on failure."""
    from app import xray
    from app.xray.config import XRayConfig

    raw = read_raw()
    if raw is None:
        return None
    try:
        return XRayConfig(raw, api_port=xray.config.api_port)
    except ValueError as exc:
        logger.error(f"Stored Xray config template is invalid: {exc}")
        return None


__all__ = ["load_from_db", "read_file", "read_raw", "save", "seed_from_file"]

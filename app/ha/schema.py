"""Guard against a panel running old code against a newer database schema.

With two panels and a rolling upgrade, the panel that did not run the migration
can otherwise come up against a schema it does not understand and fail in
confusing ways much later. Failing loudly at startup is far cheaper.
"""

import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import logger
from app.db import GetDB

ALEMBIC_INI = "alembic.ini"


def expected_head() -> str:
    config = Config(ALEMBIC_INI)
    return ScriptDirectory.from_config(config).get_current_head()


def applied_revision() -> str:
    with GetDB() as db:
        row = db.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0] if row else None


def verify_schema_is_current() -> None:
    try:
        head = expected_head()
        applied = applied_revision()
    except (SQLAlchemyError, FileNotFoundError, Exception) as exc:  # noqa: B014
        # Never block startup because the check itself could not run.
        logger.warning(f"Could not verify database schema revision: {exc}")
        return

    if head is None or applied is None:
        return

    if applied != head:
        logger.error(
            f"Database schema is at revision {applied} but this code expects "
            f"{head}. Run `alembic upgrade head` (or start the panel that owns "
            f"HA_RUN_MIGRATIONS) before starting this one."
        )
        sys.exit(1)


__all__ = ["applied_revision", "expected_head", "verify_schema_is_current"]

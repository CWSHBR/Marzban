"""Cross-panel invalidation for the two caches that actually go stale.

Most shared state is read straight from the database on every use. Only two
things are cached in process memory and mutated through the API:

* ``xray.hosts``  -- the proxy-host table, cached by ``DictStorage``
* ``xray.config`` -- the Xray config template, and with it the templates of
  nodes that carry their own (``xray.node_configs``)

A writer bumps a counter, and every panel (leader *and* follower) polls the
counters and refreshes what changed. No message bus, no Redis.

Deliberately not covered: ``get_tls()`` and ``get_secret_key()``. Those read
rows seeded once by migrations that nothing ever writes at runtime, so their
``lru_cache`` can never be stale.
"""

from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import logger
from app.db import GetDB

HOSTS = "hosts"
CORE_CONFIG = "core_config"

# Last version this process acted on. None means "not yet observed", which is
# treated as up to date so a fresh process doesn't refresh for no reason.
_seen: Dict[str, Optional[int]] = {HOSTS: None, CORE_CONFIG: None}


def bump(key: str) -> None:
    """Signal that `key` changed, so peer panels refresh their cache."""
    try:
        with GetDB() as db:
            dialect = db.bind.dialect.name
            now = "(now() AT TIME ZONE 'utc')" if dialect == "postgresql" else (
                "UTC_TIMESTAMP(6)" if dialect == "mysql" else "datetime('now')"
            )
            db.execute(
                text(
                    f"UPDATE ha_cache_versions SET version = version + 1, "
                    f"updated_at = {now} WHERE key = :key"
                ),
                {"key": key},
            )
            db.commit()
            # This process is the writer and already applied the change, so
            # record the new version to avoid a pointless self-refresh.
            row = db.execute(
                text("SELECT version FROM ha_cache_versions WHERE key = :key"),
                {"key": key},
            ).first()
            if row:
                _seen[key] = int(row[0])
    except SQLAlchemyError as exc:
        logger.warning(f"Could not bump HA cache version for {key!r}: {exc}")


def _read_versions() -> Dict[str, int]:
    with GetDB() as db:
        rows = db.execute(text("SELECT key, version FROM ha_cache_versions")).all()
    return {row[0]: int(row[1]) for row in rows}


def _refresh_hosts() -> None:
    from app import xray
    xray.hosts.update()
    logger.info("Refreshed proxy hosts cache (changed by the peer panel)")


def _refresh_core_config() -> None:
    from app import xray
    from app.ha import core_config

    config = core_config.load_from_db()
    if config is not None:
        xray.config = config
    # The same counter covers the per-node templates. They are re-read even
    # when the shared one is unchanged, and their inbounds have to be merged
    # into the catalog again whenever xray.config is a fresh object.
    xray.node_configs.reload()
    xray.hosts.update()
    logger.info("Reloaded Xray config templates (changed by the peer panel)")


_REFRESHERS = {
    HOSTS: _refresh_hosts,
    CORE_CONFIG: _refresh_core_config,
}


def poll_cache_versions() -> None:
    """Runs on both roles."""
    try:
        versions = _read_versions()
    except SQLAlchemyError as exc:
        logger.warning(f"Could not read HA cache versions: {exc}")
        return

    for key, version in versions.items():
        previous = _seen.get(key)
        _seen[key] = version
        if previous is None or previous == version:
            continue
        refresher = _REFRESHERS.get(key)
        if refresher is None:
            continue
        try:
            refresher()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"Failed to refresh cache {key!r}: {exc}")


__all__ = ["CORE_CONFIG", "HOSTS", "bump", "poll_cache_versions"]

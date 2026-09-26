"""Database-backed leader lease.

Design notes, because the details here are load-bearing:

* Every time comparison happens on the **database** clock, inside a single
  statement. Panel host clock skew therefore cannot influence who is leader.
* The only place a panel's own clock is used is the self-fencing watchdog,
  and that uses ``time.monotonic()`` so NTP steps are irrelevant.
* The incumbent stops acting as leader at ``HA_LEASE_TTL - HA_SAFETY_MARGIN``
  on its own monotonic clock, while a challenger cannot acquire before
  ``HA_LEASE_TTL`` on the database clock. That gap -- not the lease itself --
  is the safety property.
* Losing the database is indistinguishable from losing the lease, so both
  demote. A panel that cannot renew must never keep controlling nodes.
"""

import threading
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import logger
from app.db import GetDB
from app.ha import FOLLOWER, LEADER, state
from config import (
    HA_ADVERTISE_URL,
    HA_ENABLED,
    HA_LEASE_TTL,
    HA_PANEL_ID,
    HA_REACQUIRE_BACKOFF,
    HA_RENEW_INTERVAL,
    HA_SAFETY_MARGIN,
    XRAY_LOCAL_CORE_ENABLED,
)

LEASE_NAME = "panel"

# Wall-clock expressions differ per dialect. The codebase stores naive UTC
# everywhere (datetime.utcnow), so Postgres has to be pushed to naive UTC too.
_NOW = {
    "postgresql": "(now() AT TIME ZONE 'utc')",
    "mysql": "UTC_TIMESTAMP(6)",
    "sqlite": "datetime('now')",
}
_EXPIRY = {
    "postgresql": "((now() AT TIME ZONE 'utc') + make_interval(secs => :ttl))",
    "mysql": "DATE_ADD(UTC_TIMESTAMP(6), INTERVAL :ttl SECOND)",
    "sqlite": "datetime('now', '+' || :ttl || ' seconds')",
}


def _dialect(db) -> str:
    name = db.bind.dialect.name
    return name if name in _NOW else "postgresql"


def now_expr(db) -> str:
    return _NOW[_dialect(db)]


def expiry_expr(db) -> str:
    return _EXPIRY[_dialect(db)]


class LeaseManager:
    """Owns the lease-renewal thread and the self-fencing watchdog."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._blocked_until = 0.0
        self._started_at = time.time()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not HA_ENABLED:
            # Standalone panel: promote once, no lease, no threads.
            self._run_callbacks(state.promote(epoch=0, deadline=float("inf")),
                                "promote")
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._renew_loop, name="ha-lease", daemon=True)
        self._thread.start()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="ha-watchdog", daemon=True)
        self._watchdog.start()
        logger.info(f"HA enabled, panel id \"{HA_PANEL_ID}\", "
                    f"lease ttl {HA_LEASE_TTL}s, renew every {HA_RENEW_INTERVAL}s")

    def shutdown(self) -> None:
        """Release the lease so the peer can take over without waiting a TTL."""
        self._stop.set()
        if not HA_ENABLED:
            return
        if state.is_leader():
            try:
                self.release(state.epoch)
            except SQLAlchemyError as exc:
                logger.warning(f"Could not release HA lease on shutdown: {exc}")

    # -- threads ------------------------------------------------------------

    def _renew_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except SQLAlchemyError as exc:
                # Cannot reach the database: we have no idea whether we still
                # hold the lease, so we must assume we do not.
                state.set_error(str(exc))
                logger.warning(f"HA lease tick failed: {exc}")
                self._demote("database unreachable")
            except Exception as exc:  # pragma: no cover - defensive
                state.set_error(str(exc))
                logger.exception(f"Unexpected HA lease error: {exc}")
                self._demote("unexpected lease error")

            self._stop.wait(HA_RENEW_INTERVAL)

    def _watchdog_loop(self) -> None:
        """Demote even if the renewal thread is wedged in a hung DB call.

        ``state.is_leader()`` already refuses leadership past the deadline, so
        this loop exists purely to run the demotion callbacks (stop jobs,
        detach nodes) rather than to decide anything.
        """
        while not self._stop.is_set():
            # is_leader() is false either because we are an ordinary follower --
            # in which case _demote is a no-op -- or because the deadline lapsed
            # without a renewal, which is the case this thread exists for.
            if not state.is_leader():
                self._demote("self-fencing watchdog")
            self._stop.wait(1)

    def _tick(self) -> None:
        # Fence before doing anything else. If our monotonic deadline lapsed we
        # have already stopped being leader; without this a lapsed leader would
        # fall through to the acquire branch while still holding the role flag.
        if not state.is_leader():
            self._demote("self-fencing deadline lapsed")

        with GetDB() as db:
            if state.is_leader():
                if self._renew(db):
                    state.set_error(None)
                else:
                    self._demote("lease lost or stepdown requested")
            elif time.monotonic() >= self._blocked_until:
                self._acquire(db)

            self._heartbeat(db)

        # The database answered the whole tick, so whatever made the last one fail is over.
        # Clearing only on a successful renew or acquire left a panel that was demoted by a
        # database blip -- say a Patroni switchover -- showing that error for as long as it
        # stayed a follower, because a follower's healthy tick does neither.
        state.set_error(None)

    # -- lease operations ---------------------------------------------------

    def _renew(self, db) -> bool:
        """Extend our term. Returns False when we no longer hold the lease.

        The ``stepdown_requested_epoch`` predicate is what makes the UI handover
        button work: setting it makes this statement match zero rows, which
        routes the leader into the ordinary demotion path with no extra code.
        """
        stmt = text(
            f"UPDATE ha_leases SET renewed_at = {now_expr(db)}, "
            f"expires_at = {expiry_expr(db)} "
            "WHERE name = :name AND holder = :holder AND epoch = :epoch "
            f"  AND expires_at > {now_expr(db)} "
            "  AND (stepdown_requested_epoch IS NULL "
            "       OR stepdown_requested_epoch <> :epoch)"
        )
        result = db.execute(stmt, {
            "name": LEASE_NAME,
            "holder": HA_PANEL_ID,
            "epoch": state.epoch,
            "ttl": HA_LEASE_TTL,
        })
        db.commit()

        if result.rowcount != 1:
            return False

        state.refresh_deadline(time.monotonic() + HA_LEASE_TTL - HA_SAFETY_MARGIN)
        return True

    def _acquire(self, db) -> bool:
        """Take the lease if it is free or expired, starting a new epoch."""
        stmt = text(
            "UPDATE ha_leases SET holder = :holder, epoch = epoch + 1, "
            "  advertise_url = :url, stepdown_requested_epoch = NULL, "
            f"  acquired_at = {now_expr(db)}, renewed_at = {now_expr(db)}, "
            f"  expires_at = {expiry_expr(db)} "
            "WHERE name = :name "
            f"  AND (holder IS NULL OR expires_at <= {now_expr(db)})"
        )
        result = db.execute(stmt, {
            "name": LEASE_NAME,
            "holder": HA_PANEL_ID,
            "url": HA_ADVERTISE_URL or None,
            "ttl": HA_LEASE_TTL,
        })

        if result.rowcount != 1:
            db.rollback()
            return False

        # Safe to read back: our own UPDATE holds the row lock until commit.
        row = db.execute(
            text("SELECT epoch FROM ha_leases WHERE name = :name"),
            {"name": LEASE_NAME},
        ).first()
        db.commit()

        epoch = int(row[0]) if row else 0
        deadline = time.monotonic() + HA_LEASE_TTL - HA_SAFETY_MARGIN
        logger.info(f"Acquired HA leadership as \"{HA_PANEL_ID}\", epoch {epoch}")
        self._run_callbacks(state.promote(epoch, deadline), "promote")
        state.set_error(None)
        return True

    def release(self, epoch: Optional[int] = None) -> None:
        """Give the lease up voluntarily.

        Scoped to our own holder (and epoch, when given) so this can never
        release a lease that has already moved on to the other panel.
        """
        params = {"name": LEASE_NAME, "holder": HA_PANEL_ID}
        epoch_clause = ""
        if epoch is not None:
            epoch_clause = " AND epoch = :epoch"
            params["epoch"] = epoch

        with GetDB() as db:
            db.execute(
                text(
                    f"UPDATE ha_leases SET holder = NULL, expires_at = {now_expr(db)}, "
                    "  stepdown_requested_epoch = NULL "
                    "WHERE name = :name AND holder = :holder" + epoch_clause
                ),
                params,
            )
            db.commit()

    def _heartbeat(self, db) -> None:
        """Announce this panel so the dashboard can show both sides."""
        params = {
            "panel_id": HA_PANEL_ID,
            "role": LEADER if state.is_leader() else FOLLOWER,
            "version": _panel_version(),
            "url": HA_ADVERTISE_URL or None,
            "local_core": XRAY_LOCAL_CORE_ENABLED,
        }
        updated = db.execute(
            text(
                "UPDATE ha_members SET role = :role, version = :version, "
                "  advertise_url = :url, local_core_enabled = :local_core, "
                f"  last_seen = {now_expr(db)} "
                "WHERE panel_id = :panel_id"
            ),
            params,
        ).rowcount

        if not updated:
            db.execute(
                text(
                    "INSERT INTO ha_members "
                    "  (panel_id, role, version, advertise_url, local_core_enabled, "
                    "   started_at, last_seen) "
                    f"VALUES (:panel_id, :role, :version, :url, :local_core, "
                    f"        {now_expr(db)}, {now_expr(db)})"
                ),
                params,
            )
        db.commit()

    # -- helpers ------------------------------------------------------------

    def _demote(self, reason: str) -> None:
        # Captured before demote() clears it, so the release below can scope
        # itself to the term we actually held.
        epoch = state.epoch
        callbacks = state.demote()
        if callbacks is None:
            return  # already a follower

        logger.warning(f"Demoting from HA leader: {reason}")
        # Applied before running callbacks and regardless of whether there are
        # any, so a flapping database link cannot produce a node-restart storm.
        self._blocked_until = time.monotonic() + HA_REACQUIRE_BACKOFF

        # Best-effort release, so a handover takes seconds instead of a full
        # TTL. Done before the callbacks because detaching from many nodes can
        # take a while and there is no need to make the peer wait for it: our
        # role flag is already cleared, and once the peer connects, the node
        # invalidates our session so our later detach becomes a harmless no-op.
        # Fails silently when the database is the thing that broke.
        try:
            self.release(epoch)
        except SQLAlchemyError as exc:
            logger.warning(f"Could not release the HA lease while demoting: {exc}")

        self._run_callbacks(callbacks, "demote")

    @staticmethod
    def _run_callbacks(callbacks, kind: str) -> None:
        for func in callbacks:
            try:
                func()
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(f"HA {kind} callback {func!r} failed: {exc}")


def _panel_version() -> str:
    from app import __version__
    return __version__


def request_stepdown() -> bool:
    """Ask whoever currently holds the lease to give it up.

    Written to the database rather than sent over HTTP so it works from either
    panel, including when the two panels cannot reach each other.
    """
    with GetDB() as db:
        result = db.execute(
            text(
                "UPDATE ha_leases SET stepdown_requested_epoch = epoch "
                "WHERE name = :name AND holder IS NOT NULL "
                f"  AND expires_at > {now_expr(db)}"
            ),
            {"name": LEASE_NAME},
        )
        db.commit()
        return result.rowcount == 1


manager = LeaseManager()

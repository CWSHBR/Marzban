"""Leader/follower coordination for running two Marzban panels side by side.

Exactly one panel at a time holds the lease and owns the dataplane: it controls
nodes and runs every mutating background job. The other panel serves
subscriptions and the full write API, and hands its dataplane changes over
through the outbox (see app/ha/outbox.py).

The role is not configured. It is decided by a lease row in the shared
database, so "panel A" and "panel B" are host names, not roles.
"""

import threading
import time
from typing import Callable, List, Optional

from config import HA_ENABLED, HA_PANEL_ID

LEADER = "leader"
FOLLOWER = "follower"


class HAState:
    """Process-local view of this panel's role.

    Every field is guarded by ``_lock`` because the lease thread writes them
    while request handlers and scheduler jobs read them.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # With HA disabled the panel is a standalone leader forever, which
        # keeps every single-panel install behaving exactly as before.
        self._role = FOLLOWER if HA_ENABLED else LEADER
        self._epoch = 0
        self._deadline: Optional[float] = None
        self._promoted_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._on_promote: List[Callable[[], None]] = []
        self._on_demote: List[Callable[[], None]] = []

    @property
    def enabled(self) -> bool:
        return HA_ENABLED

    @property
    def panel_id(self) -> str:
        return HA_PANEL_ID or "standalone"

    def is_leader(self) -> bool:
        if not HA_ENABLED:
            return True
        with self._lock:
            if self._role != LEADER:
                return False
            # Self-fencing: once the watchdog deadline passes we are no longer
            # allowed to act as leader even if the demotion callback has not
            # run yet. Monotonic, so NTP steps cannot extend our term.
            if self._deadline is not None and time.monotonic() > self._deadline:
                return False
            return True

    @property
    def role(self) -> str:
        return LEADER if self.is_leader() else FOLLOWER

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    def set_error(self, message: Optional[str]) -> None:
        with self._lock:
            self._last_error = message

    def seconds_until_deadline(self) -> Optional[float]:
        """Remaining self-fencing budget, for observability."""
        with self._lock:
            if not HA_ENABLED or self._role != LEADER or self._deadline is None:
                return None
            return max(0.0, self._deadline - time.monotonic())

    def uptime_as_leader(self) -> Optional[float]:
        with self._lock:
            if self._role != LEADER or self._promoted_at is None:
                return None
            return time.monotonic() - self._promoted_at

    def on_promote(self, func: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._on_promote.append(func)
        return func

    def on_demote(self, func: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._on_demote.append(func)
        return func

    def refresh_deadline(self, deadline: float) -> None:
        with self._lock:
            self._deadline = deadline

    def promote(self, epoch: int, deadline: float) -> List[Callable[[], None]]:
        """Flip to leader. Returns the callbacks the caller must run."""
        with self._lock:
            self._role = LEADER
            self._epoch = epoch
            self._deadline = deadline
            self._promoted_at = time.monotonic()
            return list(self._on_promote)

    def demote(self) -> Optional[List[Callable[[], None]]]:
        """Flip to follower.

        Returns None when this panel was already a follower, or the list of
        callbacks to run when it really was demoted. The distinction matters:
        an empty callback list is still a demotion, and the caller has to apply
        the re-acquire backoff for it.

        The role flag is cleared synchronously so no further node mutation can
        start, then the caller runs the (possibly slow, possibly failing)
        callbacks.
        """
        with self._lock:
            if self._role != LEADER:
                return None
            self._role = FOLLOWER
            self._epoch = 0
            self._deadline = None
            self._promoted_at = None
            return list(self._on_demote)


state = HAState()


def is_leader() -> bool:
    return state.is_leader()


def current_epoch() -> int:
    return state.epoch


def panel_id() -> str:
    return state.panel_id


class NotLeaderError(RuntimeError):
    """Raised when a leader-only operation is attempted after losing the lease."""


def assert_leader(epoch: Optional[int] = None) -> None:
    """Fence a dataplane mutation.

    Pure in-process check -- no database round trip -- so it is cheap enough to
    call before every node operation.
    """
    if not state.is_leader():
        raise NotLeaderError("this panel is not the HA leader")
    if epoch is not None and epoch != state.epoch:
        raise NotLeaderError(
            f"stale HA epoch {epoch}, current is {state.epoch}"
        )


def on_promote(func: Callable[[], None]) -> Callable[[], None]:
    return state.on_promote(func)


def on_demote(func: Callable[[], None]) -> Callable[[], None]:
    return state.on_demote(func)


__all__ = [
    "LEADER",
    "FOLLOWER",
    "HAState",
    "NotLeaderError",
    "assert_leader",
    "current_epoch",
    "is_leader",
    "on_demote",
    "on_promote",
    "panel_id",
    "state",
]

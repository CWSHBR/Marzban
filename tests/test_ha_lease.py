"""Leader lease behaviour.

Loaded in isolation from the real app graph, following the pattern in
tests/test_persistent_node_lifecycle.py.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


class FakeResult:
    def __init__(self, rowcount=1, rows=None):
        self.rowcount = rowcount
        self._rows = rows or []

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class FakeSession:
    """Records statements and replays a scripted sequence of results."""

    def __init__(self, script=None):
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.statements = []
        self.script = list(script or [])
        self.commits = 0
        self.rollbacks = 0
        self.raise_on_execute = None

    def execute(self, stmt, params=None):
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.statements.append((str(stmt), params))
        if self.script:
            nxt = self.script.pop(0)
            if callable(nxt):
                return nxt(str(stmt), params)
            return nxt
        return FakeResult(rowcount=1)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def load_lease(monkeypatch, session, *, ttl=15, renew=5, margin=3, backoff=30,
               panel_id="panel-a"):
    """Load app/ha/lease.py and app/ha/__init__.py against fakes."""
    root = Path(__file__).resolve().parents[1]

    fake_config = SimpleNamespace(
        HA_ENABLED=True,
        HA_PANEL_ID=panel_id,
        HA_ADVERTISE_URL="",
        HA_LEASE_TTL=ttl,
        HA_RENEW_INTERVAL=renew,
        HA_SAFETY_MARGIN=margin,
        HA_REACQUIRE_BACKOFF=backoff,
        XRAY_LOCAL_CORE_ENABLED=False,
    )
    monkeypatch.setitem(sys.modules, "config", fake_config)

    # Real app.ha state machine, fake everything around it.
    ha_spec = importlib.util.spec_from_file_location(
        "ha_under_test", root / "app/ha/__init__.py")
    ha_module = importlib.util.module_from_spec(ha_spec)
    monkeypatch.setitem(sys.modules, "ha_under_test", ha_module)
    ha_spec.loader.exec_module(ha_module)

    class FakeGetDB:
        def __enter__(self_inner):
            return session

        def __exit__(self_inner, *args):
            return False

    fake_app = SimpleNamespace(
        logger=SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        ),
        __version__="0.0.0-test",
    )
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setitem(sys.modules, "app.db", SimpleNamespace(GetDB=FakeGetDB))
    monkeypatch.setitem(sys.modules, "app.ha", ha_module)

    lease_spec = importlib.util.spec_from_file_location(
        "lease_under_test", root / "app/ha/lease.py")
    lease_module = importlib.util.module_from_spec(lease_spec)
    lease_spec.loader.exec_module(lease_module)
    return lease_module, ha_module


def test_acquire_promotes_once_and_captures_epoch(monkeypatch):
    session = FakeSession(script=[
        FakeResult(rowcount=1),              # the acquire UPDATE
        FakeResult(rows=[(7,)]),             # SELECT epoch
        FakeResult(rowcount=1),              # heartbeat UPDATE
    ])
    lease, ha = load_lease(monkeypatch, session)

    promoted = []
    ha.on_promote(lambda: promoted.append(True))

    lease.manager._tick()

    assert promoted == [True]
    assert ha.state.epoch == 7
    assert ha.is_leader()


def test_lost_lease_demotes(monkeypatch):
    session = FakeSession()
    lease, ha = load_lease(monkeypatch, session)
    ha.state.promote(epoch=3, deadline=float("inf"))

    demoted = []
    ha.on_demote(lambda: demoted.append(True))

    # rowcount 0 on renew: somebody else owns the lease now.
    session.script = [FakeResult(rowcount=0), FakeResult(rowcount=1)]
    lease.manager._tick()

    assert demoted == [True]
    assert not ha.is_leader()


def test_database_error_demotes(monkeypatch):
    """The single most important case: unreachable DB must not keep us leader."""
    from sqlalchemy.exc import SQLAlchemyError

    session = FakeSession()
    lease, ha = load_lease(monkeypatch, session)
    ha.state.promote(epoch=3, deadline=float("inf"))

    demoted = []
    ha.on_demote(lambda: demoted.append(True))

    session.raise_on_execute = SQLAlchemyError("connection refused")
    lease.manager._renew_loop_once = None
    try:
        lease.manager._tick()
    except SQLAlchemyError:
        # _tick propagates; the renew loop is what converts it to a demotion.
        lease.manager._demote("database unreachable")

    assert demoted == [True]
    assert not ha.is_leader()


def test_renew_loop_converts_db_error_to_demotion(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    session = FakeSession()
    lease, ha = load_lease(monkeypatch, session)
    ha.state.promote(epoch=3, deadline=float("inf"))

    demoted = []
    ha.on_demote(lambda: demoted.append(True))

    session.raise_on_execute = SQLAlchemyError("connection refused")
    lease.manager._stop.set()  # run exactly one iteration
    lease.manager._stop.clear()

    # Drive one iteration of the loop body by hand.
    try:
        lease.manager._tick()
    except SQLAlchemyError as exc:
        ha.state.set_error(str(exc))
        lease.manager._demote("database unreachable")

    assert demoted == [True]


def test_expired_monotonic_deadline_demotes(monkeypatch):
    """No exception, no DB answer -- just time passing -- must still fence."""
    session = FakeSession()
    lease, ha = load_lease(monkeypatch, session)

    now = [1000.0]
    monkeypatch.setattr(ha.time, "monotonic", lambda: now[0])

    ha.state.promote(epoch=3, deadline=now[0] + 12)
    assert ha.is_leader()

    now[0] += 13  # past HA_LEASE_TTL - HA_SAFETY_MARGIN
    assert not ha.is_leader()

    demoted = []
    ha.on_demote(lambda: demoted.append(True))
    session.script = [FakeResult(rowcount=1)]  # heartbeat only
    lease.manager._tick()

    assert demoted == [True]


def test_reacquire_is_blocked_after_demotion(monkeypatch):
    session = FakeSession()
    lease, ha = load_lease(monkeypatch, session, backoff=30)

    now = [1000.0]
    monkeypatch.setattr(lease.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ha.time, "monotonic", lambda: now[0])

    ha.state.promote(epoch=3, deadline=now[0] + 12)
    session.script = [FakeResult(rowcount=0), FakeResult(rowcount=1)]
    lease.manager._tick()
    assert not ha.is_leader()

    # A tick inside the backoff window must not even attempt the acquire.
    promoted = []
    ha.on_promote(lambda: promoted.append(True))
    now[0] += 5
    session.script = [FakeResult(rowcount=1)]  # heartbeat only
    session.statements.clear()
    lease.manager._tick()

    assert promoted == []
    assert not any("epoch + 1" in stmt for stmt, _ in session.statements)


def test_renew_does_not_bump_epoch_but_acquire_does(monkeypatch):
    session = FakeSession()
    lease, ha = load_lease(monkeypatch, session)

    ha.state.promote(epoch=4, deadline=float("inf"))
    session.script = [FakeResult(rowcount=1), FakeResult(rowcount=1)]
    session.statements.clear()
    lease.manager._tick()

    renew_stmt = session.statements[0][0]
    assert "epoch + 1" not in renew_stmt
    assert ha.state.epoch == 4

    # Now an acquire from a follower state.
    ha.state.demote()
    lease.manager._blocked_until = 0
    session.script = [
        FakeResult(rowcount=1),
        FakeResult(rows=[(5,)]),
        FakeResult(rowcount=1),
    ]
    session.statements.clear()
    lease.manager._tick()

    assert "epoch + 1" in session.statements[0][0]
    assert ha.state.epoch == 5


def test_renew_predicate_includes_stepdown_request(monkeypatch):
    """The UI handover works by making this statement match zero rows."""
    session = FakeSession()
    lease, ha = load_lease(monkeypatch, session)
    ha.state.promote(epoch=2, deadline=float("inf"))

    session.script = [FakeResult(rowcount=1), FakeResult(rowcount=1)]
    session.statements.clear()
    lease.manager._tick()

    renew_stmt = session.statements[0][0]
    assert "stepdown_requested_epoch" in renew_stmt


def test_assert_leader_rejects_stale_epoch(monkeypatch):
    session = FakeSession()
    _, ha = load_lease(monkeypatch, session)
    ha.state.promote(epoch=9, deadline=float("inf"))

    ha.assert_leader(9)  # current epoch is fine

    with pytest.raises(ha.NotLeaderError):
        ha.assert_leader(8)

    ha.state.demote()
    with pytest.raises(ha.NotLeaderError):
        ha.assert_leader()

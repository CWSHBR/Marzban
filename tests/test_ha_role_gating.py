"""A follower must never touch nodes, and must never write node status.

These are the two things that make two panels on one database destructive, so
they get their own tests rather than being implied by the lease tests.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_operations(monkeypatch, is_leader):
    """Load app/xray/operations.py with a scripted role."""
    root = Path(__file__).resolve().parents[1]

    enqueued = []
    fan_out = []

    fake_outbox = SimpleNamespace(
        enqueue=lambda op, user_id=None, payload=None: enqueued.append(
            {"op": op, "user_id": user_id, "payload": payload}),
        OP_ADD_USER="add_user",
        OP_UPDATE_USER="update_user",
        OP_REMOVE_USER="remove_user",
        OP_RESTART_ALL="restart_all",
    )
    fake_ha = SimpleNamespace(is_leader=lambda: is_leader)

    class FakeNode:
        connected = True
        started = True

        def __init__(self):
            self.api = SimpleNamespace()

    fake_xray = SimpleNamespace(
        nodes={1: FakeNode()},
        api=SimpleNamespace(),
        config=SimpleNamespace(inbounds_by_tag={"VLESS_TCP": {}}),
        core=SimpleNamespace(restart=lambda config: fan_out.append("core_restart")),
        exc=SimpleNamespace(
            EmailExistsError=type("EmailExistsError", (Exception,), {}),
            EmailNotFoundError=type("EmailNotFoundError", (Exception,), {}),
            ConnectionError=ConnectionError,
        ),
        operations=SimpleNamespace(
            restart_node=lambda *a, **k: fan_out.append("restart_node")),
    )

    fake_app = SimpleNamespace(
        ha=fake_ha,
        logger=SimpleNamespace(info=lambda *a, **k: None),
        xray=fake_xray,
    )

    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setitem(sys.modules, "app.ha",
                        SimpleNamespace(outbox=fake_outbox, is_leader=fake_ha.is_leader))
    monkeypatch.setitem(sys.modules, "app.ha.outbox", fake_outbox)
    monkeypatch.setitem(sys.modules, "app.db",
                        SimpleNamespace(GetDB=object, crud=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "app.models.node",
                        SimpleNamespace(NodeStatus=SimpleNamespace(
                            connected="connected", connecting="connecting",
                            error="error", disabled="disabled")))
    monkeypatch.setitem(sys.modules, "app.models.proxy",
                        SimpleNamespace(ProxyTypes=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "app.utils.concurrency",
                        SimpleNamespace(threaded_function=lambda func: func))
    monkeypatch.setitem(sys.modules, "app.xray.node", SimpleNamespace(XRayNode=object))
    monkeypatch.setitem(sys.modules, "xray_api", SimpleNamespace(XRay=object))
    monkeypatch.setitem(sys.modules, "xray_api.types.account",
                        SimpleNamespace(Account=object,
                                        XTLSFlows=SimpleNamespace(NONE="none")))

    spec = importlib.util.spec_from_file_location(
        f"operations_role_{is_leader}", root / "app/xray/operations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, enqueued, fan_out


def test_follower_enqueues_instead_of_touching_nodes(monkeypatch):
    ops, enqueued, fan_out = load_operations(monkeypatch, is_leader=False)

    dbuser = SimpleNamespace(id=42, username="bob")
    ops.add_user(dbuser)
    ops.update_user(dbuser)
    ops.remove_user(dbuser)

    assert [e["op"] for e in enqueued] == ["add_user", "update_user", "remove_user"]
    assert all(e["user_id"] == 42 for e in enqueued)
    assert fan_out == []


def test_follower_captures_email_for_removal(monkeypatch):
    """The row is usually deleted right after, so the email must be captured now."""
    ops, enqueued, _ = load_operations(monkeypatch, is_leader=False)

    ops.remove_user(SimpleNamespace(id=7, username="alice"))

    assert enqueued[0]["payload"] == {"email": "7.alice"}


def test_follower_restart_all_cores_does_not_touch_nodes(monkeypatch):
    ops, enqueued, fan_out = load_operations(monkeypatch, is_leader=False)

    ops.restart_all_cores()

    assert [e["op"] for e in enqueued] == ["restart_all"]
    assert fan_out == []


def test_follower_connect_node_is_a_noop(monkeypatch):
    """The node's /connect is last-writer-wins, so this is the destructive one."""
    ops, _, _ = load_operations(monkeypatch, is_leader=False)

    status_writes = []
    monkeypatch.setattr(ops, "_change_node_status",
                        lambda *a, **k: status_writes.append(a))

    ops.connect_node(1)

    assert status_writes == []


def test_leader_restart_all_cores_fans_out(monkeypatch):
    ops, enqueued, fan_out = load_operations(monkeypatch, is_leader=True)

    ops.restart_all_cores(config={})

    assert enqueued == []
    assert "core_restart" in fan_out
    assert "restart_node" in fan_out


def test_leader_job_registry_defers_scheduling(monkeypatch):
    """Jobs must not hit the scheduler at import time on a follower."""
    root = Path(__file__).resolve().parents[1]

    added = []
    removed = []
    promote_hooks = []
    demote_hooks = []

    fake_scheduler = SimpleNamespace(
        add_job=lambda func, trigger, **kw: added.append(kw.get("id")),
        remove_job=lambda job_id: removed.append(job_id),
    )
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(
        logger=SimpleNamespace(info=lambda *a, **k: None,
                               exception=lambda *a, **k: None),
        scheduler=fake_scheduler,
    ))
    monkeypatch.setitem(sys.modules, "app.ha", SimpleNamespace(
        on_promote=lambda f: promote_hooks.append(f) or f,
        on_demote=lambda f: demote_hooks.append(f) or f,
    ))

    spec = importlib.util.spec_from_file_location(
        "ha_jobs_under_test", root / "app/ha/jobs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.leader_job(lambda: None, "interval", seconds=10)

    # Registering must not schedule anything yet.
    assert added == []

    promote_hooks[0]()
    assert len(added) == 1

    demote_hooks[0]()
    assert removed == added

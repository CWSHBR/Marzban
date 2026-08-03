"""Outbox coalescing.

The drain is level-triggered: rows are hints, and the action is derived from
the user's current state. These tests pin that behaviour on the pure
coalescing function, which is where the logic lives.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_outbox(monkeypatch, threshold=500, age=300):
    root = Path(__file__).resolve().parents[1]

    fake_config = SimpleNamespace(
        HA_OUTBOX_BACKLOG_AGE=age,
        HA_OUTBOX_BACKLOG_THRESHOLD=threshold,
        HA_OUTBOX_BATCH_SIZE=200,
    )
    fake_app = SimpleNamespace(
        logger=SimpleNamespace(
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
    )
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setitem(sys.modules, "app.db", SimpleNamespace(GetDB=object))

    spec = importlib.util.spec_from_file_location(
        "outbox_under_test", root / "app/ha/outbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(id, op, user_id=None, payload=None):
    return {"id": id, "op": op, "user_id": user_id, "payload": payload}


def test_one_action_per_user_latest_wins(monkeypatch):
    outbox = load_outbox(monkeypatch)

    batch = outbox.coalesce([
        row(1, outbox.OP_ADD_USER, user_id=10),
        row(2, outbox.OP_UPDATE_USER, user_id=10),
        row(3, outbox.OP_UPDATE_USER, user_id=11),
        row(4, outbox.OP_REMOVE_USER, user_id=10, payload={"email": "10.bob"}),
    ])

    assert set(batch["users"]) == {10, 11}
    # Highest id for user 10 is the remove.
    assert batch["users"][10]["op"] == outbox.OP_REMOVE_USER
    assert batch["users"][10]["payload"] == {"email": "10.bob"}
    assert batch["users"][11]["op"] == outbox.OP_UPDATE_USER


def test_all_ids_are_marked_done_even_when_coalesced(monkeypatch):
    outbox = load_outbox(monkeypatch)

    batch = outbox.coalesce([
        row(1, outbox.OP_ADD_USER, user_id=10),
        row(2, outbox.OP_UPDATE_USER, user_id=10),
        row(3, outbox.OP_RESTART_ALL),
    ])

    # Nothing may be left behind, or the drain would loop on it forever.
    assert batch["ids"] == [1, 2, 3]


def test_restart_all_collapses_to_a_single_flag(monkeypatch):
    outbox = load_outbox(monkeypatch)

    batch = outbox.coalesce([
        row(1, outbox.OP_RESTART_ALL),
        row(2, outbox.OP_RESTART_ALL),
        row(3, outbox.OP_RESTART_ALL),
    ])

    assert batch["restart_all"] is True
    assert batch["users"] == {}


def test_rows_without_a_user_are_ignored(monkeypatch):
    outbox = load_outbox(monkeypatch)

    batch = outbox.coalesce([
        row(1, outbox.OP_UPDATE_USER, user_id=None),
        row(2, "something_unknown", user_id=5),
    ])

    assert batch["users"] == {}
    assert batch["ids"] == [1, 2]
    assert batch["restart_all"] is False


def test_empty_batch(monkeypatch):
    outbox = load_outbox(monkeypatch)

    batch = outbox.coalesce([])

    assert batch == {"ids": [], "users": {}, "restart_all": False}


def test_drain_is_a_noop_on_a_follower(monkeypatch):
    outbox = load_outbox(monkeypatch)
    monkeypatch.setitem(sys.modules, "app",
                        SimpleNamespace(ha=SimpleNamespace(is_leader=lambda: False),
                                        logger=SimpleNamespace()))

    called = []
    monkeypatch.setattr(outbox, "_select_pending", lambda db: called.append(True))

    outbox.drain()

    assert called == []

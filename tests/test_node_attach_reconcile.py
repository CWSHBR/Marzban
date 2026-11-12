"""A promoted panel attaches to running nodes and only diffs their users."""
from types import SimpleNamespace

import pytest

from tests.test_persistent_node_lifecycle import load_operations_module


class TagNotFound(Exception):
    pass


class XrayError(Exception):
    details = "boom"


class EmailExists(Exception):
    pass


class EmailNotFound(Exception):
    pass


class ConnError(Exception):
    pass


class FakeAPI:
    def __init__(self, inbounds):
        self.inbounds = {tag: set(emails) for tag, emails in inbounds.items()}
        self.added, self.removed = [], []

    def get_inbound_users(self, tag, timeout=None):
        if tag not in self.inbounds:
            raise TagNotFound(tag)
        return [SimpleNamespace(email=email) for email in sorted(self.inbounds[tag])]

    def add_inbound_user(self, tag, user, timeout=None):
        self.added.append((tag, user.email))

    def remove_inbound_user(self, tag, email, timeout=None):
        self.removed.append((tag, email))


class DB:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def node_config(**inbounds):
    return {"inbounds": [
        {"tag": tag, "settings": {"clients": [{"email": email} for email in emails]}}
        for tag, emails in inbounds.items()
    ]}


def setup(monkeypatch, api, snapshots, users):
    ops = load_operations_module(monkeypatch)
    ops.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    ops.xray.exc = SimpleNamespace(TagNotFoundError=TagNotFound, XrayError=XrayError,
                                   EmailExistsError=EmailExists, EmailNotFoundError=EmailNotFound,
                                   ConnectionError=ConnError)
    snapshots = list(snapshots)
    ops.xray.config = SimpleNamespace(
        inbounds_by_tag={"reality": {}, "cdn": {}, "hy2": {}},
        include_db_users=lambda: snapshots.pop(0),
    )
    ops.xray.nodes[7] = SimpleNamespace(api=api)
    ops.GetDB = DB
    ops.crud = SimpleNamespace(get_user_by_id=lambda db, user_id: users.get(user_id))
    ops._user_accounts = lambda dbuser, *_: [
        (tag, SimpleNamespace(email=f"{dbuser.id}.{dbuser.username}")) for tag in dbuser.tags
    ]
    return ops


def test_reconcile_adds_missing_and_removes_stale_users(monkeypatch):
    api = FakeAPI({"reality": {"1.a", "2.b", "9.gone"}})  # cdn/hy2 not served here
    snapshot = node_config(reality=["1.a", "2.b", "3.c"], cdn=["1.a"])
    users = {3: SimpleNamespace(id=3, username="c", tags=["reality", "cdn"])}
    ops = setup(monkeypatch, api, [snapshot, snapshot], users)

    ops.reconcile_node_users(7, "DOOR-NL-2")

    assert api.added == [("reality", "3.c")]  # not cdn: the node has no such inbound
    assert api.removed == [("reality", "9.gone")]


def test_reconcile_leaves_users_that_changed_between_snapshots(monkeypatch):
    # 4.new was created and 2.b deleted by the API while the node was listed.
    api = FakeAPI({"reality": {"1.a", "4.new"}})
    before = node_config(reality=["1.a", "2.b"])
    after = node_config(reality=["1.a", "4.new"])
    users = {2: SimpleNamespace(id=2, username="b", tags=["reality"])}
    ops = setup(monkeypatch, api, [before, after], users)

    ops.reconcile_node_users(7)

    assert api.added == []
    assert api.removed == []


def load_connect(monkeypatch, leader_answers, start_response):
    ops = load_operations_module(monkeypatch)
    answers = list(leader_answers)
    ops.ha = SimpleNamespace(is_leader=lambda: answers.pop(0) if answers else False)
    started, reconciled = [], []
    node = SimpleNamespace(connected=True, start=lambda config: started.append(config) or start_response,
                           get_version=lambda: "26.6.27")
    ops.xray.nodes[1] = node
    ops.xray.config = SimpleNamespace(include_db_users=lambda: {"cfg": True})
    ops.GetDB = DB
    ops.crud = SimpleNamespace(get_node_by_id=lambda db, node_id: SimpleNamespace(id=1, name="DOOR-RU-1"))
    ops._change_node_status = lambda *a, **k: None
    ops.reconcile_node_users = lambda node_id, name=None: reconciled.append(node_id)
    return ops, started, reconciled


def test_connect_node_does_not_start_after_losing_leadership(monkeypatch):
    ops, started, reconciled = load_connect(monkeypatch, [True, False], {"attached": True})

    ops.connect_node(1)

    assert started == []
    assert reconciled == []


def test_connect_node_reconciles_only_attached_nodes(monkeypatch):
    ops, started, reconciled = load_connect(monkeypatch, [True, True], {"attached": True})
    ops.connect_node(1)
    assert started and reconciled == [1]

    ops, started, reconciled = load_connect(monkeypatch, [True, True], {"attached": False})
    ops.connect_node(1)
    assert started and reconciled == []


def test_get_inbound_users_wire_format_round_trip():
    pytest.importorskip("grpc")
    from xray_api import proxyman
    from xray_api.proto.common.protocol import user_pb2

    assert proxyman._encode_get_inbound_users_request("x" * 200)[:3] == b"\x0a\xc8\x01"
    body = b"".join(
        b"\x0a" + bytes([len(raw)]) + raw
        for raw in (user_pb2.User(email="1.a", level=0).SerializeToString(),
                    user_pb2.User(email="2.b").SerializeToString())
    )
    assert [u.email for u in proxyman._decode_get_inbound_users_response(body)] == ["1.a", "2.b"]

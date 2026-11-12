"""A node can boot from an Xray config of its own instead of the shared one."""
import importlib
from types import SimpleNamespace

import pytest

from app.xray.config import XRayConfig, template_hash
from tests.test_persistent_node_lifecycle import load_operations_module


def inbound(tag, port=443, protocol="vless", network="tcp", **extra):
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": protocol,
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {"network": network},
        **extra,
    }


def template(*inbounds, outbound="direct"):
    return {
        "inbounds": list(inbounds),
        "outbounds": [{"tag": outbound, "protocol": "freedom"}],
    }


# --- the catalog ----------------------------------------------------------


def test_catalog_adds_node_only_tags_and_never_overrides_shared_ones():
    shared = XRayConfig(template(inbound("shared-in", port=443)))
    node = XRayConfig(template(inbound("shared-in", port=8443), inbound("node-in", port=2053)))

    shared.extend_catalog(node.own_inbounds)

    assert list(shared.inbounds_by_tag) == ["shared-in", "node-in"]
    assert shared.inbounds_by_tag["shared-in"]["port"] == 443
    assert [i["tag"] for i in shared.inbounds_by_protocol["vless"]] == ["shared-in", "node-in"]
    # what the config itself defines is untouched
    assert list(shared.own_inbounds_by_tag) == ["shared-in"]


def test_catalog_extension_is_replaced_not_accumulated():
    shared = XRayConfig(template(inbound("shared-in")))
    shared.extend_catalog(XRayConfig(template(inbound("a", port=1))).own_inbounds)
    shared.extend_catalog(XRayConfig(template(inbound("b", port=2))).own_inbounds)

    assert list(shared.inbounds_by_tag) == ["shared-in", "b"]

    shared.extend_catalog([])
    assert list(shared.inbounds_by_tag) == ["shared-in"]


def test_template_hash_ignores_key_order_and_the_panel_api_port():
    a = template(inbound("in"))
    b = {"outbounds": a["outbounds"], "inbounds": a["inbounds"]}

    assert template_hash(a) == template_hash(b)
    assert XRayConfig(a, api_port=1111).template_hash == XRayConfig(b, api_port=2222).template_hash
    assert XRayConfig(a).template_hash != XRayConfig(template(inbound("in", port=444))).template_hash


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def join(self, *a, **k):
        return self

    outerjoin = filter = group_by = join

    def all(self):
        return self.rows


def _fake_db(monkeypatch, rows):
    class DB:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        def query(self, *a, **k):
            return _Rows(rows)

    class GetDB:
        def __enter__(self):
            return DB()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(importlib.import_module("app.xray.config"), "GetDB", GetDB)


def test_users_go_only_into_inbounds_the_config_itself_defines(monkeypatch):
    """The shared config must not trip over a tag that lives on a node."""
    shared = XRayConfig(template(inbound("shared-in")))
    node = XRayConfig(template(inbound("node-in", port=2053)))
    shared.extend_catalog(node.own_inbounds)
    _fake_db(monkeypatch, [SimpleNamespace(
        id=7, username="bob", type="vless",
        settings={"id": "11111111-1111-1111-1111-111111111111"},
        excluded_inbound_tags=None,
    )])

    shared_clients = shared.include_db_users().get_inbound("shared-in")["settings"]["clients"]
    node_clients = node.include_db_users().get_inbound("node-in")["settings"]["clients"]

    assert [c["email"] for c in shared_clients] == ["7.bob"]
    assert [c["email"] for c in node_clients] == ["7.bob"]
    assert shared.include_db_users().get_inbound("node-in") is None


# --- describing a candidate ----------------------------------------------


@pytest.fixture
def node_configs(monkeypatch):
    from app import xray
    module = xray.node_configs

    shared = XRayConfig(template(inbound("shared-in", port=443), inbound("ws-in", port=8080, network="ws")))
    monkeypatch.setattr(xray, "config", shared)
    monkeypatch.setattr(module, "_configs", {})
    monkeypatch.setattr(module, "_names", {})
    return module


def test_describe_classifies_inbounds_and_lists_dropped_tags(node_configs):
    candidate = node_configs.build(template(
        inbound("shared-in", port=443),
        inbound("ws-in", port=9090, network="ws"),
        inbound("node-in", port=2053),
    ))

    summary = node_configs.describe(1, candidate)

    assert {i["tag"]: i["origin"] for i in summary["inbounds"]} == {
        "shared-in": "shared", "ws-in": "override", "node-in": "node_only"}
    assert summary["dropped_tags"] == []
    assert [(w["code"], w["tag"], w["fields"]) for w in summary["warnings"]] == [
        ("client_params_differ", "ws-in", ["port"])]

    lean = node_configs.build(template(inbound("node-in", port=2053)))
    assert node_configs.describe(1, lean)["dropped_tags"] == ["shared-in", "ws-in"]


def test_describe_rejects_a_tag_reused_with_another_protocol(node_configs):
    candidate = node_configs.build(template({
        "tag": "shared-in", "port": 443, "protocol": "trojan",
        "settings": {"clients": []}, "streamSettings": {"network": "tcp"},
    }))

    with pytest.raises(ValueError, match="must keep its protocol"):
        node_configs.describe(1, candidate)


def test_describe_warns_when_two_nodes_disagree_on_a_node_only_tag(node_configs, monkeypatch):
    other = node_configs.build(template(inbound("node-in", port=2053)))
    monkeypatch.setattr(node_configs, "_configs", {2: other})
    monkeypatch.setattr(node_configs, "_names", {2: "NL-2"})

    same = node_configs.describe(1, node_configs.build(template(inbound("node-in", port=2053))))
    differs = node_configs.describe(1, node_configs.build(template(inbound("node-in", port=2054))))

    assert same["warnings"] == []
    assert [(w["code"], w["node"]) for w in differs["warnings"]] == [("node_tag_conflict", "NL-2")]
    # a node is never compared with its own stored config
    assert node_configs.describe(2, node_configs.build(template(inbound("node-in", port=2054))))["warnings"] == []


def test_build_turns_malformed_input_into_a_value_error(node_configs):
    with pytest.raises(ValueError):
        node_configs.build({"inbounds": []})
    with pytest.raises(ValueError):
        node_configs.build({"inbounds": [{"tag": "x"}], "outbounds": [{"tag": "d"}]})


# --- the dataplane --------------------------------------------------------


class FakeAPI:
    def __init__(self):
        self.calls = []

    def add_inbound_user(self, tag, user, timeout=None):
        self.calls.append(("add", tag, user.email))

    def remove_inbound_user(self, tag, email, timeout=None):
        self.calls.append(("remove", tag, email))


def _ops(monkeypatch, shared_tags, own):
    """operations with two running nodes; `own` maps node id -> its own tags."""
    ops = load_operations_module(monkeypatch)
    ops.xray.exc = SimpleNamespace(EmailExistsError=KeyError, EmailNotFoundError=KeyError,
                                   ConnectionError=OSError)
    ops.xray.api = None
    ops.xray.config = SimpleNamespace(
        inbounds_by_tag={t: {} for t in shared_tags},
        template_hash="shared-hash",
        include_db_users=lambda: {"template": "shared"},
    )
    templates = {
        node_id: SimpleNamespace(
            inbounds_by_tag={t: {} for t in tags},
            own_inbounds_by_tag={t: {} for t in tags},
            template_hash=f"own-hash-{node_id}",
            include_db_users=lambda node_id=node_id: {"template": node_id},
        )
        for node_id, tags in own.items()
    }
    ops.xray.node_configs = SimpleNamespace(get=templates.get)
    apis = {}
    for node_id in (1, 2):
        apis[node_id] = FakeAPI()
        ops.xray.nodes[node_id] = SimpleNamespace(connected=True, started=True, api=apis[node_id])
    return ops, apis


class _Account(SimpleNamespace):
    flow = None


class _ProxyType:
    """Stands in for ProxyTypes.VLESS: hashable, and builds the account."""

    @staticmethod
    def account_model(email, **settings):
        return _Account(email=email)


def _vless(ops):
    proxy_type = _ProxyType()
    ops._user_view = lambda dbuser: SimpleNamespace(
        inbounds={proxy_type: dbuser.tags},
        proxies={proxy_type: SimpleNamespace(dict=lambda **k: {})},
    )


def test_a_user_is_pushed_only_to_inbounds_each_node_serves(monkeypatch):
    ops, apis = _ops(monkeypatch, shared_tags=["shared-in", "node-in"], own={2: ["node-in"]})
    _vless(ops)

    ops._add_user_direct(SimpleNamespace(id=7, username="bob", tags=["shared-in", "node-in"]))

    # node 1 runs the shared template, whose catalog lists node-in as well
    assert apis[1].calls == [("add", "shared-in", "7.bob"), ("add", "node-in", "7.bob")]
    assert apis[2].calls == [("add", "node-in", "7.bob")]


def test_removing_a_user_touches_only_inbounds_each_node_serves(monkeypatch):
    ops, apis = _ops(monkeypatch, shared_tags=["shared-in"], own={2: ["node-in", "extra-in"]})

    ops.remove_user_by_email("7.bob")

    assert apis[1].calls == [("remove", "shared-in", "7.bob")]
    assert apis[2].calls == [("remove", "node-in", "7.bob"), ("remove", "extra-in", "7.bob")]


def test_updating_a_user_removes_it_from_inbounds_it_left_per_node(monkeypatch):
    ops, apis = _ops(monkeypatch, shared_tags=["shared-in"], own={2: ["node-in", "extra-in"]})
    _vless(ops)

    ops._update_user_direct(SimpleNamespace(id=7, username="bob", tags=["shared-in", "node-in"]))

    assert apis[1].calls == [("remove", "shared-in", "7.bob"), ("add", "shared-in", "7.bob")]
    assert apis[2].calls == [("remove", "node-in", "7.bob"), ("add", "node-in", "7.bob"),
                             ("remove", "extra-in", "7.bob")]


def test_a_node_with_its_own_template_ignores_the_shared_snapshot(monkeypatch):
    ops, _ = _ops(monkeypatch, shared_tags=["shared-in"], own={2: ["node-in"]})
    handed_over = {"template": "shared, built once by the caller"}

    assert ops._startup_config(1, handed_over) == (handed_over, "shared-hash")
    assert ops._startup_config(1) == ({"template": "shared"}, "shared-hash")
    assert ops._startup_config(2, handed_over) == ({"template": 2}, "own-hash-2")


def test_restart_boots_the_node_from_its_own_template_and_records_it(monkeypatch):
    ops, _ = _ops(monkeypatch, shared_tags=["shared-in"], own={2: ["node-in"]})
    booted, applied = [], []
    ops.xray.nodes[2] = SimpleNamespace(connected=True, restart=booted.append)
    ops.crud = SimpleNamespace(
        get_node_by_id=lambda db, node_id: SimpleNamespace(id=node_id, name="NL-2"),
        set_node_applied_config_hash=lambda db, node_id, h, only_if_unknown=False: applied.append((node_id, h)),
    )

    class DB:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    ops.GetDB = DB

    ops.restart_node(2, {"template": "shared"})

    assert booted == [{"template": 2}]
    assert applied == [(2, "own-hash-2")]


def test_a_follower_asks_the_leader_to_restart_just_that_node(monkeypatch):
    ops, _ = _ops(monkeypatch, shared_tags=["shared-in"], own={})
    queued = []
    ops.ha = SimpleNamespace(is_leader=lambda: False)
    ops.outbox = SimpleNamespace(OP_RESTART_NODE="restart_node",
                                 enqueue=lambda op, **kw: queued.append((op, kw)))

    ops.restart_node(2)

    assert queued == [("restart_node", {"payload": {"node_id": 2}})]


# --- the API --------------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    """The real app on a throwaway SQLite database, one node, sudo admin."""
    from fastapi.testclient import TestClient

    import app as app_pkg
    from app import xray
    from app.db import GetDB
    from app.db import models as db_models
    from app.db.base import Base, engine
    from app.models.admin import Admin

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with GetDB() as db:
        db.add(db_models.Node(id=1, name="NL-2", address="10.0.0.2", port=62050, api_port=62051))
        db.commit()

    shared = XRayConfig(template(inbound("shared-in", port=443)))
    monkeypatch.setattr(xray, "config", shared)
    monkeypatch.setattr(xray.node_configs, "_configs", {})
    monkeypatch.setattr(xray.node_configs, "_names", {})
    restarted = []
    monkeypatch.setattr(xray.operations, "restart_node", lambda node_id, config=None: restarted.append(node_id))

    app_pkg.app.dependency_overrides[Admin.check_sudo_admin] = lambda: Admin(username="root", is_sudo=True)
    client = TestClient(app_pkg.app)
    client.restarted = restarted
    yield client
    app_pkg.app.dependency_overrides.clear()
    xray.hosts.clear()
    Base.metadata.drop_all(engine)


OWN = template(inbound("shared-in", port=443), inbound("node-in", port=2053), outbound="warp")


def test_a_node_starts_on_the_shared_config(api):
    body = api.get("/api/node/1/config").json()

    assert body["custom"] is False
    assert body["config"] == body["shared_config"]
    assert api.get("/api/nodes").json()[0]["has_custom_config"] is False


def test_saving_stores_the_config_without_restarting(api):
    from app import xray

    res = api.put("/api/node/1/config", json=OWN)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["custom"] is True
    assert body["config"] == OWN
    assert body["updated_by"] == "root"
    assert {i["tag"]: i["origin"] for i in body["summary"]["inbounds"]} == {
        "shared-in": "shared", "node-in": "node_only"}
    # node-in was new when validated; saving registered it
    assert body["summary"]["new_tags"] == []
    assert api.restarted == []
    # the node-only tag is now a first-class inbound for users and hosts
    assert "node-in" in xray.config.inbounds_by_tag
    assert "node-in" in api.get("/api/hosts").json()
    assert xray.node_configs.get(1).get_outbound("warp")
    assert api.get("/api/nodes").json()[0]["has_custom_config"] is True


def test_save_and_restart_restarts_only_that_node(api):
    assert api.put("/api/node/1/config?restart=true", json=OWN).status_code == 200
    assert api.restarted == [1]


def test_a_new_tag_gets_a_host_at_the_node_not_at_the_panel(api):
    api.put("/api/node/1/config", json=OWN)

    hosts = api.get("/api/hosts").json()
    assert [h["address"] for h in hosts["node-in"]] == ["10.0.0.2"]
    # a shared tag keeps the stock default
    assert [h["address"] for h in hosts["shared-in"]] == ["{SERVER_IP}"]

    # saving again must not pile up a second host, nor resurrect a removed one
    api.put("/api/hosts", json={"node-in": []})
    api.put("/api/node/1/config", json=OWN)
    assert api.get("/api/hosts").json()["node-in"] == []


def test_an_invalid_config_is_rejected_and_nothing_is_stored(api):
    assert api.put("/api/node/1/config", json={"inbounds": []}).status_code == 400
    clash = template({"tag": "shared-in", "port": 443, "protocol": "trojan",
                      "settings": {"clients": []}, "streamSettings": {"network": "tcp"}})
    res = api.post("/api/node/1/config/validate", json=clash)
    assert res.status_code == 400
    assert "must keep its protocol" in res.json()["detail"]
    assert api.get("/api/node/1/config").json()["custom"] is False


def test_validate_reports_without_saving(api):
    res = api.post("/api/node/1/config/validate", json=OWN)

    assert res.status_code == 200
    assert res.json()["new_tags"] == ["node-in"]
    assert api.get("/api/node/1/config").json()["custom"] is False
    # validation has no side effects: the tag is still unknown
    assert api.post("/api/node/1/config/validate", json=OWN).json()["new_tags"] == ["node-in"]


def test_reset_returns_the_node_to_the_shared_config(api):
    from app import xray

    api.put("/api/node/1/config", json=OWN)
    body = api.delete("/api/node/1/config?restart=true").json()

    assert body["custom"] is False
    assert body["config"] == body["shared_config"]
    assert api.restarted == [1]
    assert xray.node_configs.get(1) is None
    assert "node-in" not in xray.config.inbounds_by_tag


def test_pending_compares_the_applied_template_with_the_saved_one(api):
    from app import xray
    from app.db import GetDB, crud

    assert api.get("/api/nodes").json()[0]["config_pending"] is None

    with GetDB() as db:
        crud.set_node_applied_config_hash(db, 1, xray.config.template_hash)
    assert api.get("/api/nodes").json()[0]["config_pending"] is False

    api.put("/api/node/1/config", json=OWN)
    assert api.get("/api/nodes").json()[0]["config_pending"] is True

    with GetDB() as db:
        crud.set_node_applied_config_hash(db, 1, xray.node_configs.get(1).template_hash)
    assert api.get("/api/node/1/config").json()["pending"] is False


def test_an_attached_node_only_fills_in_an_unknown_applied_template(api):
    from app.db import GetDB, crud
    from app.db import models as db_models

    def applied():
        with GetDB() as db:
            return db.query(db_models.Node).get(1).applied_config_hash

    with GetDB() as db:
        crud.set_node_applied_config_hash(db, 1, "first-contact", only_if_unknown=True)
    assert applied() == "first-contact"

    with GetDB() as db:
        crud.set_node_applied_config_hash(db, 1, "saved-later", only_if_unknown=True)
    assert applied() == "first-contact"

    with GetDB() as db:
        crud.set_node_applied_config_hash(db, 1, "restarted")
    assert applied() == "restarted"


def test_replacing_the_shared_config_keeps_node_only_tags_in_the_catalog(api):
    from app import xray

    api.put("/api/node/1/config", json=OWN)
    xray.config = XRayConfig(template(inbound("shared-in", port=443), inbound("new-in", port=444)))
    xray.node_configs.apply_catalog()

    assert list(xray.config.inbounds_by_tag) == ["shared-in", "new-in", "node-in"]


def test_deleting_the_node_takes_its_tags_out_of_the_catalog(api, monkeypatch):
    from app import xray

    monkeypatch.setattr(xray.operations, "remove_node", lambda node_id, stop=False: None)
    api.put("/api/node/1/config", json=OWN)

    assert api.delete("/api/node/1").status_code == 200
    assert "node-in" not in xray.config.inbounds_by_tag

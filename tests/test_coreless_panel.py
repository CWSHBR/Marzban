"""Panel running as a pure control plane, with no local Xray process.

The one that actually matters is the usage accounting: `node_id = None` is the
"Master" row in usage reports, so a coreless panel that still registered that
key would write a phantom node into every report forever.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


def load_record_usages(monkeypatch, local_core_enabled, node_ids=()):
    root = Path(__file__).resolve().parents[1]

    class FakeNode:
        connected = True
        started = True
        usage_coefficient = 1.0

        def __init__(self, node_id):
            self.api = SimpleNamespace(name=f"node-{node_id}")

    fake_xray = SimpleNamespace(
        api=SimpleNamespace(name="local") if local_core_enabled else None,
        nodes={nid: FakeNode(nid) for nid in node_ids},
    )
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(
        ha=SimpleNamespace(is_leader=lambda: True),
        xray=fake_xray,
    ))
    monkeypatch.setitem(sys.modules, "app.db", SimpleNamespace(GetDB=object))
    monkeypatch.setitem(sys.modules, "app.db.models", SimpleNamespace(
        Admin=object, NodeUsage=object, NodeUserUsage=object,
        System=object, User=object))
    monkeypatch.setitem(sys.modules, "app.ha.jobs",
                        SimpleNamespace(leader_job=lambda func, *a, **k: func))
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(
        DISABLE_RECORDING_NODE_USAGE=False,
        JOB_RECORD_NODE_USAGES_INTERVAL=30,
        JOB_RECORD_USER_USAGES_INTERVAL=10,
    ))
    monkeypatch.setitem(sys.modules, "xray_api", SimpleNamespace(
        XRay=object, exc=SimpleNamespace(XrayError=Exception)))

    spec = importlib.util.spec_from_file_location(
        f"record_usages_core_{local_core_enabled}",
        root / "app/jobs/record_usages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake_xray


def _captured_api_instances(monkeypatch, module, entrypoint):
    """Run the job far enough to see which APIs it decided to poll."""
    captured = {}

    def fake_get_stats(api):
        captured[getattr(api, "name", None)] = True
        return []

    monkeypatch.setattr(module, "get_users_stats", fake_get_stats, raising=False)
    monkeypatch.setattr(module, "get_outbounds_stats", fake_get_stats, raising=False)
    entrypoint()
    return captured


def test_coreless_panel_polls_only_nodes(monkeypatch):
    module, _ = load_record_usages(monkeypatch, local_core_enabled=False,
                                   node_ids=(1, 2))

    captured = _captured_api_instances(monkeypatch, module, module.record_user_usages)

    assert set(captured) == {"node-1", "node-2"}
    # No "local" entry means no node_id=None row will ever be written.
    assert "local" not in captured


def test_panel_with_local_core_still_polls_it(monkeypatch):
    module, _ = load_record_usages(monkeypatch, local_core_enabled=True,
                                   node_ids=(1,))

    captured = _captured_api_instances(monkeypatch, module, module.record_user_usages)

    assert set(captured) == {"local", "node-1"}


def test_coreless_node_usages_skips_master(monkeypatch):
    module, _ = load_record_usages(monkeypatch, local_core_enabled=False,
                                   node_ids=(1,))

    captured = _captured_api_instances(monkeypatch, module, module.record_node_usages)

    assert set(captured) == {"node-1"}


def test_usage_jobs_are_noops_on_a_follower(monkeypatch):
    module, _ = load_record_usages(monkeypatch, local_core_enabled=True,
                                   node_ids=(1,))
    monkeypatch.setitem(sys.modules, "app",
                        SimpleNamespace(ha=SimpleNamespace(is_leader=lambda: False)))
    module.ha = SimpleNamespace(is_leader=lambda: False)

    captured = _captured_api_instances(monkeypatch, module, module.record_user_usages)

    # Two panels draining the same node with reset=True would split the
    # counters arbitrarily between them.
    assert captured == {}


def test_null_core_reports_disabled_without_lying(monkeypatch):
    root = Path(__file__).resolve().parents[1]

    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *a, **k: None,
                               debug=lambda *a, **k: None)))
    monkeypatch.setitem(sys.modules, "app.xray.config",
                        SimpleNamespace(XRayConfig=dict))
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(DEBUG=False))

    spec = importlib.util.spec_from_file_location(
        "xray_core_under_test", root / "app/xray/core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    core = module.NullXRayCore()

    assert core.started is False
    assert core.version is None
    # Every lifecycle call must be a safe no-op, not an exception.
    core.start({})
    core.restart({})
    core.stop()
    with core.get_logs() as logs:
        assert list(logs) == []


_T0 = datetime(2020, 1, 1)
_T1 = datetime(2030, 1, 1)

# Cross-checked against `xray x25519 -i <priv>` from the node's own binary
# (Xray 25.x): the public key below is what the real core prints.
REALITY_PRIVATE_KEY = "cIjLBnHiXd6ZmSD2xrnHKW-t8FnHBWCwSPGAV2Xw3Uw"
REALITY_PUBLIC_KEY = "lOWdwqAaLqqY8GbzEYfunpZeyonbDQWLJ_oSU7hfHGo"


def _load_core_module(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *a, **k: None,
                               debug=lambda *a, **k: None)))
    monkeypatch.setitem(sys.modules, "app.xray.config",
                        SimpleNamespace(XRayConfig=dict))
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(DEBUG=False))

    spec = importlib.util.spec_from_file_location(
        "xray_core_x25519", root / "app/xray/core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_null_core_derives_reality_public_key(monkeypatch):
    """Without this the panel cannot even parse its own config.

    ``XRayConfig`` derives ``publicKey`` from ``privateKey`` for every reality
    inbound that omits it -- which is what Xray's own key generator produces --
    and raises when the derivation comes back empty.
    """
    core = _load_core_module(monkeypatch).NullXRayCore()

    keys = core.get_x25519(REALITY_PRIVATE_KEY)

    assert keys["public_key"] == REALITY_PUBLIC_KEY
    assert keys["private_key"] == REALITY_PRIVATE_KEY


def test_null_core_generates_a_usable_keypair(monkeypatch):
    core = _load_core_module(monkeypatch).NullXRayCore()

    keys = core.get_x25519()

    # Round-tripping proves the generated private key is in the same base64url
    # form Xray expects, not just any 32 bytes.
    assert core.get_x25519(keys["private_key"]) == keys


def test_null_core_rejects_a_malformed_private_key(monkeypatch):
    core = _load_core_module(monkeypatch).NullXRayCore()

    assert core.get_x25519("not-a-key!!") is None
    assert core.get_x25519("c2hvcnQ") is None  # decodes, but not 32 bytes


def test_coreless_panel_parses_a_reality_inbound():
    """End to end through the real XRayConfig, with the core disabled."""
    import json

    from app.xray.config import XRayConfig

    config = XRayConfig(json.dumps({
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "vless-reality",
            "listen": "0.0.0.0",
            "port": 8443,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "dest": "www.google.com:443",
                    "serverNames": ["www.google.com"],
                    "privateKey": REALITY_PRIVATE_KEY,
                    "shortIds": ["6ba85179e30d4fc2"],
                },
            },
        }],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }), api_port=12345)

    assert config.inbounds_by_tag["vless-reality"]["pbk"] == REALITY_PUBLIC_KEY


def test_coreless_usage_report_drops_the_empty_master_row(monkeypatch):
    """The "Master" row is synthesised by the CRUD, not read from the table.

    On a coreless panel nothing can ever land in it, so it would show up as a
    permanent zero-traffic node in every report.
    """
    from types import SimpleNamespace as NS

    import app.db.crud as crud

    monkeypatch.setattr(crud, "XRAY_LOCAL_CORE_ENABLED", False)
    db = _fake_usage_db(nodes=[NS(id=1, name="node-a")], usage_rows=[])

    rows = crud.get_nodes_usage(db, start=_T0, end=_T1)

    assert [r.node_name for r in rows] == ["node-a"]


def test_coreless_usage_report_keeps_master_when_it_has_history(monkeypatch):
    """Switching an existing panel to coreless must not erase its past traffic."""
    from types import SimpleNamespace as NS

    import app.db.crud as crud

    monkeypatch.setattr(crud, "XRAY_LOCAL_CORE_ENABLED", False)
    db = _fake_usage_db(
        nodes=[NS(id=1, name="node-a")],
        usage_rows=[NS(node_id=None, uplink=10, downlink=20)])

    rows = {r.node_name: r for r in crud.get_nodes_usage(db, start=_T0, end=_T1)}

    assert rows["Master"].uplink == 10
    assert rows["Master"].downlink == 20


def test_panel_with_a_local_core_still_reports_master(monkeypatch):
    from types import SimpleNamespace as NS

    import app.db.crud as crud

    monkeypatch.setattr(crud, "XRAY_LOCAL_CORE_ENABLED", True)
    db = _fake_usage_db(nodes=[NS(id=1, name="node-a")], usage_rows=[])

    rows = crud.get_nodes_usage(db, start=_T0, end=_T1)

    assert "Master" in [r.node_name for r in rows]


def _fake_usage_db(nodes, usage_rows):
    """Minimal stand-in for the two queries get_nodes_usage makes."""
    from app.db.models import Node

    class Query:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def filter(self, *_):
            return iter(self._rows)

    class DB:
        def query(self, model):
            return Query(nodes if model is Node else usage_rows)

    return DB()

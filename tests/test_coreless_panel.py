"""Panel running as a pure control plane, with no local Xray process.

The one that actually matters is the usage accounting: `node_id = None` is the
"Master" row in usage reports, so a coreless panel that still registered that
key would write a phantom node into every report forever.
"""

import importlib.util
import sys
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

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_operations_module(monkeypatch):
    module_path = Path(__file__).resolve().parents[1] / "app/xray/operations.py"
    module_name = "persistent_operations_under_test"

    fake_app = SimpleNamespace(
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        xray=SimpleNamespace(nodes={}),
    )
    fake_db = SimpleNamespace(GetDB=object, crud=SimpleNamespace())
    fake_models_node = SimpleNamespace(
        NodeStatus=SimpleNamespace(
            connected="connected",
            connecting="connecting",
            error="error",
            disabled="disabled",
        )
    )
    fake_models_proxy = SimpleNamespace(ProxyTypes=SimpleNamespace())
    fake_concurrency = SimpleNamespace(threaded_function=lambda func: func)
    fake_xray_node = SimpleNamespace(XRayNode=object)
    fake_xray_api = SimpleNamespace(XRay=object)
    fake_account = SimpleNamespace(Account=object, XTLSFlows=SimpleNamespace(NONE="none"))

    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setitem(sys.modules, "app.db", fake_db)
    monkeypatch.setitem(sys.modules, "app.models.node", fake_models_node)
    monkeypatch.setitem(sys.modules, "app.models.proxy", fake_models_proxy)
    monkeypatch.setitem(sys.modules, "app.utils.concurrency", fake_concurrency)
    monkeypatch.setitem(sys.modules, "app.xray.node", fake_xray_node)
    monkeypatch.setitem(sys.modules, "xray_api", fake_xray_api)
    monkeypatch.setitem(sys.modules, "xray_api.types.account", fake_account)

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_node_module(monkeypatch):
    module_path = Path(__file__).resolve().parents[1] / "app/xray/node.py"
    module_name = "persistent_node_under_test"

    fake_config_module = SimpleNamespace(XRayConfig=dict)
    fake_settings = SimpleNamespace(
        AUTO_RESTART_STALE_NODE=False,
        NODE_PERSISTENT_MODE=True,
    )
    fake_xray_api = SimpleNamespace(XRay=object)

    monkeypatch.setitem(sys.modules, "app.xray.config", fake_config_module)
    monkeypatch.setitem(sys.modules, "config", fake_settings)
    monkeypatch.setitem(sys.modules, "xray_api", fake_xray_api)

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_core_job_module(monkeypatch, persistent_mode=True):
    module_path = Path(__file__).resolve().parents[1] / "app/jobs/0_xray_core.py"
    module_name = "persistent_core_job_under_test"

    fake_app_obj = SimpleNamespace(on_event=lambda event: lambda func: func)
    fake_xray = SimpleNamespace(
        nodes={},
        core=SimpleNamespace(start=lambda config: None, stop=lambda: None, started=True),
        config=SimpleNamespace(include_db_users=lambda: {}),
        operations=SimpleNamespace(),
    )
    fake_scheduler = SimpleNamespace(add_job=lambda *args, **kwargs: None)
    fake_app = SimpleNamespace(
        app=fake_app_obj,
        logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        scheduler=fake_scheduler,
        xray=fake_xray,
    )
    fake_db = SimpleNamespace(GetDB=object, crud=SimpleNamespace())
    fake_models_node = SimpleNamespace(NodeStatus=SimpleNamespace(connecting="connecting"))
    fake_config = SimpleNamespace(
        JOB_CORE_HEALTH_CHECK_INTERVAL=10,
        NODE_PERSISTENT_MODE=persistent_mode,
    )
    fake_xray_exc = SimpleNamespace(XrayError=Exception)
    fake_xray_api = SimpleNamespace(exc=fake_xray_exc)

    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setitem(sys.modules, "app.db", fake_db)
    monkeypatch.setitem(sys.modules, "app.models.node", fake_models_node)
    monkeypatch.setitem(sys.modules, "config", fake_config)
    monkeypatch.setitem(sys.modules, "xray_api", fake_xray_api)
    monkeypatch.setitem(sys.modules, "xray_api.exc", fake_xray_exc)

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRuntimeNode:
    def __init__(self):
        self.detached = 0
        self.stopped = 0
        self.restarted = 0

    def detach(self):
        self.detached += 1

    def stop(self):
        self.stopped += 1

    def restart(self, config):
        self.restarted += 1


def test_remove_node_detaches_without_stop(monkeypatch):
    operations = load_operations_module(monkeypatch)
    node = FakeRuntimeNode()
    operations.xray.nodes[1] = node

    operations.remove_node(1)

    assert node.detached == 1
    assert node.stopped == 0
    assert 1 not in operations.xray.nodes


def test_remove_node_with_stop_stops_then_detaches(monkeypatch):
    operations = load_operations_module(monkeypatch)
    node = FakeRuntimeNode()
    operations.xray.nodes[1] = node

    operations.remove_node(1, stop=True)

    assert node.stopped == 1
    assert node.detached == 1
    assert 1 not in operations.xray.nodes


def test_restart_node_calls_restart_without_disconnect_on_success(monkeypatch):
    operations = load_operations_module(monkeypatch)
    node = FakeRuntimeNode()
    node.connected = True
    operations.xray.nodes[1] = node
    operations.xray.config = SimpleNamespace(include_db_users=lambda: {"config": True})
    dbnode = SimpleNamespace(id=1, name="node")
    operations.crud = SimpleNamespace(get_node_by_id=lambda db, node_id: dbnode)

    class DB:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    operations.GetDB = DB

    operations.restart_node(1)

    assert node.restarted == 1
    assert node.detached == 0
    assert node.stopped == 0


def test_shutdown_detaches_nodes_in_persistent_mode(monkeypatch):
    job = load_core_job_module(monkeypatch, persistent_mode=True)
    detached = []
    node = FakeRuntimeNode()
    job.xray.nodes[1] = node
    job.xray.operations.detach_node = lambda node_id: detached.append(node_id)

    job.app_shutdown()

    assert detached == [1]
    assert node.detached == 0
    assert node.stopped == 0


def build_rest_node(monkeypatch, responses):
    node_module = load_node_module(monkeypatch)

    rest = node_module.ReSTXRayNode.__new__(node_module.ReSTXRayNode)
    rest.address = "127.0.0.1"
    rest.api_port = 62051
    rest._session_id = "session"
    rest._started = False
    rest._node_cert = "cert"
    rest._api = None
    monkeypatch.setattr(
        node_module.ReSTXRayNode,
        "connected",
        property(lambda self: True),
    )
    rest._prepare_config = lambda config: config
    rest.connect = lambda: None
    rest.make_request = lambda path, timeout, **params: responses.pop(0)

    class FakeXRayAPI:
        def __init__(self, *args, **kwargs):
            self._channel = object()

    class Ready:
        def result(self, timeout=None):
            return None

    monkeypatch.setattr(node_module, "XRayAPI", FakeXRayAPI)
    monkeypatch.setattr(
        node_module.grpc,
        "channel_ready_future",
        lambda channel: Ready(),
    )
    return node_module, rest


class FakeConfig(dict):
    def to_json(self):
        return "{}"


def test_rest_start_attached_response_is_success(monkeypatch):
    _, rest = build_rest_node(
        monkeypatch,
        [{"started": True, "attached": True, "needs_restart": False}],
    )

    response = rest.start(FakeConfig())

    assert response["attached"] is True
    assert rest._started is True
    assert rest._api is not None


def test_rest_start_stale_response_does_not_restart_by_default(monkeypatch):
    node_module, rest = build_rest_node(
        monkeypatch,
        [{"started": True, "attached": True, "needs_restart": True, "reason": "config_changed"}],
    )
    monkeypatch.setattr(node_module, "AUTO_RESTART_STALE_NODE", False)
    rest.restart = lambda config: pytest.fail("restart must not be called")

    with pytest.raises(node_module.NodeNeedsRestartError):
        rest.start(FakeConfig())

    assert rest.needs_restart is True
    assert rest.stale_reason == "config_changed"
    assert "Explicit restart is required" in rest.status_message


def test_rest_manual_restart_uses_restart_endpoint(monkeypatch):
    calls = []
    _, rest = build_rest_node(monkeypatch, [{"started": True}])
    rest.make_request = lambda path, timeout, **params: calls.append(path) or {"started": True}

    rest.restart(FakeConfig())

    assert calls == ["/restart"]

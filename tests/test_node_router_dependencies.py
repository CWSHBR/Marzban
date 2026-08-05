"""Guards the node router against wiring ORM-backed CRUD calls to the
`get_node` endpoint, which returns a Pydantic `NodeResponse` rather than the
SQLAlchemy `Node` instance those CRUD helpers require.
"""

import ast
from pathlib import Path

import pytest

ROUTER_PATH = Path(__file__).resolve().parents[1] / "app/routers/node.py"

# Endpoints that hand their `dbnode` to CRUD helpers operating on the ORM model.
DB_BACKED_ENDPOINTS = ("modify_node", "reconnect_node", "remove_node")


def load_endpoints():
    tree = ast.parse(ROUTER_PATH.read_text())
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def dependency_name(func, arg_name):
    args = func.args.args + func.args.kwonlyargs
    defaults = ([None] * (len(func.args.args) - len(func.args.defaults))) + list(
        func.args.defaults
    ) + list(func.args.kw_defaults)

    for arg, default in zip(args, defaults):
        if arg.arg != arg_name:
            continue
        assert isinstance(default, ast.Call), f"{arg_name} is not a Depends(...) call"
        assert getattr(default.func, "id", None) == "Depends"
        return default.args[0].id

    raise AssertionError(f"{func.name} has no '{arg_name}' parameter")


@pytest.mark.parametrize("endpoint", DB_BACKED_ENDPOINTS)
def test_db_backed_endpoints_depend_on_get_dbnode(endpoint):
    func = load_endpoints()[endpoint]

    assert dependency_name(func, "dbnode") == "get_dbnode", (
        f"{endpoint} must depend on get_dbnode (ORM Node); depending on get_node "
        "yields a NodeResponse and breaks crud calls with UnmappedInstanceError"
    )


def test_get_node_endpoint_still_uses_get_dbnode():
    func = load_endpoints()["get_node"]

    assert dependency_name(func, "dbnode") == "get_dbnode"

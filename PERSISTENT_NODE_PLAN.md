# Persistent Node Dataplane Plan

## Baseline Repositories

- Panel repository: `Marzban-1`, remote `https://github.com/CWSHBR/Marzban.git`
- Node repository: `Marzban-node`, remote `https://github.com/CWSHBR/Marzban-node.git`
- Working branch in both repositories: `persistent-node-dataplane`

## Current Lifecycle Findings

### Marzban panel

- Dashboard calls panel API only. Node dashboard actions are implemented in `app/routers/node.py`.
- Node CRUD endpoints:
  - `GET /api/node/settings`
  - `POST /api/node`
  - `GET /api/nodes`
  - `PUT /api/node/{node_id}`
  - `POST /api/node/{node_id}/reconnect`
  - `DELETE /api/node/{node_id}`
  - `GET /api/nodes/usage`
  - `WS /api/node/{node_id}/logs`
- REST node client is `ReSTXRayNode` in `app/xray/node.py`.
- Legacy RPyC node client is `RPyCXRayNode` in `app/xray/node.py`.
- Runtime lifecycle orchestration is in `app/xray/operations.py`.
- Panel startup/shutdown node lifecycle is in `app/jobs/0_xray_core.py`.
- Usage collection is in `app/jobs/record_usages.py` and uses Xray gRPC stats through `node.api`, not the node REST API.
- Current panel shutdown calls `node.disconnect()` for every runtime node. In REST mode this calls `/disconnect`; today the node stops Xray on disconnect.
- Current remove/delete path calls `xray.operations.remove_node()`, which calls `disconnect()` and therefore stops Xray indirectly in REST mode.
- Current update/reconnect path removes the runtime node object and reconnects. Because `remove_node()` uses `disconnect()`, update and reconnect can stop Xray.

### Marzban-node

- REST control API is implemented in `rest_service.py`.
- RPyC legacy service is implemented in `rpyc_service.py`.
- Xray process wrapper and node-side config mutation are implemented in `xray.py`.
- REST service endpoints:
  - `POST /`
  - `POST /ping`
  - `POST /connect`
  - `POST /disconnect`
  - `POST /start`
  - `POST /stop`
  - `POST /restart`
  - `WS /logs`
- Current REST `/connect` creates a new control session, but if already connected and Xray is running it stops Xray.
- Current REST `/disconnect` clears the session and stops Xray if it is running.
- Current REST `/start` is not idempotent for an already running core. The panel catches `Xray is started already` and calls `/restart`.
- Current REST `/stop` is explicit stop and already maps to stopping Xray.
- Current REST `/restart` explicitly restarts Xray with the supplied config.
- REST mode requires `SSL_CLIENT_CERT_FILE`; RPyC can run with or without a client CA and must not be broken.

## Problem Statement

The control session and dataplane process are coupled. Loss of panel process, panel shutdown, reconnect, update, delete, or control disconnect can stop Xray on the node. Persistent dataplane mode must decouple these concepts:

- control session attached/detached is panel control state;
- Xray started/stopped is dataplane state.

## Planned Changes

### Marzban-node

Files expected to change:

- `config.py`
- `.env.example`
- `rest_service.py`
- tests to be added under `tests/`

Planned behavior behind `XRAY_PERSISTENT_MODE=false` by default:

- `/connect` in persistent mode creates a new session and attaches to a running Xray without stopping it.
- `/disconnect` in persistent mode detaches only and leaves Xray running.
- `/start` in persistent mode becomes idempotent:
  - start when stopped;
  - attach when incoming config hash matches current running config;
  - return `needs_restart=true` when config differs or panel IP changed.
- `/stop` remains explicit Xray stop.
- `/restart` remains explicit Xray restart.
- State response gains compatible extra fields such as `persistent_mode`, `attached`, `needs_restart`, `reason`, `running_config_hash`, and `running_panel_ip`.
- Hashing uses stable JSON serialization of the panel config.
- Optional restore from disk can be added if low risk; otherwise it will remain documented as not implemented.

Tests to add:

- persistent `/connect` does not stop a running core;
- persistent `/disconnect` does not stop a running core;
- `/stop` stops;
- `/restart` restarts;
- `/start` with same config attaches without restart;
- `/start` with different config returns `needs_restart=true`;
- panel IP change returns `needs_restart=true` without restart;
- non-persistent mode keeps old stop-on-connect/disconnect behavior.

### Marzban panel

Files expected to change:

- `config.py`
- `.env.example`
- `app/xray/node.py`
- `app/xray/operations.py`
- `app/jobs/0_xray_core.py`
- `app/routers/node.py`
- `app/models/node.py` and/or node response fields if status exposure is extended
- tests to be added under `tests/`

Planned behavior behind `NODE_PERSISTENT_MODE=false` / `AUTO_RESTART_STALE_NODE=false` by default:

- REST client gains explicit `detach()` separate from `stop()`.
- `disconnect()` becomes a compatibility wrapper and should not be used for new lifecycle decisions without an explicit stop flag.
- Panel shutdown detaches only in persistent mode.
- Reconnect detaches locally and reconnects; it does not stop Xray.
- Disable calls explicit stop.
- Delete calls explicit stop before removing the node record unless future API exposes detach-only delete.
- Manual restart calls explicit restart.
- `/start` response with `attached=true` and `needs_restart=false` is treated as success.
- `/start` response with `needs_restart=true` is surfaced as node status/message without auto-restart when auto-restart is disabled.

Tests to add:

- shutdown uses detach, not stop, in persistent mode;
- reconnect does not stop;
- disable stops;
- delete stops;
- manual restart calls restart;
- attached start response is successful;
- stale start response does not auto-restart when auto-restart is disabled.

## Baseline Tests

- `python3 -m pytest tests` in `Marzban-1`: failed because `pytest` is not installed in the active Python.
- `.venv/bin/python -m pytest tests` in `Marzban-1`: failed because `pytest` is not installed in the local `.venv`.
- `python3 -m pytest` in `Marzban-node`: failed because `pytest` is not installed in the active Python.
- `Marzban-node` currently has no committed `tests/` directory.

## Risks

- Panel IP is embedded into the node-side Xray API routing rule. If a new control session comes from a different IP, persistent mode must not silently restart the dataplane; it should report stale state and require explicit restart.
- Existing code uses `disconnect()` as both detach and stop-by-side-effect. The panel needs explicit method names to avoid accidental dataplane stops.
- RPyC mode has different semantics and must remain compatible.
- Changing node status enum can affect dashboard/API clients. Prefer extra response fields unless enum expansion is clearly safe.
- Existing dirty worktree files in `Marzban-1` are unrelated user changes and must not be reverted or included accidentally.

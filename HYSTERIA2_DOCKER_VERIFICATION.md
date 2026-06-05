# Hysteria2 Docker verification

## Build

Command:

```bash
docker build -t marzban-hysteria2-test .
```

Result: pass. The local image was built from the repository Dockerfile.

## Xray version

Command:

```bash
docker run --rm marzban-hysteria2-test xray version
```

Result:

```text
Xray 26.1.23 (Xray, Penetrates Everything.) 0a42dba (go1.25.6 linux/arm64)
```

## Backend start

Command:

```bash
docker run -d --name marzban-hy2-test -e SUDO_USERNAME=admin -e SUDO_PASSWORD=admin -e DOCS=true marzban-hysteria2-test
```

Result: pass. Alembic migrations ran, including `4dbb93282645 add hysteria proxy type`, and logs showed:

```text
Xray core 26.1.23 started
Application startup complete.
```

## Hysteria2 inbound validation

Command: `PUT /api/core/config` inside the container against `http://127.0.0.1:8000`.

Result: pass. The accepted inbound used:

```json
{
  "tag": "hy2-in-8443",
  "port": 8443,
  "protocol": "hysteria",
  "settings": {
    "version": 2,
    "users": []
  },
  "streamSettings": {
    "network": "hysteria",
    "security": "tls",
    "hysteriaSettings": {
      "version": 2
    }
  }
}
```

`GET /api/inbounds` returned:

```json
{
  "hysteria": [
    {
      "tag": "hy2-in-8443",
      "protocol": "hysteria",
      "network": "hysteria",
      "tls": "tls",
      "port": 8443
    }
  ]
}
```

## User creation

Command: `POST /api/user` with:

```json
{
  "username": "hy2_test",
  "proxies": {
    "hysteria": {
      "auth": "test-auth"
    }
  },
  "inbounds": {
    "hysteria": ["hy2-in-8443"]
  },
  "status": "active"
}
```

Result: pass. API returned `200` and the user response included `proxies.hysteria.auth`.

## Subscription link

Result: pass. User response and subscription output included:

```text
hysteria2://test-auth@185.130.225.19:8443?security=tls&sni=localhost#...
```

## Runtime config

Confirmed by running `XRayConfig(...).include_db_users()` inside the container:

```json
{
  "version": 2,
  "users": [
    {
      "auth": "test-auth",
      "email": "1.hy2_test",
      "level": 0
    }
  ],
  "has_clients": false
}
```

Confirmed:

- `protocol: hysteria`
- `settings.version: 2`
- `settings.users` contains the test user
- `settings.clients` is not used for Hysteria2

## Core restart

Command: `POST /api/core/restart`.

Result: pass. API returned `200`; logs showed repeated successful `Xray core 26.1.23 started` messages.

## Tests In Docker

Command:

```bash
docker run --rm -v /Users/bulatshaykhraziev/Documents/projects/Marzban-1/tests:/code/tests marzban-hysteria2-test bash -lc "pip install pytest >/tmp/pytest-install.log && PYTHONPATH=/code pytest tests/test_hysteria2_flow.py"
```

Result:

```text
10 passed, 46 warnings
```

## Regression

- Shadowsocks: pass for baseline Docker startup because the default `xray_config.json` Shadowsocks inbound started before the Hysteria2 config replacement.
- VLESS: not covered by Docker E2E in this run; existing code path is covered by unchanged dynamic gRPC branch and focused tests assert Hysteria2 does not alter client insertion for other protocols.
- VMess: not covered by Docker E2E in this run; existing code path is covered by unchanged dynamic gRPC branch.
- Trojan: not covered by Docker E2E in this run; existing code path is covered by unchanged dynamic gRPC branch.

## Known limitations

- Hysteria2 runtime user updates use full config restart fallback because this repository does not include Hysteria account protobuf support under `xray_api/proto`.
- Local host access required executing API checks from inside the container because Marzban binds to `127.0.0.1` when TLS files are not configured.
- Local host Python is 3.14, while this project image uses Python 3.12. Local `pytest` could not run because pinned `pydantic-core` and `grpcio` failed to build on Python 3.14.

#!/usr/bin/env python3
"""Drill for the single-server, coreless, no-HA deployment.

Answers one question: can this branch run as an ordinary single panel that
owns no local Xray and drives a marzban-node on the same host? Every HA code
path must be inert, and nothing that used to work may have regressed.

Driven by tests/e2e/standalone/run.sh.
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

PANEL = "https://127.0.0.1:8003"
NODE = "node"
# Must match tests/e2e/standalone/xray_config.json: the panel derives this from
# privateKey, so it is also the assertion that the derivation is correct.
EXPECTED_REALITY_PBK = "lOWdwqAaLqqY8GbzEYfunpZeyonbDQWLJ_oSU7hfHGo"
COMPOSE = None

_failures = []
_checks = 0


def api(base, path, method="GET", token=None, body=None, timeout=15):
    url = f"{base}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw
    except Exception as exc:
        return 0, str(exc)


def login(base):
    url = f"{base}/api/admin/token"
    data = b"username=admin&password=admin"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode())["access_token"]


def raw_get(url):
    try:
        with urllib.request.urlopen(url, timeout=20, context=SSL_CTX) as resp:
            return resp.read().decode()
    except Exception:
        return None


def compose(*args, check=True):
    return subprocess.run(["docker", "compose", "-f", COMPOSE, *args],
                          capture_output=True, text=True, check=check)


def check(name, condition, detail=""):
    global _checks
    _checks += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(f"{name} {detail}".strip())
    return bool(condition)


def wait_for(label, predicate, timeout=90, interval=1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:
            last = exc
        time.sleep(interval)
    print(f"  ...timed out after {timeout}s waiting for {label} (last={last!r})")
    return None


def node_row(token):
    code, rows = api(PANEL, "/nodes", token=token)
    if code != 200:
        return None
    return next((n for n in (rows or []) if n["name"] == NODE), None)


def install_node_cert(certificate):
    node_dir = os.path.join(os.path.dirname(os.path.abspath(COMPOSE)), "node-data")
    os.makedirs(node_dir, exist_ok=True)
    with open(os.path.join(node_dir, "ssl_client_cert.pem"), "w") as handle:
        handle.write(certificate)
    compose("--profile", "node", "up", "-d", NODE, check=False)


def section(title):
    print(f"\n=== {title} ===")


def main():
    global COMPOSE
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", required=True)
    COMPOSE = parser.parse_args().compose_file

    section("the panel boots at all with no local Xray binary")
    token = wait_for("panel login", lambda: login(PANEL), timeout=180)
    if not check("panel answers /api/admin/token", token):
        return finish()

    # The reality inbound in xray_config.json ships privateKey and no
    # publicKey. Parsing it is what used to abort startup on a coreless panel,
    # so simply being logged in already proves half of it.
    code, config = api(PANEL, "/core/config", token=token)
    check("panel serves its own core config", code == 200, f"HTTP {code}")

    section("the local core reports disabled without pretending otherwise")
    code, core = api(PANEL, "/core", token=token)
    check("GET /api/core answers", code == 200, f"HTTP {code}")
    if code == 200:
        check("core reports enabled=false", core.get("enabled") is False, str(core))
        check("core reports started=false", core.get("started") is False, str(core))
        check("core reports no version", core.get("version") is None, str(core))

    section("reality publicKey is derived without the xray binary")
    code, user_probe = api(PANEL, "/user", method="POST", token=token,
                           body={"username": f"reality{int(time.time())}",
                                 "proxies": {"vless": {}},
                                 "inbounds": {"vless": ["vless-reality"]},
                                 "data_limit": 0, "expire": 0})
    check("a user can be created on a reality inbound", code == 200,
          f"HTTP {code}: {user_probe}")
    if code == 200:
        sub = raw_get(PANEL + user_probe["links"][0]) if user_probe.get("links") else None
        link = (user_probe.get("links") or [""])[0]
        check("the subscription link carries the derived publicKey",
              EXPECTED_REALITY_PBK in link,
              f"link={link[:160]}")

    section("HA is completely inert")
    code, ha = api(PANEL, "/ha/status", token=token)
    check("GET /api/ha/status answers", code == 200, f"HTTP {code}")
    if code == 200:
        check("ha_enabled is false", ha["self"]["ha_enabled"] is False, str(ha["self"]))
        # A standalone panel must still consider itself leader, or it would
        # enqueue every dataplane change into an outbox nobody drains.
        check("the panel still calls itself leader",
              ha["self"]["role"] == "leader", ha["self"]["role"])
        check("no lease row is claimed", ha.get("lease") is None, str(ha.get("lease")))
        check("coreless mode is reported",
              ha["self"]["local_core_enabled"] is False)

    section("the node on this same host is adopted")
    code, settings = api(PANEL, "/node/settings", token=token)
    check("panel exposes its client certificate",
          code == 200 and "certificate" in (settings or {}), f"HTTP {code}")
    if code != 200:
        return finish()
    install_node_cert(settings["certificate"])

    code, created = api(PANEL, "/node", method="POST", token=token,
                        body={"name": NODE, "address": NODE, "port": 62050,
                              "api_port": 62051, "add_as_new_host": True,
                              "usage_coefficient": 1})
    check("node created", code in (200, 409), f"HTTP {code}: {created}")

    connected = wait_for("node to reach connected",
                         lambda: (node_row(token) or {}).get("status") == "connected",
                         timeout=180)
    check("node reports connected", connected is not None, str(node_row(token)))

    # `status` is written the moment the panel finishes connecting, but
    # is_running/is_attached come from the health check's snapshot, which is
    # only JOB_CORE_HEALTH_CHECK_INTERVAL fresh. Reading them straight after
    # `connected` reads the previous pass, when the node was genuinely down.
    live = wait_for("the health check to observe the node",
                    lambda: (node_row(token) or {}).get("is_attached") is True,
                    timeout=60)
    row = node_row(token) or {}
    check("node reports an attached control session", live is not None, str(row))
    check("node reports a running xray", row.get("is_running") is True, str(row))
    check("node reports its xray version",
          bool(row.get("xray_version")), str(row.get("xray_version")))

    section("writes reach the dataplane directly, with no outbox hop")
    username = f"sa{int(time.time())}"
    code, user = api(PANEL, "/user", method="POST", token=token,
                     body={"username": username, "proxies": {"shadowsocks": {}},
                           "inbounds": {"shadowsocks": ["shadowsocks"]},
                           "data_limit": 0, "expire": 0})
    check("user created", code == 200, f"HTTP {code}: {user}")

    # A standalone panel is its own leader, so nothing may ever queue up.
    time.sleep(3)
    code, ha = api(PANEL, "/ha/status", token=token)
    if code == 200:
        check("outbox stays empty on a standalone panel",
              ha["outbox"]["pending"] == 0, str(ha["outbox"]))

    if code == 200 and user:
        body = raw_get(PANEL + user["subscription_url"])
        check("the subscription is served", bool(body), "empty response")

    section("usage accounting attributes traffic to the node, not to a phantom master")
    usage = wait_for(
        "a node usage row",
        lambda: api(PANEL, "/nodes/usage", token=token)[1],
        timeout=90)
    if usage is not None:
        rows = usage.get("usages", [])
        names = [u.get("node_name") for u in rows]
        print(f"  usage rows: {names}")
        # node_id=None renders as "Master". A coreless panel has nothing to
        # attribute to it, so an empty row must not be synthesised.
        check("no phantom Master row in node usage", "Master" not in names,
              str(names))
        check("the real node is accounted for", NODE in names, str(names))

    section("restarting the core restarts the node instead of erroring")
    code, _ = api(PANEL, "/core/restart", method="POST", token=token)
    check("POST /api/core/restart succeeds", code == 200, f"HTTP {code}")
    back = wait_for("node to come back after the restart",
                    lambda: (node_row(token) or {}).get("status") == "connected",
                    timeout=120)
    check("node is connected again after a core restart", back is not None,
          str(node_row(token)))

    section("the panel survives a restart and re-adopts the node")
    compose("restart", "panel", check=False)
    token = wait_for("panel to come back", lambda: login(PANEL), timeout=180)
    if check("panel answers again after a restart", token):
        back = wait_for("node to be re-adopted",
                        lambda: (node_row(token) or {}).get("status") == "connected",
                        timeout=180)
        check("node re-adopted after a panel restart", back is not None,
              str(node_row(token)))

    return finish()


def finish():
    print("\n" + "=" * 60)
    if _failures:
        print(f"FAILED: {len(_failures)} of {_checks} checks")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"ALL {_checks} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

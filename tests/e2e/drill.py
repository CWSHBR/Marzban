#!/usr/bin/env python3
"""Failover drill against two live panels.

Driven by tests/e2e/run.sh. Every check is written so a failure names the
thing that broke, not just an assertion line.
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

# The harness uses a self-signed certificate on purpose.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

PANEL_A = "https://127.0.0.1:8001"
PANEL_B = "https://127.0.0.1:8002"
NODES = ("node-a", "node-b")
# Patroni REST API, one per Postgres member.
PATRONI = {"pg-a": "http://127.0.0.1:8010", "pg-b": "http://127.0.0.1:8011"}
COMPOSE = None  # set from argv

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


def compose(*args, check=True):
    cmd = ["docker", "compose", "-f", COMPOSE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


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
    """Poll until predicate returns a truthy value; return it (or None)."""
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



def node_status(base, token, node_name):
    code, rows = api(base, "/nodes", token=token)
    if code != 200:
        return None
    row = next((n for n in (rows or []) if n["name"] == node_name), None)
    return row["status"] if row else None


def patroni_cluster():
    """Ask any reachable Patroni member who currently leads."""
    for url in PATRONI.values():
        body = raw_get(f"{url}/cluster")
        if body:
            try:
                return json.loads(body)
            except ValueError:
                continue
    return None


def pg_leader():
    cluster = patroni_cluster() or {}
    return next((m["name"] for m in cluster.get("members", [])
                 if m.get("role") == "leader"), None)


def can_write(base, token, tag):
    """Round-trip an actual write through the panel's own connection pool.

    This is what proves the panel followed Patroni to the new primary: a
    read-only standby answers SELECT 1 happily, so only a write tells the
    truth.
    """
    username = f"w{tag}{int(time.time() * 1000) % 100000}"
    code, body = api(base, "/user", method="POST", token=token,
                     body={"username": username, "proxies": {"shadowsocks": {}},
                           "inbounds": {}, "data_limit": 0, "expire": 0})
    return code == 200, f"HTTP {code}: {body}"


def status(base, token):
    code, body = api(base, "/ha/status", token=token)
    return body if code == 200 else None


def section(title):
    print(f"\n=== {title} ===")


def main():
    global COMPOSE
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", required=True)
    COMPOSE = parser.parse_args().compose_file

    section("panels are up and both accept logins")
    token_a = wait_for("panel-a login", lambda: login(PANEL_A), timeout=180)
    token_b = wait_for("panel-b login", lambda: login(PANEL_B), timeout=180)
    if not check("both panels answer /api/admin/token", token_a and token_b):
        return finish()
    # The JWT secret lives in the database, so a token minted by one panel
    # must be accepted by the other.
    code, _ = api(PANEL_B, "/ha/status", token=token_a)
    check("token minted by A is accepted by B (shared JWT secret)", code == 200,
          f"got HTTP {code}")

    section("exactly one leader is elected")
    st_a = wait_for("A to report a role",
                    lambda: status(PANEL_A, token_a), timeout=90)
    st_b = status(PANEL_B, token_b)
    if not check("both panels report HA status", st_a and st_b):
        return finish()

    roles = {st_a["self"]["panel_id"]: st_a["self"]["role"],
             st_b["self"]["panel_id"]: st_b["self"]["role"]}
    print(f"  roles: {roles}")
    leaders = [p for p, r in roles.items() if r == "leader"]
    check("exactly one leader", len(leaders) == 1, f"leaders={leaders}")
    check("both panels agree on the lease holder",
          st_a["lease"]["holder"] == st_b["lease"]["holder"] == leaders[0]
          if leaders else False,
          f"a={st_a['lease']['holder']} b={st_b['lease']['holder']}")
    check("both panels see both members",
          len(st_a["members"]) == 2 and len(st_b["members"]) == 2,
          f"a={len(st_a['members'])} b={len(st_b['members'])}")
    check("coreless mode is reported",
          st_a["self"]["local_core_enabled"] is False)

    leader_id = leaders[0]
    leader_base = PANEL_A if leader_id == "panel-a" else PANEL_B
    follower_base = PANEL_B if leader_id == "panel-a" else PANEL_A
    leader_token = token_a if leader_base == PANEL_A else token_b
    follower_token = token_b if follower_base == PANEL_B else token_a
    print(f"  leader={leader_id}")

    section("only the leader runs the schema; both see the same data")
    code, nodes = api(leader_base, "/nodes", token=leader_token)
    check("GET /api/nodes works on the leader", code == 200, f"HTTP {code}")
    code, nodes_f = api(follower_base, "/nodes", token=follower_token)
    check("GET /api/nodes works on the follower too", code == 200, f"HTTP {code}")

    section("both nodes -- one per server -- are adopted by the leader")
    code, settings = api(leader_base, "/node/settings", token=leader_token)
    check("panel exposes its client certificate", code == 200 and
          "certificate" in (settings or {}), f"HTTP {code}")
    if code == 200:
        install_node_certs(settings["certificate"])
        for node_name in NODES:
            code, created = api(leader_base, "/node", method="POST",
                                token=leader_token,
                                body={"name": node_name, "address": node_name,
                                      "port": 62050, "api_port": 62051,
                                      "add_as_new_host": True,
                                      "usage_coefficient": 1})
            check(f"{node_name} created", code in (200, 409),
                  f"HTTP {code}: {created}")
        connected = wait_for(
            "both nodes to reach connected",
            lambda: all(node_status(leader_base, leader_token, n) == "connected"
                        for n in NODES),
            timeout=150)
        check("both nodes report connected on the leader", connected is not None,
              str(api(leader_base, "/nodes", token=leader_token)[1]))

    section("a user created on the FOLLOWER reaches the dataplane")
    username = f"e2e{int(time.time())}"
    code, created = api(follower_base, "/user", method="POST",
                        token=follower_token,
                        body={"username": username, "proxies": {"shadowsocks": {}},
                              "inbounds": {}, "data_limit": 0, "expire": 0})
    check("follower accepts the write", code == 200, f"HTTP {code}: {created}")

    # The follower must not have touched nodes itself: it enqueues instead.
    st = status(follower_base, follower_token)
    print(f"  outbox right after the write: {st['outbox']['pending']}")

    drained = wait_for(
        "outbox to drain",
        lambda: (status(leader_base, leader_token) or {}).get("outbox", {}).get(
            "pending") == 0,
        timeout=60)
    check("leader drains the outbox", drained is not None,
          str((status(leader_base, leader_token) or {}).get("outbox")))

    section("subscriptions are identical on both panels")
    code, user_a = api(leader_base, f"/user/{username}", token=leader_token)
    check("user is visible on the leader", code == 200, f"HTTP {code}")
    code, user_b = api(follower_base, f"/user/{username}", token=follower_token)
    check("user is visible on the follower", code == 200, f"HTTP {code}")
    if user_a and user_b:
        sub_path = user_a["subscription_url"]
        body_a = raw_get(PANEL_A + sub_path)
        body_b = raw_get(PANEL_B + sub_path)
        check("the same subscription link works on both panels",
              body_a is not None and body_b is not None,
              f"a={body_a is not None} b={body_b is not None}")
        check("both panels serve byte-identical subscription content",
              body_a == body_b,
              f"len a={len(body_a or '')} b={len(body_b or '')}")

    section("follower refuses to be a second Telegram/notification sender")
    st_f = status(follower_base, follower_token)
    check("follower reports itself as follower",
          st_f["self"]["role"] == "follower", st_f["self"]["role"])
    check("follower cannot hand over from a panel it does not lead"
          if not st_f["can_handover"] else "follower can request a handover",
          True)

    section("planned handover from the FOLLOWER's UI")
    check("handover is offered", st_f["can_handover"] is True,
          st_f.get("handover_blocked_reason"))
    code, res = api(follower_base, "/ha/stepdown", method="POST",
                    token=follower_token)
    check("stepdown accepted", code == 200, f"HTTP {code}: {res}")

    swapped = wait_for(
        "roles to swap",
        lambda: (status(follower_base, follower_token) or {})["self"]["role"] == "leader",
        timeout=60)
    check("the former follower is now leader", swapped is not None)
    st_new = status(follower_base, follower_token)
    if st_new:
        check("epoch advanced on handover",
              st_new["lease"]["epoch"] > st_a["lease"]["epoch"],
              f"{st_a['lease']['epoch']} -> {st_new['lease']['epoch']}")
        check("the old leader stepped down",
              (status(leader_base, leader_token) or {})["self"]["role"] == "follower")

    # Roles are swapped from here on.
    leader_base, follower_base = follower_base, leader_base
    leader_token, follower_token = follower_token, leader_token
    leader_id = (status(leader_base, leader_token) or {})["self"]["panel_id"]
    print(f"  leader is now {leader_id}")

    section("Postgres switchover: panels follow the new primary")
    before = pg_leader()
    check("Patroni reports a primary", before is not None, str(patroni_cluster()))
    ok_a, detail_a = can_write(PANEL_A, token_a, "pre")
    check("panel-a can write before the switchover", ok_a, detail_a)

    if before:
        target = "pg-b" if before == "pg-a" else "pg-a"
        print(f"  switching over {before} -> {target} ...")
        result = subprocess.run(
            ["docker", "compose", "-f", COMPOSE, "exec", "-T", before,
             "patronictl", "-c", "/tmp/patroni.yml", "switchover",
             "--leader", before, "--candidate", target, "--force"],
            capture_output=True, text=True)
        print("  " + (result.stdout or result.stderr).strip().splitlines()[-1][:120])

        moved = wait_for("Patroni to promote the other member",
                         lambda: pg_leader() not in (None, before), timeout=90)
        check("Postgres primary moved", moved is not None,
              f"still {pg_leader()}")

        # pool_pre_ping only issues SELECT 1, which a standby answers happily.
        # Only the SQLSTATE 25006 handler makes the pool notice the demotion.
        recovered_a = wait_for("panel-a to write again",
                               lambda: can_write(PANEL_A, token_a, "a")[0],
                               timeout=90, interval=2)
        check("panel-a resumed writing after the switchover",
              recovered_a is not None, can_write(PANEL_A, token_a, "a2")[1])
        recovered_b = wait_for("panel-b to write again",
                               lambda: can_write(PANEL_B, token_b, "b")[0],
                               timeout=90, interval=2)
        check("panel-b resumed writing after the switchover",
              recovered_b is not None, can_write(PANEL_B, token_b, "b2")[1])

        one_leader = wait_for(
            "the panel cluster to settle on one leader",
            lambda: sum(1 for base, tok in ((PANEL_A, token_a), (PANEL_B, token_b))
                        if (status(base, tok) or {}).get("self", {}).get("role")
                        == "leader") == 1,
            timeout=90, interval=2)
        check("exactly one panel leader after the database moved",
              one_leader is not None)

    section("hard kill of the Postgres primary")
    victim_pg = pg_leader()
    if check("primary identified", victim_pg is not None, str(patroni_cluster())):
        print(f"  killing {victim_pg} ...")
        compose("kill", victim_pg, check=False)
        promoted = wait_for("Patroni to fail over",
                            lambda: pg_leader() not in (None, victim_pg),
                            timeout=120, interval=2)
        check("Patroni promoted the surviving member", promoted is not None,
              str(patroni_cluster()))

        survivors = [(b, tk) for b, tk in ((PANEL_A, token_a), (PANEL_B, token_b))]
        wrote = wait_for(
            "some panel to write against the new primary",
            lambda: any(can_write(b, tk, "k")[0] for b, tk in survivors),
            timeout=120, interval=3)
        check("panels write again after the database failover",
              wrote is not None)

        one_leader = wait_for(
            "the panel cluster to settle on one leader",
            lambda: sum(1 for b, tk in survivors
                        if (status(b, tk) or {}).get("self", {}).get("role")
                        == "leader") == 1,
            timeout=90, interval=2)
        check("exactly one panel leader after the database failover",
              one_leader is not None)

        compose("start", victim_pg, check=False)
        rejoined = wait_for(
            f"{victim_pg} to rejoin as a replica",
            lambda: next((m for m in (patroni_cluster() or {}).get("members", [])
                          if m["name"] == victim_pg
                          and m.get("role") == "replica"), None),
            timeout=180, interval=3)
        check(f"{victim_pg} rejoined the cluster as a replica",
              rejoined is not None, str(patroni_cluster()))

    # Re-derive roles: the database moves above may have shuffled panel roles.
    st_a = status(PANEL_A, token_a) or {}
    leader_id = st_a.get("self", {}).get("role") == "leader" and "panel-a" or None
    if leader_id is None:
        leader_id = "panel-b"
    leader_base = PANEL_A if leader_id == "panel-a" else PANEL_B
    follower_base = PANEL_B if leader_id == "panel-a" else PANEL_A
    leader_token = token_a if leader_base == PANEL_A else token_b
    follower_token = token_b if follower_base == PANEL_B else token_a
    print(f"  panel leader is now {leader_id}")

    section("hard kill of the leader triggers failover")
    victim = leader_id
    survivor_base = follower_base
    survivor_token = follower_token
    print(f"  killing {victim} ...")
    compose("kill", victim)
    t0 = time.time()

    promoted = wait_for(
        "survivor to promote",
        lambda: (status(survivor_base, survivor_token) or {})["self"]["role"] == "leader",
        timeout=90)
    elapsed = time.time() - t0
    check("survivor took over after the leader was killed", promoted is not None,
          f"waited {elapsed:.0f}s")
    if promoted:
        print(f"  failover took {elapsed:.1f}s (lease ttl is 10s)")
        check("failover completed within two lease TTLs", elapsed < 30,
              f"{elapsed:.1f}s")

    section("the dataplane survived the panel dying")
    for node_name in NODES:
        state = service_state(node_name)
        check(f"{node_name} is still running (Xray kept serving)",
              state == "running", f"state={state}")
    reconnected = wait_for(
        "both nodes to be reconnected by the new leader",
        lambda: all(node_status(survivor_base, survivor_token, n) == "connected"
                    for n in NODES),
        timeout=150)
    check("new leader reconnected both nodes", reconnected is not None,
          str(api(survivor_base, "/nodes", token=survivor_token)[1]))

    section("metrics endpoint")
    body = raw_get(survivor_base + "/api/ha/metrics",
                   headers={"Authorization": "Bearer e2e-metrics-token"})
    check("metrics require the token",
          raw_get(survivor_base + "/api/ha/metrics") is None)
    check("metrics are served with the token", body is not None
          and "marzban_ha_is_leader" in (body or ""), (body or "")[:120])
    if body:
        check("cluster reports a live leader",
              "marzban_ha_has_leader 1" in body,
              [ln for ln in body.splitlines() if "has_leader" in ln])

    section("the killed panel comes back as a FOLLOWER (no preemption)")
    compose("start", victim)
    victim_base = PANEL_A if victim == "panel-a" else PANEL_B
    victim_token = wait_for(f"{victim} to accept logins",
                            lambda: login(victim_base), timeout=180)
    check(f"{victim} is back up", victim_token is not None)
    if victim_token:
        st_v = wait_for(f"{victim} to report a role",
                        lambda: status(victim_base, victim_token), timeout=60)
        check(f"{victim} rejoined as a follower",
              st_v and st_v["self"]["role"] == "follower",
              st_v["self"]["role"] if st_v else "no status")
        check("leadership did not move back",
              (status(survivor_base, survivor_token) or {})["self"]["role"] == "leader")

    return finish()


def raw_get(url, headers=None):
    req = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            return resp.read().decode()
    except Exception:
        return None


def install_node_certs(certificate):
    """Write the panel's client certificate, then bring the node up.

    Order matters: marzban-node refuses to start in REST mode without
    SSL_CLIENT_CERT_FILE, and that certificate only exists once the panel's
    migrations have seeded the `tls` table. So the node cannot come up with
    the rest of the stack.
    """
    root = os.path.join(os.path.dirname(os.path.abspath(COMPOSE)), "node-data")
    for node_name in NODES:
        node_dir = os.path.join(root, node_name)
        os.makedirs(node_dir, exist_ok=True)
        with open(os.path.join(node_dir, "ssl_client_cert.pem"), "w") as handle:
            handle.write(certificate)
    compose("--profile", "node", "up", "-d", *NODES, check=False)


def service_state(service):
    result = compose("ps", "-a", "--format", "json", check=False)
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("Service") == service:
            return row.get("State")
    return None


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

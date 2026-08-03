#!/usr/bin/env bash
# End-to-end drill for the two-server A/B deployment.
#
# Brings up the whole thing the way it is meant to run in production:
#   panel-a + node-a + pg-a   |   panel-b + node-b + pg-b   |   etcd quorum
# Postgres is managed by Patroni and reached through a libpq multi-host list,
# with no TCP load balancer in between.
#
#   ./tests/e2e/run.sh            build images, run the drill, tear down
#   KEEP=1 ./tests/e2e/run.sh     leave the stack running afterwards
#   SKIP_BUILD=1 ./tests/e2e/run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
COMPOSE="$HERE/docker-compose.yml"
NODE_REPO="${NODE_REPO:-$(cd "$REPO/../Marzban-node" 2>/dev/null && pwd || true)}"

dc() { docker compose -f "$COMPOSE" --profile node "$@"; }

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    echo
    echo "--- tearing down ---"
    dc down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo
    echo "--- stack left running (KEEP=1) ---"
    echo "    panels   https://127.0.0.1:8001  https://127.0.0.1:8002"
    echo "    patroni  http://127.0.0.1:8010   http://127.0.0.1:8011"
  fi
}
trap cleanup EXIT

if [ ! -f "$HERE/certs/fullchain.pem" ]; then
  echo "--- generating a self-signed certificate for the panels ---"
  mkdir -p "$HERE/certs"
  openssl req -x509 -newkey rsa:2048 \
    -keyout "$HERE/certs/key.pem" -out "$HERE/certs/fullchain.pem" \
    -days 3650 -nodes -subj "/CN=marzban-e2e" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
fi

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  echo "--- building panel image ---"
  docker build -t marzban-ha:e2e "$REPO"

  if [ -n "$NODE_REPO" ] && [ -f "$NODE_REPO/Dockerfile" ]; then
    echo "--- building node image from $NODE_REPO ---"
    docker build -t marzban-node:e2e "$NODE_REPO"
  else
    echo "!! Marzban-node checkout not found; set NODE_REPO to its path." >&2
    exit 1
  fi

  echo "--- building patroni image ---"
  docker compose -f "$COMPOSE" build pg-a
fi

echo "--- starting etcd and the Patroni cluster ---"
dc down -v --remove-orphans >/dev/null 2>&1 || true
# The nodes cannot boot until the drill writes the panel's client certificate,
# which only exists once the panel's migrations have seeded the tls table.
rm -rf "$HERE/node-data"
mkdir -p "$HERE/node-data/node-a" "$HERE/node-data/node-b"
docker compose -f "$COMPOSE" up -d etcd1 etcd2 etcd3 pg-a pg-b

echo "--- waiting for a streaming replica ---"
for _ in $(seq 1 60); do
  if curl -s http://127.0.0.1:8010/cluster 2>/dev/null | grep -q '"state": "streaming"'; then
    break
  fi
  sleep 3
done
curl -s http://127.0.0.1:8010/cluster | python3 -c \
  "import json,sys; [print('   ', m['name'], m['role'], m['state']) for m in json.load(sys.stdin)['members']]" \
  || true

echo "--- starting the panels ---"
docker compose -f "$COMPOSE" up -d panel-a panel-b

echo "--- running the drill ---"
set +e
python3 "$HERE/drill.py" --compose-file "$COMPOSE"
RESULT=$?
set -e

if [ $RESULT -ne 0 ]; then
  for svc in panel-a panel-b pg-a pg-b; do
    echo "--- $svc logs (tail) ---"
    docker compose -f "$COMPOSE" logs --tail 40 "$svc" || true
  done
fi

exit $RESULT

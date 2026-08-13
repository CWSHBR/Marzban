#!/usr/bin/env bash
# End-to-end drill for the single-server, coreless, no-HA deployment.
#
#   postgres + panel (XRAY_LOCAL_CORE_ENABLED=false, HA_ENABLED=false) + node
#
# This is the step to run before touching the A/B topology.
#
#   ./tests/e2e/standalone/run.sh            build images, drill, tear down
#   KEEP=1 ./tests/e2e/standalone/run.sh     leave the stack running
#   SKIP_BUILD=1 ./tests/e2e/standalone/run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
COMPOSE="$HERE/docker-compose.yml"
CERTS="$HERE/../certs"
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
    echo "    panel  https://127.0.0.1:8003   (admin / admin)"
  fi
}
trap cleanup EXIT

if [ ! -f "$CERTS/fullchain.pem" ]; then
  echo "--- generating a self-signed certificate for the panel ---"
  mkdir -p "$CERTS"
  openssl req -x509 -newkey rsa:2048 \
    -keyout "$CERTS/key.pem" -out "$CERTS/fullchain.pem" \
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
fi

echo "--- starting postgres and the panel ---"
dc down -v --remove-orphans >/dev/null 2>&1 || true
# The node cannot boot until the drill writes the panel's client certificate,
# which only exists once the panel's migrations have seeded the tls table.
rm -rf "$HERE/node-data"
mkdir -p "$HERE/node-data"
docker compose -f "$COMPOSE" up -d postgres panel

echo "--- running the drill ---"
set +e
python3 "$HERE/drill.py" --compose-file "$COMPOSE"
RESULT=$?
set -e

if [ $RESULT -ne 0 ]; then
  for svc in panel node postgres; do
    echo "--- $svc logs (tail) ---"
    docker compose -f "$COMPOSE" logs --tail 60 "$svc" || true
  done
fi

exit $RESULT

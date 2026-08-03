#!/usr/bin/env bash
# End-to-end failover drill for the A/B high-availability setup.
#
#   ./tests/e2e/run.sh            build images, run the drill, tear down
#   KEEP=1 ./tests/e2e/run.sh     leave the stack running afterwards
#   SKIP_BUILD=1 ./tests/e2e/run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
COMPOSE="$HERE/docker-compose.yml"
NODE_REPO="${NODE_REPO:-$(cd "$REPO/../Marzban-node" 2>/dev/null && pwd || true)}"

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    echo
    echo "--- tearing down ---"
    docker compose -f "$COMPOSE" --profile node down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo
    echo "--- stack left running (KEEP=1); panels on :8001 and :8002 ---"
  fi
}
trap cleanup EXIT

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

echo "--- starting the stack ---"
docker compose -f "$COMPOSE" down -v --remove-orphans >/dev/null 2>&1 || true
# The node is deliberately left out here: it cannot start until the drill
# has written the panel's client certificate into tests/e2e/node-data.
rm -rf "$HERE/node-data"; mkdir -p "$HERE/node-data"
docker compose -f "$COMPOSE" up -d

echo "--- running the drill ---"
set +e
python3 "$HERE/drill.py" --compose-file "$COMPOSE"
RESULT=$?
set -e

if [ $RESULT -ne 0 ]; then
  echo
  echo "--- panel-a logs (tail) ---"
  docker compose -f "$COMPOSE" logs --tail 60 panel-a || true
  echo "--- panel-b logs (tail) ---"
  docker compose -f "$COMPOSE" logs --tail 60 panel-b || true
fi

exit $RESULT

#!/usr/bin/env bash
# Runs once, on the member that bootstraps the cluster. Creates the role and
# database the panels connect with. Patroni passes the connection string as $1.
set -euo pipefail

CONN="$1"

psql "$CONN" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marzban') THEN
    CREATE ROLE marzban LOGIN PASSWORD 'marzban';
  END IF;
END
$$;
SQL

# CREATE DATABASE cannot run inside a transaction block, hence the separate call.
psql "$CONN" -v ON_ERROR_STOP=1 -tAc \
  "SELECT 1 FROM pg_database WHERE datname = 'marzban'" | grep -q 1 \
  || psql "$CONN" -v ON_ERROR_STOP=1 -c "CREATE DATABASE marzban OWNER marzban"

#!/usr/bin/env bash
# Render a Patroni config for this member and hand over to Patroni.
set -euo pipefail

NODE_NAME="${NODE_NAME:?NODE_NAME is required}"
SCOPE="${PATRONI_SCOPE:-marzban}"
ETCD_HOSTS="${ETCD_HOSTS:-etcd:2379}"
DATA_DIR=/data/pgdata

mkdir -p "$DATA_DIR" /var/run/postgresql
chown -R postgres:postgres /data /var/run/postgresql
chmod 700 "$DATA_DIR"

cat > /tmp/patroni.yml <<YAML
scope: ${SCOPE}
name: ${NODE_NAME}

restapi:
  listen: 0.0.0.0:8008
  connect_address: ${NODE_NAME}:8008

etcd3:
  hosts: ${ETCD_HOSTS}

bootstrap:
  dcs:
    # Tightened from Patroni's defaults so the drill does not spend minutes
    # waiting for a database failover.
    ttl: 20
    loop_wait: 5
    retry_timeout: 5
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      parameters:
        wal_level: replica
        hot_standby: "on"
        max_wal_senders: 10
        max_replication_slots: 10
        wal_keep_size: 128MB
  initdb:
    - encoding: UTF8
    - data-checksums
  pg_hba:
    - local all all trust
    - host replication replicator 0.0.0.0/0 md5
    - host all all 0.0.0.0/0 md5
  post_init: /post_init.sh

postgresql:
  listen: 0.0.0.0:5432
  connect_address: ${NODE_NAME}:5432
  data_dir: ${DATA_DIR}
  pgpass: /tmp/pgpass
  authentication:
    replication:
      username: replicator
      password: replpass
    superuser:
      username: postgres
      password: postgres
  parameters:
    unix_socket_directories: /var/run/postgresql

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: false
  nosync: false
YAML

chown postgres:postgres /tmp/patroni.yml
exec gosu postgres patroni /tmp/patroni.yml

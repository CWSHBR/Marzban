# Patroni-managed Postgres, matching what a two-server Marzban HA deployment
# actually runs. There is no official Patroni image, so this is the usual
# recipe: the stock Postgres image plus Patroni in a venv.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-dev \
       gcc libpq-dev curl iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/patroni \
    && /opt/patroni/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/patroni/bin/pip install --no-cache-dir "patroni[etcd3]==4.0.4" psycopg2-binary

ENV PATH=/opt/patroni/bin:$PATH

COPY entrypoint.sh /entrypoint.sh
COPY post_init.sh /post_init.sh
RUN chmod +x /entrypoint.sh /post_init.sh

EXPOSE 5432 8008
ENTRYPOINT ["/entrypoint.sh"]

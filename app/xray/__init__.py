from random import randint
from typing import TYPE_CHECKING, Dict, Sequence

from app.models.proxy import ProxyHostSecurity
from app.utils.store import DictStorage
from app.utils.system import check_port
from app.xray import operations
from app.xray.config import XRayConfig
from app.xray.core import NullXRayCore, XRayCore
from app.xray.node import XRayNode
from config import (XRAY_ASSETS_PATH, XRAY_EXECUTABLE_PATH, XRAY_JSON,
                    XRAY_JSON_DB_BACKED, XRAY_LOCAL_CORE_ENABLED)
from xray_api import XRay as XRayAPI
from xray_api import exceptions, types
from xray_api import exceptions as exc

if XRAY_LOCAL_CORE_ENABLED:
    core = XRayCore(XRAY_EXECUTABLE_PATH, XRAY_ASSETS_PATH)
else:
    core = NullXRayCore()

# Search for a free API port. Still needed when the local core is disabled:
# api_port is what gets injected as API_INBOUND into the config shipped to
# nodes, and xray.config is read all over the codebase.
try:
    for api_port in range(randint(10000, 60000), 65536):
        if not check_port(api_port):
            break
finally:
    config = XRayConfig(XRAY_JSON, api_port=api_port)
    del api_port

if XRAY_JSON_DB_BACKED:
    # The database is the source of truth for the template so two panels can't
    # drift. The file is only ever read to seed the row on first boot.
    from app import logger as _logger
    from app.ha import core_config as _core_config

    _stored = _core_config.seed_from_file()
    if _stored is not None:
        try:
            config = XRayConfig(_stored, api_port=config.api_port)
        except ValueError as _exc:
            _logger.error(
                f"Stored Xray config template is invalid, falling back to "
                f"{XRAY_JSON}: {_exc}"
            )

# Templates that individual nodes carry instead of the shared one. Loaded here
# so the inbound catalog is complete before the first request is served.
from app.xray import node_configs  # noqa: E402

try:
    node_configs.reload()
except Exception as _exc:  # a config feature must never stop the panel booting
    from app import logger as _boot_logger
    _boot_logger.warning(f"Could not load per-node Xray configs: {_exc}")

# None on a coreless panel. Deliberately not a null object: a fake gRPC channel
# would surface as confusing timeouts instead of an obvious guard.
api = XRayAPI(config.api_host, config.api_port) if XRAY_LOCAL_CORE_ENABLED else None

nodes: Dict[int, XRayNode] = {}

# Liveness observed by the core health check every JOB_CORE_HEALTH_CHECK_INTERVAL.
# Request handlers read this instead of probing nodes themselves: `.connected`
# and `.started` are each a blocking HTTPS POST with a 3s timeout.
node_liveness: Dict[int, dict] = {}


if TYPE_CHECKING:
    from app.db.models import ProxyHost


@DictStorage
def hosts(storage: dict):
    from app.db import GetDB, crud

    storage.clear()
    with GetDB() as db:
        for inbound_tag in config.inbounds_by_tag:
            inbound_hosts: Sequence[ProxyHost] = crud.get_hosts(db, inbound_tag)

            storage[inbound_tag] = [
                {
                    "remark": host.remark,
                    "address": [i.strip() for i in host.address.split(',')] if host.address else [],
                    "port": host.port,
                    "path": host.path if host.path else None,
                    "sni": [i.strip() for i in host.sni.split(',')] if host.sni else [],
                    "host": [i.strip() for i in host.host.split(',')] if host.host else [],
                    "alpn": host.alpn.value,
                    "fingerprint": host.fingerprint.value,
                    # None means the tls is not specified by host itself and
                    #  complies with its inbound's settings.
                    "tls": None
                    if host.security == ProxyHostSecurity.inbound_default
                    else host.security.value,
                    "allowinsecure": host.allowinsecure,
                    "mux_enable": host.mux_enable,
                    "fragment_setting": host.fragment_setting,
                    "noise_setting": host.noise_setting,
                    "random_user_agent": host.random_user_agent,
                    "use_sni_as_host": host.use_sni_as_host,
                } for host in inbound_hosts if not host.is_disabled
            ]


__all__ = [
    "config",
    "hosts",
    "core",
    "api",
    "nodes",
    "node_configs",
    "operations",
    "exceptions",
    "exc",
    "types",
    "XRayConfig",
    "XRayCore",
    "XRayNode",
]

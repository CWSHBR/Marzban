"""Xray config templates that belong to a single node.

By default every node boots from the one shared template (``xray.config``).
A node may instead carry a full template of its own, stored on its row in the
shared database, so it can differ in anything: outbounds, routing, DNS, the
parameters of an inbound, or the very set of inbounds.

Two things follow from "the very set of inbounds":

* Users, hosts and subscriptions are keyed by inbound tag for the whole fleet,
  and read ``xray.config.inbounds_by_tag`` in dozens of places. So the tags a
  node defines on its own are merged into that catalog (``apply_catalog``)
  rather than teaching every caller about nodes.
* For a tag that several templates define, the catalog can hold only one set of
  client-facing settings. The shared template wins, then the node with the
  lowest id. ``describe`` reports where a template disagrees with the entry
  subscriptions are actually built from.
"""

import json
from typing import Dict, Optional

from sqlalchemy.exc import SQLAlchemyError

from app import logger, xray
from app.db import GetDB, crud
from app.xray.config import XRayConfig

# node id -> parsed template. Replaced wholesale by reload(), never mutated.
_configs: Dict[int, XRayConfig] = {}
_names: Dict[int, str] = {}


def build(raw: dict) -> XRayConfig:
    """Parse and validate a template. Raises ValueError like the shared one does."""
    try:
        return XRayConfig(raw, api_port=xray.config.api_port)
    except OSError as exc:
        # A certificateFile/keyFile path the panel cannot read.
        raise ValueError(f"cannot read a file referenced by the config: {exc}")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed config: {exc!r}")


def get(node_id: int) -> Optional[XRayConfig]:
    """The node's own template, or None when it runs the shared one."""
    return _configs.get(node_id)


def template_for(node_id: int) -> XRayConfig:
    custom = _configs.get(node_id)
    return custom if custom is not None else xray.config


def apply_catalog() -> None:
    """Merge node-only inbounds into the shared catalog.

    Must run again whenever ``xray.config`` is replaced, since the extension
    lives on that object.
    """
    extra = []
    for node_id in sorted(_configs):
        extra.extend(_configs[node_id].own_inbounds)
    xray.config.extend_catalog(extra)


def reload() -> None:
    """Re-read every node's template from the database and rebuild the catalog."""
    global _configs, _names

    try:
        with GetDB() as db:
            rows = [(node.id, node.name, node.xray_config)
                    for node in crud.get_nodes_with_custom_config(db)]
    except SQLAlchemyError as exc:
        logger.warning(f"Could not load per-node Xray configs: {exc}")
        return

    configs, names = {}, {}
    for node_id, name, raw in rows:
        # JSON columns come back decoded on most drivers and as text on some.
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        try:
            configs[node_id] = build(raw)
            names[node_id] = name
        except ValueError as exc:
            # Saved configs are validated, so this means the environment
            # changed under it. The node keeps working on the shared template.
            logger.error(
                f"Own Xray config of node \"{name}\" is invalid, the node falls "
                f"back to the shared one: {exc}"
            )

    _configs, _names = configs, names
    apply_catalog()


def _diff(a: dict, b: dict) -> list:
    return sorted(key for key in set(a) | set(b) if a.get(key) != b.get(key))


def describe(node_id: int, candidate: XRayConfig) -> dict:
    """How ``candidate`` relates to the catalog if ``node_id`` were to run it.

    Raises ValueError for the one thing that cannot work: a tag reused with
    another protocol, because a user's credentials are per (protocol, tag).
    """
    shared = xray.config.own_inbounds_by_tag
    others = {}
    for other_id in sorted(_configs):
        if other_id == node_id:
            continue
        for tag, settings in _configs[other_id].own_inbounds_by_tag.items():
            others.setdefault(tag, (other_id, settings))

    inbounds, warnings = [], []
    for settings in candidate.own_inbounds:
        tag = settings["tag"]
        changed = []

        if tag in shared:
            reference, owner = shared[tag], None
        elif tag in others:
            owner, reference = others[tag]
        else:
            reference, owner = None, None

        if reference is not None:
            if reference["protocol"] != settings["protocol"]:
                where = "the shared config" if owner is None else f"node \"{_names.get(owner, owner)}\""
                raise ValueError(
                    f"inbound \"{tag}\" is {settings['protocol']} here but "
                    f"{reference['protocol']} in {where}; a tag must keep its protocol"
                )
            changed = _diff(reference, settings)

        if tag in shared:
            origin = "override" if changed else "shared"
            if changed:
                warnings.append({
                    "code": "client_params_differ",
                    "tag": tag,
                    "fields": changed,
                    "message": (
                        f"Inbound \"{tag}\" differs from the shared config in "
                        f"{', '.join(changed)}. Subscriptions are built from the "
                        f"shared one; use a separate tag if clients must see the difference."
                    ),
                })
        else:
            origin = "node_only"
            if changed:
                name = _names.get(owner, owner)
                warnings.append({
                    "code": "node_tag_conflict",
                    "tag": tag,
                    "fields": changed,
                    "node": name,
                    "message": (
                        f"Inbound \"{tag}\" is also defined by node \"{name}\" with "
                        f"different {', '.join(changed)}. Subscriptions are built "
                        f"from that node's version."
                    ),
                })

        inbounds.append({
            "tag": tag,
            "protocol": settings["protocol"],
            "port": settings.get("port"),
            "network": settings.get("network"),
            "tls": settings.get("tls"),
            "origin": origin,
            "changed": changed,
        })

    return {
        "inbounds": inbounds,
        # Shared inbounds this node would stop serving: hosts that point
        # clients at it for these tags go dead.
        "dropped_tags": [tag for tag in shared if tag not in candidate.own_inbounds_by_tag],
        "warnings": warnings,
    }


__all__ = ["apply_catalog", "build", "describe", "get", "reload", "template_for"]

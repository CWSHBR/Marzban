import threading
from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from app import ha, logger, xray
from app.db import GetDB, crud
from app.ha import outbox
from app.models.node import NodeStatus
from app.models.proxy import ProxyTypes
from app.utils.concurrency import threaded_function
from app.xray.node import XRayNode
from xray_api import XRay as XRayAPI
from xray_api.types.account import Account, XTLSFlows

if TYPE_CHECKING:
    from app.db import User as DBUser
    from app.db.models import Node as DBNode
    from app.models.user import UserResponse


@lru_cache(maxsize=None)
def get_tls():
    from app.db import GetDB, get_tls_certificate
    with GetDB() as db:
        tls = get_tls_certificate(db)
        return {
            "key": tls.key,
            "certificate": tls.certificate
        }


@threaded_function
def _add_user_to_inbound(api: XRayAPI, inbound_tag: str, account: Account):
    try:
        api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
    except (xray.exc.EmailExistsError, xray.exc.ConnectionError):
        pass


@threaded_function
def _remove_user_from_inbound(api: XRayAPI, inbound_tag: str, email: str):
    try:
        api.remove_inbound_user(tag=inbound_tag, email=email, timeout=30)
    except (xray.exc.EmailNotFoundError, xray.exc.ConnectionError):
        pass


@threaded_function
def _alter_inbound_user(api: XRayAPI, inbound_tag: str, account: Account):
    try:
        api.remove_inbound_user(tag=inbound_tag, email=account.email, timeout=30)
    except (xray.exc.EmailNotFoundError, xray.exc.ConnectionError):
        pass
    try:
        api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
    except (xray.exc.EmailExistsError, xray.exc.ConnectionError):
        pass


def _template_of(node_id):
    """The template a node runs: its own, or the shared one."""
    custom = xray.node_configs.get(node_id)
    return custom if custom is not None else xray.config


def _own_tags(template) -> dict:
    """Inbounds a template itself defines (the catalog may list more)."""
    return getattr(template, "own_inbounds_by_tag", template.inbounds_by_tag)


def _dataplanes():
    """(api, template) of every running Xray: the local core, then the nodes."""
    if xray.api is not None:
        yield xray.api, xray.config
    for node_id, node in list(xray.nodes.items()):
        if node.connected and node.started:
            yield node.api, _template_of(node_id)


def _startup_config(node_id, shared=None):
    """Config to boot a node with, and the hash of the template it came from.

    Callers build the shared config once and hand it to every node; a node
    with a template of its own ignores that and gets its own users snapshot.
    """
    custom = xray.node_configs.get(node_id)
    if custom is not None:
        return custom.include_db_users(), custom.template_hash
    if shared is None:
        shared = xray.config.include_db_users()
    return shared, getattr(xray.config, "template_hash", None)


def _record_applied(node_id, config_hash, only_if_unknown: bool = False) -> None:
    """Remember which template the node is now running, for "pending" in the UI."""
    with GetDB() as db:
        try:
            crud.set_node_applied_config_hash(db, node_id, config_hash, only_if_unknown)
        except SQLAlchemyError:
            db.rollback()


def restart_all_cores(config=None):
    if not ha.is_leader():
        # A follower must not touch nodes; the leader picks this up and does
        # one restart pass at the end of its next drain.
        outbox.enqueue(outbox.OP_RESTART_ALL)
        return

    if config is None:
        config = xray.config.include_db_users()

    logger.info("Restarting Xray core for protocols that require full config reload")
    xray.core.restart(config)
    for node_id, node in list(xray.nodes.items()):
        if node.connected:
            xray.operations.restart_node(node_id, config)


def add_user(dbuser: "DBUser"):
    """Push a new user to the dataplane, or hand it to the leader."""
    if not ha.is_leader():
        outbox.enqueue(outbox.OP_ADD_USER, user_id=dbuser.id)
        return
    _add_user_direct(dbuser)


def _user_view(dbuser: "DBUser"):
    from app.models.user import UserResponse

    return UserResponse.model_validate(dbuser)


def _user_accounts(dbuser: "DBUser", inbounds_by_tag: dict = None, user=None):
    """(inbound_tag, account) for every inbound the user is on.

    With ``inbounds_by_tag`` (the inbounds one dataplane serves) only those are
    yielded, and the account is shaped by that dataplane's own settings: a node
    may run the same tag over another transport than the shared template.
    """
    if user is None:
        user = _user_view(dbuser)
    email = f"{dbuser.id}.{dbuser.username}"

    for proxy_type, inbound_tags in user.inbounds.items():
        for inbound_tag in inbound_tags:
            if inbounds_by_tag is None:
                inbound = xray.config.inbounds_by_tag.get(inbound_tag, {})
            elif inbound_tag in inbounds_by_tag:
                inbound = inbounds_by_tag[inbound_tag]
            else:
                continue

            try:
                proxy_settings = user.proxies[proxy_type].dict(no_obj=True)
            except KeyError:
                pass
            account = proxy_type.account_model(email=email, **proxy_settings)

            # XTLS currently only supports transmission methods of TCP and mKCP
            if getattr(account, 'flow', None) and (
                inbound.get('network', 'tcp') not in ('tcp', 'kcp')
                or
                (
                    inbound.get('network', 'tcp') in ('tcp', 'kcp')
                    and
                    inbound.get('tls') not in ('tls', 'reality')
                )
                or
                inbound.get('header_type') == 'http'
            ):
                account.flow = XTLSFlows.NONE

            yield inbound_tag, account


def _add_user_direct(dbuser: "DBUser"):
    user = _user_view(dbuser)
    for api, template in _dataplanes():
        for inbound_tag, account in _user_accounts(dbuser, _own_tags(template), user):
            _add_user_to_inbound(api, inbound_tag, account)


def remove_user(dbuser: "DBUser"):
    """Remove a user from the dataplane, or hand it to the leader.

    The email is captured at enqueue time because the caller usually deletes
    the row right after, leaving nothing for the drain to reconstruct from.
    """
    email = f"{dbuser.id}.{dbuser.username}"
    if not ha.is_leader():
        outbox.enqueue(outbox.OP_REMOVE_USER, user_id=dbuser.id,
                       payload={"email": email})
        return
    remove_user_by_email(email)


def remove_user_by_email(email: str):
    for api, template in _dataplanes():
        for inbound_tag in _own_tags(template):
            _remove_user_from_inbound(api, inbound_tag, email)


def update_user(dbuser: "DBUser"):
    """Re-sync a user with the dataplane, or hand it to the leader."""
    if not ha.is_leader():
        outbox.enqueue(outbox.OP_UPDATE_USER, user_id=dbuser.id)
        return
    _update_user_direct(dbuser)


def _update_user_direct(dbuser: "DBUser"):
    email = f"{dbuser.id}.{dbuser.username}"
    user = _user_view(dbuser)

    for api, template in _dataplanes():
        tags = _own_tags(template)
        active_inbounds = set()
        for inbound_tag, account in _user_accounts(dbuser, tags, user):
            active_inbounds.add(inbound_tag)
            _alter_inbound_user(api, inbound_tag, account)

        for inbound_tag in tags:
            if inbound_tag not in active_inbounds:
                # remove disabled inbounds
                _remove_user_from_inbound(api, inbound_tag, email)


def _expected_emails_by_tag(node_id: int = None):
    """Emails each inbound should carry, per the database, as the node config does."""
    template = _template_of(node_id)
    tags = _own_tags(template)
    config = template.include_db_users()
    expected = {}
    for inbound in config.get("inbounds", []):
        tag = inbound.get("tag")
        if tag not in tags:
            continue
        settings = inbound.get("settings") or {}
        clients = settings.get("clients") or settings.get("users") or []
        expected[tag] = {client["email"] for client in clients if client.get("email")}
    return expected


def reconcile_node_users(node_id: int, node_name: str = None) -> None:
    """Bring the users of an attached (not restarted) node in line with the DB.

    Attaching keeps the running Xray and its connections, but its user list is
    whatever the previous leader last pushed: writes that fell between that
    leader's last push and this promotion are missing. So diff each inbound
    against the database and apply only the difference. A user is added only if
    two database snapshots agree it should be there, and removed only if
    neither has it, so a concurrent API write is not undone.
    """
    node = xray.nodes.get(node_id)
    if node is None:
        return
    api = node.api

    tags_served = _own_tags(_template_of(node_id))
    before = _expected_emails_by_tag(node_id)
    present = {}
    for tag in before:
        try:
            present[tag] = {user.email for user in api.get_inbound_users(tag=tag, timeout=30)}
        except xray.exc.TagNotFoundError:
            continue  # this node does not serve the inbound (node-side INBOUNDS)
        except xray.exc.XrayError as exc:
            logger.warning(f"Could not list users of \"{tag}\" on node {node_name or node_id}: {exc.details}")
    after = _expected_emails_by_tag(node_id)

    missing = {}
    removed = 0
    for tag, emails in present.items():
        wanted = before.get(tag, set()) & after.get(tag, set())
        for email in wanted - emails:
            user_id = email.split(".", 1)[0]
            if user_id.isdigit():
                missing.setdefault(int(user_id), set()).add(tag)
        for email in emails - (before.get(tag, set()) | after.get(tag, set())):
            try:
                api.remove_inbound_user(tag=tag, email=email, timeout=30)
                removed += 1
            except (xray.exc.EmailNotFoundError, xray.exc.ConnectionError):
                pass

    added = 0
    if missing:
        with GetDB() as db:
            for user_id, tags in missing.items():
                dbuser = crud.get_user_by_id(db, user_id)
                if dbuser is None:
                    continue
                for inbound_tag, account in _user_accounts(dbuser, tags_served):
                    if inbound_tag not in tags:
                        continue
                    try:
                        api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
                        added += 1
                    except (xray.exc.EmailExistsError, xray.exc.ConnectionError):
                        pass

    if added or removed:
        logger.info(f"Reconciled users on \"{node_name or node_id}\" node: +{added} -{removed}")


def detach_node(node_id: int):
    if node_id in xray.nodes:
        try:
            xray.nodes[node_id].detach()
        except Exception:
            pass
        finally:
            try:
                del xray.nodes[node_id]
            except KeyError:
                pass


def stop_node(node_id: int):
    if node_id in xray.nodes:
        try:
            xray.nodes[node_id].stop()
        except Exception:
            pass
        finally:
            detach_node(node_id)


def remove_node(node_id: int, stop: bool = False):
    if stop:
        stop_node(node_id)
        return
    detach_node(node_id)


def add_node(dbnode: "DBNode"):
    remove_node(dbnode.id)

    tls = get_tls()
    xray.nodes[dbnode.id] = XRayNode(address=dbnode.address,
                                     port=dbnode.port,
                                     api_port=dbnode.api_port,
                                     ssl_key=tls['key'],
                                     ssl_cert=tls['certificate'],
                                     usage_coefficient=dbnode.usage_coefficient)

    return xray.nodes[dbnode.id]


def _change_node_status(node_id: int, status: NodeStatus, message: str = None, version: str = None):
    with GetDB() as db:
        try:
            dbnode = crud.get_node_by_id(db, node_id)
            if not dbnode:
                return

            if dbnode.status == NodeStatus.disabled:
                remove_node(dbnode.id, stop=True)
                return

            crud.update_node_status(db, dbnode, status, message, version)
        except SQLAlchemyError:
            db.rollback()


# Guards against two threads connecting the same node at once. Promotion fans
# out one thread per node simultaneously, so the previous check-then-set on a
# plain dict was a genuine race.
_connecting_nodes = set()
_connecting_lock = threading.Lock()


def _claim_connecting(node_id) -> bool:
    with _connecting_lock:
        if node_id in _connecting_nodes:
            return False
        _connecting_nodes.add(node_id)
        return True


def _release_connecting(node_id) -> None:
    with _connecting_lock:
        _connecting_nodes.discard(node_id)


@threaded_function
def connect_node(node_id, config=None):
    # Fencing: never touch a node once this panel stopped being leader.
    if not ha.is_leader():
        return

    if not _claim_connecting(node_id):
        return

    try:
        with GetDB() as db:
            dbnode = crud.get_node_by_id(db, node_id)

        if not dbnode:
            return

        try:
            node = xray.nodes[dbnode.id]
            assert node.connected
        except (KeyError, AssertionError):
            node = xray.operations.add_node(dbnode)

        _change_node_status(node_id, NodeStatus.connecting)
        logger.info(f"Connecting to \"{dbnode.name}\" node")

        config, template_hash = _startup_config(node_id, config)

        # The steps above hit the database and can stall for seconds; if this
        # panel was demoted meanwhile, starting the node would restart Xray
        # under the new leader's feet.
        if not ha.is_leader():
            return

        res = node.start(config)
        version = node.get_version()
        _change_node_status(node_id, NodeStatus.connected, version=version)
        attached = isinstance(res, dict) and res.get("attached")
        logger.info(f"Connected to \"{dbnode.name}\" node, xray run on v{version}"
                    + (" (attached, not restarted)" if attached else ""))
        if attached:
            reconcile_node_users(node_id, dbnode.name)
        # An attached node keeps whatever it was running, so only a real
        # (re)start makes this template the applied one. The exception is a
        # node nothing is known about yet (first contact after this column
        # appeared): a template never changes without a restart being asked
        # for, so it is taken to be running the current one.
        _record_applied(node_id, template_hash, only_if_unknown=bool(attached))

    except Exception as e:
        _change_node_status(node_id, NodeStatus.error, message=str(e))
        logger.info(f"Unable to connect to \"{dbnode.name}\" node")

    finally:
        _release_connecting(node_id)


@threaded_function
def restart_node(node_id, config=None):
    if not ha.is_leader():
        outbox.enqueue(outbox.OP_RESTART_NODE, payload={"node_id": node_id})
        return

    with GetDB() as db:
        dbnode = crud.get_node_by_id(db, node_id)

    if not dbnode:
        return

    try:
        node = xray.nodes[dbnode.id]
    except KeyError:
        node = xray.operations.add_node(dbnode)

    if not node.connected:
        return connect_node(node_id, config)

    try:
        logger.info(f"Restarting Xray core of \"{dbnode.name}\" node")

        config, template_hash = _startup_config(node_id, config)

        node.restart(config)
        _record_applied(node_id, template_hash)
        logger.info(f"Xray core of \"{dbnode.name}\" node restarted")
    except Exception as e:
        _change_node_status(node_id, NodeStatus.error, message=str(e))
        logger.info(f"Unable to restart node {node_id}")
        try:
            node.detach()
        except Exception:
            pass


__all__ = [
    "add_user",
    "remove_user",
    "remove_user_by_email",
    "update_user",
    "restart_all_cores",
    "add_node",
    "detach_node",
    "remove_node",
    "stop_node",
    "connect_node",
    "restart_node",
]

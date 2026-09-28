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


def _user_accounts(dbuser: "DBUser"):
    """(inbound_tag, account) for every inbound the user is on."""
    from app.models.user import UserResponse

    user = UserResponse.model_validate(dbuser)
    email = f"{dbuser.id}.{dbuser.username}"

    for proxy_type, inbound_tags in user.inbounds.items():
        for inbound_tag in inbound_tags:
            inbound = xray.config.inbounds_by_tag.get(inbound_tag, {})

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
    for inbound_tag, account in _user_accounts(dbuser):
        if xray.api is not None:
            _add_user_to_inbound(xray.api, inbound_tag, account)  # main core
        for node in list(xray.nodes.values()):
            if node.connected and node.started:
                _add_user_to_inbound(node.api, inbound_tag, account)


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
    for inbound_tag in xray.config.inbounds_by_tag:
        if xray.api is not None:
            _remove_user_from_inbound(xray.api, inbound_tag, email)
        for node in list(xray.nodes.values()):
            if node.connected and node.started:
                _remove_user_from_inbound(node.api, inbound_tag, email)


def update_user(dbuser: "DBUser"):
    """Re-sync a user with the dataplane, or hand it to the leader."""
    if not ha.is_leader():
        outbox.enqueue(outbox.OP_UPDATE_USER, user_id=dbuser.id)
        return
    _update_user_direct(dbuser)


def _update_user_direct(dbuser: "DBUser"):
    email = f"{dbuser.id}.{dbuser.username}"

    active_inbounds = []
    for inbound_tag, account in _user_accounts(dbuser):
        active_inbounds.append(inbound_tag)
        if xray.api is not None:
            _alter_inbound_user(xray.api, inbound_tag, account)  # main core
        for node in list(xray.nodes.values()):
            if node.connected and node.started:
                _alter_inbound_user(node.api, inbound_tag, account)

    for inbound_tag in xray.config.inbounds_by_tag:
        if inbound_tag in active_inbounds:
            continue
        # remove disabled inbounds
        if xray.api is not None:
            _remove_user_from_inbound(xray.api, inbound_tag, email)
        for node in list(xray.nodes.values()):
            if node.connected and node.started:
                _remove_user_from_inbound(node.api, inbound_tag, email)


def _expected_emails_by_tag():
    """Emails each inbound should carry, per the database, as the node config does."""
    config = xray.config.include_db_users()
    expected = {}
    for inbound in config.get("inbounds", []):
        tag = inbound.get("tag")
        if tag not in xray.config.inbounds_by_tag:
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

    before = _expected_emails_by_tag()
    present = {}
    for tag in before:
        try:
            present[tag] = {user.email for user in api.get_inbound_users(tag=tag, timeout=30)}
        except xray.exc.TagNotFoundError:
            continue  # this node does not serve the inbound (node-side INBOUNDS)
        except xray.exc.XrayError as exc:
            logger.warning(f"Could not list users of \"{tag}\" on node {node_name or node_id}: {exc.details}")
    after = _expected_emails_by_tag()

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
                for inbound_tag, account in _user_accounts(dbuser):
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

        if config is None:
            config = xray.config.include_db_users()

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

    except Exception as e:
        _change_node_status(node_id, NodeStatus.error, message=str(e))
        logger.info(f"Unable to connect to \"{dbnode.name}\" node")

    finally:
        _release_connecting(node_id)


@threaded_function
def restart_node(node_id, config=None):
    if not ha.is_leader():
        outbox.enqueue(outbox.OP_RESTART_ALL)
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

        if config is None:
            config = xray.config.include_db_users()

        node.restart(config)
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

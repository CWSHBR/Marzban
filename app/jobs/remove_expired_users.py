import logging

from app import ha, logger
from app.ha.jobs import leader_job
from app.db import GetDB, crud
from app.models.admin import Admin
from app.utils import report
from config import USER_AUTODELETE_INCLUDE_LIMITED_ACCOUNTS

SYSTEM_ADMIN = Admin(username='system', is_sudo=True, telegram_id=None, discord_webhook=None)


def remove_expired_users():
    # Leader-only: two panels running this would duplicate every status
    # change, notification and dataplane call.
    if not ha.is_leader():
        return

    with GetDB() as db:
        deleted_users = crud.autodelete_expired_users(db, USER_AUTODELETE_INCLUDE_LIMITED_ACCOUNTS)

        for user in deleted_users:
            report.user_deleted(user.username, SYSTEM_ADMIN,
                                user_admin=Admin.model_validate(user.admin) if user.admin else None
                                )
            logger.log(logging.INFO, "Expired user %s deleted." % user.username)


leader_job(remove_expired_users, 'interval', coalesce=True, hours=6, max_instances=1)

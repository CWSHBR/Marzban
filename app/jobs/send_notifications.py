from datetime import datetime as dt
from datetime import timedelta as td
from typing import Any, Dict, List

from requests import Session

from app import ha, logger
from app.db import GetDB
from app.db.models import NotificationReminder
from app.ha import notifications as outbox
from app.ha.jobs import leader_job
from config import (JOB_SEND_NOTIFICATIONS_INTERVAL,
                    NUMBER_OF_RECURRENT_NOTIFICATIONS,
                    RECURRENT_NOTIFICATIONS_TIMEOUT, WEBHOOK_ADDRESS,
                    WEBHOOK_SECRET)

session = Session()

headers = {"x-webhook-secret": WEBHOOK_SECRET} if WEBHOOK_SECRET else None

# One batch per tick. Large enough to drain a backlog quickly, small enough
# that a single failed POST doesn't retry thousands of rows at once.
BATCH_SIZE = 200


def send(data: List[Dict[Any, Any]]) -> bool:
    """Send the notification to the webhook address provided by WEBHOOK_ADDRESS

    Args:
        data (List[Dict[Any, Any]]): list of json encoded notifications

    Returns:
        bool: returns True if an ok response received
    """

    result_list = []
    for webhook in WEBHOOK_ADDRESS:
        result = send_req(w_address=webhook, data=data)
        result_list.append(result)
    if True in result_list:
        return True
    else:
        return False


def send_req(w_address: str, data):
    try:
        logger.debug(f"Sending {len(data)} webhook updates to {w_address}")
        r = session.post(w_address, json=data, headers=headers)
        if r.ok:
            return True
        logger.error(r)
    except Exception as err:
        logger.error(err)
    return False


def send_notifications():
    # Leader-only: only one panel may deliver, or every webhook fires twice.
    if not ha.is_leader():
        return

    claimed = outbox.claim(limit=BATCH_SIZE,
                           max_tries=NUMBER_OF_RECURRENT_NOTIFICATIONS)
    if not claimed:
        return

    ids = [row["id"] for row in claimed]
    if send([row["payload"] for row in claimed]):
        outbox.mark_sent(ids)
        return

    outbox.reschedule(ids, delay_seconds=RECURRENT_NOTIFICATIONS_TIMEOUT)


def delete_expired_reminders() -> None:
    if not ha.is_leader():
        return

    with GetDB() as db:
        db.query(NotificationReminder).filter(NotificationReminder.expires_at < dt.utcnow()).delete()
        db.commit()


def purge_sent_notifications() -> None:
    if not ha.is_leader():
        return
    outbox.purge_sent()


if WEBHOOK_ADDRESS:
    # No shutdown flush any more: the queue is durable, and flushing on the way
    # out would race with the panel that is taking over.
    logger.info("Send webhook job started")
    leader_job(send_notifications, "interval",
               seconds=JOB_SEND_NOTIFICATIONS_INTERVAL)
    leader_job(delete_expired_reminders, "interval", hours=2,
               start_date=dt.utcnow() + td(minutes=1))
    leader_job(purge_sent_notifications, "interval", hours=6)

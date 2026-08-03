"""Scheduler registrations for the HA sync loops."""

from app import scheduler
from app.ha import cache, outbox
from app.ha.jobs import leader_job
from app.ha.members import purge_stale_members
from config import HA_CACHE_POLL_INTERVAL, HA_ENABLED, HA_OUTBOX_DRAIN_INTERVAL

if HA_ENABLED:
    # Runs on both roles: a follower has to notice host/config edits made by
    # the leader just as much as the other way round.
    scheduler.add_job(cache.poll_cache_versions, "interval",
                      seconds=HA_CACHE_POLL_INTERVAL,
                      id="ha-cache-poll", replace_existing=True,
                      coalesce=True, max_instances=1)

    leader_job(outbox.drain, "interval",
               seconds=HA_OUTBOX_DRAIN_INTERVAL,
               coalesce=True, max_instances=1)
    leader_job(outbox.purge_done, "interval", hours=1)
    leader_job(purge_stale_members, "interval", hours=1)

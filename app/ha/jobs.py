"""Scheduler jobs that only exist while this panel is the leader.

Jobs register themselves at import time, but the scheduler must not actually
run them on a follower -- and the set has to change when the role changes, not
only at process start. So registration is deferred: ``leader_job`` records the
spec, and the promote/demote callbacks add and remove the real APScheduler
jobs.

Job bodies should *still* begin with ``if not ha.is_leader(): return``. That
covers the window where APScheduler has already dispatched a job at the moment
demotion happens.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from app import logger, scheduler
from app.ha import on_demote, on_promote


@dataclass
class JobSpec:
    func: Callable
    trigger: str
    kwargs: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"ha-leader:{self.func.__module__}.{self.func.__qualname__}"


_registry: List[JobSpec] = []


def leader_job(func: Callable, trigger: str = "interval", **kwargs) -> Callable:
    """Register a job to run only while this panel holds the lease.

    Mirrors ``scheduler.add_job(func, trigger, **kwargs)``.
    """
    _registry.append(JobSpec(func=func, trigger=trigger, kwargs=kwargs))
    return func


def registered() -> List[JobSpec]:
    return list(_registry)


def _start_leader_jobs() -> None:
    for spec in _registry:
        try:
            scheduler.add_job(
                spec.func, spec.trigger,
                id=spec.id, replace_existing=True, **spec.kwargs
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"Failed to schedule leader job {spec.id}: {exc}")
    logger.info(f"Started {len(_registry)} leader-only scheduler jobs")


def _stop_leader_jobs() -> None:
    for spec in _registry:
        try:
            scheduler.remove_job(spec.id)
        except Exception:
            # JobLookupError when it was never scheduled; nothing to undo.
            pass
    logger.info("Stopped leader-only scheduler jobs")


on_promote(_start_leader_jobs)
on_demote(_stop_leader_jobs)


__all__ = ["JobSpec", "leader_job", "registered"]

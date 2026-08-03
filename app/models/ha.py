from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class HAMemberInfo(BaseModel):
    panel_id: str
    role: str
    version: Optional[str] = None
    advertise_url: Optional[str] = None
    local_core_enabled: bool = True
    started_at: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    # Seconds since the last heartbeat, measured on the database clock so panel
    # clock skew never shows up here.
    last_seen_age: Optional[float] = None
    is_stale: bool = True
    is_self: bool = False


class HALeaseInfo(BaseModel):
    holder: Optional[str] = None
    epoch: int = 0
    advertise_url: Optional[str] = None
    acquired_at: Optional[datetime] = None
    renewed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    expires_in: Optional[float] = None
    stepdown_requested: bool = False


class HASelfInfo(BaseModel):
    panel_id: str
    role: str
    epoch: int = 0
    ha_enabled: bool = False
    local_core_enabled: bool = True
    # Remaining self-fencing budget before this panel demotes itself.
    fencing_seconds_left: Optional[float] = None
    leader_for_seconds: Optional[float] = None
    last_error: Optional[str] = None


class HAOutboxInfo(BaseModel):
    pending: Optional[int] = None
    oldest_created_at: Optional[datetime] = None
    oldest_age_seconds: Optional[float] = None
    # True once the drain switches to a single full node reload instead of
    # per-user updates.
    backlog_bailout: bool = False


class HANodesInfo(BaseModel):
    by_status: Dict[str, int] = {}
    needs_restart: int = 0


class HAStatus(BaseModel):
    # `self` is a reserved name in Python, so the field is `self_` internally
    # and aliased on the wire. Done with an alias rather than by overriding
    # model_dump: FastAPI re-validates the serialized dict against this model,
    # so a rename done in model_dump would fail that second validation.
    self_: HASelfInfo = Field(alias="self", serialization_alias="self")
    lease: Optional[HALeaseInfo] = None
    members: List[HAMemberInfo] = []
    outbox: HAOutboxInfo = HAOutboxInfo()
    notifications: HAOutboxInfo = HAOutboxInfo()
    nodes: HANodesInfo = HANodesInfo()
    # True when a handover has somewhere to go: another panel is alive and this
    # cluster currently has a leader.
    can_handover: bool = False
    handover_target: Optional[str] = None
    handover_blocked_reason: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)

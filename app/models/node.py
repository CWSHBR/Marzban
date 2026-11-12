from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import ConfigDict, BaseModel, Field


class NodeStatus(str, Enum):
    connected = "connected"
    connecting = "connecting"
    error = "error"
    disabled = "disabled"


class NodeSettings(BaseModel):
    min_node_version: str = "v0.2.0"
    certificate: str


class Node(BaseModel):
    name: str
    address: str
    port: int = 62050
    api_port: int = 62051
    usage_coefficient: float = Field(gt=0, default=1.0)


class NodeCreate(Node):
    add_as_new_host: bool = True
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "DE node",
            "address": "192.168.1.1",
            "port": 62050,
            "api_port": 62051,
            "add_as_new_host": True,
            "usage_coefficient": 1
        }
    })


class NodeModify(Node):
    name: Optional[str] = Field(None, nullable=True)
    address: Optional[str] = Field(None, nullable=True)
    port: Optional[int] = Field(None, nullable=True)
    api_port: Optional[int] = Field(None, nullable=True)
    status: Optional[NodeStatus] = Field(None, nullable=True)
    usage_coefficient: Optional[float] = Field(None, nullable=True)
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "DE node",
            "address": "192.168.1.1",
            "port": 62050,
            "api_port": 62051,
            "status": "disabled",
            "usage_coefficient": 1.0
        }
    })


class NodeResponse(Node):
    id: int
    xray_version: Optional[str] = None
    status: NodeStatus
    message: Optional[str] = None
    is_running: Optional[bool] = None
    is_attached: Optional[bool] = None
    needs_restart: Optional[bool] = None
    status_message: Optional[str] = None
    # True when the node carries its own Xray config instead of the shared one.
    has_custom_config: bool = False
    xray_config_updated_at: Optional[datetime] = None
    xray_config_updated_by: Optional[str] = None
    # The template in effect was saved but the node has not been restarted
    # with it yet. None when no panel has (re)started the node so far.
    config_pending: Optional[bool] = None
    model_config = ConfigDict(from_attributes=True)


class NodeConfigInbound(BaseModel):
    tag: str
    protocol: str
    port: Optional[Any] = None
    network: Optional[str] = None
    tls: Optional[str] = None
    # shared: same as the shared config; override: same tag, other settings;
    # node_only: a tag the shared config does not have.
    origin: str
    changed: List[str] = []


class NodeConfigWarning(BaseModel):
    code: str
    tag: str
    fields: List[str] = []
    node: Optional[str] = None
    message: str


class NodeConfigSummary(BaseModel):
    inbounds: List[NodeConfigInbound]
    dropped_tags: List[str] = []
    warnings: List[NodeConfigWarning] = []
    # node-only tags the panel has never seen: on save each gets a host at
    # the node's address (the default one would point at the panel instead)
    new_tags: List[str] = []


class NodeConfigResponse(BaseModel):
    node_id: int
    custom: bool
    # The node's own template when `custom`, otherwise the shared one.
    config: Dict[str, Any]
    shared_config: Dict[str, Any]
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    pending: Optional[bool] = None
    summary: Optional[NodeConfigSummary] = None


class NodeUsageResponse(BaseModel):
    node_id: Optional[int] = None
    node_name: str
    uplink: int
    downlink: int


class NodesUsageResponse(BaseModel):
    usages: List[NodeUsageResponse]

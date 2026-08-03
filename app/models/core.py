from typing import Optional

from pydantic import BaseModel


class CoreStats(BaseModel):
    # False when the panel runs as a pure control plane with no local Xray.
    # version is then None, so it must be optional.
    enabled: bool = True
    version: Optional[str] = None
    started: bool
    logs_websocket: str

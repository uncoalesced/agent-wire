"""Strict output schemas. Unknown keys are dropped on validation (outbound firewall layer 3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WireComment(_Strict):
    id: str
    body: str
    author_handle: str | None = None
    timestamp: datetime | None = None
    score: int = 0
    depth: int = 0
    is_op: bool = False
    is_accepted: bool = False
    replies: list["WireComment"] = []


class WireDocument(_Strict):
    url: str
    channel: str
    title: str | None = None
    author_handle: str | None = None
    timestamp: datetime | None = None
    score: int | None = None
    body: str = ""
    comments: list[WireComment] = []
    metadata: dict[str, str | int | float | bool | None] = {}
    # Pipeline telemetry, never rendered into the document text.
    stage: int = 1
    auth: str = "none"
    raw_bytes: int = 0


class WireSearchHit(_Strict):
    url: str
    title: str
    snippet: str = ""
    source: str = "web"
    rank_score: float = 0.0


class WireTelemetry(_Strict):
    channel: str
    tokens: int
    saved_pct: int = 0
    auth: str = "none"
    stage: int = 1
    elapsed_ms: int = 0

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
    score_kind: str = "points"  # or "replies" where a site hides comment points (HN)
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
    # Filled where the platform exposes them; wire_research scores freshness and engagement.
    timestamp: datetime | None = None
    author: str | None = None
    link: str | None = None  # the article a post points at (HN/Reddit/Techmeme), for dedup
    engagement: dict[str, int] = {}  # score, comments, likes, reposts, views, volume ...


class WireTelemetry(_Strict):
    channel: str
    tokens: int
    saved_pct: int = 0
    auth: str = "none"
    stage: int = 1
    elapsed_ms: int = 0

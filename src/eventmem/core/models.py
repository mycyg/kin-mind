from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def utc(value: str) -> str:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("A timezone is required")
    return d.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Model):
    project: str = "personal"
    persona: str = "default"
    collection: str = "default"
    world: str = "real"

    def key(self) -> str:
        return self.model_dump_json()


Kind = Literal[
    "episode",
    "fact",
    "state",
    "preference",
    "procedure",
    "relationship",
    "commitment",
    "reminder",
    "prediction",
    "diary",
    "summary",
    "portrait",
    "self_narrative",
    "knowledge",
    "checkpoint",
    "observation",
]
State = Literal[
    "active", "unverified", "superseded", "refuted", "retracted", "archived"
]


class SourceInput(Model):
    namespace: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=500)
    version: str = Field(default="1", min_length=1, max_length=100)
    session: str = ""
    scope: Scope = Field(default_factory=Scope)
    occurred_at: str = Field(default_factory=now)
    text: str = Field(default="", max_length=10_000_000)
    media_type: str = "text/plain"
    title: str = Field(default="", max_length=1000)
    origin: str = ""
    kind: Kind = "observation"
    authority: Literal["explicit", "operation", "document", "model"] = "explicit"
    extract: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    _time = field_validator("occurred_at")(utc)


class RecordInput(Model):
    id: str | None = None
    kind: Kind
    title: str = Field(default="", max_length=1000)
    content: str = Field(min_length=1, max_length=1_000_000)
    scope: Scope = Field(default_factory=Scope)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    valid_from: str = Field(default_factory=now)
    valid_until: str | None = None
    status: State = "active"
    confirmation: Literal[
        "explicit", "observed", "documented", "inferred", "verified"
    ] = "explicit"
    importance: float = Field(default=0.5, ge=0, le=1)
    generated: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)
    locator: dict[str, Any] = Field(default_factory=dict)

    _time = field_validator("valid_from")(utc)

    @field_validator("valid_until")
    @classmethod
    def check_until(cls, value: str | None) -> str | None:
        return utc(value) if value else None

    @model_validator(mode="after")
    def consistency(self):
        if self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.generated and self.confirmation not in ("inferred", "verified"):
            self.confirmation = "inferred"
        if self.generated and self.kind in (
            "fact",
            "state",
            "preference",
            "relationship",
            "commitment",
        ):
            if self.confirmation != "verified" and self.status == "active":
                self.status = "unverified"
        return self


class RevisionInput(Model):
    expected_revision: int = Field(ge=1)
    command_id: str = Field(min_length=1, max_length=200)
    action: Literal[
        "correct",
        "confirm",
        "retract",
        "replace",
        "refute",
        "archive",
        "restore",
        "rollback",
    ]
    content: str | None = Field(default=None, max_length=1_000_000)
    reason: str = Field(default="", max_length=2000)
    replacement_id: str | None = None
    target_revision: int | None = None
    attributes: dict[str, Any] | None = None


class RecallQuery(Model):
    """Everything a recall may be asked, except what it is for. The chat model's tools take
    this shape, so they cannot declare a purpose: what they recall is experience."""

    query: str = Field(default="", max_length=4000)
    scope: Scope = Field(default_factory=Scope)
    scenario: Literal[
        "tool",
        "companion",
        "knowledge",
        "research",
        "creative",
        "support",
        "operations",
    ] = "tool"
    mode: Literal["fast", "deep"] = "fast"
    at: str | None = None
    known_at: str | None = None
    history: bool = False
    include_shared: bool = True
    kinds: list[Kind] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
    budget: int | None = Field(default=None, ge=0, le=32000)
    session: str | None = None
    phase: Literal["search", "startup", "passive", "read", "compact"] = "search"
    host_mode: Literal["append", "replace"] = "append"
    vector: list[float] | None = Field(default=None, max_length=8192)
    index: str | None = None
    explain: bool = False

    @field_validator("at", "known_at")
    @classmethod
    def check_time(cls, value: str | None) -> str | None:
        return utc(value) if value else None


RecallPurpose = Literal["experience_recall", "self_knowledge_view", "audit"]


class RecallRequest(RecallQuery):
    # Not `purpose`: that name belongs to the context budget (`Contexts.build`, the host's
    # memory-context). `history` keeps its status and expiry meaning only.
    recall_purpose: RecallPurpose = Field(
        default="experience_recall",
        description="What the read is for. Omit it for the default: a server that predates "
        "this field rejects it. experience_recall returns lived experience only. "
        "self_knowledge_view also returns role configuration, its examples and self-claims, "
        "each labelled with its class. audit returns everything, labelled. history only "
        "lifts the status and expiry checks.",
    )


class ContactPolicy(Model):
    id: str = "default"
    scope: Scope = Field(default_factory=Scope)
    enabled: bool = False
    channel: str | None = None
    timezone: str = "UTC"
    quiet_start: int = Field(default=22, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)
    max_per_day: int = Field(default=3, ge=0, le=100)
    min_interval_minutes: int = Field(default=60, ge=0)
    require_confirmation: bool = True
    idempotent_channel: bool = False
    greeting_text: str = Field(
        default="想聊聊今天的近况吗？", min_length=1, max_length=2000
    )
    triggers: list[
        Literal["reminder", "commitment", "anniversary", "checkin", "greeting"]
    ] = Field(default_factory=lambda: ["reminder", "commitment"])
    allowed_kinds: list[Kind] = Field(
        default_factory=lambda: ["reminder", "commitment"]
    )

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
        return value

    @field_validator("channel")
    @classmethod
    def valid_channel(cls, value: str | None) -> str | None:
        from urllib.parse import urlparse

        if value:
            url = urlparse(value)
            if (
                url.scheme not in ("https", "http")
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError("Use an HTTP(S) callback URL without credentials")
            if url.scheme == "http" and url.hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                raise ValueError("Non-local callbacks require HTTPS")
        return value


class ScheduleInput(Model):
    command_id: str
    policy_id: str = "default"
    record_id: str
    due_at: str
    trigger: Literal["reminder", "commitment", "anniversary", "checkin", "greeting"] = (
        "reminder"
    )
    recurrence: Literal["none", "daily", "weekly", "yearly"] = "none"
    _time = field_validator("due_at")(utc)


class ModelRole(Model):
    endpoint: str
    model: str
    protocol: Literal["openai", "anthropic"] = "openai"
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    dimensions: int | None = Field(default=None, ge=1, le=8192)
    preprocessing: str = "text-v1"
    local_embedding: bool = False
    input_price_per_million: float = Field(default=0, ge=0)
    output_price_per_million: float = Field(default=0, ge=0)


class Page(Model):
    items: list[dict[str, Any]]
    cursor: str | None = None


class Result(Model):
    id: str
    revision: int
    status: str
    source_ids: list[str] = Field(default_factory=list)

# Generated from contracts/openapi.json.
from __future__ import annotations
from typing import Any, Literal
from typing_extensions import Required, TypedDict

class Body_restore_backup(TypedDict, total=False):
    file: Required[str]

class Body_upload_source(TypedDict, total=False):
    metadata: Required[str]
    file: Required[str]

class ContactPolicy(TypedDict, total=False):
    id: str
    scope: Scope
    enabled: bool
    channel: str | None
    timezone: str
    quiet_start: int
    quiet_end: int
    max_per_day: int
    min_interval_minutes: int
    require_confirmation: bool
    idempotent_channel: bool
    greeting_text: str
    triggers: list[Literal['reminder', 'commitment', 'anniversary', 'checkin', 'greeting']]
    allowed_kinds: list[Literal['episode', 'fact', 'state', 'preference', 'procedure', 'relationship', 'commitment', 'reminder', 'prediction', 'diary', 'summary', 'portrait', 'self_narrative', 'knowledge', 'checkpoint', 'observation']]

class CreateRecord(TypedDict, total=False):
    record: Required[RecordInput]
    command_id: Required[str]

class FamilyChange(TypedDict, total=False):
    expected_revision: Required[int]
    action: Required[Literal['publish', 'archive', 'merge', 'split', 'revise', 'rollback']]
    members: list[str] | None
    target: str | None
    title: str | None
    target_revision: int | None

class FamilyCreate(TypedDict, total=False):
    scope: Scope
    title: Required[str]
    members: Required[list[str]]
    kind: Literal['family', 'volume']

class FeedbackRequest(TypedDict, total=False):
    record_id: Required[str]
    type: Required[Literal['displayed', 'read', 'adopted', 'verified', 'corrected', 'unknown', 'same_file_observed']]
    session: str
    attributes: dict[str, Any]
    key: str | None

class GraphCommand(TypedDict, total=False):
    scope: Required[Scope]
    request: Required[dict[str, Any]]

class HTTPValidationError(TypedDict, total=False):
    detail: list[ValidationError]

class HostEvent(TypedDict, total=False):
    event: Required[Literal['start', 'pre_action', 'tool', 'message', 'compact', 'end', 'boundary']]
    payload: Required[dict[str, Any]]

class MaintenanceRequest(TypedDict, total=False):
    kind: Required[Literal['organize', 'diary', 'summary', 'portrait', 'self_narrative', 'prediction', 'rebuild', 'build_vectors', 'purge_vectors']]
    scope: Scope
    since: str
    command_id: Required[str]
    title: str

class ModelRole(TypedDict, total=False):
    endpoint: Required[str]
    model: Required[str]
    protocol: Literal['openai', 'anthropic']
    api_key_env: str | None
    timeout_seconds: float
    dimensions: int | None
    preprocessing: str
    local_embedding: bool
    input_price_per_million: float
    output_price_per_million: float

class RecallItem(TypedDict, total=False):
    id: Required[str]
    title: Required[str]
    kind: Required[Literal['episode', 'fact', 'state', 'preference', 'procedure', 'relationship', 'commitment', 'reminder', 'prediction', 'diary', 'summary', 'portrait', 'self_narrative', 'knowledge', 'checkpoint', 'observation']]
    status: Required[Literal['active', 'unverified', 'superseded', 'refuted', 'retracted', 'archived']]
    revision: Required[int]
    source_ids: Required[list[str]]
    read_url: Required[str]
    locator: Required[dict[str, Any]]
    generated: Required[bool]
    confirmation: Required[str]

class RecallRequest(TypedDict, total=False):
    query: str
    scope: Scope
    scenario: Literal['tool', 'companion', 'knowledge', 'research', 'creative', 'support', 'operations']
    mode: Literal['fast', 'deep']
    at: str | None
    known_at: str | None
    history: bool
    include_shared: bool
    kinds: list[Literal['episode', 'fact', 'state', 'preference', 'procedure', 'relationship', 'commitment', 'reminder', 'prediction', 'diary', 'summary', 'portrait', 'self_narrative', 'knowledge', 'checkpoint', 'observation']]
    limit: int
    budget: int | None
    session: str | None
    phase: Literal['search', 'startup', 'passive', 'read', 'compact']
    host_mode: Literal['append', 'replace']
    vector: list[float] | None
    index: str | None
    explain: bool
    recall_purpose: Literal['experience_recall', 'self_knowledge_view', 'audit']

class RecallResult(TypedDict, total=False):
    items: Required[list[RecallItem]]
    text: Required[str]
    tokens: Required[int]
    budget: Required[int]
    accounts: Required[dict[str, int]]
    generation: Required[int]
    latency_ms: Required[float]
    cursor: Required[str | None]
    session_used: Required[int]
    instruction_authority: Required[str]

class RecordInput(TypedDict, total=False):
    id: str | None
    kind: Required[Literal['episode', 'fact', 'state', 'preference', 'procedure', 'relationship', 'commitment', 'reminder', 'prediction', 'diary', 'summary', 'portrait', 'self_narrative', 'knowledge', 'checkpoint', 'observation']]
    title: str
    content: Required[str]
    scope: Scope
    source_ids: list[str]
    evidence_ids: list[str]
    parent_id: str | None
    valid_from: str
    valid_until: str | None
    status: Literal['active', 'unverified', 'superseded', 'refuted', 'retracted', 'archived']
    confirmation: Literal['explicit', 'observed', 'documented', 'inferred', 'verified']
    importance: float
    generated: bool
    attributes: dict[str, Any]
    locator: dict[str, Any]

class RecordResult(TypedDict, total=False):
    id: Required[str]
    kind: Required[Literal['episode', 'fact', 'state', 'preference', 'procedure', 'relationship', 'commitment', 'reminder', 'prediction', 'diary', 'summary', 'portrait', 'self_narrative', 'knowledge', 'checkpoint', 'observation']]
    title: Required[str]
    content: Required[str]
    scope: Required[ScopeResult]
    status: Required[Literal['active', 'unverified', 'superseded', 'refuted', 'retracted', 'archived']]
    revision: Required[int]
    source_ids: Required[list[str]]
    generated: Required[bool]
    confirmation: Required[str]
    valid_from: Required[str]
    valid_until: Required[str | None]
    received_at: Required[str]
    read_url: Required[str]
    locator: Required[dict[str, Any]]

class RelationRequest(TypedDict, total=False):
    subject: Required[str]
    predicate: Required[str]
    object: Required[str]
    attributes: dict[str, Any]

class RevisionInput(TypedDict, total=False):
    expected_revision: Required[int]
    command_id: Required[str]
    action: Required[Literal['correct', 'confirm', 'retract', 'replace', 'refute', 'archive', 'restore', 'rollback']]
    content: str | None
    reason: str
    replacement_id: str | None
    target_revision: int | None
    attributes: dict[str, Any] | None

class ScheduleChange(TypedDict, total=False):
    expected_revision: Required[int]
    action: Required[Literal['cancel', 'pause', 'resume', 'snooze', 'confirm']]
    due_at: str | None

class ScheduleInput(TypedDict, total=False):
    command_id: Required[str]
    policy_id: str
    record_id: Required[str]
    due_at: Required[str]
    trigger: Literal['reminder', 'commitment', 'anniversary', 'checkin', 'greeting']
    recurrence: Literal['none', 'daily', 'weekly', 'yearly']

class Scope(TypedDict, total=False):
    project: str
    persona: str
    collection: str
    world: str

class ScopeResult(TypedDict, total=False):
    project: Required[str]
    persona: Required[str]
    collection: Required[str]
    world: Required[str]

class SessionBoundary(TypedDict, total=False):
    session: Required[str]
    scope: Scope
    event: Required[Literal['start', 'end', 'compact', 'checkpoint']]
    scenario: Literal['tool', 'companion', 'knowledge']
    host_mode: Literal['append', 'replace']
    command_id: Required[str]
    checkpoint: dict[str, Any]

class SourceInput(TypedDict, total=False):
    namespace: Required[str]
    key: Required[str]
    version: str
    session: str
    scope: Scope
    occurred_at: str
    text: str
    media_type: str
    title: str
    origin: str
    kind: Literal['episode', 'fact', 'state', 'preference', 'procedure', 'relationship', 'commitment', 'reminder', 'prediction', 'diary', 'summary', 'portrait', 'self_narrative', 'knowledge', 'checkpoint', 'observation']
    authority: Literal['explicit', 'operation', 'document', 'model']
    extract: bool
    metadata: dict[str, Any]

class SourceResult(TypedDict, total=False):
    id: Required[str]
    namespace: Required[str]
    key: Required[str]
    version: Required[str]
    scope: Required[ScopeResult]
    status: Required[str]
    revision: Required[int]
    source_ids: Required[list[str]]
    mechanical: Required[str]
    model: Required[str]
    received_at: Required[str]
    occurred_at: Required[str]
    read_url: Required[str]
    title: Required[str]
    media_type: Required[str]
    byte_length: Required[int]
    attachment_url: Required[str]

class ValidationError(TypedDict, total=False):
    loc: Required[list[str | int]]
    msg: Required[str]
    type: Required[str]
    input: Any
    ctx: dict[str, Any]

class WebSource(TypedDict, total=False):
    url: Required[str]
    scope: Scope
    title: str

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .db import Conflict, Missing, dumps
from .engine import Engine, uid
from .jobs import Worker
from .models import (
    ContactPolicy,
    Model,
    ModelRole,
    RecallRequest,
    RecordInput,
    RevisionInput,
    ScheduleInput,
    Scope,
    SourceInput,
)
from .organize import Organizer
from .responses import RecallResult, RecordResult, SourceResult
from .scheduler import Scheduler


class CreateRecord(Model):
    record: RecordInput
    command_id: str


class RelationRequest(Model):
    subject: str
    predicate: str
    object: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphCommand(Model):
    scope: Scope
    request: dict[str, Any]


class FeedbackRequest(Model):
    record_id: str
    type: Literal[
        "displayed",
        "read",
        "adopted",
        "verified",
        "corrected",
        "unknown",
        "same_file_observed",
    ]
    session: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    key: str | None = None


class MaintenanceRequest(Model):
    kind: Literal[
        "organize",
        "diary",
        "summary",
        "portrait",
        "self_narrative",
        "prediction",
        "rebuild",
        "build_vectors",
        "purge_vectors",
    ]
    scope: Scope = Field(default_factory=Scope)
    since: str = ""
    command_id: str
    title: str = ""


class FamilyCreate(Model):
    scope: Scope = Field(default_factory=Scope)
    title: str
    members: list[str] = Field(max_length=1000)
    kind: Literal["family", "volume"] = "volume"


class FamilyChange(Model):
    expected_revision: int = Field(ge=1)
    action: Literal["publish", "archive", "merge", "split", "revise", "rollback"]
    members: list[str] | None = None
    target: str | None = None
    title: str | None = None
    target_revision: int | None = None


class ScheduleChange(Model):
    expected_revision: int = Field(ge=1)
    action: Literal["cancel", "pause", "resume", "snooze", "confirm"]
    due_at: str | None = None


class SessionBoundary(Model):
    session: str
    scope: Scope = Field(default_factory=Scope)
    event: Literal["start", "end", "compact", "checkpoint"]
    scenario: Literal["tool", "companion", "knowledge"] = "tool"
    host_mode: Literal["append", "replace"] = "append"
    command_id: str
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class HostEvent(Model):
    event: Literal[
        "start", "pre_action", "tool", "message", "compact", "end", "boundary"
    ]
    payload: dict[str, Any]


class WebSource(Model):
    url: str = Field(max_length=500)
    scope: Scope = Field(default_factory=Scope)
    title: str = ""


def boundary(engine, request):
    if request.event in {"end", "checkpoint", "compact"} and request.checkpoint:
        allowed = {
            "goals",
            "confirmed_progress",
            "unverified_results",
            "blockers",
            "commitments",
            "next_entry",
        }
        checkpoint = {k: v for k, v in request.checkpoint.items() if k in allowed}
        source = engine.receive(
            SourceInput(
                namespace="checkpoint",
                key=request.command_id,
                session=request.session,
                scope=request.scope,
                text=dumps(checkpoint),
                kind="checkpoint",
                authority="operation",
                title="Session checkpoint",
                metadata=checkpoint,
            )
        )
        if checkpoint.get("next_entry"):
            engine.enqueue(
                "prefetch",
                {
                    "scope": request.scope.model_dump(),
                    "cue": str(checkpoint["next_entry"]),
                },
                f"prefetch:{source['id']}",
            )
    if request.event in {"start", "compact"}:
        return engine.recall(
            RecallRequest(
                scope=request.scope,
                session=request.session,
                scenario=request.scenario,
                phase="startup" if request.event == "start" else "compact",
                host_mode=request.host_mode,
            )
        )
    return {
        "session": request.session,
        "status": "saved",
        "checkpoint": request.checkpoint,
    }


def credential(root):
    path = Path(root) / "local-token"
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            pass
    return path.read_text().strip()


def create_app(root=None, *, engine=None, token=None, workers=True, mcp_enabled=True):
    engine = engine or Engine(root or Path.home() / ".memorypalace")
    auth_token = token or credential(engine.db.root)
    worker = Worker(engine)
    mcp_server = None
    if mcp_enabled:
        from .mcp import create_mcp

        mcp_server = create_mcp(engine)
        mcp_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        # Initialize tokenization before accepting latency-sensitive requests.
        from .retrieval import tokens

        await asyncio.to_thread(tokens, "MemoryPalace")
        thread = threading.Thread(
            target=worker.run, name="memorypalace-worker", daemon=True
        )
        if workers:
            thread.start()
        if mcp_server:
            async with mcp_server.session_manager.run():
                yield
        else:
            yield
        worker.stopped.set()
        if workers:
            await asyncio.to_thread(thread.join, 6)

    app = FastAPI(
        title="MemoryPalace",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/v1/docs",
        openapi_url="/v1/openapi.json",
        redoc_url=None,
    )
    app.state.engine = engine
    app.state.worker = worker

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "Host is not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {
            f"http://127.0.0.1:{request.url.port or 80}",
            f"http://localhost:{request.url.port or 80}",
            f"http://[::1]:{request.url.port or 80}",
        }:
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        if request.url.path.startswith(("/v1", "/mcp")):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not secrets.compare_digest(supplied, auth_token):
                return JSONResponse(
                    {"detail": "Local credential required"}, status_code=401
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        # Additive: `detail` is unchanged, the taxonomy says what moved and whether
        # the caller may retry. Static classification only, never the payload.
        from kin_mind.conflicts import classify

        found = classify(exc)
        return JSONResponse({"detail": str(exc), "kind": found.kind,
                             **({"code": found.code} if found.code else {})}, status_code=409)

    @app.exception_handler(Missing)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/v1/health", operation_id="health")
    def health() -> dict:
        return {"status": "ready", "version": "1.0.0", "schema": 1}

    @app.post("/v1/sources", operation_id="receive_source", response_model=SourceResult)
    def receive_source(source: SourceInput) -> dict:
        return engine.receive(source)

    @app.post("/v1/sources/batch", operation_id="receive_batch")
    def receive_batch(sources: list[SourceInput] = Body(max_length=100)) -> dict:
        # Each receipt is durable independently; errors identify the exact item.
        results = []
        for i, source in enumerate(sources):
            try:
                results.append({"index": i, "result": engine.receive(source)})
            except (Conflict, ValueError) as exc:
                results.append({"index": i, "error": str(exc)})
        return {"items": results, "cursor": None}

    @app.post(
        "/v1/sources/upload", operation_id="upload_source", response_model=SourceResult
    )
    async def upload_source(metadata: str = Form(), file: UploadFile = File()) -> dict:
        source = SourceInput.model_validate_json(metadata)
        raw = await file.read(256 * 1024 * 1024 + 1)
        if len(raw) > 256 * 1024 * 1024:
            raise HTTPException(413, "Attachment exceeds 256 MiB; split the source")
        return await asyncio.to_thread(engine.receive, source, raw)

    @app.get(
        "/v1/sources/{source_id}",
        operation_id="read_source",
        response_model=SourceResult,
    )
    def read_source(
        source_id: str, cursor: str = "", limit: int = Query(100, ge=1, le=200)
    ) -> dict:
        return engine.source(source_id, cursor=cursor, limit=limit)

    @app.post("/v1/sources/url", operation_id="import_url")
    def import_url(request: WebSource) -> dict:
        from .maintenance import snapshot_url

        return snapshot_url(engine, request.url, request.scope, request.title)

    @app.get("/v1/sources/{source_id}/content", operation_id="read_attachment")
    def read_attachment(source_id: str):
        source = engine.source(source_id)
        # Force download for active formats; no uploaded HTML executes in console origin.
        return FileResponse(
            engine.source(source_id, content=True),
            media_type=source["media_type"],
            filename=Path(source["title"] or "source").name,
        )

    @app.get("/v1/sources/{source_id}/clip", operation_id="read_clip")
    def read_clip(source_id: str, start: float = 0, end: float = 30):
        from .media import clip

        raw, mime = clip(engine, source_id, start, end)
        return Response(raw, media_type=mime)

    @app.get("/v1/sources/{source_id}/page", operation_id="read_page")
    def read_page(source_id: str, page: int = Query(1, ge=1)):
        import io

        import pypdfium2 as pdfium

        source = engine.source(source_id)
        if source["media_type"] != "application/pdf":
            raise ValueError("Source is not a PDF")
        document = pdfium.PdfDocument(engine.source(source_id, content=True))
        try:
            if page > len(document):
                raise ValueError("Page is out of range")
            pdf_page = document[page - 1]
            bitmap = pdf_page.render(scale=min(2, 1800 / max(pdf_page.get_size())))
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            pdf_page.close()
            return Response(buffer.getvalue(), media_type="image/png")
        finally:
            document.close()

    @app.post("/v1/memories", operation_id="create_memory", response_model=RecordResult)
    def create_memory(request: CreateRecord) -> dict:
        return engine.add_record(request.record, request.command_id)

    @app.get("/v1/memories", operation_id="list_memories")
    def list_memories(
        project: str = "personal",
        persona: str = "default",
        collection: str = "default",
        world: str = "real",
        kind: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        group: Literal["diary", "timeline"] | None = None,
        query: str | None = Query(None, max_length=4000),
    ) -> dict:
        return engine.list_records(
            Scope(project=project, persona=persona, collection=collection, world=world),
            kind,
            status,
            cursor,
            limit,
            group,
            query,
        )

    @app.post("/v1/recall", operation_id="recall", response_model=RecallResult)
    def recall(request: RecallRequest) -> dict:
        return engine.recall(request)

    @app.get(
        "/v1/memories/{record_id}",
        operation_id="read_memory",
        response_model=RecordResult,
    )
    def read_memory(
        record_id: str,
        at: str | None = None,
        known_at: str | None = None,
        offset: int = Query(0, ge=0),
        length: int = Query(12000, ge=1, le=32000),
        budget: int = Query(4000, ge=1, le=32000),
        session: str | None = None,
    ) -> dict:
        from .models import utc
        from .reading import read_segment

        return read_segment(
            engine,
            record_id,
            at=utc(at) if at else None,
            known_at=utc(known_at) if known_at else None,
            offset=offset,
            length=length,
            budget=budget,
            session=session,
        )

    @app.get("/v1/memories/{record_id}/revisions", operation_id="read_revisions")
    def read_revisions(
        record_id: str, cursor: int = 2147483647, limit: int = Query(20, ge=1, le=100)
    ) -> dict:
        rows = engine.history(record_id, cursor, limit + 1)
        for row in rows:
            content = row["data"]["content"]
            row["data"].update(content=content[:4000], content_length=len(content))
        return {
            "items": rows[:limit],
            "cursor": str(rows[limit - 1]["revision"]) if len(rows) > limit else None,
        }

    @app.post(
        "/v1/memories/{record_id}/revisions",
        operation_id="revise_memory",
        response_model=RecordResult,
    )
    def revise_memory(record_id: str, request: RevisionInput) -> dict:
        return engine.revise(record_id, request)

    @app.delete("/v1/objects/{object_id}", operation_id="delete_object")
    def delete_object(object_id: str) -> dict:
        return engine.delete(object_id)

    @app.get("/v1/objects/{object_id}/deletion", operation_id="preview_deletion")
    def preview_deletion(object_id: str) -> dict:
        from .maintenance import deletion_preview

        return deletion_preview(engine, object_id)

    @app.post("/v1/relations", operation_id="create_relation")
    def create_relation(request: RelationRequest) -> dict:
        return engine.relate(
            request.subject, request.predicate, request.object, request.attributes
        )

    @app.post("/v1/feedback", operation_id="record_feedback")
    def record_feedback(request: FeedbackRequest) -> dict:
        return engine.feedback(
            request.record_id,
            request.type,
            request.session,
            request.attributes,
            request.key,
        )

    @app.post("/v1/sessions/boundary", operation_id="session_boundary")
    def session_boundary(request: SessionBoundary) -> dict:
        return boundary(engine, request)

    @app.post("/v1/host/events", operation_id="host_event")
    def host_event(request: HostEvent) -> dict:
        from .hosts import handle

        return handle(engine, request.event, request.payload)

    @app.post("/v1/maintenance", operation_id="run_maintenance")
    def run_maintenance(request: MaintenanceRequest) -> dict:
        jid = engine.enqueue(
            request.kind,
            request.model_dump(exclude={"kind", "command_id"}),
            request.command_id,
        )
        return {"id": jid, "status": "pending"}

    @app.post("/v1/maintenance/downloads/{kind}", operation_id="create_download")
    def create_download(kind: Literal["backup", "export"]) -> dict:
        from .maintenance import make_download

        return make_download(engine, kind)

    @app.get("/v1/maintenance/files/{name}", operation_id="download_export")
    def download_export(name: str):
        from .maintenance import download_path

        return FileResponse(
            download_path(engine, name),
            filename=name,
            media_type="application/octet-stream",
        )

    @app.post("/v1/maintenance/restore", operation_id="restore_backup")
    async def restore_backup(file: UploadFile = File()) -> dict:
        import tempfile

        from .transfer import restore

        staging = engine.db.root / "restores"
        staging.mkdir(exist_ok=True, mode=0o700)
        target = staging / uid("restore")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "restore.tar.gz"
            with path.open("wb") as output:
                total = 0
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 16 * 1024**3:
                        raise HTTPException(413, "Backup exceeds 16 GiB")
                    output.write(chunk)
            return await asyncio.to_thread(restore, path, target)

    @app.get("/v1/scopes", operation_id="list_scopes")
    def list_scopes() -> dict:
        with engine.db.connect() as conn:
            return {
                "items": [
                    json.loads(r[0])
                    for r in conn.execute(
                        "SELECT DISTINCT scope FROM records WHERE deleted=0 LIMIT 200"
                    )
                ],
                "cursor": None,
            }

    @app.get("/v1/jobs", operation_id="list_jobs")
    def list_jobs(
        state: str | None = None, cursor: str = "", limit: int = Query(50, ge=1, le=200)
    ) -> dict:
        with engine.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE id>?"
                + (" AND state=?" if state else "")
                + " ORDER BY id LIMIT ?",
                [cursor] + ([state] if state else []) + [limit + 1],
            ).fetchall()
            return {
                "items": [
                    dict(r) | {"payload": json.loads(r["payload"])}
                    for r in rows[:limit]
                ],
                "cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    @app.post("/v1/jobs/{job_id}/{action}", operation_id="control_job")
    def control_job(job_id: str, action: Literal["cancel", "retry"]) -> dict:
        return worker.control(job_id, action)

    @app.get("/v1/families", operation_id="list_families")
    def list_families(
        project: str = "personal",
        persona: str = "default",
        collection: str = "default",
        world: str = "real",
        cursor: str = "",
        limit: int = Query(50, ge=1, le=100),
    ) -> dict:
        rows = Organizer(engine).list(
            Scope(project=project, persona=persona, collection=collection, world=world),
            limit + 1,
            cursor,
        )
        for row in rows:
            row["member_count"] = len(row["members"])
            row["members"] = row["members"][:30]
            row["positions"] = {}
        return {
            "items": rows[:limit],
            "cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @app.get("/v1/families/{family_id}", operation_id="read_family")
    def read_family(
        family_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
    ) -> dict:
        with engine.db.connect() as conn:
            row = conn.execute(
                "SELECT data FROM families WHERE id=?", (family_id,)
            ).fetchone()
            if not row:
                raise Missing(family_id)
            data = json.loads(row[0])
            members = data["members"]
            data.update(
                members=members[offset : offset + limit],
                member_count=len(members),
                cursor=str(offset + limit) if offset + limit < len(members) else None,
                positions={},
            )
            return data

    @app.post("/v1/families", operation_id="create_family")
    def create_family(request: FamilyCreate) -> dict:
        return Organizer(engine).create(
            request.scope, request.title, request.members, request.kind
        )

    @app.post("/v1/families/{family_id}", operation_id="change_family")
    def change_family(family_id: str, request: FamilyChange) -> dict:
        return Organizer(engine).change(
            family_id,
            request.expected_revision,
            request.action,
            **request.model_dump(exclude={"expected_revision", "action"}),
        )

    @app.get("/v1/graph", operation_id="read_graph")
    def read_graph(
        project: str = "personal",
        persona: str = "default",
        collection: str = "default",
        world: str = "real",
        family_id: str | None = None,
        limit: int = Query(150, ge=1, le=300),
        focus: str | None = None,
        query: str = "",
        since: str | None = None,
        until: str | None = None,
        layer: Literal["evidence", "association"] | None = None,
        kind: str | None = None,
        cursor: int = Query(0, ge=0),
        hops: int = Query(1, ge=0, le=3),
    ) -> dict:
        from kin_mind.memory import MemoryContinuity
        from kin_mind.state import Mind
        scope = Scope(project=project, persona=persona, collection=collection, world=world)
        memory = MemoryContinuity(Mind(engine, scope))
        if not family_id and memory.settings()["graph"]:
            result = memory.graph.read(focus=focus, query=query, since=since, until=until, layer=layer, kind=kind, cursor=cursor, limit=limit, hops=hops)
            if memory.settings()["sharing"]:
                result["nodes"] = [memory.sharing.decorate(n) for n in result["nodes"]]
            return result
        return Organizer(engine).graph(
            scope,
            family_id,
            limit,
        )

    @app.get("/v1/graph/object/{identifier}", operation_id="read_graph_object")
    def read_graph_object(identifier: str, project: str = "personal", persona: str = "default", collection: str = "default", world: str = "real") -> dict:
        from kin_mind.memory import MemoryContinuity
        from kin_mind.state import Mind
        memory = MemoryContinuity(Mind(engine, Scope(project=project, persona=persona, collection=collection, world=world)))
        return memory.sharing.decorate(memory.graph.detail(identifier))

    @app.get("/v1/graph/thread/{identifier}", operation_id="read_event_thread")
    def read_event_thread(identifier: str, project: str = "personal", persona: str = "default", collection: str = "default", world: str = "real", query: str = "", cursor: int = Query(0, ge=0), budget: int = Query(2000, ge=128, le=32000), detail: Literal["index", "summary", "original"] = "summary", expected_revision: int | None = None) -> dict:
        from kin_mind.context import Contexts
        from kin_mind.state import Mind
        return Contexts(Mind(engine, Scope(project=project, persona=persona, collection=collection, world=world))).event_thread(identifier, query=query, cursor=cursor, budget=budget, detail=detail, expected_revision=expected_revision)

    @app.post("/v1/graph/revisions", operation_id="revise_graph")
    def revise_graph(request: GraphCommand) -> dict:
        from kin_mind.graph import EventGraph
        from kin_mind.state import Mind
        return EventGraph(Mind(engine, request.scope)).revise(request.request)

    @app.post("/v1/graph/reply-references", operation_id="register_reply_references")
    def register_reply_references(request: GraphCommand) -> dict:
        from kin_mind.sharing import ShareLedger
        from kin_mind.state import Mind
        return ShareLedger(Mind(engine, request.scope)).register(request.request)

    @app.get("/v1/conversation/habits", operation_id="read_conversation_habits")
    def read_conversation_habits(project: str = "personal", persona: str = "default", collection: str = "default", world: str = "real") -> dict:
        from kin_mind.habits import ConversationHabits
        from kin_mind.state import Mind
        return ConversationHabits(Mind(engine, Scope(project=project,persona=persona,collection=collection,world=world))).read()

    @app.post("/v1/conversation/habits", operation_id="update_conversation_habits")
    def update_conversation_habits(request: GraphCommand) -> dict:
        from kin_mind.habits import ConversationHabits
        from kin_mind.state import Mind
        return ConversationHabits(Mind(engine, request.scope)).update(request.request)

    @app.post("/v1/conversation/reply-choice", operation_id="choose_reply")
    def choose_reply(request: GraphCommand) -> dict:
        from kin_mind.habits import ConversationHabits
        from kin_mind.state import Mind
        return ConversationHabits(Mind(engine, request.scope)).choose_reply(request.request)

    @app.get("/v1/autonomy/plans", operation_id="read_autonomous_plans")
    def read_autonomous_plans(project: str = "personal", persona: str = "default", collection: str = "default", world: str = "real", identifier: str | None = None, status: str | None = None, cursor: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=100), history: bool = False) -> dict:
        from kin_mind.plans import AutonomousPlans
        from kin_mind.state import Mind
        return AutonomousPlans(Mind(engine, Scope(project=project, persona=persona, collection=collection, world=world))).read(identifier, status=status, cursor=cursor, limit=limit, history=history)

    @app.post("/v1/autonomy/plans", operation_id="manage_autonomous_plan")
    def manage_autonomous_plan(request: GraphCommand) -> dict:
        from kin_mind.plans import AutonomousPlans
        from kin_mind.state import Mind
        return AutonomousPlans(Mind(engine, request.scope)).manage(request.request)

    @app.get("/v1/autonomy/procedures", operation_id="read_procedure_memory")
    def read_procedure_memory(project: str = "personal", persona: str = "default", collection: str = "default", world: str = "real", query: str = "", identifier: str | None = None, limit: int = Query(12, ge=1, le=100)) -> dict:
        from kin_mind.procedures import Procedures
        from kin_mind.state import Mind
        return Procedures(Mind(engine, Scope(project=project, persona=persona, collection=collection, world=world))).read(query, identifier, limit=limit)

    @app.put("/v1/contact/policies", operation_id="configure_contact")
    def configure_contact(request: ContactPolicy) -> dict:
        return Scheduler(engine).policy(request)

    @app.get("/v1/contact/{table}", operation_id="list_contact")
    def list_contact(
        table: Literal["policies", "schedules", "outbox"],
        cursor: str = "",
        limit: int = Query(50, ge=1, le=200),
    ) -> dict:
        with engine.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE id>? ORDER BY id LIMIT ?",
                (cursor, limit + 1),
            ).fetchall()
            return {
                "items": [
                    dict(r) | {"data": json.loads(r["data"])} for r in rows[:limit]
                ],
                "cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    @app.post("/v1/contact/schedules", operation_id="create_schedule")
    def create_schedule(request: ScheduleInput) -> dict:
        return Scheduler(engine).schedule(request)

    @app.post("/v1/contact/schedules/{schedule_id}", operation_id="change_schedule")
    def change_schedule(schedule_id: str, request: ScheduleChange) -> dict:
        return Scheduler(engine).control(
            schedule_id, request.action, request.expected_revision, request.due_at
        )

    @app.post(
        "/v1/contact/outbox/{delivery_id}/ack", operation_id="acknowledge_delivery"
    )
    def acknowledge_delivery(delivery_id: str) -> dict:
        return Scheduler(engine).acknowledge(delivery_id)

    @app.post("/v1/contact/tick", operation_id="contact_tick")
    def contact_tick(deliver: bool = False) -> dict:
        return Scheduler(engine).tick(deliver=deliver)

    @app.get("/v1/overview", operation_id="overview")
    def overview() -> dict:
        return engine.overview()

    @app.get("/v1/settings/{key}", operation_id="read_settings")
    def read_settings(
        key: Literal[
            "models", "budgets", "scenarios", "connections", "maintenance", "parsers"
        ],
    ) -> dict:
        return engine.settings(key)

    @app.put("/v1/settings/models", operation_id="configure_models")
    def configure_models(request: dict[str, ModelRole]) -> dict:
        allowed = {
            "extraction",
            "conflict",
            "summary",
            "rerank",
            "embedding",
            "vision",
            "visual_embedding",
            "asr",
            "prediction",
            "query",
            "answer",
            "judge",
        }
        if set(request) - allowed:
            raise ValueError("Unknown model role")
        return engine.settings(
            "models", {k: v.model_dump() for k, v in request.items()}
        )

    @app.put("/v1/settings/{key}", operation_id="configure_settings")
    def configure_settings(
        key: Literal["budgets", "scenarios", "connections", "maintenance", "parsers"],
        value: dict = Body(),
    ) -> dict:
        if key == "budgets":
            for scenario, limits in value.items():
                if not isinstance(limits, dict) or any(
                    k not in {"startup", "passive", "cumulative"}
                    or not isinstance(v, int)
                    or not 0 <= v <= 128000
                    for k, v in limits.items()
                ):
                    raise ValueError("Invalid token budget")
        return engine.settings(key, value)

    if mcp_server:
        app.mount("/mcp", mcp_app)
    static = Path(__file__).parents[1] / "web"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="console")
    else:

        @app.get("/", include_in_schema=False)
        def console_unbuilt():
            return Response(
                "MemoryPalace console assets are not built. Run npm ci && npm run build in console/.",
                media_type="text/plain",
            )

    return app

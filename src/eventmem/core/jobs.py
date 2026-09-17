from __future__ import annotations

import json
import threading
import time
import uuid

from .db import Conflict, Deleted, Missing, digest
from .envelopes import current_message
from .models import RecordInput, Scope, now
from .providers import NotConfigured, ProviderError, Providers


class Worker:
    def __init__(self, engine, lease_seconds=90):
        self.engine = engine
        self.owner = uuid.uuid4().hex
        self.lease_seconds = lease_seconds
        self.stopped = threading.Event()
        self.last_maintenance = float("-inf")

    def claim(self):
        if self.engine.interactive_until > time.monotonic():
            return None
        with self.engine.db.connect(write=True) as conn:
            foreground = bool(conn.execute("SELECT 1 FROM mind_foreground_leases WHERE expires_at>? LIMIT 1", (time.time(),)).fetchone())
            expired = conn.execute("SELECT * FROM jobs WHERE state='running' AND lease_until<? AND attempts>=max_attempts",
                                   (time.time(),)).fetchall()
            conn.execute(
                "UPDATE jobs SET state='failed',error='Lease expired after maximum attempts' WHERE state='running' AND lease_until<? AND attempts>=max_attempts",
                (time.time(),),
            )
            for lost in expired:
                if lost["kind"] == "event_digest" or lost["kind"].startswith("lifecycle_"):
                    from kin_mind.lifecycle import job_failed
                    job_failed(conn, lost, "failed", "Lease expired after maximum attempts")
            conn.execute(
                "UPDATE jobs SET state='failed',error='Dependency failed or canceled' WHERE state='pending' AND EXISTS(SELECT 1 FROM job_dependencies d JOIN jobs parent ON parent.id=d.dependency_id WHERE d.job_id=jobs.id AND parent.state IN ('failed','canceled'))"
            )
            row = conn.execute(
                "SELECT * FROM jobs j WHERE ((state IN ('pending','retry') AND available<=?) OR (state='running' AND lease_until<?)) AND "
                "(kind!='event_digest' OR ((SELECT COUNT(*) FROM jobs busy WHERE busy.kind='event_digest' AND busy.state='running' AND busy.lease_until>strftime('%s','now'))<2 AND NOT EXISTS(SELECT 1 FROM jobs busy WHERE busy.kind='event_digest' AND busy.state='running' AND busy.lease_until>strftime('%s','now') AND json_extract(busy.payload,'$.event_id')=json_extract(j.payload,'$.event_id') AND json_extract(busy.payload,'$.scope')=json_extract(j.payload,'$.scope')))) AND "
                "(?=0 OR priority<=30) AND "
                "NOT EXISTS(SELECT 1 FROM job_dependencies d LEFT JOIN jobs parent ON parent.id=d.dependency_id WHERE d.job_id=j.id AND (parent.state IS NULL OR parent.state!='complete')) ORDER BY priority,available,id LIMIT 1",
                (time.time(), time.time(), int(foreground)),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET state='running',owner=?,lease_until=?,fence=fence+1,attempts=attempts+1,updated_at=? WHERE id=?",
                (self.owner, time.time() + self.lease_seconds, now(), row["id"]),
            )
            return dict(
                conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def owns(self, conn, job):
        row = conn.execute(
            "SELECT state,owner,fence,lease_until FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
        return (
            row
            and row["state"] == "running"
            and row["owner"] == self.owner
            and row["fence"] == job["fence"]
            and row["lease_until"] > time.time()
        )

    def renew(self, job, done):
        while not done.wait(max(0.05, self.lease_seconds / 3)):
            with self.engine.db.connect(write=True) as conn:
                if not self.owns(conn, job):
                    return
                conn.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=?",
                    (time.time() + self.lease_seconds, job["id"]),
                )

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        done = threading.Event()
        heartbeat = threading.Thread(target=self.renew, args=(job, done), daemon=True)
        heartbeat.start()
        try:
            from kin_mind.model_runtime import background_calls
            with background_calls():
                apply = self.prepare(job)
            with self.engine.db.connect(write=True) as conn:
                if not self.owns(conn, job):
                    return True
                apply(conn)
                conn.execute(
                    "UPDATE jobs SET state='complete',lease_until=NULL,error=NULL,updated_at=? WHERE id=?",
                    (now(), job["id"]),
                )
                self.engine.db.bump(conn)
        except Exception as exc:
            with self.engine.db.connect(write=True) as conn:
                if self.owns(conn, job):
                    capacity_wait = str(exc) in {"deepseek-background-capacity", "deepseek-foreground-priority"}
                    state = (
                        "retry" if capacity_wait else
                        "waiting_config"
                        if isinstance(exc, (NotConfigured, ImportError))
                        else "canceled"
                        if isinstance(exc, (Deleted, Missing))
                        else "failed"
                        if job["attempts"] >= job["max_attempts"]
                        else "retry"
                    )
                    if capacity_wait:
                        conn.execute("UPDATE jobs SET attempts=MAX(0,attempts-1) WHERE id=?", (job["id"],))
                    error = (
                        str(exc)
                        if isinstance(
                            exc, (NotConfigured, ValueError, Conflict, ProviderError)
                        )
                        else type(exc).__name__
                    )
                    conn.execute(
                        "UPDATE jobs SET state=?,error=?,available=?,lease_until=NULL,updated_at=? WHERE id=?",
                        (
                            state,
                            error[:500],
                            time.time() + min(300, 2 ** job["attempts"]),
                            now(),
                            job["id"],
                        ),
                    )
                    if job["kind"] == "event_digest" or job["kind"].startswith("lifecycle_"):
                        from kin_mind.lifecycle import job_failed
                        job_failed(conn, job, state, error)
        finally:
            done.set()
            heartbeat.join(timeout=1)
        return True

    def prepare(self, job):
        engine, kind = self.engine, job["kind"]
        payload = json.loads(job["payload"])
        if kind == "procedure_replay":
            from kin_mind.procedures import prepare_replay
            return prepare_replay(engine, payload)
        if kind == "event_digest" or kind.startswith("lifecycle_"):
            from kin_mind.lifecycle import prepare_job
            return prepare_job(engine, job, payload)
        if kind == "parse":
            from .media import parse

            records = parse(engine, payload["source_id"])

            def apply(conn):
                analysis_jobs = []
                for record in records:
                    engine._insert(conn, record)
                    if record.locator.get("analysis_role"):
                        analysis_jobs.append(
                            engine.enqueue(
                                "analyze_media",
                                {"record_id": record.id},
                                f"analyze:{record.id}",
                                conn=conn,
                            )
                        )
                sid = payload["source_id"]
                if analysis_jobs:
                    conn.execute(
                        "UPDATE sources SET model='pending' WHERE id=?", (sid,)
                    )
                    finish = engine.enqueue(
                        "media_complete",
                        {"source_id": sid},
                        f"media-complete:{sid}",
                        analysis_jobs,
                        conn=conn,
                    )
                    extraction = "job_" + digest(f"extract:{sid}")[:32]
                    if conn.execute(
                        "SELECT 1 FROM jobs WHERE id=?", (extraction,)
                    ).fetchone():
                        conn.execute(
                            "INSERT OR IGNORE INTO job_dependencies VALUES(?,?)",
                            (extraction, finish),
                        )
                engine.supersede_source_versions(conn, sid)
                conn.execute(
                    "UPDATE sources SET mechanical='complete' WHERE id=?", (sid,)
                )
                source = engine._source(
                    conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
                )
                if (
                    not source.get("extract")
                    and not analysis_jobs
                    and source["media_type"].startswith(("image/", "audio/", "video/"))
                ):
                    conn.execute(
                        "UPDATE sources SET model='complete' WHERE id=?", (sid,)
                    )

            return apply
        if kind == "media_complete":
            source = engine.source(payload["source_id"])
            return (
                (lambda conn: None)
                if source.get("extract")
                else (
                    lambda conn: conn.execute(
                        "UPDATE sources SET model='complete' WHERE id=?",
                        (source["id"],),
                    )
                )
            )
        if kind == "analyze_media":
            record = engine.get(payload["record_id"])
            if not record["attributes"].get("analysis_pending"):
                return lambda conn: None
            locator = record["locator"]
            path = (
                engine.db.blobs / locator["blob"]
                if locator.get("blob")
                else engine.source(locator["source_id"], content=True)
            )
            provider = Providers(engine)
            if locator["analysis_role"] == "asr":
                response = provider.transcribe(path)
                segments = response.get("segments") or [
                    {"start": 0, "end": 300, "text": response.get("text", "")}
                ]
                content = "\n".join(s["text"] for s in segments).strip()
            else:
                response = provider.json(
                    "vision",
                    'Describe visible content and transcribe visible text, preserving tables. Return {"description":"...","ocr":"..."}.',
                    {"locator": locator},
                    image=(locator.get("mime", "image/png"), path.read_bytes()),
                )
                content = (
                    response.get("description", "") + "\n" + response.get("ocr", "")
                ).strip()
                segments = []
            if not content:
                content = "No recognizable text or description returned"

            def apply(conn):
                current = engine._get(conn, record["id"])
                if current["revision"] != record["revision"]:
                    raise Conflict("Attachment annotation changed during analysis")
                current.update(content=content, generated=True, confirmation="inferred")
                current["attributes"]["analysis_pending"] = False
                if segments:
                    current["locator"]["segments"] = [
                        {
                            "start_seconds": locator["start_seconds"]
                            + float(s.get("start", 0)),
                            "end_seconds": locator["start_seconds"]
                            + float(s.get("end", 0)),
                            "text": s["text"],
                        }
                        for s in segments
                    ]
                engine._save_revision(
                    conn, current, "media_analysis", "Configured model analysis"
                )

            return apply
        if kind == "extract_complete":
            engine.source(payload["source_id"])
            return lambda conn: conn.execute(
                "UPDATE sources SET model='complete' WHERE id=?",
                (payload["source_id"],),
            )
        if kind in {"extract", "extract_part"}:
            sid = payload["source_id"]
            source = engine.source(sid)
            if source["mechanical"] != "complete":
                raise Conflict("Mechanical parsing has not completed")
            channel_body = None
            if (
                source["namespace"].startswith("host:")
                and source.get("metadata", {}).get("host_event") == "message"
                and source.get("metadata", {}).get("role") == "user"
            ):
                raw = engine.source(sid, content=True).read_text(encoding="utf-8")
                body = current_message(raw)
                if body != raw:
                    channel_body = body
            if kind == "extract_part":
                text = (
                    current_message(payload["text"])
                    if channel_body is not None
                    else payload["text"]
                )
            else:
                with engine.db.connect() as conn:
                    rows = conn.execute(
                        "SELECT r.data FROM records r JOIN evidence e ON e.record_id=r.id WHERE e.source_id=? AND r.deleted=0 AND r.status IN ('active','unverified') AND (json_extract(r.data,'$.generated')=0 OR (json_extract(r.data,'$.locator.source_id')=? AND json_extract(r.data,'$.locator.analysis_role') IN ('asr','vision'))) ORDER BY r.id",
                        (sid, sid),
                    )
                    batches, current = [], ""
                    for row in rows:
                        content = json.loads(row[0])["content"]
                        for offset in range(0, len(content), 47000):
                            piece = content[offset : offset + 48000]
                            if current and len(current) + len(piece) + 1 > 48000:
                                batches.append(current)
                                current = ""
                            current += ("\n" if current else "") + piece
                    if current:
                        batches.append(current)
                if len(batches) > 1:

                    def schedule_parts(conn):
                        dependencies = []
                        for i, text in enumerate(batches):
                            dependencies.append(
                                engine.enqueue(
                                    "extract_part",
                                    {"source_id": sid, "text": text, "part": i},
                                    f"extract-part:{sid}:{i}",
                                    conn=conn,
                                )
                            )
                        engine.enqueue(
                            "extract_complete",
                            {"source_id": sid},
                            f"extract-complete:{sid}",
                            dependencies,
                            conn=conn,
                        )

                    return schedule_parts
                text = batches[0] if batches else ""
            result = Providers(engine).json(
                "extraction",
                'Extract facts, preferences, relationships, commitments, procedures and episodes. Return {"candidates":[{"kind":"fact","content":"...","quote":"exact source excerpt","title":"...","attributes":{}}]}. Only include conclusions supported by an exact quote. Inferences remain unverified.',
                {"source_id": sid, "text": text, "scope": source["scope"]},
            )
            proposals = []
            for i, candidate in enumerate(result.get("candidates", [])[:100]):
                quote = candidate.get("quote", "")
                if (
                    not quote
                    or quote not in text
                    or (channel_body is not None and quote not in channel_body)
                ):
                    continue
                proposals.append(
                    RecordInput(
                        id="mem_"
                        + digest([sid, "extracted", payload.get("part", 0), i])[:32],
                        kind=candidate["kind"],
                        content=candidate["content"],
                        title=candidate.get("title", ""),
                        source_ids=[sid],
                        scope=Scope(**source["scope"]),
                        valid_from=source["occurred_at"],
                        generated=True,
                        confirmation="inferred",
                        attributes=candidate.get("attributes", {}),
                        locator={"quote": quote, "source_id": sid},
                    )
                )

            def apply(conn):
                for record in proposals:
                    created = engine._insert(conn, record)
                    engine.enqueue(
                        "conflict",
                        {"record_id": created["id"]},
                        f"conflict:{created['id']}",
                        conn=conn,
                    )
                if kind == "extract":
                    conn.execute(
                        "UPDATE sources SET model='complete' WHERE id=?", (sid,)
                    )

            return apply
        if kind == "conflict":
            data = engine.get(payload["record_id"])
            from .models import RecallRequest

            # Model proposals can describe conflicts; only a typed user/domain
            # command can replace an active authoritative assertion.
            hits = engine.recall(
                RecallRequest(
                    query=data["content"][:2000],
                    scope=Scope(**data["scope"]),
                    history=True,
                    limit=10,
                )
            )
            result = Providers(engine).json(
                "conflict",
                'Return {"relations":[{"id":"existing id","relation":"coexists|refutes|supports","reason":"..."}]}. Consider time, scope, version and independent evidence.',
                {"candidate": data, "existing": hits["items"], "scope": data["scope"]},
            )

            def apply(conn):
                for relation in result.get("relations", [])[:20]:
                    if (
                        relation.get("id") in {r["id"] for r in hits["items"]}
                        and relation["id"] != data["id"]
                        and relation.get("relation")
                        in {"coexists", "refutes", "supports"}
                    ):
                        engine._relation(
                            conn,
                            data["id"],
                            relation["relation"],
                            relation["id"],
                            {
                                "generated": True,
                                "confirmed": False,
                                "reason": relation.get("reason", ""),
                            },
                        )

            return apply
        if kind in {"embed", "visual_embed"}:
            record = engine.get(payload["record_id"])
            if record["revision"] != payload["revision"]:
                return lambda conn: None
            if kind == "visual_embed":
                locator = record["locator"]
                raw = (
                    (engine.db.blobs / locator["blob"]).read_bytes()
                    if locator.get("blob")
                    else engine.source(locator["source_id"], content=True).read_bytes()
                )
                vector, index_id = Providers(engine).visual_embed(image=raw)
                vectors = [vector]
            else:
                vectors, index_id = Providers(engine).embed(
                    [record["title"] + "\n" + record["content"]]
                )
            from .vectors import VectorIndex

            def apply(conn):
                current = engine._get(conn, record["id"])
                if current["revision"] == record["revision"]:
                    VectorIndex(engine, index_id).upsert(
                        [
                            {
                                "id": record["id"],
                                "scope": Scope(**record["scope"]).key(),
                                "revision": record["revision"],
                                "vector": vectors[0],
                            }
                        ]
                    )

            return apply
        if kind in ("diary", "summary", "portrait", "self_narrative", "prediction"):
            scope = Scope(**payload["scope"])
            with engine.db.connect() as conn:
                rows = conn.execute(
                    "SELECT data FROM records WHERE scope=? AND deleted=0 AND status='active' AND updated_at>=? AND json_extract(data,'$.generated')=0 ORDER BY updated_at DESC LIMIT 100",
                    (scope.key(), payload.get("since", "")),
                ).fetchall()
            records = [
                json.loads(r[0]) for r in rows if not json.loads(r[0])["generated"]
            ]
            from .read_policy import ReadPolicy

            # A narrative tells what happened. A role agreement, its examples, a self-claim or
            # a host envelope did not happen, so none of them is handed to the narrator.
            policy = ReadPolicy.load(engine, scope, "experience_recall")
            if policy.enabled:
                records = [r for r in records if policy.visible(r)]
            result = Providers(engine).json(
                "summary" if kind != "prediction" else "prediction",
                'Return {"content":"...","evidence_ids":[id,...]}. Write only supported observations, preserving uncertainty. Predictions must be explicitly tentative. Do not invent feelings or user commitments.',
                {"kind": kind, "records": records, "scope": scope.model_dump()},
            )
            evidence = list(
                dict.fromkeys(
                    r
                    for r in result.get("evidence_ids", [])
                    if r in {d["id"] for d in records}
                )
            )
            if not evidence:
                raise ValueError("Narrative has no valid evidence")
            source_ids = sorted(
                {sid for r in records if r["id"] in evidence for sid in r["source_ids"]}
            )
            record = RecordInput(
                id="mem_" + digest(job["id"])[:32],
                kind=kind,
                title=payload.get("title", kind),
                content=result["content"],
                scope=scope,
                source_ids=source_ids,
                evidence_ids=evidence,
                generated=True,
                confirmation="inferred",
                attributes={"generated_at": now(), "model_role": "summary"},
            )
            return lambda conn: engine._insert(conn, record)
        if kind == "prefetch":
            from .db import tokenize
            from .models import RecallRequest

            request = RecallRequest(
                query=str(payload["cue"])[:4000],
                scope=Scope(**payload["scope"]),
                budget=2000,
            )
            result = engine.recall(request)

            def apply(conn):
                conn.execute(
                    "DELETE FROM prefetch WHERE scope=?", (request.scope.key(),)
                )
                for cue in list(dict.fromkeys(tokenize(request.query).split()))[:30]:
                    for record in result["items"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO prefetch VALUES(?,?,?,?)",
                            (
                                request.scope.key(),
                                cue,
                                record["id"],
                                record["revision"],
                            ),
                        )

            return apply
        if kind == "organize":
            from .organize import prepare_communities

            return prepare_communities(engine, Scope(**payload["scope"]))
        if kind == "rebuild":

            def apply(conn):
                conn.execute("DELETE FROM search")
                for row in conn.execute(
                    "SELECT data FROM records WHERE deleted=0"
                ).fetchall():
                    data = json.loads(row[0])
                    engine._index_text(conn, data)
                    engine._dirty(conn, data)

            return apply
        if kind in ("build_vectors", "purge_vectors"):
            from .vectors import VectorIndex

            with engine.db.connect() as conn:
                indexes = [r[0] for r in conn.execute("SELECT id FROM vector_indexes")]
            # Derived work is safely repeatable after lease loss; canonical reads
            # still reject deleted or superseded records.
            for index_id in indexes:
                index = VectorIndex(engine, index_id)
                index.build() if kind == "build_vectors" else index.purge()
            return lambda conn: None
        raise ValueError(f"Unknown job kind: {kind}")

    def run(self):
        from .scheduler import Scheduler

        scheduler = Scheduler(self.engine)
        while not self.stopped.is_set():
            self.replay_hosts()
            self.schedule_maintenance()
            scheduler.tick()
            if not self.run_once():
                self.stopped.wait(0.5)

    def schedule_maintenance(self):
        config = self.engine.settings("maintenance")
        interval = max(30, min(86400, config.get("interval_seconds", 60)))
        if (
            time.monotonic() - self.last_maintenance < interval
            or self.engine.interactive_until > time.monotonic()
        ):
            return
        self.last_maintenance = time.monotonic()
        from datetime import datetime, timedelta, timezone

        with self.engine.db.connect(write=True) as conn:
            from kin_mind.lifecycle import schedule
            schedule(self.engine, conn, now())
            scopes = conn.execute(
                "SELECT r.scope,COUNT(*),MAX(d.revision) FROM dirty d JOIN records r ON r.id=d.record_id WHERE r.deleted=0 AND r.status='active' AND d.revision=r.revision GROUP BY r.scope LIMIT 30"
            ).fetchall()
            for scope, count, _ in scopes:
                if count >= max(2, config.get("organize_batch", 20)):
                    active = conn.execute(
                        "SELECT 1 FROM jobs WHERE kind='organize' AND state IN ('pending','running','retry','waiting_config') AND json_extract(payload,'$.scope')=json(?) LIMIT 1",
                        (scope,),
                    ).fetchone()
                    if not active:
                        versions = conn.execute("SELECT d.record_id,d.revision FROM dirty d JOIN records r ON r.id=d.record_id WHERE r.scope=? AND r.deleted=0 AND r.status='active' AND d.revision=r.revision ORDER BY d.record_id", (scope,)).fetchall()
                        self.engine.enqueue(
                            "organize",
                            {"scope": json.loads(scope)},
                            f"auto-organize:{digest(scope)}:{digest([tuple(v) for v in versions])}",
                            conn=conn,
                        )
                if config.get("narratives", False):
                    date = datetime.now(timezone.utc).date().isoformat()
                    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                    self.engine.enqueue(
                        "diary",
                        {
                            "scope": json.loads(scope),
                            "since": since[:10] + "T00:00:00+00:00",
                            "title": date,
                        },
                        f"auto-diary:{digest(scope)}:{date}",
                        conn=conn,
                    )

    def replay_hosts(self):
        from .hosts import handle

        spool = self.engine.db.root / "host-spool"
        if not spool.exists():
            return
        for path in sorted(spool.glob("*.json"))[:20]:
            try:
                data = json.loads(path.read_text())
                # A replay cannot inject into a past host context; only receipt
                # events are replayed. Startup/compact are fetched again live.
                if data["event"] in {"tool", "message", "boundary", "end", "compact"}:
                    handle(
                        self.engine, data["event"], data["payload"], receipt_only=True
                    )
                path.unlink(missing_ok=True)
            except Deleted:
                path.unlink(missing_ok=True)
            except Exception:
                continue

    def control(self, jid, action):
        if action not in {"cancel", "retry"}:
            raise ValueError("Unknown job action")
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise Missing(jid)
            conn.execute(
                "UPDATE jobs SET state=?,available=?,owner=NULL,lease_until=NULL,fence=fence+1,error=NULL,attempts=0 WHERE id=?",
                ("canceled" if action == "cancel" else "pending", time.time(), jid),
            )
        return {"id": jid, "status": "canceled" if action == "cancel" else "pending"}

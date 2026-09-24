from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid

from .db import Conflict, Deleted, Missing, digest, dumps
from .envelopes import current_message
from .models import RecordInput, Scope, now
from .providers import NotConfigured, ProviderError, Providers, drop_answers, job_answers, keep_answers

# The attribute keys an extracted record may carry. Everything else a model writes there
# is dropped: `constraint`, `origin_kind`, `self_knowledge`, `completed` and the like steer
# retrieval, the read policy and reminders.
EXTRACTED_ATTRIBUTES = frozenset({"topic", "tags", "entities", "confidence", "language", "category"})
EXTRACTION_KINDS = ("extract", "extract_part", "extract_complete")


# Errors that indict the environment, not the item: the next item would fail the same way.
# derivative_recovery stops a batch on them; here they never count against a job.
ENVIRONMENTAL_ERRORS = frozenset({
    "FileNotFoundError", "ConnectError", "ReadError", "RemoteProtocolError",
    "TimeoutException", "ConnectTimeout", "ReadTimeout",
})
# Of those, the ones that prove no model was reached. Every other one may have been billed,
# so it is only free for the kinds that call no paid model.
UNSENT_ERRORS = frozenset({"FileNotFoundError", "ConnectError", "ConnectTimeout"})
# Kinds that never call a paid model. They are also the only ones a worker takes while the
# foreground holds its lease, because a paid background call would only be refused then.
MODEL_FREE_KINDS = ("embed", "visual_embed", "parse", "media_complete", "extract_complete",
                    "build_vectors", "purge_vectors", "rebuild", "erase_history", "vector_optimize")
# How long a kind whose environment just failed is left alone, and the longest wait between
# retries of one job.
ENVIRONMENT_PAUSE = 60
RETRY_CAP = 300
# How long the loop backs off after one of its own steps failed.
LOOP_BACKOFF = 5
# A worker neither going round nor renewing a job's lease for this long is not alive; one
# waiting for another process's loop to let go of the store looks again this often.
HEALTH_STALE = 300
STANDBY_SECONDS = 5
# What a model admission wait asks for, as model_lanes.RETRY_SECONDS does.
RETRY_SECONDS = 30
# A host receipt that keeps failing is moved aside after this many tries, and one that can
# never succeed at once. The directory is inside the spool, so nothing leaves the store.
SPOOL_ATTEMPTS = 5
SPOOL_REJECTED = "rejected"

log = logging.getLogger("eventmem.worker")


class Wait(RuntimeError):
    """A job that cannot run yet for a reason outside itself: retried later, never counted."""


def environmental(kind, exc):
    """Whether this failure says nothing about the job, so it costs the job no attempt."""
    name = type(exc).__name__
    return name in UNSENT_ERRORS or (kind in MODEL_FREE_KINDS and (
        name in ENVIRONMENTAL_ERRORS or isinstance(exc, ProviderError)))


# Full-text indexes whose deleted rows have to leave the index itself after an erase.
TEXT_INDEXES = ("search", "mind_graph_search", "mind_memory_search")


def purge_text_indexes(conn):
    """An FTS5 delete only marks a row deleted: its tokens stay in the index's segments until
    those are merged. After an explicit erase they are merged at once, so no deleted word is
    left in the index."""
    for table in TEXT_INDEXES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")


# Records whose text index entry one rebuild step rewrites.
REBUILD_BATCH = 500
# The vector store is compacted once a day, when the store is quiet, at the end of the queue.
VECTOR_OPTIMIZE_PRIORITY = 250


def rebuild_step(engine, job, payload):
    """The text index rebuilt a batch at a time, in the payload's scope when it names one
    (E3-15). The words are segmented before the write lock is taken, one short transaction
    writes them, and the next batch is a job of its own. Only the index is rebuilt: a record
    whose text did not change invalidates nothing derived from it, so no digest, embedding or
    judgment is redone for it (E2-09)."""
    from .db import tokenize

    scope = Scope(**payload["scope"]).key() if payload.get("scope") else None
    after = int(payload.get("after", 0))
    with engine.db.connect() as conn:
        rows = conn.execute(
            "SELECT rowid,id,revision,deleted,data FROM records WHERE rowid>?"
            + (" AND scope=?" if scope else "") + " ORDER BY rowid LIMIT ?",
            [after] + ([scope] if scope else []) + [REBUILD_BATCH],
        ).fetchall()
        entries = []
        for row in rows:
            data = json.loads(row["data"])
            words = (tokenize(data["title"] + " " + data["content"])
                     if not row["deleted"] and engine._indexable(conn, data) else None)
            entries.append((row["rowid"], row["id"], row["revision"], words))
    base = payload.get("base") or job["unique_key"]

    def apply(conn):
        for rowid, rid, revision, words in entries:
            current = conn.execute("SELECT revision,deleted FROM records WHERE rowid=?", (rowid,)).fetchone()
            if current is None or current["revision"] != revision:
                continue  # changed since it was read: its own save indexed it
            conn.execute("DELETE FROM search WHERE rowid=?", (rowid,))
            if words is not None and not current["deleted"]:
                conn.execute("INSERT INTO search(rowid,id,tokens) VALUES(?,?,?)", (rowid, rid, words))
        if len(entries) == REBUILD_BATCH:
            last = entries[-1][0]
            engine.enqueue("rebuild", {**payload, "base": base, "after": last}, f"{base}@{last}",
                           conn=conn, priority=job.get("priority", 100))
        elif not scope:
            # The whole store went through: what is left in the index belongs to no record.
            conn.execute("DELETE FROM search WHERE rowid NOT IN (SELECT rowid FROM records WHERE deleted=0)")

    return apply


class Worker:
    def __init__(self, engine, lease_seconds=90):
        self.engine = engine
        self.owner = uuid.uuid4().hex
        self.lease_seconds = lease_seconds
        self.stopped = threading.Event()
        self.last_maintenance = float("-inf")
        # The heartbeat: when the loop last went round, when a job it is running last renewed
        # its lease, and what its last own failure was. `/v1/health` reads these, so a loop
        # that stopped is visible where the host looks.
        self.last_tick = None
        self.last_beat = None
        self.failures = 0
        self.last_error = None
        # Another process's loop holds this store, so this one waits (S1-11).
        self.standby = False
        self.paused = {}
        # Host receipts that failed for a passing reason: name -> (tries, not before).
        self.spool_failures = {}

    def health(self, *, stale=HEALTH_STALE):
        """The worker as the service answers for it: alive while its loop goes round, or while
        the job it is running keeps renewing its lease; and how long since either."""
        beats = [t for t in (self.last_tick, self.last_beat) if t is not None]
        age = None if not beats else max(0.0, time.time() - max(beats))
        return {"alive": age is not None and age <= stale and not self.stopped.is_set() and not self.standby,
                "standby": self.standby,
                "last_tick_age_seconds": None if age is None else round(age, 1),
                "loop_failures": self.failures, "last_error": self.last_error}

    def claim(self):
        if self.engine.interactive_until > time.monotonic():
            return None
        now_seconds = time.time()
        paused = sorted(kind for kind, until in self.paused.items() if until > now_seconds)
        with self.engine.db.connect(write=True) as conn:
            foreground = bool(conn.execute("SELECT 1 FROM mind_foreground_leases WHERE expires_at>? LIMIT 1", (time.time(),)).fetchone())
            expired = conn.execute("SELECT * FROM jobs WHERE state='running' AND lease_until<? AND attempts>=max_attempts",
                                   (time.time(),)).fetchall()
            conn.execute(
                "UPDATE jobs SET state='failed',error='Lease expired after maximum attempts' WHERE state='running' AND lease_until<? AND attempts>=max_attempts",
                (time.time(),),
            )
            for lost in expired:
                self.settled(conn, lost, "failed", "Lease expired after maximum attempts")
            orphaned = conn.execute(
                "SELECT * FROM jobs WHERE state='pending' AND EXISTS(SELECT 1 FROM job_dependencies d JOIN jobs parent ON parent.id=d.dependency_id WHERE d.job_id=jobs.id AND parent.state IN ('failed','canceled'))"
            ).fetchall()
            conn.execute(
                "UPDATE jobs SET state='failed',error='Dependency failed or canceled' WHERE state='pending' AND EXISTS(SELECT 1 FROM job_dependencies d JOIN jobs parent ON parent.id=d.dependency_id WHERE d.job_id=jobs.id AND parent.state IN ('failed','canceled'))"
            )
            for lost in orphaned:
                self.settled(conn, lost, "failed", "Dependency failed or canceled")
            # While the foreground holds its lease only work that calls no paid model is
            # taken: the admission would refuse every other job, and taking it anyway only
            # re-ran its preparation every two seconds for the length of the conversation.
            free = ",".join("?" for _ in MODEL_FREE_KINDS)
            skip = ",".join("?" for _ in paused)
            row = conn.execute(
                "SELECT * FROM jobs j WHERE ((state IN ('pending','retry') AND available<=?) OR (state='running' AND lease_until<?)) AND "
                "(kind!='event_digest' OR ((SELECT COUNT(*) FROM jobs busy WHERE busy.kind='event_digest' AND busy.state='running' AND busy.lease_until>strftime('%s','now'))<2 AND NOT EXISTS(SELECT 1 FROM jobs busy WHERE busy.kind='event_digest' AND busy.state='running' AND busy.lease_until>strftime('%s','now') AND json_extract(busy.payload,'$.event_id')=json_extract(j.payload,'$.event_id') AND json_extract(busy.payload,'$.scope')=json_extract(j.payload,'$.scope')))) AND "
                f"(?=0 OR kind IN ({free})) AND "
                + (f"kind NOT IN ({skip}) AND " if paused else "") +
                "NOT EXISTS(SELECT 1 FROM job_dependencies d LEFT JOIN jobs parent ON parent.id=d.dependency_id WHERE d.job_id=j.id AND (parent.state IS NULL OR parent.state!='complete')) ORDER BY priority,available,id LIMIT 1",
                (time.time(), time.time(), int(foreground), *MODEL_FREE_KINDS, *paused),
            ).fetchone()
            if not row:
                return None
            if row["state"] == "running":
                # A lease that ran out under another worker: the attempt it paid for is lost,
                # and that is written down rather than repeated silently.
                self.engine_metric(conn, "job_lease_reclaimed", {"kind": row["kind"], "attempts": row["attempts"]})
            conn.execute(
                "UPDATE jobs SET state='running',owner=?,lease_until=?,fence=fence+1,attempts=attempts+1,updated_at=? WHERE id=?",
                (self.owner, time.time() + self.lease_seconds, now(), row["id"]),
            )
            return dict(
                conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            )

    @staticmethod
    def engine_metric(conn, name, data):
        """A metric inside the caller's transaction: counts and static names only."""
        conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)",
                     (name, 1, now(), dumps(data)))

    @staticmethod
    def settled(conn, job, state, error):
        """What a job that ended without completing leaves behind besides its own row."""
        if job["kind"] == "event_digest" or job["kind"].startswith("lifecycle_"):
            from kin_mind.lifecycle import job_failed
            job_failed(conn, job, state, error)
        if job["kind"] in EXTRACTION_KINDS and state == "failed":
            # The source's extraction ended: a part that failed for good leaves the source
            # failed, never pending for ever beside the parts that did commit.
            source_id = json.loads(job["payload"]).get("source_id")
            conn.execute("UPDATE sources SET model='failed' WHERE id=? AND model='pending'", (source_id,))

    def owns(self, conn, job, *, late=False):
        """Whether this run still holds the job. `late` asks only whether anybody took it over:
        the state, the owner and the fence are this run's, whatever the clock says of the lease."""
        row = conn.execute(
            "SELECT state,owner,fence,lease_until FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
        return bool(
            row
            and row["state"] == "running"
            and row["owner"] == self.owner
            and row["fence"] == job["fence"]
            and (late or row["lease_until"] > time.time())
        )

    def renew(self, job, done):
        while not done.wait(max(0.05, self.lease_seconds / 3)):
            try:
                with self.engine.db.connect(write=True) as conn:
                    # A lease that lapsed while nobody took the job over is renewed at the next
                    # beat, not given up: the machine slept, the job did not end (CR-MEM-09).
                    if not self.owns(conn, job, late=True):
                        return
                    conn.execute(
                        "UPDATE jobs SET lease_until=? WHERE id=?",
                        (time.time() + self.lease_seconds, job["id"]),
                    )
                self.last_beat = time.time()
            except sqlite3.OperationalError:
                # A busy database is a reason to try again at the next beat, not to stop
                # renewing and let a paid result be thrown away when the lease runs out.
                continue

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        done = threading.Event()
        heartbeat = threading.Thread(target=self.renew, args=(job, done), daemon=True)
        heartbeat.start()
        try:
            from kin_mind.model_runtime import background_calls
            with background_calls(), job_answers(job) as answers:
                apply = self.prepare(job)
            with self.engine.db.connect(write=True) as conn:
                if not self.owns(conn, job):
                    if not self.owns(conn, job, late=True):
                        # Taken over: another run holds the job now. What this one paid for stays
                        # with the job, for that run to use once it asks the very same (CR-MEM-09).
                        self.engine_metric(conn, "job_result_discarded", {"kind": job["kind"], "attempts": job["attempts"],
                                                                          "answers_kept": keep_answers(conn, job, answers)})
                        return True
                    # The lease ran out while the model answered — a sleeping laptop, a lock held
                    # too long — and nobody took the job over. Inside this write transaction nobody
                    # can: the lease is renewed and the paid result committed with it, instead of
                    # being thrown away and paid for again (CR-MEM-09).
                    conn.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() + self.lease_seconds, job["id"]))
                    self.engine_metric(conn, "job_lease_late_commit", {"kind": job["kind"], "attempts": job["attempts"]})
                apply(conn)
                drop_answers(conn, job["id"])
                conn.execute(
                    "UPDATE jobs SET state='complete',lease_until=NULL,error=NULL,updated_at=? WHERE id=?",
                    (now(), job["id"]),
                )
                self.engine.db.bump(conn)
        except Exception as exc:
            from kin_mind.model_runtime import ModelAdmissionWait

            with self.engine.db.connect(write=True) as conn:
                if self.owns(conn, job, late=True):
                    wait = isinstance(exc, (ModelAdmissionWait, Wait))
                    unbilled = wait or environmental(job["kind"], exc)
                    state = (
                        "retry" if unbilled else
                        "waiting_config"
                        if isinstance(exc, (NotConfigured, ImportError))
                        else "canceled"
                        if isinstance(exc, (Deleted, Missing))
                        else "failed"
                        if job["attempts"] >= job["max_attempts"]
                        else "retry"
                    )
                    if unbilled:
                        # Waiting for admission, or an environment that is down, is not an
                        # attempt at this job: nothing about the job failed.
                        conn.execute("UPDATE jobs SET attempts=MAX(0,attempts-1) WHERE id=?", (job["id"],))
                    if unbilled and not wait:
                        self.paused[job["kind"]] = time.time() + ENVIRONMENT_PAUSE
                    error = (
                        str(exc)
                        if isinstance(
                            exc, (NotConfigured, ValueError, Conflict, ProviderError, ModelAdmissionWait, Wait)
                        )
                        else type(exc).__name__
                    )
                    delay = (RETRY_SECONDS if wait else ENVIRONMENT_PAUSE if unbilled
                             else min(RETRY_CAP, 2 ** job["attempts"]))
                    conn.execute(
                        "UPDATE jobs SET state=?,error=?,available=?,lease_until=NULL,updated_at=? WHERE id=?",
                        (
                            state,
                            error[:500],
                            time.time() + delay,
                            now(),
                            job["id"],
                        ),
                    )
                    self.settled(conn, job, state, error)
                    if state in ("failed", "canceled"):
                        drop_answers(conn, job["id"])
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
            if not text.strip():
                # Nothing current is left to read: the source is done without a model call.
                return (lambda conn: None) if kind == "extract_part" else (
                    lambda conn: conn.execute("UPDATE sources SET model='complete' WHERE id=?", (sid,)))
            result = Providers(engine).json(
                "extraction",
                'Extract facts, preferences, relationships, commitments, procedures and episodes. Return {"candidates":[{"kind":"fact","content":"...","quote":"exact source excerpt","title":"...","attributes":{}}]}. Only include conclusions supported by an exact quote. Inferences remain unverified.',
                {"source_id": sid, "text": text, "scope": source["scope"]},
            )
            proposals, dropped = [], 0
            candidates = result.get("candidates", [])
            for i, candidate in enumerate(candidates[:100] if isinstance(candidates, list) else []):
                quote = candidate.get("quote", "") if isinstance(candidate, dict) else ""
                if (
                    not isinstance(quote, str)
                    or not quote
                    or quote not in text
                    or (channel_body is not None and quote not in channel_body)
                ):
                    continue
                attributes = candidate.get("attributes")
                try:
                    proposals.append(
                        RecordInput(
                            id="mem_"
                            + digest([sid, "extracted", payload.get("part", 0), i])[:32],
                            kind=candidate["kind"],
                            content=candidate["content"],
                            title=candidate.get("title") or "",
                            source_ids=[sid],
                            scope=Scope(**source["scope"]),
                            valid_from=source["occurred_at"],
                            generated=True,
                            confirmation="inferred",
                            # Descriptive keys only: the rest steer retrieval, the read policy
                            # and reminders, and a quoted page must not be able to set them.
                            attributes={k: v for k, v in (attributes if isinstance(attributes, dict) else {}).items()
                                        if k in EXTRACTED_ATTRIBUTES},
                            locator={"quote": quote, "source_id": sid},
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    # One malformed candidate is dropped alone; the rest of the batch stands.
                    dropped += 1
            if dropped:
                engine.db.metric("extraction_candidates_dropped", dropped, {"kind": kind})

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
                if engine._get(conn, data["id"])["revision"] != data["revision"]:
                    raise Conflict("Conflict evidence changed during execution")
                seen = {item["id"]: item for item in hits["items"]}
                for relation in result.get("relations", [])[:20]:
                    if (
                        relation.get("id") in seen
                        and relation["id"] != data["id"]
                        and relation.get("relation")
                        in {"coexists", "refutes", "supports"}
                    ):
                        target = seen[relation["id"]]
                        if engine._get(conn, target["id"])["revision"] != target["revision"]:
                            raise Conflict("Conflict evidence changed during execution")
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

            # The vector table is a store of its own, so its write is made here, outside the
            # database's write lock: a merge that takes seconds on a table with many versions
            # used to hold every other writer for as long. A row written for a revision that
            # has just moved on is harmless, because readers match vectors by revision.
            with engine.db.connect() as conn:
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
            return lambda conn: None
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
                '只返回 {"content":"...","evidence_ids":[id,...]}。只写来源支持的观察，保留不确定性；预测明确标为猜测，不编造感受或用户承诺。',
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
            return rebuild_step(engine, job, payload)
        if kind in ("build_vectors", "purge_vectors"):
            from .vectors import VectorIndex

            with engine.db.connect() as conn:
                indexes = [r[0] for r in conn.execute("SELECT id FROM vector_indexes")]
            # Derived work is safely repeatable after lease loss; canonical reads
            # still reject deleted or superseded records.
            for index_id in indexes:
                index = VectorIndex(engine, index_id)
                index.build() if kind == "build_vectors" else index.purge()
            if kind == "purge_vectors":
                return purge_text_indexes
            return lambda conn: None
        if kind == "vector_optimize":
            # Only while nothing else is running and nobody is being answered: a quiet store
            # is the condition this runs under, and waiting for it costs the job nothing.
            with engine.db.connect() as conn:
                busy = conn.execute("SELECT 1 FROM jobs WHERE state='running' AND id!=? AND lease_until>? LIMIT 1",
                                    (job["id"], time.time())).fetchone()
                foreground = conn.execute("SELECT 1 FROM mind_foreground_leases WHERE expires_at>? LIMIT 1",
                                          (time.time(),)).fetchone()
            if busy or foreground or engine.interactive_until > time.monotonic():
                raise Wait("store-not-quiet")
            from .vectors import optimize_all

            report = optimize_all(engine)

            def apply(conn):
                self.engine_metric(conn, "vector_optimized", {
                    "indexes": len(report), "versions_removed": sum(r["versions_before"] - r["versions_after"] for r in report),
                    "rows_unchanged": all(r["rows_before"] == r["rows_after"] for r in report)})

            return apply
        if kind == "erase_history":
            # What an explicit delete took out of the records, taken out of every revision of
            # the mind's state as well, a batch at a time; see kin_mind.erasure.
            from kin_mind.erasure import history_step

            return history_step(engine, payload)
        raise ValueError(f"Unknown job kind: {kind}")

    def hold_store(self):
        """One background loop per store (S1-11). A second service started on the same root,
        even for the moment before it finds its port taken, waits here instead of claiming
        jobs, and takes over when the first one's lock is gone. Returns the held lock, or None
        once stopped."""
        import fcntl

        handle = open(self.engine.db.root / "worker.lock", "a")
        while not self.stopped.is_set():
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.standby = False
                return handle
            except BlockingIOError:
                self.standby = True
                self.stopped.wait(STANDBY_SECONDS)
        handle.close()
        return None

    def run(self):
        """The one background loop. Every step is caught on its own: a database that stayed
        locked past its timeout, or one malformed row, used to end the thread for good while
        the service went on answering that it was ready."""
        held = self.hold_store()
        if held is None:
            return
        try:
            self._loop()
        finally:
            held.close()

    def _loop(self):
        from .scheduler import Scheduler

        scheduler = None
        while not self.stopped.is_set():
            self.last_tick = time.time()
            busy = False
            for name in ("replay_hosts", "maintenance", "scheduler", "jobs"):
                try:
                    if name == "replay_hosts":
                        self.replay_hosts()
                    elif name == "maintenance":
                        self.schedule_maintenance()
                    elif name == "scheduler":
                        scheduler = scheduler or Scheduler(self.engine)
                        scheduler.tick()
                    else:
                        busy = self.run_once()
                except Exception as exc:  # noqa: BLE001 - the loop outlives any one step
                    self.failures += 1
                    self.last_error = {"step": name, "error": type(exc).__name__, "at": now()}
                    log.warning("worker step %s failed: %s", name, type(exc).__name__)
                    self.stopped.wait(LOOP_BACKOFF)
            if not busy:
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
            from kin_mind.erasure import resume_history
            from kin_mind.lifecycle import schedule
            schedule(self.engine, conn, now())
            resume_history(self.engine, conn)
            scopes = conn.execute(
                "SELECT r.scope,COUNT(*),MAX(d.revision) FROM dirty d JOIN records r ON r.id=d.record_id WHERE r.deleted=0 AND r.status='active' AND d.revision=r.revision GROUP BY r.scope LIMIT 30"
            ).fetchall()
            for scope, count, _ in scopes:
                if count >= max(2, config.get("organize_batch", 20)):
                    # The payload's scope is stored with sorted keys and is compared in that form.
                    active = conn.execute(
                        "SELECT 1 FROM jobs WHERE kind='organize' AND state IN ('pending','running','retry','waiting_config') AND json_extract(payload,'$.scope')=json(?) LIMIT 1",
                        (dumps(json.loads(scope)),),
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
            # Once a day the vector store is compacted and its old versions dropped: it keeps a
            # manifest per write, and without this the manifests grow with the square of the
            # rows (DB1-02, K4-22). The job waits for a quiet store.
            if conn.execute("SELECT 1 FROM vector_indexes LIMIT 1").fetchone():
                day = datetime.now(timezone.utc).date().isoformat()
                self.engine.enqueue("vector_optimize", {"day": day}, f"vector-optimize:{day}",
                                    conn=conn, priority=VECTOR_OPTIMIZE_PRIORITY)
        # An isolation migration that stopped on a refusal keeps every read strict until it is
        # finished; it is finished here rather than whenever somebody notices (K4-14).
        from kin_mind.isolation_migration import resume_stalled
        resume_stalled(self.engine)

    def replay_hosts(self):
        """Replay the receipts a hook could not deliver, oldest first.

        A receipt that can never be received — its body is invalid, or the same source already
        holds other content — is moved to `host-spool/rejected/` at once. One that fails for a
        passing reason is tried again with a growing wait and moved there after
        SPOOL_ATTEMPTS. Either way it stops blocking the receipts behind it, it is counted, and
        nothing is deleted except a receipt whose source was erased on purpose."""
        from pydantic import ValidationError

        from .hosts import handle

        spool = self.engine.db.root / "host-spool"
        if not spool.exists():
            return
        failures = self.spool_failures

        def written(path):
            try:
                return path.stat().st_mtime
            except OSError:
                return float("inf")

        waiting = [path for path in spool.glob("*.json")
                   if failures.get(path.name, (0, 0))[1] <= time.time()]
        for path in sorted(waiting, key=lambda p: (written(p), p.name))[:20]:
            try:
                data = json.loads(path.read_text())
                # A replay cannot inject into a past host context; only receipt
                # events are replayed. Startup/compact are fetched again live.
                if data["event"] in {"tool", "message", "boundary", "end", "compact"}:
                    handle(
                        self.engine, data["event"], data["payload"], receipt_only=True
                    )
                path.unlink(missing_ok=True)
                failures.pop(path.name, None)
            except Deleted:
                path.unlink(missing_ok=True)
                failures.pop(path.name, None)
            except (ValidationError, ValueError, KeyError, TypeError, Conflict) as exc:
                self._reject(path, type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - a busy store is a reason to wait
                count = failures.get(path.name, (0, 0))[0] + 1
                if count >= SPOOL_ATTEMPTS:
                    self._reject(path, type(exc).__name__)
                else:
                    failures[path.name] = (count, time.time() + min(RETRY_CAP, 2 ** count * 5))

    def _reject(self, path, reason):
        rejected = path.parent / SPOOL_REJECTED
        rejected.mkdir(exist_ok=True, mode=0o700)
        try:
            path.replace(rejected / path.name)
        except FileNotFoundError:
            pass
        self.spool_failures.pop(path.name, None)
        self.engine.db.metric("host_spool_rejected", 1, {"reason": reason})

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

    def recover(self, job_ids, *, command_id, target=None):
        """Requeue failed jobs once, keeping the failure linked to the recovery.

        `control(retry)` answers an operator's click; it drops the error and leaves
        nothing behind. A derivative recovery needs the opposite bookkeeping: the
        failed row keeps its identity, the failure moves into `job_recovery`, and a
        second call for the same batch finds no `failed` row left to touch. Only
        `failed` rows move, never to `complete`; a job that failed again after an
        earlier recovery is a new failure and takes a new command id."""
        if not command_id or not 1 <= len(job_ids) <= 50 or len(set(job_ids)) != len(job_ids):
            raise ValueError("Recovery requires a sourced command and unique bounded jobs")
        recovered, skipped = [], []
        with self.engine.db.connect(write=True) as conn:
            for jid in job_ids:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                if not row:
                    raise Missing(jid)
                if row["state"] != "failed":
                    skipped.append({"id": jid, "state": row["state"]})
                    continue
                payload = json.loads(row["payload"])
                conn.execute(
                    "INSERT OR IGNORE INTO job_recovery VALUES(?,?,?,?,?,?,?,?)",
                    (jid, command_id, row["kind"],
                     target or dumps(payload),
                     row["state"], row["error"], row["attempts"], now()),
                )
                moved = conn.execute(
                    "UPDATE jobs SET state='pending',available=?,owner=NULL,lease_until=NULL,fence=fence+1,error=NULL,attempts=0,updated_at=? WHERE id=? AND state='failed'",
                    (time.time(), now(), jid),
                ).rowcount
                if moved:
                    recovered.append(jid)
                else:
                    # Lost the race to another recovery; the audit row above is the
                    # loser too, so take it back out.
                    conn.execute(
                        "DELETE FROM job_recovery WHERE job_id=? AND command_id=?",
                        (jid, command_id),
                    )
                    skipped.append({"id": jid, "state": "changed-during-recovery"})
        return {"recovered": recovered, "skipped": skipped, "command_id": command_id}

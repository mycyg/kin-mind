from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .db import Conflict, Database, Deleted, Missing, digest, dumps, tokenize
from .envelopes import current_message
from .models import RecordInput, RevisionInput, Scope, SourceInput, now


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Engine:
    """All authoritative writes, including host adapters, pass through this class.

    A successful receipt is durable. Model calls and derived indexes are never part
    of that receipt transaction. Write transactions use SQLite's cross-process lock.
    """

    def __init__(self, root: str | Path):
        self.db = Database(root)
        self.cache: dict = {}
        self.cache_lock = threading.RLock()
        self.interactive_until = 0.0

    def command(self, conn, key, payload, run):
        hashed = digest(payload)
        row = conn.execute("SELECT * FROM commands WHERE id=?", (key,)).fetchone()
        if row:
            if row["digest"] != hashed:
                # The key, not the payload: the digests stay out of the failure record.
                raise Conflict("Idempotency key reused with different content", target=key)
            return json.loads(row["result"])
        result = run()
        conn.execute("INSERT INTO commands VALUES(?,?,?)", (key, hashed, dumps(result)))
        return result

    def receive(self, source: SourceInput, attachment: bytes | None = None) -> dict:
        identity = [source.namespace, source.key, source.version, source.scope.key()]
        sid = "src_" + digest(identity)[:32]
        raw = attachment if attachment is not None else source.text.encode()
        record_text = source.text
        if (
            source.namespace.startswith("host:")
            and source.metadata.get("host_event") == "message"
            and source.metadata.get("role") == "user"
        ):
            record_text = current_message(record_text)
        hash_ = digest(raw)
        # Only immutable blob IO precedes the transaction; unreferenced blobs are GC'd.
        blob = self.db.blob(raw)
        with self.db.connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM tombstones WHERE key=?", (sid,)).fetchone():
                raise Deleted("This source was explicitly deleted")
            if not (self.db.blobs / blob).exists():
                self.db.blob(raw)
            previous = conn.execute(
                "SELECT * FROM sources WHERE id=?", (sid,)
            ).fetchone()
            if previous:
                if previous["hash"] != hash_:
                    raise Conflict("Source changed: provide a new version")
                return self._source(previous)
            stamp = now()
            data = source.model_dump(exclude={"text"})
            data["byte_length"] = len(raw)
            conn.execute(
                "INSERT INTO sources(id,namespace,source_key,version,scope,session,received_at,occurred_at,hash,blob,data,model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    source.namespace,
                    source.key,
                    source.version,
                    source.scope.key(),
                    source.session,
                    stamp,
                    source.occurred_at,
                    hash_,
                    blob,
                    dumps(data),
                    "pending"
                    if source.extract
                    or (
                        attachment is not None
                        and source.media_type.startswith(("image/", "audio/", "video/"))
                    )
                    else "not_requested",
                ),
            )
            # Deterministic text imports can be committed with the receipt.
            if attachment is None and source.text and len(source.text) <= 1_000_000:
                confirmation = {
                    "explicit": "explicit",
                    "operation": "observed",
                    "document": "documented",
                    "model": "inferred",
                }[source.authority]
                record = RecordInput(
                    id="mem_" + digest([sid, "root"])[:32],
                    kind=source.kind,
                    title=source.title,
                    content=record_text,
                    scope=source.scope,
                    source_ids=[sid],
                    valid_from=source.occurred_at,
                    confirmation=confirmation,
                    generated=source.authority == "model",
                    attributes=source.metadata,
                )
                self._insert(conn, record)
                if source.kind in {"knowledge", "checkpoint"}:
                    self.supersede_source_versions(conn, sid)
                conn.execute(
                    "UPDATE sources SET mechanical='complete' WHERE id=?", (sid,)
                )
            else:
                self.enqueue("parse", {"source_id": sid}, f"parse:{sid}", conn=conn)
            if source.extract:
                dependencies = (
                    ["job_" + digest(f"parse:{sid}")[:32]]
                    if attachment is not None
                    or not source.text
                    or len(source.text) > 1_000_000
                    else []
                )
                self.enqueue(
                    "extract",
                    {"source_id": sid},
                    f"extract:{sid}",
                    dependencies,
                    conn=conn,
                )
            self.db.bump(conn)
            return self._source(
                conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
            )

    def supersede_source_versions(self, conn, sid):
        source = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        metadata = json.loads(source["data"])
        checkpoint = metadata.get("kind") == "checkpoint"
        # Checkpoints replace only the same session's state. Document versions
        # follow their declared effective time, independent of delivery order.
        rows = conn.execute(
            "SELECT r.id,s.id source_id,s.occurred_at,s.received_at FROM records r JOIN evidence e ON e.record_id=r.id JOIN sources s ON s.id=e.source_id WHERE s.namespace=? AND s.scope=? AND r.kind=? AND r.status='active' AND "
            + ("s.session=?" if checkpoint else "s.source_key=?"),
            (
                source["namespace"],
                source["scope"],
                "checkpoint" if checkpoint else "knowledge",
                source["session"] if checkpoint else source["source_key"],
            ),
        ).fetchall()
        if not rows:
            return
        newest = max(rows, key=lambda r: (r["occurred_at"], r["received_at"]))[
            "source_id"
        ]
        for row in rows:
            if row["source_id"] != newest:
                data = self._get(conn, row["id"])
                data["status"] = "superseded"
                data["attributes"]["new_source_id"] = newest
                self._save_revision(
                    conn,
                    data,
                    "checkpoint" if checkpoint else "document_version",
                    "Updated current source",
                )

    @staticmethod
    def _source(row):
        value = json.loads(row["data"])
        value.update(
            {
                k: row[k]
                for k in ("id", "hash", "received_at", "mechanical", "model", "deleted")
            }
        )
        value.update(
            status="deleted" if row["deleted"] else "received",
            revision=1,
            source_ids=[row["id"]],
            read_url=f"/v1/sources/{row['id']}",
            attachment_url=f"/v1/sources/{row['id']}/content",
        )
        return value

    def source(self, sid, *, content=False, cursor="", limit=100):
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE id=? AND deleted=0", (sid,)
            ).fetchone()
            if not row:
                raise Missing(sid)
            if content:
                return self.db.blobs / row["blob"]
            result = self._source(row)
            rows = [
                r[0]
                for r in conn.execute(
                    "SELECT e.record_id FROM evidence e JOIN records r ON r.id=e.record_id WHERE e.source_id=? AND r.deleted=0 AND r.id>? ORDER BY r.id LIMIT ?",
                    (sid, cursor, limit + 1),
                )
            ]
            result["record_ids"] = rows[:limit]
            result["cursor"] = rows[limit - 1] if len(rows) > limit else None
            return result

    def _insert(self, conn, record: RecordInput):
        rid = record.id or uid("mem")
        if conn.execute("SELECT 1 FROM tombstones WHERE key=?", (rid,)).fetchone():
            raise Deleted(rid, code="tombstoned")
        existing = conn.execute(
            "SELECT data FROM records WHERE id=?", (rid,)
        ).fetchone()
        if existing:
            stored = json.loads(existing[0])
            if (
                stored["content"] != record.content
                or stored["scope"] != record.scope.model_dump()
                or stored["kind"] != record.kind
            ):
                raise Conflict("Record id already belongs to different content", target=rid)
            return stored
        for sid in set(record.source_ids):
            row = conn.execute(
                "SELECT scope FROM sources WHERE id=? AND deleted=0", (sid,)
            ).fetchone()
            if not row:
                raise Missing(f"Source {sid}")
            if row[0] != record.scope.key():
                raise Conflict("Source and record scopes differ")
        for evidence in set(
            record.evidence_ids + ([record.parent_id] if record.parent_id else [])
        ):
            row = self._get(conn, evidence)
            if row["scope"] != record.scope.model_dump():
                raise Conflict("Evidence and record scopes differ")
        stamp = now()
        data = record.model_dump()
        if not record.source_ids:
            data["attributes"]["source_completeness"] = "missing"
        data.update(
            id=rid,
            revision=1,
            received_at=stamp,
            updated_at=stamp,
            source_ids=sorted(set(record.source_ids)),
            evidence_ids=sorted(set(record.evidence_ids)),
            read_url=f"/v1/memories/{rid}",
            instruction_authority="data",
        )
        conn.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                rid,
                record.kind,
                record.scope.key(),
                record.status,
                record.valid_from,
                record.valid_until,
                stamp,
                stamp,
                1,
                record.importance,
                record.parent_id,
                dumps(data),
            ),
        )
        conn.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?)",
            (rid, 1, stamp, "create", "", dumps(data)),
        )
        for sid in data["source_ids"]:
            conn.execute("INSERT INTO evidence VALUES(?,?)", (rid, sid))
        for evidence in data["evidence_ids"]:
            conn.execute("INSERT INTO dependencies VALUES(?,?)", (rid, evidence))
        if record.parent_id:
            self._relation(
                conn, rid, "part_of", record.parent_id, {"basis": "document structure"}
            )
        self._index_text(conn, data)
        self._dirty(conn, data)
        return data

    def add_record(self, record: RecordInput, command_id: str) -> dict:
        with self.db.connect(write=True) as conn:

            def run():
                result = self._insert(conn, record)
                self.db.bump(conn)
                return result

            return self.command(conn, f"record:{command_id}", record.model_dump(), run)

    def _dirty(self, conn, data):
        from kin_mind.procedures import invalidate_source
        invalidate_source(self, conn, data)
        from kin_mind.lifecycle import record_changed
        record_changed(conn, data)
        conn.execute(
            "INSERT INTO dirty VALUES(?,?) ON CONFLICT(record_id) DO UPDATE SET revision=excluded.revision",
            (data["id"], data["revision"]),
        )
        self.enqueue(
            "embed",
            {"record_id": data["id"], "revision": data["revision"]},
            f"embed:{data['id']}:{data['revision']}",
            conn=conn,
        )
        if data.get("locator", {}).get("type") in {"image", "keyframe"}:
            self.enqueue(
                "visual_embed",
                {"record_id": data["id"], "revision": data["revision"]},
                f"visual-embed:{data['id']}:{data['revision']}",
                conn=conn,
            )

    @staticmethod
    def _index_text(conn, data):
        rowid = conn.execute(
            "SELECT rowid FROM records WHERE id=?", (data["id"],)
        ).fetchone()[0]
        conn.execute("DELETE FROM search WHERE rowid=?", (rowid,))
        conn.execute(
            "INSERT INTO search(rowid,id,tokens) VALUES(?,?,?)",
            (rowid, data["id"], tokenize(data["title"] + " " + data["content"])),
        )

    @staticmethod
    def _get(conn, rid, at=None, known_at=None):
        current = conn.execute(
            "SELECT * FROM records WHERE id=? AND deleted=0", (rid,)
        ).fetchone()
        if not current:
            raise Missing(rid)
        if known_at:
            row = conn.execute(
                "SELECT data FROM revisions WHERE record_id=? AND changed_at<=? ORDER BY revision DESC LIMIT 1",
                (rid, known_at),
            ).fetchone()
            if not row:
                raise Missing(rid)
            return json.loads(row[0])
        return json.loads(current["data"])

    def get(self, rid, at=None, known_at=None):
        with self.db.connect() as conn:
            data = self._get(conn, rid, at, known_at)
            if at:
                data["valid_at_query"] = data["valid_from"] <= at and (
                    not data["valid_until"] or data["valid_until"] > at
                )
            data["independent_sources"] = conn.execute(
                "SELECT COUNT(DISTINCT s.hash) FROM evidence e JOIN sources s ON s.id=e.source_id WHERE e.record_id=? AND s.deleted=0",
                (rid,),
            ).fetchone()[0]
            return data

    def history(self, rid, cursor=2147483647, limit=50):
        with self.db.connect() as conn:
            self._get(conn, rid)
            return [
                dict(r) | {"data": json.loads(r["data"])}
                for r in conn.execute(
                    "SELECT * FROM revisions WHERE record_id=? AND revision<? ORDER BY revision DESC LIMIT ?",
                    (rid, cursor, limit),
                )
            ]

    def _save_revision(self, conn, data, action, reason):
        data["revision"] += 1
        data["updated_at"] = now()
        checked = RecordInput.model_validate(
            {k: v for k, v in data.items() if k in RecordInput.model_fields}
        )
        data.update(checked.model_dump())
        conn.execute(
            "UPDATE records SET kind=?,scope=?,status=?,valid_from=?,valid_until=?,updated_at=?,revision=?,importance=?,data=? WHERE id=?",
            (
                data["kind"],
                checked.scope.key(),
                data["status"],
                data["valid_from"],
                data["valid_until"],
                data["updated_at"],
                data["revision"],
                data["importance"],
                dumps(data),
                data["id"],
            ),
        )
        conn.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?,?)",
            (
                data["id"],
                data["revision"],
                data["updated_at"],
                action,
                reason,
                dumps(data),
            ),
        )
        self._index_text(conn, data)
        self._dirty(conn, data)

    def revise(self, rid, change: RevisionInput):
        with self.db.connect(write=True) as conn:

            def run():
                data = self._get(conn, rid)
                if data["revision"] != change.expected_revision:
                    # A formatted message is never a static literal, so this one
                    # carries its classification and its versions as keywords.
                    raise Conflict(
                        f"Revision changed; current revision is {data['revision']}",
                        kind="runtime", code="record-revision-changed", target=rid,
                        expected=change.expected_revision, actual=data["revision"],
                    )
                statuses = {
                    "confirm": "active",
                    "retract": "retracted",
                    "replace": "superseded",
                    "refute": "refuted",
                    "archive": "archived",
                    "restore": "active",
                }
                if change.action == "rollback":
                    old = conn.execute(
                        "SELECT data FROM revisions WHERE record_id=? AND revision=?",
                        (rid, change.target_revision),
                    ).fetchone()
                    if not old:
                        raise Missing("Revision")
                    restored = json.loads(old[0])
                    for key in (
                        "content",
                        "status",
                        "attributes",
                        "valid_from",
                        "valid_until",
                        "confirmation",
                    ):
                        data[key] = restored[key]
                elif change.action in statuses:
                    data["status"] = statuses[change.action]
                if change.action in ("confirm", "correct"):
                    data["confirmation"] = "verified"
                    data["status"] = "active"
                if (
                    change.action == "correct"
                    and change.content is None
                    and change.attributes is None
                ):
                    raise ValueError("Correction requires content or attributes")
                if change.content is not None:
                    if not change.content.strip():
                        raise ValueError("Content cannot be empty")
                    data["content"] = change.content
                if change.attributes is not None:
                    data["attributes"] = data["attributes"] | change.attributes
                if change.action == "replace":
                    other = self._get(conn, change.replacement_id)
                    if (
                        other["scope"] != data["scope"]
                        or other["id"] == rid
                        or other["status"] != "active"
                    ):
                        raise Conflict(
                            "Replacement must be another active record in the same scope"
                        )
                    data["attributes"]["superseded_by"] = other["id"]
                    self._relation(
                        conn,
                        rid,
                        "superseded_by",
                        other["id"],
                        {"reason": change.reason},
                    )
                self._save_revision(conn, data, change.action, change.reason)
                # Invalidate generated conclusions, never silently promote a stale summary.
                pending = [rid]
                visited = {rid}
                while pending:
                    parent = pending.pop()
                    for dep in conn.execute(
                        "SELECT record_id FROM dependencies WHERE evidence_id=?",
                        (parent,),
                    ).fetchall():
                        if dep[0] in visited:
                            continue
                        visited.add(dep[0])
                        pending.append(dep[0])
                        child = self._get(conn, dep[0])
                        if child["generated"] and child["status"] == "active":
                            child["status"] = "unverified"
                            child["attributes"]["stale_evidence"] = rid
                            self._save_revision(
                                conn, child, "evidence_changed", change.reason
                            )
                if data["status"] != "active" or data["attributes"].get("completed"):
                    # The scheduler's one cancel rule: a delivery whose request may be
                    # on the network is never called canceled.
                    from .scheduler import withdraw

                    withdraw(
                        conn,
                        "schedule_id IN (SELECT id FROM schedules WHERE record_id=?)",
                        (rid,),
                    )
                    conn.execute(
                        "UPDATE schedules SET state='canceled',revision=revision+1,data=json_set(data,'$.generation',COALESCE(json_extract(data,'$.generation'),0)+1) WHERE record_id=?",
                        (rid,),
                    )
                else:
                    # A dispatched delivery keeps its frozen body, so a retry repeats
                    # the same bytes. One still waiting takes the new text, and a claim
                    # on it is released so that it is frozen again from this revision.
                    conn.execute(
                        "UPDATE outbox SET lease_until=CASE WHEN lease_until=json_extract(data,'$.claim.lease_until') THEN NULL ELSE lease_until END,"
                        "data=json_remove(json_set(data,'$.text',?,'$.record_revision',?),'$.claim') "
                        "WHERE schedule_id IN (SELECT id FROM schedules WHERE record_id=?) AND state IN ('suggested','ready','retry') AND json_extract(data,'$.dispatch.body') IS NULL",
                        (data["content"], data["revision"], rid),
                    )
                self.db.bump(conn)
                return data

            return self.command(
                conn, f"revision:{change.command_id}", [rid, change.model_dump()], run
            )

    def _relation(self, conn, subject, predicate, object_, attributes):
        left, right = self._get(conn, subject), self._get(conn, object_)
        if left["scope"] != right["scope"]:
            raise Conflict("Cross-scope relations are not allowed")
        rid = "rel_" + digest([subject, predicate, object_])[:32]
        scope = Scope(**left["scope"]).key()
        conn.execute(
            "INSERT INTO relations VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (rid, subject, predicate, object_, scope, dumps(attributes)),
        )
        return {
            "id": rid,
            "subject": subject,
            "predicate": predicate,
            "object": object_,
            "attributes": attributes,
        }

    def relate(self, subject, predicate, object_, attributes=None):
        if predicate not in {
            "coexists",
            "refutes",
            "supports",
            "superseded_by",
            "part_of",
            "follows",
            "about",
            "verifies",
            "counterexample",
            "related",
        }:
            raise ValueError("Unknown relationship")
        with self.db.connect(write=True) as conn:
            result = self._relation(conn, subject, predicate, object_, attributes or {})
            self.db.bump(conn)
            return result

    def list_records(
        self,
        scope: Scope,
        kind=None,
        status=None,
        cursor=None,
        limit=50,
        group=None,
        query=None,
    ):
        clauses = ["scope=?", "deleted=0"]
        values = [scope.key()]
        if query:
            words = list(dict.fromkeys(tokenize(query).split()))[:40]
            if words:
                clauses.append("id IN (SELECT id FROM search WHERE search MATCH ?)")
                values.append(
                    " AND ".join('"' + w.replace('"', '""') + '"' for w in words)
                )
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        groups = {
            "diary": ["diary", "summary", "portrait", "self_narrative", "prediction"],
            "timeline": [
                "episode",
                "checkpoint",
                "relationship",
                "commitment",
                "reminder",
                "state",
            ],
        }
        if group in groups:
            clauses.append("kind IN (" + ",".join("?" for _ in groups[group]) + ")")
            values.extend(groups[group])
        if status:
            clauses.append("status=?")
            values.append(status)
        if cursor:
            clauses.append("id>?")
            values.append(cursor)
        limit = max(1, min(200, limit))
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id,data FROM records WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT ?",
                values + [limit + 1],
            ).fetchall()
        items = []
        for row in rows[:limit]:
            data = json.loads(row["data"])
            data.update(content_length=len(data["content"]), preview=True)
            data["content"] = data["content"][:2000]
            data["attributes"] = {}
            data["locator"] = {
                key: value
                for key, value in data.get("locator", {}).items()
                if key
                in {
                    "type",
                    "page",
                    "row",
                    "start_seconds",
                    "end_seconds",
                    "char_start",
                    "char_end",
                }
            }
            data["source_count"] = len(data["source_ids"])
            data["source_ids"] = data["source_ids"][:20]
            data["evidence_ids"] = data.get("evidence_ids", [])[:20]
            items.append(data)
        return {
            "items": items,
            "cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def delete(self, rid):
        """Erase source-backed records and derived conclusions, retaining only tombstones.

        Corrections/retractions use revisions. Explicit deletion removes historical text.
        An explicitly reimported new source must have a new source identity/version.
        """
        with self.db.connect(write=True) as conn:
            todo = {rid}
            source_ids = {rid} if rid.startswith("src_") else set()
            # Erasing a record also erases its raw source, whose bytes may contain
            # the same text. Other records citing that source belong to the erase
            # set; the preview endpoint exposes this set before a console delete.
            source_ids.update(
                r[0]
                for r in conn.execute(
                    "SELECT source_id FROM evidence WHERE record_id=?", (rid,)
                )
            )
            for sid in source_ids:
                todo.update(
                    r[0]
                    for r in conn.execute(
                        "SELECT record_id FROM evidence WHERE source_id=?", (sid,)
                    )
                )
            deleted = set()
            while todo:
                current = todo.pop()
                if current in deleted:
                    continue
                deleted.add(current)
                todo.update(
                    r[0]
                    for r in conn.execute(
                        "SELECT record_id FROM dependencies WHERE evidence_id=?",
                        (current,),
                    )
                )
                todo.update(
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM records WHERE parent_id=?", (current,)
                    )
                )
                # Derived narratives cite multiple sources. Only original sources
                # of the selected object are erased; unrelated cited sources stay.
            blobs_to_remove = set()
            for sid in source_ids:
                row = conn.execute(
                    "SELECT blob FROM sources WHERE id=?", (sid,)
                ).fetchone()
                if row:
                    blobs_to_remove.add(row[0])
            for current in deleted:
                row = conn.execute(
                    "SELECT rowid FROM records WHERE id=?", (current,)
                ).fetchone()
                if row:
                    conn.execute("DELETE FROM search WHERE rowid=?", (row[0],))
                    old_data = self._get(conn, current)
                    from kin_mind.lifecycle import record_deleted
                    record_deleted(conn, old_data, now())
                    if old_data["locator"].get("blob"):
                        blobs_to_remove.add(old_data["locator"]["blob"])
                family_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT family_id FROM members WHERE record_id=?", (current,)
                    )
                ]
                for family_id in family_ids:
                    conn.execute(
                        "DELETE FROM family_revisions WHERE id=?", (family_id,)
                    )
                    conn.execute("DELETE FROM families WHERE id=?", (family_id,))
                    conn.execute("DELETE FROM members WHERE family_id=?", (family_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO tombstones VALUES(?,?)", (current, now())
                )
                conn.execute("DELETE FROM records WHERE id=?", (current,))
                conn.execute("DELETE FROM revisions WHERE record_id=?", (current,))
                conn.execute("DELETE FROM evidence WHERE record_id=?", (current,))
                conn.execute(
                    "DELETE FROM dependencies WHERE record_id=? OR evidence_id=?",
                    (current, current),
                )
                conn.execute(
                    "DELETE FROM relations WHERE subject=? OR object=?",
                    (current, current),
                )
                conn.execute("DELETE FROM members WHERE record_id=?", (current,))
                conn.execute("DELETE FROM dirty WHERE record_id=?", (current,))
                conn.execute("DELETE FROM feedback WHERE record_id=?", (current,))
                conn.execute(
                    "DELETE FROM outbox WHERE schedule_id IN (SELECT id FROM schedules WHERE record_id=?)",
                    (current,),
                )
                conn.execute("DELETE FROM schedules WHERE record_id=?", (current,))
            for sid in source_ids:
                if not conn.execute(
                    "SELECT 1 FROM evidence WHERE source_id=? LIMIT 1", (sid,)
                ).fetchone():
                    conn.execute(
                        "INSERT OR IGNORE INTO tombstones VALUES(?,?)", (sid, now())
                    )
                    conn.execute("DELETE FROM sources WHERE id=?", (sid,))
            # Stored command responses and session sets may contain deleted text.
            conn.execute("DELETE FROM commands")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM prefetch")
            conn.execute("DELETE FROM metrics")
            for job in conn.execute("SELECT id,payload FROM jobs").fetchall():
                payload = json.loads(job["payload"])
                if (
                    payload.get("record_id") in deleted
                    or payload.get("source_id") in source_ids
                ):
                    conn.execute(
                        "UPDATE jobs SET state='canceled',payload='{}',error=NULL WHERE id=?",
                        (job["id"],),
                    )
            self.enqueue("purge_vectors", {}, f"purge:{uuid.uuid4().hex}", conn=conn)
            self.db.bump(conn)
        with self.db.connect(write=True) as conn:
            for blob in blobs_to_remove:
                if (
                    not conn.execute(
                        "SELECT 1 FROM sources WHERE blob=? LIMIT 1", (blob,)
                    ).fetchone()
                    and not conn.execute(
                        "SELECT 1 FROM records WHERE json_extract(data,'$.locator.blob')=? LIMIT 1",
                        (blob,),
                    ).fetchone()
                ):
                    (self.db.blobs / blob).unlink(missing_ok=True)
        with self.cache_lock:
            self.cache.clear()
        self.gc_blobs()
        return {
            "id": rid,
            "status": "deleted",
            "deleted_count": len(deleted),
            "revision": 0,
            "source_ids": [],
        }

    def gc_blobs(self):
        # A write lock prevents GC racing a committed receipt; recent orphan blobs
        # are retained briefly because blob creation intentionally precedes receipt.
        with self.db.connect(write=True) as conn:
            used = {
                r[0] for r in conn.execute("SELECT blob FROM sources WHERE deleted=0")
            }
            used.update(
                r[0]
                for r in conn.execute(
                    "SELECT json_extract(data,'$.locator.blob') FROM records WHERE deleted=0 AND json_extract(data,'$.locator.blob') IS NOT NULL"
                )
            )
            for path in self.db.blobs.iterdir():
                if path.name not in used and path.stat().st_mtime < time.time() - 60:
                    path.unlink(missing_ok=True)

    def enqueue(self, kind, payload, key, dependencies=(), *, conn=None, priority=100):
        if conn is None:
            with self.db.connect(write=True) as transaction:
                return self.enqueue(kind, payload, key, dependencies, conn=transaction, priority=priority)
        jid = "job_" + digest(key)[:32]
        existing = conn.execute(
            "SELECT kind,payload FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        if existing and (
            existing["kind"] != kind or existing["payload"] != dumps(payload)
        ):
            raise Conflict("Job key reused with a different task")
        stamp = now()
        conn.execute(
            "INSERT OR IGNORE INTO jobs(id,kind,unique_key,payload,state,available,created_at,updated_at,priority) VALUES(?,?,?,?,'pending',?,?,?,?)",
            (jid, kind, key, dumps(payload), time.time(), stamp, stamp, priority),
        )
        for dependency in dependencies:
            conn.execute(
                "INSERT OR IGNORE INTO job_dependencies VALUES(?,?)", (jid, dependency)
            )
        return jid

    def settings(self, key, value=None):
        with self.db.connect(write=value is not None) as conn:
            if value is not None:
                conn.execute(
                    "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                    (key, dumps(value)),
                )
                self.db.bump(conn)
                # Explicit configuration changes make pending model jobs retryable.
                conn.execute(
                    "UPDATE jobs SET state='pending',available=? WHERE state='waiting_config'",
                    (time.time(),),
                )
            row = conn.execute(
                "SELECT data FROM settings WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else {}

    def feedback(self, rid, type_, session="", attributes=None, key=None):
        if type_ not in {
            "displayed",
            "read",
            "adopted",
            "verified",
            "corrected",
            "unknown",
            "same_file_observed",
        }:
            raise ValueError("Unsupported feedback type")
        with self.db.connect(write=True) as conn:
            self._get(conn, rid)
            fid = key or uid("feedback")
            conn.execute(
                "INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?)",
                (fid, rid, session, type_, now(), dumps(attributes or {})),
            )
        return {"id": fid, "type": type_}

    def recall(self, request):
        from .retrieval import recall

        return recall(self, request)

    def overview(self):
        with self.db.connect() as conn:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sources", "records", "families", "schedules")
            }
            counts["jobs"] = {
                r[0]: r[1]
                for r in conn.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state")
            }
            counts["job_details"] = [
                {"state": row[0], "kind": row[1], "error": row[2], "count": row[3]}
                for row in conn.execute(
                    "SELECT state,kind,error,COUNT(*) FROM jobs "
                    "WHERE state IN ('failed','retry','waiting_config') "
                    "GROUP BY state,kind,error ORDER BY COUNT(*) DESC LIMIT 50"
                )
            ]
            counts["statistics_scope"] = "store"
            counts["kinds"] = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT kind,COUNT(*) FROM records WHERE deleted=0 GROUP BY kind"
                )
            }
            counts["generation"] = self.db.generation(conn)
            counts["dirty"] = conn.execute("SELECT COUNT(*) FROM dirty").fetchone()[0]
            counts["indexes"] = [
                json.loads(r[0])
                for r in conn.execute("SELECT data FROM vector_indexes")
            ]
            metrics = conn.execute(
                "SELECT name,value,data FROM metrics ORDER BY id DESC LIMIT 2000"
            ).fetchall()
        times = sorted(r["value"] for r in metrics if r["name"] == "recall_ms")
        counts["latency"] = {
            "samples": len(times),
            "p95_ms": times[min(len(times) - 1, int(len(times) * 0.95))]
            if times
            else None,
        }
        counts["model_cost"] = sum(
            r["value"] for r in metrics if r["name"] == "model_cost"
        )
        counts["model_tokens"] = sum(
            r["value"] for r in metrics if r["name"] == "model_tokens"
        )
        return counts

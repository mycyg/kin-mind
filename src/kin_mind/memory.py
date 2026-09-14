"""Source-backed work and disclosure ledger shared by every owner channel.

Only the authenticated host supplies runtime events. Semantic proposals can add
links and summaries; they cannot certify a file operation or a delivery.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from eventmem.core.db import Conflict, Missing, digest, dumps, tokenize
from eventmem.core.models import Model, RecordInput, SourceInput

from .computer import redact
from .state import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_memory_config(scope TEXT PRIMARY KEY,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_runtime_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,
 kind TEXT NOT NULL,occurred_at TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_runtime_scope ON mind_runtime_events(scope,seq);
CREATE TABLE IF NOT EXISTS mind_memory_nodes(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,revision INTEGER NOT NULL,
 updated_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_memory_node_scope ON mind_memory_nodes(scope,kind,updated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS mind_memory_search USING fts5(id UNINDEXED,tokens);
CREATE TABLE IF NOT EXISTS mind_memory_revisions(
 id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS mind_artifact_aliases(
 scope TEXT NOT NULL,fingerprint TEXT NOT NULL,work_id TEXT NOT NULL,
 PRIMARY KEY(scope,fingerprint));
CREATE TABLE IF NOT EXISTS mind_semantic_cursor(
 scope TEXT PRIMARY KEY,seq INTEGER NOT NULL DEFAULT 0,next_review TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_semantic_sources(
 scope TEXT NOT NULL,source_id TEXT NOT NULL,event_id TEXT NOT NULL,
 PRIMARY KEY(scope,source_id));
CREATE TABLE IF NOT EXISTS mind_memory_access(
 scope TEXT NOT NULL,session TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 depth TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(scope,session,id,revision,depth));
CREATE TABLE IF NOT EXISTS mind_memory_migrations(
 scope TEXT NOT NULL,name TEXT NOT NULL,cursor INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,name));
"""

DEFAULTS = {"records": False, "semantic": False, "context": False, "idle": False,
            "version": "memory-continuity-v1", "review_min_minutes": 20,
            "review_max_minutes": 120, "first_review_minutes": 20}


class MemoryNote(Model):
    key: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=4000)
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["episode", "knowledge", "preference", "commitment"] = "episode"
    evidence_ids: list[str] = Field(min_length=1, max_length=16)
    about_ids: list[str] = Field(default_factory=list, max_length=12)


class MemoryLink(Model):
    subject: str
    object: str
    relation: Literal["about", "part_of", "follows", "supports", "refutes", "related"] = "related"
    evidence_ids: list[str] = Field(min_length=1, max_length=16)


class Disclosure(Model):
    share_id: str
    topic: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1600)
    about_ids: list[str] = Field(default_factory=list, max_length=12)
    previous_share_ids: list[str] = Field(default_factory=list, max_length=12)
    mode: Literal["new", "development", "reflection", "reminiscence", "duplicate"] = "new"


class MemoryAssessment(Model):
    notes: list[MemoryNote] = Field(default_factory=list, max_length=8)
    links: list[MemoryLink] = Field(default_factory=list, max_length=16)
    disclosures: list[Disclosure] = Field(default_factory=list, max_length=12)


def fingerprint_file(path):
    """Read bytes without executing/extracting archives; ignore ZIP packaging."""
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Artifact must be a file")
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    result = {"sha256": h.hexdigest(), "name": path.name, "bytes": path.stat().st_size,
              "path": str(path)}
    if zipfile.is_zipfile(path):
        members, total = [], 0
        with zipfile.ZipFile(path) as archive:
            infos = [x for x in archive.infolist() if not x.is_dir()]
            if len(infos) > 10000 or sum(x.file_size for x in infos) > 512 * 1024 * 1024:
                result["member_status"] = "budget-exceeded"
                return result
            for info in infos:
                member = hashlib.sha256()
                with archive.open(info) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > 512 * 1024 * 1024:
                            raise ValueError("Archive expanded beyond declared budget")
                        member.update(chunk)
                members.append({"name": info.filename, "sha256": member.hexdigest(), "bytes": info.file_size})
        # Member names remain evidence, while the bag of member contents handles
        # renamed root folders and changed ZIP compression/timestamps.
        result.update(members=members, members_sha256=digest(sorted((x["sha256"], x["bytes"]) for x in members)))
    return result


class MemoryContinuity:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA)

    def settings(self, conn=None):
        if conn is None:
            with self.engine.db.connect() as connection:
                return self.settings(connection)
        row = conn.execute("SELECT data FROM mind_memory_config WHERE scope=?", (self.scope.key(),)).fetchone()
        return DEFAULTS | (json.loads(row[0]) if row else {})

    def configure(self, values):
        if set(values) - set(DEFAULTS):
            raise ValueError("Unknown memory setting")
        for key in ("records", "semantic", "context", "idle"):
            if key in values and type(values[key]) is not bool:
                raise ValueError("Feature flags are boolean")
        with self.engine.db.connect(write=True) as conn:
            config = self.settings(conn) | values
            if not 20 <= config["review_min_minutes"] <= config["first_review_minutes"] <= config["review_max_minutes"] <= 120:
                raise ValueError("Review range must be within 20..120 minutes")
            conn.execute("INSERT OR REPLACE INTO mind_memory_config VALUES(?,?)", (self.scope.key(), dumps(config)))
            due = (timestamp(self.mind.clock()) + timedelta(minutes=config["first_review_minutes"])).isoformat()
            conn.execute("INSERT OR IGNORE INTO mind_semantic_cursor(scope,next_review,data) VALUES(?,?,?)",
                         (self.scope.key(), due, "{}"))
        return config

    def _id(self, kind, key):
        return kind + "_" + digest([self.scope.key(), key])[:32]

    def _get(self, conn, identifier):
        row = conn.execute("SELECT data FROM mind_memory_nodes WHERE id=? AND scope=?", (identifier, self.scope.key())).fetchone()
        if not row:
            raise Missing(identifier)
        return json.loads(row[0])

    def _put(self, conn, node):
        row = conn.execute("SELECT revision,data FROM mind_memory_nodes WHERE id=? AND scope=?", (node["id"], self.scope.key())).fetchone()
        previous = json.loads(row["data"]) if row else None
        compare = {k: v for k, v in node.items() if k not in {"revision", "updated_at"}}
        if previous and compare == {k: v for k, v in previous.items() if k not in {"revision", "updated_at"}}:
            return previous
        node = {**node, "revision": (row["revision"] if row else 0) + 1, "updated_at": self.mind.clock()}
        conn.execute("INSERT OR REPLACE INTO mind_memory_nodes VALUES(?,?,?,?,?,?)",
                     (node["id"], self.scope.key(), node["kind"], node["revision"], node["updated_at"], dumps(node)))
        conn.execute("INSERT INTO mind_memory_revisions VALUES(?,?,?)", (node["id"], node["revision"], dumps(node)))
        conn.execute("DELETE FROM mind_memory_search WHERE id=?", (node["id"],))
        search_text = " ".join(str(node.get(k, "")) for k in ("title", "topic", "summary", "name", "task_ids", "about_ids"))
        search_text += " " + " ".join(b.get("text", "") for b in node.get("bubbles", {}).values())
        conn.execute("INSERT INTO mind_memory_search VALUES(?,?)", (node["id"], tokenize(search_text)))
        return node

    def ingest(self, request):
        """Host-only durable event. A replay must keep the original event ID/data."""
        if not self.settings()["records"]:
            return {"state": "disabled"}
        event = dict(request)
        kind = event.get("kind")
        if kind not in {"owner-message", "assistant-message", "artifact-created", "artifact-observed", "delivery", "task-result", "exploration-result"}:
            raise ValueError("Unsupported runtime event")
        if not event.get("id") or not event.get("at"):
            raise ValueError("Runtime event requires stable id and observation time")
        timestamp(event["at"])
        event = redact(event)
        event_id = self._id("runtime", event["id"])
        fingerprint = digest(event)
        with self.engine.db.connect() as conn:
            old = conn.execute("SELECT * FROM mind_runtime_events WHERE id=?", (event_id,)).fetchone()
            if old:
                if old["digest"] != fingerprint:
                    raise Conflict("Runtime event id belongs to different data")
                return {"id": event_id, "seq": old["seq"], "state": "recorded", **json.loads(old["data"])["receipt"]}
        artifact = dict(event.get("artifact") or {})
        if artifact.get("path"):
            observed = fingerprint_file(artifact["path"])
            if artifact.get("sha256") and artifact["sha256"] != observed["sha256"]:
                raise Conflict("Observed artifact version changed before ingestion")
            artifact = {**artifact, **observed, **({"name": artifact["name"]} if artifact.get("name") else {})}
        if artifact and not artifact.get("sha256"):
            raise ValueError("Artifacts require a host-observed fingerprint")
        if event.get("state") == "accepted" and not event.get("message_id"):
            raise ValueError("Accepted delivery requires a platform message ID")
        authority = "explicit" if kind == "owner-message" else "model" if kind in {"assistant-message", "exploration-result"} else "operation"
        source_id = event.get("source_id")
        if not source_id:
            source_id = self.engine.receive(SourceInput(
                namespace="kin-runtime", key=event_id, scope=self.scope, session=event.get("session", "host"),
                text=dumps({**event, "artifact": artifact}) if kind != "owner-message" else event.get("text", ""),
                authority=authority, occurred_at=event["at"], extract=False,
                metadata={"host_event": kind, "role": "user" if kind == "owner-message" else "assistant",
                          "runtime_event_id": event_id, "channel": event.get("channel"), "internal": kind not in {"owner-message", "assistant-message"}},
            ))["id"]
        with self.engine.db.connect(write=True) as conn:
            # Another host may have committed while file/source IO ran.
            old = conn.execute("SELECT * FROM mind_runtime_events WHERE id=?", (event_id,)).fetchone()
            if old:
                if old["digest"] != fingerprint:
                    raise Conflict("Conflicting runtime event")
                return {"id": event_id, "seq": old["seq"], "state": "recorded", **json.loads(old["data"])["receipt"]}
            refs = self.mind._evidence(conn, [source_id])
            if not self.mind._fresh(conn, refs):
                raise Conflict("Runtime evidence needs review")
            root = refs[0]["record_id"]
            receipt = {"source_id": source_id, "record_id": root}
            work_id = event.get("work_id")
            if artifact:
                aliases = [artifact["sha256"], artifact.get("members_sha256")]
                for alias in filter(None, aliases):
                    matched = conn.execute("SELECT work_id FROM mind_artifact_aliases WHERE scope=? AND fingerprint=?", (self.scope.key(), alias)).fetchone()
                    if matched:
                        work_id = matched[0]
                        break
                work_id = work_id or self._id("work", event.get("task_id") or artifact.get("members_sha256") or artifact["sha256"])
                try:
                    work = self._get(conn, work_id)
                except Missing:
                    work = {"id": work_id, "kind": "work", "title": artifact["name"], "versions": [], "event_ids": [], "source_ids": [], "record_ids": [], "task_ids": [], "created_by": "unknown"}
                version_id = self._id("artifact", artifact["sha256"])
                version = self._put(conn, {"id": version_id, "kind": "artifact", "work_id": work_id, **artifact, "basis": "observed", "source_ids": list(dict.fromkeys([*self._optional_sources(conn, version_id), source_id]))})
                work["versions"] = list(dict.fromkeys([*work["versions"], version_id]))
                work["event_ids"] = list(dict.fromkeys([*work["event_ids"], event_id]))
                work["source_ids"] = list(dict.fromkeys([*work["source_ids"], source_id]))
                work["record_ids"] = list(dict.fromkeys([*work["record_ids"], root]))
                if event.get("task_id"):
                    work["task_ids"] = list(dict.fromkeys([*work["task_ids"], event["task_id"]]))
                if kind == "artifact-created":
                    if refs[0]["authority"] != "operation":
                        raise Conflict("Creator provenance requires a host operation receipt")
                    work["created_by"] = event.get("actor", "Kin")
                for ref in self.mind._evidence(conn, event.get("input_source_ids", [])):
                    work["source_ids"] = list(dict.fromkeys([*work["source_ids"], ref["source_id"]]))
                    work["record_ids"] = list(dict.fromkeys([*work["record_ids"], ref["record_id"]]))
                    self.engine._relation(conn, root, "follows", ref["record_id"], {"basis": "observed", "runtime_event_id": event_id})
                work["last_event"] = kind
                work["last_at"] = max(work.get("last_at", event["at"]), event["at"])
                self._put(conn, work)
                for alias in filter(None, aliases):
                    conn.execute("INSERT OR IGNORE INTO mind_artifact_aliases VALUES(?,?,?)", (self.scope.key(), alias, work_id))
                receipt.update(work_id=work_id, artifact_id=version["id"])
            if kind == "delivery":
                share_id = self._id("share", [event.get("channel"), event.get("delivery_id") or event["id"]])
                try:
                    share = self._get(conn, share_id)
                except Missing:
                    share = {"id": share_id, "kind": "share", "bubbles": {}, "source_ids": [], "record_ids": [], "about_ids": [], "semantic_state": "pending", "first_at": event["at"]}
                bubble = event.get("bubble_id") or event.get("delivery_id") or event["id"]
                prior = share["bubbles"].get(bubble, {})
                content = event.get("text", "")
                if prior.get("text", content) != content:
                    raise Conflict("Bubble content changed under the same ID")
                state = event.get("state", "prepared")
                if state not in {"prepared", "pending", "unconfirmed", "accepted", "canceled"}:
                    raise ValueError("Invalid delivery state")
                if prior.get("state") != "accepted":
                    share["bubbles"][bubble] = {"id": bubble, "text": content, "state": state, "message_id": event.get("message_id"), "at": event["at"], "artifact_id": receipt.get("artifact_id")}
                elif event.get("message_id") and prior["message_id"] != event["message_id"]:
                    raise Conflict("Accepted bubble cannot change platform ID")
                states = {b["state"] for b in share["bubbles"].values()}
                share.update(channel=event.get("channel"), delivery_id=event.get("delivery_id"), visibility="unverified",
                             last_at=max(share.get("last_at", event["at"]), event["at"]),
                             expected_bubbles=event.get("expected_bubbles", share.get("expected_bubbles")),
                             source_ids=list(dict.fromkeys([*share["source_ids"], source_id])), record_ids=list(dict.fromkeys([*share["record_ids"], root])))
                complete = not share["expected_bubbles"] or len(share["bubbles"]) >= share["expected_bubbles"]
                share["state"] = "accepted" if states == {"accepted"} and complete else "partial" if "accepted" in states else "unconfirmed" if "unconfirmed" in states else "prepared"
                if work_id:
                    share["about_ids"] = list(dict.fromkeys([*share["about_ids"], work_id]))
                self._put(conn, share)
                receipt["share_id"] = share_id
            cursor = conn.execute("INSERT INTO mind_runtime_events(id,scope,kind,occurred_at,digest,data) VALUES(?,?,?,?,?,?)",
                                  (event_id, self.scope.key(), kind, event["at"], fingerprint, dumps({**event, "artifact": artifact, "source_id": source_id, "receipt": receipt})))
            return {"id": event_id, "seq": cursor.lastrowid, "state": "recorded", **receipt}

    def _optional_sources(self, conn, identifier):
        try:
            return self._get(conn, identifier).get("source_ids", [])
        except Missing:
            return []

    def _fresh(self, conn, node):
        try:
            return bool(node.get("source_ids")) and self.mind._fresh(conn, self.mind._evidence(conn, node["source_ids"]))
        except (Missing, Conflict):
            return False

    def history(self, kind, *, query="", identifier=None, cursor=0, limit=20, include_history=False):
        if kind not in {"share", "work", "artifact"} or not 1 <= limit <= 100 or int(cursor) < 0:
            raise ValueError("Invalid history query")
        with self.engine.db.connect() as conn:
            if identifier:
                node = self._get(conn, identifier)
                # Linked records can be read through either history interface.
                nodes = [node]
                for linked in node.get("versions", []) + node.get("about_ids", []):
                    try:
                        nodes.append(self._get(conn, linked))
                    except Missing:
                        pass
            else:
                params = [self.scope.key(), kind]
                if query:
                    words = list(dict.fromkeys(tokenize(query).split()))[:40]
                    match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
                    if match:
                        base = "FROM mind_memory_nodes n JOIN mind_memory_search f ON f.id=n.id WHERE n.scope=? AND n.kind=? AND mind_memory_search MATCH ?"
                        params.append(match)
                        order = "ORDER BY bm25(mind_memory_search),COALESCE(json_extract(n.data,'$.last_at'),n.updated_at) DESC,n.id"
                    else:
                        return {"items": [], "cursor": None, "total": 0}
                else:
                    base = "FROM mind_memory_nodes n WHERE n.scope=? AND n.kind=?"
                    order = "ORDER BY COALESCE(json_extract(n.data,'$.last_at'),n.updated_at) DESC,n.id"
                total = conn.execute("SELECT count(*) " + base, params).fetchone()[0]
                rows = conn.execute("SELECT n.data " + base + " " + order + " LIMIT ? OFFSET ?", params + [limit, int(cursor)]).fetchall()
                nodes = [json.loads(r[0]) for r in rows]
            for node in nodes:
                node["needs_review"] = not self._fresh(conn, node)
                node["instruction_authority"] = "data"
                if include_history and identifier:
                    node["history"] = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_memory_revisions WHERE id=? ORDER BY revision", (node["id"],))]
        if identifier:
            total, nodes = len(nodes), nodes[int(cursor):int(cursor) + limit]
        return {"items": nodes, "cursor": int(cursor) + limit if total > int(cursor) + limit else None, "total": total}

    def semantic_context(self, query="", event_limit=24):
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT * FROM mind_semantic_cursor WHERE scope=?", (self.scope.key(),)).fetchone()
            cursor = dict(row) if row else {"seq": 0, "next_review": self.mind.clock(), "revision": 0, "data": "{}"}
            pending = conn.execute("SELECT seq,data FROM mind_runtime_events WHERE scope=? AND seq>? AND COALESCE(json_extract(data,'$.historical'),0)=0 ORDER BY seq LIMIT ?", (self.scope.key(), cursor["seq"], event_limit)).fetchall()
            recent = conn.execute("SELECT seq,data FROM mind_runtime_events WHERE scope=? AND kind IN ('owner-message','assistant-message','delivery') ORDER BY occurred_at DESC,seq DESC LIMIT 16", (self.scope.key(),)).fetchall()
            latest_owner_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM mind_runtime_events WHERE scope=? AND kind='owner-message' AND COALESCE(json_extract(data,'$.historical'),0)=0", (self.scope.key(),)).fetchone()[0]
        if not query:
            owner_messages = [json.loads(r["data"]) for r in recent if json.loads(r["data"]).get("kind") == "owner-message"]
            query = " ".join(e.get("text", "") for e in owner_messages[:2])
        return {"cursor": cursor["seq"], "through_seq": pending[-1]["seq"] if pending else cursor["seq"],
                "pending_events": [{"seq": r["seq"], **json.loads(r["data"])} for r in pending],
                "recent_interaction": [json.loads(r["data"]) for r in reversed(recent)],
                "works": self.history("work", query=query, limit=3)["items"],
                "shares": self.history("share", query=query, limit=12)["items"],
                "next_review": cursor["next_review"], "revision": cursor["revision"], "latest_owner_seq": latest_owner_seq}

    def queue_history(self, jobs, agent_version):
        """A resumable low-priority semantic pass. Old evidence cannot create a
        new emotion or wish; current user events always run first."""
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT * FROM mind_memory_migrations WHERE scope=? AND name='semantic'", (self.scope.key(),)).fetchone()
            cursor, data = (row["cursor"], json.loads(row["data"])) if row else (0, {})
        if data.get("job_id"):
            job = jobs.status(data["job_id"])
            if job["state"] != "complete":
                return {"state": "pending", "job_id": data["job_id"]}
            cursor = data["through_seq"]
        with self.engine.db.connect() as conn:
            rows = conn.execute("SELECT seq,data FROM mind_runtime_events WHERE scope=? AND seq>? AND json_extract(data,'$.historical')=1 ORDER BY seq LIMIT 16", (self.scope.key(), cursor)).fetchall()
        if not rows:
            return {"state": "complete", "cursor": cursor}
        sources = list(dict.fromkeys(json.loads(r["data"])["source_id"] for r in rows))
        receipt = jobs.enqueue(sources, agent_version, origin="reflection", stimulus="memory-backfill")
        data = {"job_id": receipt["id"], "through_seq": rows[-1]["seq"]}
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)", (self.scope.key(), "semantic", cursor, dumps(data)))
        return {"state": "pending", **data}

    def due(self):
        config = self.settings()
        if not config["idle"] or not config["semantic"]:
            return None
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT * FROM mind_semantic_cursor WHERE scope=?", (self.scope.key(),)).fetchone()
        return dict(row) if row and timestamp(row["next_review"]) <= timestamp(self.mind.clock()) else None

    def queue_idle(self, actions):
        due = self.due()
        if not due:
            return None
        with self.engine.db.connect(write=True) as conn:
            state = self.mind._load(conn)
            refs = state.get("action_policy", {}).get("evidence", [])
            if not refs or not self.mind._fresh(conn, refs):
                return None
            return actions.emit(conn, "idle-review", due["next_review"], {
                "evidence_ids": [r["record_id"] for r in refs], "agent_version": state["agent_version"],
                "reason": "Reconsider current interests and motives without inventing an owner message", "due_at": due["next_review"]})

    def apply_assessment(self, conn, assessment, refs, event_id, through_seq, next_minutes, receipt, *, schedule=True):
        """Called inside the same transaction as affect/concerns/wishes."""
        allowed_sources = {r["source_id"] for r in refs}
        allowed_records = {r["record_id"] for r in refs}
        def evidence(ids):
            selected = self.mind._evidence(conn, ids)
            if any(r["source_id"] not in allowed_sources and r["record_id"] not in allowed_records for r in selected) or not self.mind._fresh(conn, selected):
                raise Conflict("Semantic evidence is outside the evaluated source set")
            return selected
        for note in assessment.notes:
            sources = evidence(note.evidence_ids)
            node_id = "mem_" + digest([self.scope.key(), event_id, note.key])[:32]
            record = self.engine._insert(conn, RecordInput(id=node_id, scope=self.scope, kind=note.kind,
                title=note.title, content=note.content, source_ids=sorted({r["source_id"] for r in sources}),
                evidence_ids=sorted({r["record_id"] for r in sources}), generated=True, confirmation="inferred",
                attributes={"semantic_event": event_id, "key": note.key, "model": receipt.get("model"), "about_ids": note.about_ids}))
            for identifier in note.about_ids:
                for root in self._record_ids(conn, identifier):
                    self.engine._relation(conn, record["id"], "about", root, {"basis": "inferred", "event_id": event_id})
        for link in assessment.links:
            evidence(link.evidence_ids)
            for left in self._record_ids(conn, link.subject):
                for right in self._record_ids(conn, link.object):
                    if left != right:
                        self.engine._relation(conn, left, link.relation, right, {"basis": "inferred", "event_id": event_id, "evidence_ids": link.evidence_ids})
        for proposal in assessment.disclosures:
            share = self._get(conn, proposal.share_id)
            if share["kind"] != "share" or not self._fresh(conn, share):
                raise Conflict("Disclosure needs current delivery evidence")
            for identifier in proposal.about_ids + proposal.previous_share_ids:
                self._record_ids(conn, identifier)
            topic_id = self._id("topic", proposal.topic.strip().casefold())
            try:
                topic = self._get(conn, topic_id)
            except Missing:
                topic = {"id": topic_id, "kind": "topic", "title": proposal.topic, "source_ids": [], "record_ids": []}
            topic["source_ids"] = list(dict.fromkeys(topic["source_ids"] + share["source_ids"]))
            topic["record_ids"] = list(dict.fromkeys(topic["record_ids"] + share["record_ids"]))
            self._put(conn, topic)
            share.update(semantic_state="assessed", topic=proposal.topic, topic_id=topic_id, summary=proposal.summary,
                         mode=proposal.mode, previous_share_ids=proposal.previous_share_ids,
                         about_ids=list(dict.fromkeys([*share.get("about_ids", []), *proposal.about_ids])),
                         assessment_event=event_id, assessment_receipt=receipt)
            self._put(conn, share)
        for ref in refs:
            conn.execute("INSERT OR IGNORE INTO mind_semantic_sources VALUES(?,?,?)", (self.scope.key(), ref["source_id"], event_id))
        if not schedule:
            return
        config = self.settings(conn)
        minutes = max(config["review_min_minutes"], min(config["review_max_minutes"], next_minutes))
        next_at = (timestamp(self.mind.clock()) + timedelta(minutes=minutes)).isoformat()
        conn.execute("INSERT INTO mind_semantic_cursor(scope,seq,next_review,revision,data) VALUES(?,?,?,1,?) ON CONFLICT(scope) DO UPDATE SET seq=MAX(seq,excluded.seq),next_review=excluded.next_review,revision=revision+1,data=excluded.data",
                     (self.scope.key(), through_seq, next_at, dumps({"event_id": event_id, "receipt": receipt, "minutes": minutes})))

    def _record_ids(self, conn, identifier):
        if identifier.startswith("src_"):
            refs = self.mind._evidence(conn, [identifier])
            if not self.mind._fresh(conn, refs):
                raise Conflict("Linked source needs review")
            return [r["record_id"] for r in refs]
        if identifier.startswith("mem_"):
            record = self.engine._get(conn, identifier)
            if record["scope"] != self.scope.model_dump():
                raise Conflict("Cross-scope reference")
            return [identifier]
        node = self._get(conn, identifier)
        if not self._fresh(conn, node):
            raise Conflict("Linked memory needs review")
        return node.get("record_ids", []) or [r["record_id"] for r in self.mind._evidence(conn, node["source_ids"])]

    def access(self, session, identifier, revision, depth):
        if depth not in {"index", "summary", "original", "used", "shared"}:
            raise ValueError("Unknown access depth")
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO mind_memory_access VALUES(?,?,?,?,?,?)",
                         (self.scope.key(), session, identifier, revision, depth, self.mind.clock()))

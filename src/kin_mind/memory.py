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

from . import memory_items
from .autonomy_schema import optimized
from .computer import redact
from .dialogue import recent_dialogue
from .graph import EventGraph, GraphAssessment, query_terms
from .habits import ConversationHabits
from .sharing import CoverageAssessment, ShareLedger
from .state import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_memory_config(scope TEXT PRIMARY KEY,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_runtime_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,
 kind TEXT NOT NULL,occurred_at TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_runtime_scope ON mind_runtime_events(scope,seq);
CREATE INDEX IF NOT EXISTS mind_runtime_time ON mind_runtime_events(scope,julianday(occurred_at),seq);
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
CREATE TABLE IF NOT EXISTS mind_action_schedule(
 scope TEXT PRIMARY KEY,next_review TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_memory_access(
 scope TEXT NOT NULL,session TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 depth TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(scope,session,id,revision,depth));
CREATE TABLE IF NOT EXISTS mind_memory_migrations(
 scope TEXT NOT NULL,name TEXT NOT NULL,cursor INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,name));
"""

DEFAULTS = {"records": False, "semantic": False, "context": False, "idle": False, "operational_lanes": False,
            "manifests": False, "manifest_restore": False, "context_receipts": False, "continuity_overviews": False, "continuity_quality": False,
            "sharing": False, "graph": False, "associations": False, "graph_recall": False,
            "event_lifecycle": False, "adaptive_recall": False, "auto_volumes": False,
            "temperature_shadow": False, "temperature_ranking": False,
            "temperature_shadow_started_at": None, "temperature_validation": None,
            "semantic_actions": False, "autonomous_plans": False, "creative_execution": False,
            # Off by default: the fixed score gates that used to decide whether to speak or explore.
            # A score is context, so only an explicit opt-in brings them back. The stored thresholds
            # are left as they are, so switching this on restores the previous behaviour exactly.
            "legacy_drive_thresholds": False,
            # Default-on optimization: an unchanged wait is recorded without a plan revision.
            "plan_review_record_only": True,
            # Default-on: one refused proposal section no longer fails the whole appraisal.
            "appraisal_section_isolation": True,
            # Stage 2, registered once here and read through autonomy_schema.optimized():
            # each one off restores the stage-1 behavior of its work package.
            "attempt_ledger": True, "idempotency_fingerprint": True, "manifest_rebase": True,
            "appraisal_reuse": True, "appraisal_revalidation": True, "model_lanes": True,
            "semantic_cache_v2": True, "memory_item_isolation": True,
            # Stage 3 WP B3: a long reply is reviewed in chunks, an interrupted group's unsent
            # remainder can be reviewed again, and an earlier tail settles through this call.
            # Off restores the stage-2 limits, which refuse such a group for ever.
            "chunked_reply_review": True,
            # Stage 3: purpose-typed recall. Off restores `history` admitting self-knowledge, the
            # prefix-only envelope filters, relation seeds taken before validation and bare labels.
            "recall_purpose_policy": True,
            # Registered once here and read through autonomy_schema.optimized(). Each one off takes
            # its sections out of the appraisal schema and prompt and blanks them before validation,
            # which is the previous behavior exactly. The last two carry no appraisal section.
            "trait_ledger": True, "behavior_chain": True, "expression_intent": True,
            "next_move_audit": True, "wish_version_review": True, "rest_review_window": True,
            # Stage 5, read through autonomy_schema.optimized(). `evidence_key_index` off takes the
            # evidence key table out of the dedupe guard and leaves only the scan of the snapshots,
            # which is the previous behavior exactly. `history_legacy_guard` off stops paying for
            # that scan, and belongs only to a store whose snapshots no longer carry the key: while
            # both are on the guard refuses on either answer and records any disagreement.
            "evidence_key_index": True, "history_legacy_guard": True,
            # Stage 5: a recovery command asks the lease ledger and the process table instead of
            # believing its caller's `workers_stopped`. Off restores that boolean as the only
            # check, which is the stage-4 behaviour of all four recovery commands exactly.
            "liveness_checks": True,
            # Stage 5, and the one flag of it read through autonomy_schema.enabled(): off unless
            # a store says otherwise. Every other flag here switches how something runs, so
            # defaulting it on costs nothing but speed if it is wrong. This one switches what a
            # revision is written as, and the release that deploys it has to stay a release the
            # host can go back to — which it stops being the moment the first patch row is
            # committed. So the code ships off, and turning it on is its own decision, taken once
            # the history reads clean. Off, `_history` writes exactly the row it wrote before.
            "history_patches": False,
            # Stage 5 housekeeping, all three off and all three read through
            # autonomy_schema.enabled(). `context_cache_sweep` is the only hard delete in the
            # programme: it removes rows of `mind_context_cache`, which hold compressed context
            # and nothing else, and a removed row costs one model call to build again. It stays
            # off until an owner has been told exactly that and has agreed to it, and off the
            # cache keeps every row and an erase leaves the compressed copies behind, as today.
            # `metrics_name_ring` gives the telemetry table a ring per name instead of one
            # shared ring; off is the shared ring, unchanged. `vector_optimize` only unlocks a
            # command, which still refuses unless the store is provably quiet and still writes
            # nothing without `--apply`.
            "context_cache_sweep": False, "metrics_name_ring": False, "vector_optimize": False,
            "usage_reinforcement": False, "reinforcement_ranking": False, "procedure_learning": False,
            "reinforcement_started_at": None, "reinforcement_validation": None,
            "version": "memory-continuity-v1", "review_min_minutes": 20,
            "review_max_minutes": 120, "first_review_minutes": 20,
            # The ceiling while the rhythm rests or the owner's quiet hours run, so a night costs
            # one appraisal instead of one every two hours. Applied only there, and only with
            # `rest_review_window` on; everywhere else `review_max_minutes` still decides.
            "review_rest_max_minutes": 480,
            # What the owner is called, for the recall lane that looks for their own words. Empty
            # by default: with none configured that lane uses generic first/second-person words.
            "recall_owner_aliases": [],
            # Charged appraisal attempts before a job is quarantined for repair.
            "max_charged_attempts": 5}


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


from .lifecycle import EventRoute


class MemoryAssessment(Model):
    notes: list[MemoryNote] = Field(default_factory=list, max_length=8)
    links: list[MemoryLink] = Field(default_factory=list, max_length=16)
    disclosures: list[Disclosure] = Field(default_factory=list, max_length=12)
    graph: GraphAssessment = Field(default_factory=GraphAssessment)
    coverage: CoverageAssessment = Field(default_factory=CoverageAssessment)
    event_routes: list[EventRoute] = Field(default_factory=list, max_length=12)


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
        try:
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
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as error:
            # A split archive tail may have an EOCD but no local members.
            # Its byte hash still proves the delivered file; it cannot prove
            # member contents until the complete archive is available.
            result.update(member_status="unavailable", member_error=type(error).__name__)
            return result
        # Member names remain evidence, while the bag of member contents handles
        # renamed root folders and changed ZIP compression/timestamps.
        result.update(members=members, members_sha256=digest(sorted((x["sha256"], x["bytes"]) for x in members)))
    return result


class MemoryContinuity:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA + memory_items.SCHEMA)
        self.graph = EventGraph(mind)
        self.sharing = ShareLedger(mind)
        self.habits = ConversationHabits(mind)

    def settings(self, conn=None):
        if conn is None:
            with self.engine.db.connect() as connection:
                return self.settings(connection)
        row = conn.execute("SELECT data FROM mind_memory_config WHERE scope=?", (self.scope.key(),)).fetchone()
        return DEFAULTS | (json.loads(row[0]) if row else {})

    def configure(self, values):
        if set(values) - set(DEFAULTS):
            raise ValueError("Unknown memory setting")
        for key in ("records", "semantic", "context", "idle", "operational_lanes", "sharing", "graph", "associations", "graph_recall", "manifests", "manifest_restore", "context_receipts", "continuity_overviews", "continuity_quality", "event_lifecycle", "adaptive_recall", "auto_volumes", "temperature_shadow", "temperature_ranking", "semantic_actions", "autonomous_plans", "creative_execution", "usage_reinforcement", "reinforcement_ranking", "procedure_learning", "plan_review_record_only", "appraisal_section_isolation",
                    "attempt_ledger", "idempotency_fingerprint", "manifest_rebase", "appraisal_reuse",
                    "appraisal_revalidation", "model_lanes", "semantic_cache_v2", "memory_item_isolation",
                    "chunked_reply_review", "recall_purpose_policy",
                    "trait_ledger", "behavior_chain", "expression_intent", "next_move_audit",
                    "wish_version_review", "rest_review_window", "legacy_drive_thresholds",
                    "evidence_key_index", "history_legacy_guard", "liveness_checks",
                    "history_patches", "context_cache_sweep", "metrics_name_ring", "vector_optimize"):
            if key in values and type(values[key]) is not bool:
                raise ValueError("Feature flags are boolean")
        with self.engine.db.connect(write=True) as conn:
            previous = self.settings(conn)
            config = previous | values
            if config["usage_reinforcement"] and not previous["usage_reinforcement"]:
                config["reinforcement_started_at"] = self.mind.clock()
                config["reinforcement_validation"] = None
            if config["reinforcement_ranking"]:
                from .reinforcement import validate_enable
                validate_enable(conn, self.scope.key(), config, self.mind.clock())
            if config["temperature_shadow"] and (not previous["temperature_shadow"] or not config["temperature_shadow_started_at"]):
                config["temperature_shadow_started_at"] = self.mind.clock()
                config["temperature_validation"] = None
            if config["temperature_ranking"]:
                from .lifecycle import observation_days
                started, validation = config["temperature_shadow_started_at"], config["temperature_validation"] or {}
                if (not config["temperature_shadow"] or not started or
                    timestamp(self.mind.clock()) - timestamp(started) < timedelta(days=7) or
                    observation_days(conn, self.scope.key(), started, self.mind.clock()) < 7):
                    raise Conflict("Cooling needs seven full days of shadow observation")
                if (validation.get("critical_recall") != 1 or validation.get("recall_at_8", 0) < .9 or
                    any(validation.get(k) != 0 for k in ("wrong_merges", "unsupported_upgrades", "stale_facts", "background_reinforcement")) or
                    not validation.get("evaluated_at") or timestamp(validation["evaluated_at"]) < timestamp(started) + timedelta(days=7)):
                    raise Conflict("Cooling needs a current successful replay validation")
            if not 20 <= config["review_min_minutes"] <= config["first_review_minutes"] <= config["review_max_minutes"] <= 120:
                raise ValueError("Review range must be within 20..120 minutes")
            if (type(config["review_rest_max_minutes"]) is not int
                    or not config["review_max_minutes"] <= config["review_rest_max_minutes"] <= 720):
                raise ValueError("The resting review ceiling must be between the ordinary one and 720 minutes")
            aliases = config["recall_owner_aliases"]
            if (type(aliases) is not list or len(aliases) > 8
                    or any(type(a) is not str or not a.strip() or len(a) > 40 for a in aliases)):
                raise ValueError("Owner aliases are up to eight short names")
            if type(config["max_charged_attempts"]) is not int or not 1 <= config["max_charged_attempts"] <= 20:
                raise ValueError("Charged appraisal attempts must be between 1 and 20")
            conn.execute("INSERT OR REPLACE INTO mind_memory_config VALUES(?,?)", (self.scope.key(), dumps(config)))
            if values.get("event_lifecycle") is False:
                conn.execute("DELETE FROM mind_foreground_leases WHERE scope=?", (self.scope.key(),))
            for flag, kind in (("event_lifecycle", "event_digest"), ("event_lifecycle", "lifecycle_backfill"), ("auto_volumes", "lifecycle_volumes"), ("temperature_shadow", "lifecycle_temperature")):
                if values.get(flag):
                    conn.execute("UPDATE jobs SET state='pending',error=NULL WHERE kind=? AND state='waiting_config' "
                                 "AND error=? AND json_extract(payload,'$.scope')=json(?)",
                                 (kind, "Lifecycle feature disabled: " + flag, self.scope.key()))
            due = (timestamp(self.mind.clock()) + timedelta(minutes=config["first_review_minutes"])).isoformat()
            conn.execute("INSERT OR IGNORE INTO mind_semantic_cursor(scope,next_review,data) VALUES(?,?,?)",
                         (self.scope.key(), due, "{}"))
            conn.execute("INSERT OR IGNORE INTO mind_action_schedule(scope,next_review,data) VALUES(?,?,?)",
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
        if self.settings(conn)["graph"] or self.settings(conn)["sharing"]:
            self.graph.project_memory(conn, node)
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
        # Receipt metadata is recorded on the first commit. A delivery retry
        # may observe a later receipt time without changing the original event.
        fingerprint = digest({k:v for k,v in event.items() if k not in {"received_at", "time_basis"}})
        def same_observation(old):
            if old["digest"] == fingerprint:
                return True
            # Older hosts included receipt metadata in the immutable digest.
            stored, original = json.loads(old["data"]), dict(event)
            for key in ("received_at", "time_basis"):
                if key in stored:
                    original[key] = stored[key]
                else:
                    original.pop(key, None)
            return old["digest"] == digest(original)
        with self.engine.db.connect() as conn:
            old = conn.execute("SELECT * FROM mind_runtime_events WHERE id=?", (event_id,)).fetchone()
            if old:
                if not same_observation(old):
                    raise Conflict("Runtime event id belongs to different data")
                return {"id": event_id, "seq": old["seq"], "state": "recorded", **json.loads(old["data"])["receipt"]}
        artifact = dict(event.get("artifact") or {})
        if artifact.get("path"):
            observed = fingerprint_file(artifact["path"])
            if artifact.get("sha256") and artifact["sha256"] != observed["sha256"]:
                raise Conflict("Observed artifact version changed before ingestion",
                               expected=artifact["sha256"], actual=observed["sha256"])
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
                if not same_observation(old):
                    raise Conflict("Conflicting runtime event")
                return {"id": event_id, "seq": old["seq"], "state": "recorded", **json.loads(old["data"])["receipt"]}
            refs = self.mind._evidence(conn, [source_id])
            if not self.mind._fresh(conn, refs):
                raise Conflict("Runtime evidence needs review")
            root = refs[0]["record_id"]
            receipt = {"source_id": source_id, "record_id": root}
            if kind == "owner-message":
                conn.execute("INSERT OR IGNORE INTO mind_reply_inputs VALUES(?,?,?,?)", (self.scope.key(), event["id"], source_id, event["at"]))
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
                    # The bubble's identity only; the two bodies stay where they are.
                    raise Conflict("Bubble content changed under the same ID", target=bubble)
                state = event.get("state", "prepared")
                if state not in {"prepared", "pending", "unconfirmed", "accepted", "canceled"}:
                    raise ValueError("Invalid delivery state")
                if prior.get("state") != "accepted":
                    share["bubbles"][bubble] = {"id": bubble, "text": content, "state": state, "message_id": event.get("message_id"), "at": event["at"], "artifact_id": receipt.get("artifact_id"),
                        "references": event.get("references", prior.get("references", [])), "draft_id": event.get("draft_id")}
                elif event.get("message_id") and prior["message_id"] != event["message_id"]:
                    raise Conflict("Accepted bubble cannot change platform ID")
                states = {b["state"] for b in share["bubbles"].values()}
                share.update(channel=event.get("channel"), delivery_id=event.get("delivery_id"), visibility="unverified",
                             last_at=max(share.get("last_at", event["at"]), event["at"]),
                             expected_bubbles=event.get("expected_bubbles", share.get("expected_bubbles")),
                             source_ids=list(dict.fromkeys([*share["source_ids"], source_id])), record_ids=list(dict.fromkeys([*share["record_ids"], root])))
                complete = not share["expected_bubbles"] or len(share["bubbles"]) >= share["expected_bubbles"]
                share["state"] = "accepted" if states == {"accepted"} and complete else "partial" if "accepted" in states else "unconfirmed" if "unconfirmed" in states else "canceled" if states == {"canceled"} else "prepared"
                if work_id:
                    share["about_ids"] = list(dict.fromkeys([*share["about_ids"], work_id]))
                self._put(conn, share)
                receipt["share_id"] = share_id
                if self.settings(conn)["sharing"]:
                    self.sharing.settle(conn, share)
            if self.settings(conn)["graph"]:
                receipt["event_id"] = self.graph.runtime(conn, event, receipt, event_id)["id"]
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
                    words = query_terms(query)
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
                if self.settings(conn)["sharing"]:
                    if node["kind"] == "share":
                        node["content_references"] = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_share_coverage WHERE scope=? AND share_id=? ORDER BY at DESC LIMIT 40",(self.scope.key(),node["id"]))]
                    elif node["kind"] in {"work","artifact"}:
                        try:
                            node["share_coverage"] = self.sharing.coverage(conn, node["id"])
                        except Missing:
                            pass
                if include_history and identifier:
                    node["history"] = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_memory_revisions WHERE id=? ORDER BY revision", (node["id"],))]
        if identifier:
            total, nodes = len(nodes), nodes[int(cursor):int(cursor) + limit]
        return {"items": nodes, "cursor": int(cursor) + limit if total > int(cursor) + limit else None, "total": total}

    def semantic_context(self, query="", event_limit=24, *, operational=False):
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT * FROM mind_semantic_cursor WHERE scope=?", (self.scope.key(),)).fetchone()
            cursor = dict(row) if row else {"seq": 0, "next_review": self.mind.clock(), "revision": 0, "data": "{}"}
            pending = conn.execute("SELECT seq,data FROM mind_runtime_events WHERE scope=? AND seq>? AND COALESCE(json_extract(data,'$.historical'),0)=0 ORDER BY seq LIMIT ?", (self.scope.key(), cursor["seq"], event_limit)).fetchall()
            latest_owner_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM mind_runtime_events WHERE scope=? AND kind='owner-message' AND COALESCE(json_extract(data,'$.historical'),0)=0", (self.scope.key(),)).fetchone()[0]
        recent = recent_dialogue(self.mind)
        if not query:
            owner_messages = [e for e in recent if e.get("kind") == "owner-message"]
            query = " ".join(e.get("text", "") for e in owner_messages[-2:])
        from eventmem.core.read_policy import ReadPolicy
        with self.engine.db.connect() as conn:
            # Everything an appraisal is shown is evidence of what happened, so it is selected
            # and labelled by one experience read, families and identity evidence included.
            policy = ReadPolicy.load(self.engine, self.scope, "experience_recall", conn=conn)
            graph_context = self.graph.candidates(conn, query, policy=policy) if not operational and (self.settings(conn)["graph"] or self.settings(conn)["sharing"]) else []
            for node in graph_context:
                node["needs_review"] = not self.graph.fresh(conn, node)
                if self.settings(conn)["event_lifecycle"] and node["kind"] == "event" and not node["needs_review"]:
                    from .lifecycle import EventLifecycle
                    from .adaptive_recall import evidence_excerpt
                    members = EventLifecycle(self.mind, self.graph).snapshot(conn, node["id"], policy=policy)["records"]
                    node["identity_evidence"] = [{"id": r["id"], "revision": r["revision"],
                        "basis": policy.basis(r), "occurred_at": r["valid_from"],
                        "text": evidence_excerpt(r["content"], query)[0], "excerpt_only": evidence_excerpt(r["content"], query)[1]}
                        for r in sorted(members.values(), key=lambda r: r["valid_from"], reverse=True)[:2]]
                if node["kind"] in {"finding", "exploration", "work"}:
                    node["share_coverage"] = self.sharing.coverage(conn, node["id"])
            topic_candidates = []
            if self.settings(conn)["auto_volumes"] and graph_context:
                from .adaptive_recall import evidence_excerpt
                anchors = list(dict.fromkeys(rid for n in graph_context for rid in [
                    *n.get("record_ids", []), *[r["record_id"] for r in n.get("evidence", [])],
                    *[r["id"] for r in n.get("identity_evidence", [])]]))[:80]
                if anchors:
                    slots = ",".join("?" for _ in anchors)
                    families = conn.execute("SELECT data FROM families WHERE scope=? AND kind='family' AND state='candidate' "
                        f"AND EXISTS (SELECT 1 FROM json_each(families.data,'$.members') WHERE value IN ({slots})) "
                        "ORDER BY revision DESC,id LIMIT 4", [self.scope.key(), *anchors]).fetchall()
                    for row in families:
                        family = json.loads(row[0])
                        members = []
                        for rid in family["members"][:12]:
                            try:
                                refs = self.mind._evidence(conn, [rid])
                                if not self.mind._fresh(conn, refs):
                                    continue
                                record = self.engine._get(conn, rid)
                            except (Conflict, Missing):
                                continue
                            if not policy.visible(record):
                                continue
                            excerpt, partial = evidence_excerpt(record["content"], query, budget=250)
                            members.append({"id": rid, "revision": record["revision"], "basis": policy.basis(record),
                                "text": excerpt, "excerpt_only": partial, "occurred_at": record["valid_from"]})
                        if len(members) >= 2:
                            topic_candidates.append({"id": family["id"], "revision": family["revision"], "title": family["title"],
                                "candidate_only": True, "members": members, "omitted_count": len(family["members"]) - len(members)})
        return {"cursor": cursor["seq"], "through_seq": pending[-1]["seq"] if pending else cursor["seq"],
                "graph_candidates": graph_context,
                "topic_candidates": topic_candidates,
                "event_lifecycle_enabled": self.settings()["event_lifecycle"],
                "conversation_habits": self.habits.read(),
                "pending_events": [{"seq": r["seq"], **json.loads(r["data"])} for r in pending],
                "recent_interaction": recent,
                "works": [] if operational else self.history("work", query=query, limit=3)["items"],
                "shares": [] if operational else self.history("share", query=query, limit=12)["items"],
                "next_review": cursor["next_review"], "revision": cursor["revision"], "latest_owner_seq": latest_owner_seq}

    def queue_unorganized(self, jobs, agent_version, limit=16):
        """One more memory-only pass for the sources whose only carrier a commit had to drop.

        It runs as history, so nothing is scored again and no wish is made; this is what organises
        that experience later, in either lane mode. One job at a time. A source the pass withholds
        again, or whose job ended without organising it, is left for an operator, never retried for ever."""
        scope, at = self.scope.key(), self.mind.clock()
        with self.engine.db.connect() as conn:
            if not optimized(conn, scope, memory_items.SWITCH):
                return {"state": "disabled"}
            rows = memory_items.waiting(conn, scope)
        if not rows:
            return {"state": "idle"}
        ended = {}
        for job_id in dict.fromkeys(r["data"].get("job_id") for r in rows if r["state"] == "queued"):
            status = jobs.status(job_id) if job_id else None
            state = status["state"] if status else "missing"
            if state in {"pending", "running", "batched"}:
                return {"state": "pending", "job_id": job_id}
            ended[job_id] = state
        ready, unavailable = [], []
        with self.engine.db.connect() as conn:
            for row in [r for r in rows if r["state"] == "pending"][:limit]:
                try:
                    current = self.mind._fresh(conn, self.mind._evidence(conn, [row["source_id"]]))
                except (Missing, Conflict):
                    current = False
                (ready if current else unavailable).append(row["source_id"])
        receipt = None
        if ready:
            try:
                receipt = jobs.enqueue(ready, agent_version, origin="reflection", stimulus="memory-backfill")
            except (Missing, Conflict):
                # A source moved between the check and the enqueue: the next review looks again.
                ready = []
        reasons = {}
        with self.engine.db.connect(write=True) as conn:
            for row in rows:
                if row["state"] == "queued":
                    reason = "job-" + ended[row["data"].get("job_id")]
                    memory_items.mark(conn, scope, [row["source_id"]], "abandoned", at, reason=reason)
                    reasons[reason] = reasons.get(reason, 0) + 1
            memory_items.mark(conn, scope, unavailable, "abandoned", at, reason="source-unavailable")
            if receipt:
                memory_items.mark(conn, scope, ready, "queued", at, job_id=receipt["id"])
        if unavailable:
            reasons["source-unavailable"] = len(unavailable)
        if reasons:
            self.engine.db.metric("memory_sources_abandoned", sum(reasons.values()), {"reasons": reasons})
        return {"state": "queued", "job_id": receipt["id"], "sources": len(ready)} if receipt else {"state": "idle"}

    def queue_history(self, jobs, agent_version):
        """A resumable low-priority semantic pass. Old evidence cannot create a
        new emotion or wish; current user events always run first."""
        self.queue_unorganized(jobs, agent_version)
        with self.engine.db.connect() as conn:
            row = conn.execute("SELECT * FROM mind_memory_migrations WHERE scope=? AND name='semantic'", (self.scope.key(),)).fetchone()
            cursor, data = (row["cursor"], json.loads(row["data"])) if row else (0, {})
        if data.get("job_id"):
            job = jobs.status(data["job_id"])
            if job["state"] not in {"complete", "needs-repair"}:
                return {"state": "pending", "job_id": data["job_id"]}
            if job["state"] == "needs-repair":
                data.setdefault("deferred_repairs", []).append(data["job_id"])
            cursor = data["through_seq"]
        with self.engine.db.connect() as conn:
            rows = conn.execute("SELECT seq,data FROM mind_runtime_events WHERE scope=? AND seq>? AND json_extract(data,'$.historical')=1 ORDER BY seq LIMIT 16", (self.scope.key(), cursor)).fetchall()
        if not rows:
            return {"state": "complete", "cursor": cursor}
        sources = list(dict.fromkeys(json.loads(r["data"])["source_id"] for r in rows))
        receipt = jobs.enqueue(sources, agent_version, origin="reflection", stimulus="memory-backfill")
        data = {"job_id": receipt["id"], "through_seq": rows[-1]["seq"], "deferred_repairs": data.get("deferred_repairs", [])}
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)", (self.scope.key(), "semantic", cursor, dumps(data)))
        return {"state": "pending", **data}

    def due(self):
        config = self.settings()
        if not config["idle"] or not config["semantic"]:
            return None
        with self.engine.db.connect() as conn:
            table = "mind_action_schedule" if config["operational_lanes"] else "mind_semantic_cursor"
            row = conn.execute(f"SELECT * FROM {table} WHERE scope=?", (self.scope.key(),)).fetchone()
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

    def commit_action(self, conn, refs, event_id, next_minutes, receipt, *, max_minutes=None):
        """The action clock progresses even when historical enrichment cannot.

        `max_minutes`: the ceiling the request allowed, which is the resting one while the owner
        is asleep. Unset means the ordinary ceiling, exactly as before."""
        config = self.settings(conn)
        minutes = max(config["review_min_minutes"], min(max_minutes or config["review_max_minutes"], next_minutes))
        next_at = (timestamp(self.mind.clock()) + timedelta(minutes=minutes)).isoformat()
        conn.execute("INSERT INTO mind_action_schedule VALUES(?,?,1,?) ON CONFLICT(scope) DO UPDATE SET next_review=excluded.next_review,revision=revision+1,data=excluded.data",
                     (self.scope.key(), next_at, dumps({"event_id": event_id, "receipt": receipt, "minutes": minutes, "last_success": self.mind.clock()})))
        for ref in refs:
            conn.execute("INSERT OR IGNORE INTO mind_semantic_sources VALUES(?,?,?)", (self.scope.key(), ref["source_id"], event_id))

    def apply_assessment(self, conn, assessment, refs, event_id, through_seq, next_minutes, receipt, *, schedule=True, processed_refs=None, max_minutes=None):
        """Called inside the same transaction as affect/concerns/wishes.

        Returns what the host dropped item by item (memory_items), shaped like a refused section."""
        items = memory_items.Items(conn, optimized(conn, self.scope.key(), memory_items.SWITCH))
        items.run(lambda: self._apply_items(conn, items, assessment, refs, event_id, receipt))
        processed = refs if processed_refs is None else processed_refs
        # A source whose only carrier was dropped has not been organised: it stays out of the index
        # and waits in the ledger for its memory-only pass (queue_unorganized).
        withheld = items.withheld(conn, processed)
        for ref in processed:
            if ref["source_id"] not in withheld:
                conn.execute("INSERT OR IGNORE INTO mind_semantic_sources VALUES(?,?,?)", (self.scope.key(), ref["source_id"], event_id))
        abandoned = memory_items.settle(conn, self.scope.key(), [r["source_id"] for r in processed], withheld, event_id, self.mind.clock())
        items.metric(conn, self.mind.clock(), event_id, withheld, abandoned)
        if schedule:
            # The cursor follows the events this evaluation was given and scored, withheld or not:
            # held back, it would hand an already scored event to a full appraisal a second time.
            config = self.settings(conn)
            minutes = max(config["review_min_minutes"], min(max_minutes or config["review_max_minutes"], next_minutes))
            next_at = (timestamp(self.mind.clock()) + timedelta(minutes=minutes)).isoformat()
            conn.execute("INSERT INTO mind_semantic_cursor(scope,seq,next_review,revision,data) VALUES(?,?,?,1,?) ON CONFLICT(scope) DO UPDATE SET seq=MAX(seq,excluded.seq),next_review=excluded.next_review,revision=revision+1,data=excluded.data",
                         (self.scope.key(), through_seq, next_at, dumps({"event_id": event_id, "receipt": receipt, "minutes": minutes})))
        return items.records()

    def _apply_items(self, conn, items, assessment, refs, event_id, receipt):
        """One pass over every item of the section; memory_items repeats it when a cascade reaches back."""
        allowed_sources = {r["source_id"] for r in refs}
        allowed_records = {r["record_id"] for r in refs}
        def evidence(ids):
            # Two different faults used to share one message. Evidence this evaluation never
            # saw is the proposal's own fault and is blocked; cited evidence that has since
            # moved is a version, and the next attempt can be built on the current one.
            records = [record for identifier in ids for record in self._record_ids(conn, identifier)]
            selected = self.mind._evidence(conn, list(dict.fromkeys(records)))
            outside = next((r for r in selected if r["source_id"] not in allowed_sources and r["record_id"] not in allowed_records), None)
            if outside:
                raise Conflict("Semantic evidence is outside the evaluated source set", target=outside["record_id"])
            stale = next((r for r in selected if not self.mind._fresh(conn, [r])), None)
            if stale:
                raise Conflict("Cited semantic evidence is no longer current",
                               target=stale["record_id"], expected=stale["revision"])
            return selected
        note_aliases = {note.key: "mem_" + digest([self.scope.key(), event_id, note.key])[:32] for note in assessment.notes}
        if len(note_aliases) != len(assessment.notes):
            raise Conflict("Memory note keys must be unique in an assessment")
        config = self.settings(conn)
        graph = assessment.graph
        if not config["associations"]:
            graph = graph.model_copy(update={"nodes": [n for n in graph.nodes if n.kind != "association" and n.basis != "internal_thought"],
                "edges": [e for e in graph.edges if e.relation != "association" and e.basis != "internal_thought"]})
        graph_aliases = {n.key: n.id or self.graph.identifier(n.kind, [event_id, n.key]) for n in graph.nodes} if config["graph"] else {}
        if graph_aliases.keys() & note_aliases.keys():
            raise Conflict("Memory and graph keys must be unique in an assessment")
        aliases = {**note_aliases, **graph_aliases}
        resolve = lambda identifier: aliases.get(identifier, identifier)
        # Each item is applied whole or not at all (memory_items): its key is its place in the proposal, a note
        # introduces its key and the id that key stands for, and whatever names a dropped key goes with it.
        note_keys = memory_items.positions("notes", assessment.notes)
        for key, note in zip(note_keys, assessment.notes):
            with items.item("note", key, names=(note.key, aliases[note.key]), needs=note.about_ids, evidence=note.evidence_ids) as live:
                if not live:
                    continue
                sources = evidence(note.evidence_ids)
                node_id = aliases[note.key]
                self.engine._insert(conn, RecordInput(id=node_id, scope=self.scope, kind=note.kind,
                    title=note.title, content=note.content, source_ids=sorted({r["source_id"] for r in sources}),
                    evidence_ids=sorted({r["record_id"] for r in sources}), generated=True, confirmation="inferred",
                    attributes={"semantic_event": event_id, "key": note.key, "model": receipt.get("model"), "about_ids": [resolve(i) for i in note.about_ids]}))
                items.carry(key, {r["source_id"] for r in sources})
        # Materialize both kinds of nodes before resolving cross-kind links.
        # This remains inside the caller's transaction: a bad edge rolls back
        # notes and graph nodes together, including their revision history.
        if config["graph"]:
            self.graph.apply(conn, graph, refs, event_id, receipt, external_aliases=note_aliases, items=items,
                             keys=(memory_items.positions("graph.nodes", assessment.graph.nodes, graph.nodes),
                                   memory_items.positions("graph.edges", assessment.graph.edges, graph.edges)))
        if config["event_lifecycle"] and assessment.event_routes:
            from .lifecycle import EventLifecycle
            # The host issues these command ids itself, from the appraisal and the route key.
            # A later judgment that rewrites one of them is an explicit revision, not a client
            # reusing an id: it is validated again in full and keeps its before/after record.
            EventLifecycle(self.mind, self.graph).apply_routes(conn, assessment.event_routes, refs, event_id, aliases, revise=True, items=items)
        for key, note in zip(note_keys, assessment.notes):
            # The second part of the same item: refused here, the note inserted above goes too.
            with items.item("note", key) as live:
                if not live:
                    continue
                for identifier in note.about_ids:
                    for root in self._record_ids(conn, resolve(identifier)):
                        if aliases[note.key] != root:
                            self.engine._relation(conn, aliases[note.key], "about", root, {"basis": "inferred", "event_id": event_id})
        for key, link in zip(memory_items.positions("links", assessment.links), assessment.links):
            with items.item("link", key, needs=(link.subject, link.object)) as live:
                if not live:
                    continue
                evidence(link.evidence_ids)
                for left in self._record_ids(conn, resolve(link.subject)):
                    for right in self._record_ids(conn, resolve(link.object)):
                        if left != right:
                            self.engine._relation(conn, left, link.relation, right, {"basis": "inferred", "event_id": event_id, "evidence_ids": link.evidence_ids})
        for key, proposal in zip(memory_items.positions("disclosures", assessment.disclosures), assessment.disclosures):
            with items.item("disclosure", key, needs=proposal.about_ids) as live:
                if not live:
                    continue
                proposal = proposal.model_copy(update={"about_ids": [resolve(i) for i in proposal.about_ids]})
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
        if config["sharing"]:
            def evaluated(share_id):
                return any(r["source_id"] in allowed_sources for r in self.mind._evidence(conn, self._get(conn, share_id)["source_ids"]))
            # Isolated, each mapping answers for its own share inside its savepoint; otherwise one
            # lookup for all of them before any is applied, as before.
            allowed_shares = evaluated if items.enabled else {m.share_id for m in assessment.coverage.mappings if evaluated(m.share_id)}
            self.sharing.apply(conn, assessment.coverage, allowed_shares, items=items)

    def _record_ids(self, conn, identifier):
        if identifier.startswith(("graph_", "explore_")):
            node = self.graph.ensure(conn, identifier)
            if not self.graph.fresh(conn, node):
                raise Conflict("Graph reference needs review")
            return [r["record_id"] for r in node["evidence"]]
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

    def access(self, session, identifier, revision, depth, *, origin="automatic_injection", usage_id=None):
        if depth not in {"index", "summary", "original", "used", "shared"}:
            raise ValueError("Unknown access depth")
        with self.engine.db.connect(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO mind_memory_access VALUES(?,?,?,?,?,?)",
                         (self.scope.key(), session, identifier, revision, depth, self.mind.clock()))
            if depth != "index" and (self.settings(conn)["temperature_shadow"] or self.settings(conn)["usage_reinforcement"]):
                from .lifecycle import record_usage
                from .reinforcement import input_key
                record_usage(conn, self.scope.key(), identifier,
                             input_key(conn, self.scope.key(), usage_id) or digest([session, self.mind.clock()[:10]]),
                             origin, self.mind.clock(), {"depth": depth, "revision": revision})

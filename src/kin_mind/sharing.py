"""Finding-level disclosure coverage, independent of the appraisal queue."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from eventmem.core.db import Conflict, Missing, dumps
from eventmem.core.models import Model

from .graph import EventGraph

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_share_coverage(
 scope TEXT NOT NULL,recipient TEXT NOT NULL,unit_id TEXT NOT NULL,version INTEGER NOT NULL,
 bubble_id TEXT NOT NULL,share_id TEXT NOT NULL,state TEXT NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,recipient,unit_id,version,bubble_id));
CREATE TABLE IF NOT EXISTS mind_coverage_revisions(
 scope TEXT NOT NULL,unit_id TEXT NOT NULL,version INTEGER NOT NULL,bubble_id TEXT NOT NULL,
 revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,unit_id,version,bubble_id,revision));
CREATE INDEX IF NOT EXISTS mind_coverage_recent ON mind_share_coverage(scope,recipient,unit_id,version,at DESC);
CREATE INDEX IF NOT EXISTS mind_coverage_share ON mind_share_coverage(scope,share_id);
CREATE TABLE IF NOT EXISTS mind_share_reservations(
 scope TEXT NOT NULL,recipient TEXT NOT NULL,unit_id TEXT NOT NULL,version INTEGER NOT NULL,
 draft_id TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,recipient,unit_id,version));
CREATE TABLE IF NOT EXISTS mind_reply_references(
 scope TEXT NOT NULL,reply_id TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,reply_id));
CREATE INDEX IF NOT EXISTS mind_reply_body ON mind_reply_references(scope,digest);
"""


class ContentReference(Model):
    unit_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    activity_ref: dict | None = None
    mode: Literal["new", "development", "reflection", "reminiscence", "retelling", "duplicate"] = "new"
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def original(self):
        if not self.activity_ref and (not self.unit_id or self.version is None):
            raise ValueError("A content reference needs a finding version or an original activity")
        return self


class CoverageMapping(Model):
    share_id: str
    bubble_id: str
    references: list[ContentReference] = Field(max_length=12)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)


class CoverageAssessment(Model):
    mappings: list[CoverageMapping] = Field(default_factory=list, max_length=24)


class ShareLedger:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        self.graph = EventGraph(mind)
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA)

    def units(self, conn, owner_id, findings, source_ids, *, owner_kind="exploration", at=None):
        """Finding identities come from their saved source and position, never their text."""
        refs = self.graph.proof(conn, source_ids)
        result = []
        saved_units = [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM mind_graph_nodes WHERE scope=? AND kind='finding' AND json_extract(data,'$.owner_id')=? ORDER BY rowid",
            (self.scope.key(), owner_id))]
        saved_units = [unit for unit in saved_units if set(unit.get("source_ids", [])) == set(source_ids)]
        for index, content in enumerate(findings):
            if not isinstance(content, str) or not content.strip():
                continue
            uid = saved_units[index]["id"] if index < len(saved_units) else self.graph.identifier("finding", [owner_id, source_ids, index])
            try:
                existing_unit = self.graph.get(conn, uid)
                result.append(existing_unit)
                continue
            except Missing:
                pass
            unit = self.graph._put(conn, {"id": uid, "kind": "finding", "title": content.split("\n", 1)[0], "text": content,
                "owner_id": owner_id, "owner_kind": owner_kind, "content_version": 1, "source_ids": source_ids,
                "source_position": index,
                "evidence": refs, "basis": "documented" if owner_kind == "exploration" else "inferred", "occurred_at": at or self.mind.clock()})
            self.graph.link(conn, uid, "part_of", owner_id, refs, reason="Content unit in this result version")
            result.append(unit)
        return result

    def exploration(self, conn, identifier, data):
        result = data.get("result") or {}
        if not data.get("source_id") or not result.get("findings"):
            return []
        owner = self.graph.ensure(conn, identifier)
        row = conn.execute("SELECT created_at FROM mind_explorations WHERE scope=? AND id=?",(self.scope.key(),identifier)).fetchone()
        self.graph._put(conn,{**owner,"title":data.get("selected_brief") or data.get("topic") or owner["title"],"text":result.get("summary", ""),"occurred_at":owner["evidence"][0]["occurred_at"] if owner.get("evidence") else row["created_at"] if row else owner["occurred_at"]})
        units = self.units(conn, identifier, result["findings"], [data["source_id"]], at=data.get("completed_at"))
        from .activities import canonical_activity_ref
        activity = canonical_activity_ref(self.mind, "exploration", identifier, conn=conn)
        return [self.graph._put(conn, {**unit, "activity_ref": activity}) for unit in units]

    @staticmethod
    def activity_id(activity):
        return "activity:" + activity["kind"] + ":" + activity["id"]

    def valid_reference(self, conn, raw, *, historical=False):
        ref = ContentReference.model_validate(raw)
        if ref.activity_ref:
            from .activities import validate_activity_ref
            activity = validate_activity_ref(self.mind, ref.activity_ref, conn=conn, historical=historical)
            if activity["kind"] == "dream" or not ref.unit_id or ref.unit_id.startswith("activity:"):
                identifier = self.activity_id(activity)
                if ref.unit_id and ref.unit_id != identifier:
                    raise Conflict("Activity reference cannot name another content unit")
                return ref.model_copy(update={"unit_id": identifier, "version": activity["version"], "activity_ref": activity}), {
                    "id": identifier, "kind": "activity", "content_version": activity["version"],
                    "activity_ref": activity, "basis": "imagined" if activity["kind"] == "dream" else "documented", "evidence": False,
                    "needs_review": False}
        unit = self.graph.get(conn, ref.unit_id)
        if unit["kind"] != "finding":
            raise Conflict("Share reference must name a saved finding", target=ref.unit_id)
        if unit.get("state") == "deleted":
            raise Missing("Share source was deleted")
        for source_id in unit.get("source_ids", []):
            if not conn.execute("SELECT 1 FROM sources WHERE id=? AND scope=? AND deleted=0", (source_id, self.scope.key())).fetchone():
                raise Missing("Share source was deleted")
        ref = ref.model_copy(update={"version": unit.get("content_version", 1)})
        activity = ref.activity_ref or unit.get("activity_ref")
        if activity:
            from .activities import validate_activity_ref
            activity = validate_activity_ref(self.mind, activity, conn=conn, historical=historical)
            if activity["kind"] != "exploration" or activity["id"] != unit.get("owner_id"):
                raise Conflict("Finding does not belong to the referenced activity")
            ref = ref.model_copy(update={"activity_ref": activity})
        return ref, unit

    def reference_sources(self, conn, references, *, historical=False):
        """Original content dependencies for the parent text, without claiming psychological causes."""
        sources = set()
        for raw in references:
            ref, unit = self.valid_reference(conn, raw, historical=historical)
            sources.update(unit.get("source_ids", []))
            if ref.activity_ref:
                sources.add(ref.activity_ref["source_id"])
        return sorted(sources)

    def coverage(self, conn, identifier, *, recipient="owner"):
        if isinstance(identifier, dict):
            ref, unit = self.valid_reference(conn, {"activity_ref": identifier}, historical=True)
            identifier = ref.unit_id
        elif identifier.startswith("activity:"):
            from .activities import canonical_activity_ref
            _, kind, activity_id = identifier.split(":", 2)
            activity = canonical_activity_ref(self.mind, kind, activity_id, conn=conn)
            _, unit = self.valid_reference(conn, {"activity_ref": activity}, historical=True)
        else:
            unit = self.graph.get(conn, identifier)
        if unit["kind"] not in {"finding", "activity"}:
            rows = conn.execute("SELECT data FROM mind_graph_nodes WHERE scope=? AND kind='finding' AND json_extract(data,'$.owner_id')=? AND state='active' ORDER BY id", (self.scope.key(), identifier)).fetchall()
            children = [self.coverage(conn, json.loads(row[0])["id"], recipient=recipient) for row in rows]
            states = [c["state"] for c in children]
            state = "shared" if states and all(s == "shared" for s in states) else "partial" if "shared" in states or "partial" in states else "unconfirmed" if "unconfirmed" in states else "unshared"
            return {"id": identifier, "state": state, "units": children, "total": len(children), "shared": states.count("shared"), "visibility": "unverified"}
        args = (self.scope.key(), recipient, identifier, unit.get("content_version", 1))
        where = "scope=? AND recipient=? AND unit_id=? AND version=?"
        total = conn.execute(f"SELECT COUNT(*) FROM mind_share_coverage WHERE {where}", args).fetchone()[0]
        def checked(raw):
            value = json.loads(raw)
            return value
        deliveries = [checked(r[0]) for r in conn.execute(f"SELECT data FROM mind_share_coverage WHERE {where} ORDER BY at DESC,bubble_id LIMIT 5", args)]
        accepted = None
        for row in conn.execute(f"SELECT data FROM mind_share_coverage WHERE {where} AND state='accepted' ORDER BY at DESC,bubble_id", args):
            candidate = checked(row[0])
            if (candidate.get("message_id") and candidate.get("mapping_state") != "retracted"
                    and (candidate.get("basis") != "semantic-mapping" or candidate.get("confidence", 0) >= 0.8)):
                accepted = candidate
                break
        state = "shared" if accepted else "unconfirmed" if total else "unshared"
        return {"id": identifier, "version": unit.get("content_version", 1), "state": state,
                "last_shared_at": accepted["at"] if accepted else None, "deliveries": deliveries,
                "delivery_count": total, "visibility": "unverified",
                **({"activity_ref": unit["activity_ref"], "coverage_scope": "registered-content"} if unit.get("activity_ref") else {})}

    def _save_coverage(self, conn, data):
        keys = (self.scope.key(), "owner", data["unit_id"], data["version"], data["bubble_id"])
        prior = conn.execute("SELECT data FROM mind_share_coverage WHERE scope=? AND recipient=? AND unit_id=? AND version=? AND bubble_id=?", keys).fetchone()
        old = json.loads(prior[0]) if prior else {}
        compare = lambda value: {k:v for k,v in value.items() if k != "revision"}
        if old and compare(old) == compare(data):
            return old
        data = {**data, "revision": old.get("revision", 0)+1}
        conn.execute("INSERT OR REPLACE INTO mind_share_coverage VALUES(?,?,?,?,?,?,?,?,?)", (*keys, data["share_id"], data["state"], data["at"], dumps(data)))
        conn.execute("INSERT INTO mind_coverage_revisions VALUES(?,?,?,?,?,?)", (self.scope.key(),data["unit_id"],data["version"],data["bubble_id"],data["revision"],dumps(data)))
        if data["state"] == "accepted" and data.get("message_id") and not data.get("needs_review") and (data.get("activity_ref") or {}).get("kind") != "dream":
            from .lifecycle import configured, record_usage
            if configured(conn, self.scope.key(), "temperature_shadow") or configured(conn, self.scope.key(), "usage_reinforcement"):
                record_usage(conn, self.scope.key(), data["unit_id"], data.get("usage_id") or data["share_id"],
                             "reply_reference", data["at"], {"message_id": data["message_id"], "version": data["version"]})
        return data

    def settle(self, conn, share, *, mappings=None):
        """Only persisted, host-observed bubbles can change delivery coverage."""
        supplied = {m.bubble_id: m for m in mappings or []}
        for bid, bubble in share.get("bubbles", {}).items():
            mapping = supplied.get(bid)
            raw_refs = [r.model_dump() for r in mapping.references] if mapping else bubble.get("references", [])
            for raw in raw_refs:
                try:
                    ref, _unit = self.valid_reference(conn, raw, historical=not mapping)
                except (Missing, Conflict, ValueError):
                    self._settle_unavailable_reference(conn, share, bid, bubble, raw, mapping=mapping)
                    continue
                prior = conn.execute("SELECT data FROM mind_share_coverage WHERE scope=? AND recipient=? AND unit_id=? AND version=? AND bubble_id=?", (self.scope.key(), "owner", ref.unit_id, ref.version, bid)).fetchone()
                if prior and json.loads(prior[0])["state"] == "accepted" and not mapping:
                    continue
                data = {"unit_id": ref.unit_id, "version": ref.version, "bubble_id": bid, "share_id": share["id"],
                    "state": bubble["state"], "message_id": bubble.get("message_id"), "channel": share.get("channel"),
                    "conversation_id": bubble.get("conversation_id") or share.get("conversation_id"),
                    "at": bubble["at"], "mode": ref.mode, "reason": mapping.reason if mapping else ref.reason,
                    "basis": "semantic-mapping" if mapping else "registered-reference", "confidence": mapping.confidence if mapping else 1,
                    "needs_review": bool(mapping and mapping.confidence < 0.8 or _unit.get("needs_review")), "visibility": "unverified"}
                if ref.activity_ref:
                    data["activity_ref"] = ref.activity_ref
                data["source_ids"] = self.reference_sources(conn, [ref.model_dump(exclude_none=True)], historical=not mapping)
                # The registration of the reply this bubble belongs to, by its id: not the newest
                # registration anywhere with the same words (E3-25).
                registered = self._registered(conn, bubble)
                data["usage_id"] = registered or share.get("delivery_id") or share["id"]
                refs = self.graph.available_proof(conn, share["source_ids"])
                if _unit["kind"] == "finding":
                    edge = self.graph.link(conn, ref.unit_id, "shares", share["id"], refs, role=bid, basis="inferred" if mapping else "observed", reason=data["reason"] or "Registered content reference and delivery receipt")
                    data.update(relation_id=edge["id"], relation_revision=edge["revision"])
                self._save_coverage(conn, data)
                reservation = conn.execute("SELECT draft_id,state,data FROM mind_share_reservations WHERE scope=? AND recipient='owner' AND unit_id=? AND version=?", (self.scope.key(), ref.unit_id, ref.version)).fetchone()
                draft = bubble.get("draft_id") or share.get("delivery_id")
                if reservation and draft in {reservation["draft_id"], *json.loads(reservation["data"]).get("draft_ids", [])}:
                    reserved = json.loads(reservation["data"])
                    states = reserved.setdefault("draft_states", {})
                    states[draft] = "unconfirmed" if bubble["state"] == "accepted" and not bubble.get("message_id") else bubble["state"]
                    for group, members in reserved.get("batch_drafts", {}).items():
                        states[group] = self._aggregate_states([states.get(member, "prepared") for member in members])
                    reservation_state = self._aggregate_states(list(states.values()))
                    conn.execute("UPDATE mind_share_reservations SET state=?,data=? WHERE scope=? AND recipient='owner' AND unit_id=? AND version=?", (reservation_state, dumps(reserved), self.scope.key(), ref.unit_id, ref.version))
            if not mapping and bubble.get("state") == "accepted" and bubble.get("message_id"):
                self._settle_unavailable_reference(conn, share, bid, bubble, None)

    def _settle_unavailable_reference(self, conn, share, bid, bubble, raw, *, mapping=None):
        """A late real receipt can close an existing coverage record without reviving its content."""
        if mapping or bubble.get("state") != "accepted" or not bubble.get("message_id"):
            return
        for row in conn.execute("SELECT data FROM mind_share_coverage WHERE scope=? AND share_id=? AND bubble_id=?", (self.scope.key(), share["id"], bid)).fetchall():
            previous = json.loads(row[0])
            if previous.get("state") == "accepted":
                continue
            data = {**previous, "state": "accepted", "message_id": bubble["message_id"], "at": bubble["at"],
                    "channel": share.get("channel"), "conversation_id": bubble.get("conversation_id") or share.get("conversation_id"),
                    "needs_review": True, "content_state": "unavailable-or-changed"}
            self._save_coverage(conn, data)
            keys = (self.scope.key(), previous["unit_id"], previous["version"])
            row = conn.execute("SELECT draft_id,state,data FROM mind_share_reservations WHERE scope=? AND recipient='owner' AND unit_id=? AND version=?", keys).fetchone()
            draft = bubble.get("draft_id") or share.get("delivery_id")
            if row:
                reserved = self._reservation_data(row)
                if draft in {row["draft_id"], *reserved.get("draft_ids", [])}:
                    states = reserved.setdefault("draft_states", {})
                    states[draft] = "accepted"
                    for group, members in reserved.get("batch_drafts", {}).items():
                        states[group] = self._aggregate_states([states.get(member, "prepared") for member in members])
                    conn.execute("UPDATE mind_share_reservations SET state=?,data=? WHERE scope=? AND recipient='owner' AND unit_id=? AND version=?",
                        (self._aggregate_states(list(states.values())), dumps(reserved), *keys))

    def _registered(self, conn, bubble):
        reply_id = bubble.get("reply_id")
        if not reply_id:
            return None
        row = conn.execute("SELECT data FROM mind_reply_references WHERE scope=? AND reply_id=?",
                           (self.scope.key(), reply_id)).fetchone()
        return reply_id if row else None

    def apply(self, conn, assessment, allowed_share_ids, *, items=None, allowed_activity_sources=None):
        """`allowed_share_ids`: the evaluated shares, or a test of one share id. `items` (memory_items.Items):
        the memory section applies each mapping whole or not at all; without it every fault raises, as before."""
        from .memory import MemoryContinuity
        from .memory_items import Items, positions
        items = items or Items(conn, False)
        memory = MemoryContinuity.__new__(MemoryContinuity)
        memory.mind, memory.engine, memory.scope = self.mind, self.engine, self.scope
        for key, mapping in zip(positions("coverage.mappings", assessment.mappings), assessment.mappings):
            with items.item("coverage-mapping", key, needs=[r.unit_id for r in mapping.references if r.unit_id and not r.activity_ref]) as live:
                if not live:
                    continue
                evaluated = allowed_share_ids(mapping.share_id) if callable(allowed_share_ids) else mapping.share_id in allowed_share_ids
                if not evaluated:
                    raise Conflict("Coverage mapping is outside evaluated delivery evidence")
                share = memory._get(conn, mapping.share_id)
                if mapping.bubble_id not in share.get("bubbles", {}):
                    raise Conflict("Coverage mapping refers to an unknown bubble")
                references = [self.valid_reference(conn, r.model_dump())[0] for r in mapping.references]
                if allowed_activity_sources is not None:
                    bubble = share["bubbles"][mapping.bubble_id]
                    identity = lambda ref: tuple(ref.get(k) for k in ("kind", "id", "source_id"))
                    registered = set()
                    if bubble.get("state") == "accepted" and bubble.get("message_id"):
                        for raw in bubble.get("references", []):
                            try:
                                original, _ = self.valid_reference(conn, raw)
                                if original.activity_ref:
                                    registered.add(identity(original.activity_ref))
                            except (Missing, Conflict, ValueError):
                                continue
                    for ref in references:
                        if (ref.activity_ref and ref.activity_ref["source_id"] not in allowed_activity_sources
                                and identity(ref.activity_ref) not in registered):
                            raise Conflict("Coverage activity was neither supplied to the evaluation nor registered on the accepted bubble", code="coverage-activity-outside-evaluation", target=ref.activity_ref["id"])
                desired = {(r.unit_id, r.version) for r in references}
                for row in conn.execute("SELECT data FROM mind_share_coverage WHERE scope=? AND share_id=? AND bubble_id=?", (self.scope.key(), mapping.share_id, mapping.bubble_id)).fetchall():
                    previous = json.loads(row[0])
                    if (previous["unit_id"], previous["version"]) not in desired:
                        self._save_coverage(conn, {**previous, "needs_review": True, "reason": mapping.reason, "mapping_state": "retracted"})
                self.settle(conn, share, mappings=[mapping])

    def register(self, request):
        reply_id = request.get("reply_id")
        bubbles = request.get("bubbles")
        if not reply_id or not isinstance(bubbles, list) or not 1 <= len(bubbles) <= 64:
            raise ValueError("A reference registration needs a reply ID and public bubbles")
        with self.engine.db.connect(write=True) as conn:
            checked = []
            for index, item in enumerate(bubbles):
                if not isinstance(item.get("text"), str) or not item["text"].strip():
                    raise ValueError("Reference text cannot be empty")
                refs = [self.valid_reference(conn, r)[0].model_dump(exclude_none=True) for r in item.get("references", [])]
                checked.append({"text": item["text"], "bubble_id": item.get("bubble_id") or item.get("id") or f"{reply_id}:bubble:{index}",
                                "bubble_index": index, "references": refs,
                                "source_ids": self.reference_sources(conn, refs)})
            data = {"reply_id": reply_id, "bubbles": checked, "state": "registered", "at": self.mind.clock()}
            if request.get("draft_id"):
                self.reserve(conn, [r for b in checked for r in b["references"]], request["draft_id"])
            # The empty legacy column is schema compatibility only. Reply and bubble IDs
            # carry the relationship; words can be revised without an integrity test.
            conn.execute("INSERT OR REPLACE INTO mind_reply_references VALUES(?,?,?,?)", (self.scope.key(), reply_id, "", dumps(data)))
            return data

    @staticmethod
    def _reservation_data(row):
        data = json.loads(row["data"]) if row else {}
        if row:
            data.setdefault("draft_states", {}).setdefault(row["draft_id"], row["state"])
        return data

    @staticmethod
    def _check_draft(data, draft_id):
        state = data.get("draft_states", {}).get(draft_id)
        if state in {"pending", "unconfirmed"}:
            raise Conflict("This draft requires its original receipt to be reconciled", code="share-draft-awaiting-reconciliation", target=draft_id)
        if state == "accepted":
            raise Conflict("This draft already has an accepted receipt", code="share-draft-already-accepted", target=draft_id)

    @staticmethod
    def _aggregate_states(states):
        if states and all(state == "accepted" for state in states):
            return "accepted"
        if "unconfirmed" in states:
            return "unconfirmed"
        if "pending" in states or "accepted" in states:
            return "pending"
        return "canceled" if states and all(state == "canceled" for state in states) else "prepared"

    def reserve(self, conn, references, draft_id):
        """Coordinate a real draft ID. Sharing an activity again is a free choice."""
        for row in conn.execute("SELECT draft_id,state,data FROM mind_share_reservations WHERE scope=?", (self.scope.key(),)):
            self._check_draft(self._reservation_data(row), draft_id)
        for raw in references:
            ref, _ = self.valid_reference(conn, raw)
            keys = (self.scope.key(), "owner", ref.unit_id, ref.version)
            prior = conn.execute("SELECT draft_id,state,data FROM mind_share_reservations WHERE scope=? AND recipient=? AND unit_id=? AND version=?", keys).fetchone()
            data = self._reservation_data(prior)
            self._check_draft(data, draft_id)
            data.update(ref.model_dump(exclude_none=True))
            data.setdefault("draft_states", {})[draft_id] = "prepared"
            data["draft_ids"] = list(dict.fromkeys([*data.get("draft_ids", []), draft_id]))
            conn.execute("INSERT OR REPLACE INTO mind_share_reservations VALUES(?,?,?,?,?,?,?)", (*keys, prior["draft_id"] if prior else draft_id, "prepared", dumps(data)))

    def prepare(self, request):
        """Reserve actual send identities, separately from a reply's content registration."""
        entries = request.get("entries") or [{"draft_id": request.get("draft_id"), "references": request.get("references", [])}]
        group = request.get("batch_id") or request.get("draft_id")
        if not group or any(not entry.get("draft_id") for entry in entries):
            raise ValueError("Send preparation needs original draft identities")
        with self.engine.db.connect(write=True) as conn:
            self.reserve(conn, [ref for entry in entries for ref in entry.get("references", [])], group)
            for entry in entries:
                for raw in entry.get("references", []):
                    ref, _ = self.valid_reference(conn, raw)
                    args = (self.scope.key(), "owner", ref.unit_id, ref.version)
                    row = conn.execute("SELECT data FROM mind_share_reservations WHERE scope=? AND recipient=? AND unit_id=? AND version=?", args).fetchone()
                    data = json.loads(row[0])
                    data["draft_ids"] = sorted({*data.get("draft_ids", []), entry["draft_id"]})
                    data.setdefault("batch_drafts", {}).setdefault(group, [])
                    if entry["draft_id"] not in data["batch_drafts"][group]:
                        data["batch_drafts"][group].append(entry["draft_id"])
                    data.setdefault("draft_states", {}).setdefault(entry["draft_id"], "prepared")
                    conn.execute("UPDATE mind_share_reservations SET data=? WHERE scope=? AND recipient=? AND unit_id=? AND version=?", (dumps(data), *args))
            return {"state": "ready", "draft_id": group, "checked": [self._checked_entry(conn, entry) for entry in entries]}

    def _checked_entry(self, conn, entry):
        return {**entry, "state": "ready", "references": [self.valid_reference(conn, raw)[0].model_dump(exclude_none=True)
                                                           for raw in entry.get("references", [])]}

    def preview(self, request):
        """The same validation without acquiring or changing a send reservation."""
        entries = request.get("entries") or [{"draft_id": request.get("draft_id"), "text": request.get("text", ""), "references": request.get("references", [])}]
        group = request.get("batch_id") or request.get("draft_id")
        if not group or any(not entry.get("draft_id") for entry in entries):
            raise ValueError("Send preparation needs original draft identities")
        with self.engine.db.connect() as conn:
            checked = [self._checked_entry(conn, entry) for entry in entries]
            for row in conn.execute("SELECT draft_id,state,data FROM mind_share_reservations WHERE scope=?", (self.scope.key(),)):
                self._check_draft(self._reservation_data(row), group)
            return {"state": "ready", "draft_id": group, "checked": checked}

    def resolve_reply_context(self, conn, event):
        """Resolve only real platform references to accepted bubbles, within their original channel/chat."""
        context = event.get("reply_context") if isinstance(event.get("reply_context"), dict) else {}
        route = {**context, **{k: event[k] for k in ("channel", "conversation_id", "reply_to_message_id", "root_message_id", "thread_id") if event.get(k) is not None}}
        channel, conversation = route.get("channel"), route.get("conversation_id")
        parent = route.get("reply_to_message_id")
        target = parent or route.get("root_message_id")
        if not channel or not conversation or not target:
            return {"state": "unresolved", "candidates": []}
        candidates = []
        for row in conn.execute("SELECT data FROM mind_memory_nodes WHERE scope=? AND kind='share'", (self.scope.key(),)):
            share = json.loads(row[0])
            if share.get("channel") != channel:
                continue
            for bubble in share.get("bubbles", {}).values():
                if (bubble.get("state") != "accepted" or target not in [bubble.get("message_id"), *(bubble.get("message_ids") or [])]
                        or (bubble.get("conversation_id") or share.get("conversation_id")) != conversation):
                    continue
                try:
                    proof = self.graph.available_proof(conn, share.get("source_ids", []))
                    if not proof or not self.mind._fresh(conn, proof):
                        continue
                except (Missing, Conflict):
                    continue
                valid, withheld = [], False
                for raw in bubble.get("references", []):
                    try:
                        ref, _ = self.valid_reference(conn, raw)
                        valid.append(ref.model_dump(exclude_none=True))
                    except (Missing, Conflict, ValueError):
                        withheld = True
                if withheld:
                    # The original reference stood for this body's content. A missing/deleted or
                    # replaced original cannot leave an otherwise unbound copy of those words.
                    continue
                candidates.append({"share_id": share["id"], "bubble_id": bubble["id"], "message_id": target,
                    "channel": channel, "conversation_id": conversation, "text": bubble.get("text", ""),
                    "references": valid, "activity_refs": list({self.activity_id(r["activity_ref"]): r["activity_ref"] for r in valid if r.get("activity_ref")}.values())})
        return {"state": "resolved" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved",
                "basis": "platform-reply" if parent else "platform-root", "candidates": candidates}

    def references(self, conn, text=None, reply_id=None, *, bubble_id=None, bubble_index=None):
        """Read the registered relationship by reply/bubble identity; text is legacy ignored input."""
        if not reply_id:
            return []
        row = conn.execute("SELECT data FROM mind_reply_references WHERE scope=? AND reply_id=?", (self.scope.key(), reply_id)).fetchone()
        if row:
            bubbles = json.loads(row[0])["bubbles"]
            selected = [bubble for index, bubble in enumerate(bubbles)
                if (bubble_id is not None and bubble.get("bubble_id") == bubble_id)
                or (bubble_index is not None and (bubble.get("bubble_index", index)) == bubble_index)
                or (bubble_id is None and bubble_index is None and len(bubbles) == 1)]
            if len(selected) == 1:
                return [self.valid_reference(conn, raw)[0].model_dump(exclude_none=True) for raw in selected[0].get("references", [])]
        return []

    def cancel(self, draft_id):
        with self.engine.db.connect(write=True) as conn:
            for row in conn.execute("SELECT unit_id,version,draft_id,state,data FROM mind_share_reservations WHERE scope=?", (self.scope.key(),)).fetchall():
                data = json.loads(row["data"])
                if draft_id not in {row["draft_id"], *data.get("draft_ids", [])}:
                    continue
                states = data.setdefault("draft_states", {})
                for identifier in {draft_id, *data.get("batch_drafts", {}).get(draft_id, [])}:
                    if states.get(identifier, row["state"] if identifier == row["draft_id"] else "prepared") == "prepared":
                        states[identifier] = "canceled"
                state = self._aggregate_states(list(states.values()))
                conn.execute("UPDATE mind_share_reservations SET state=?,data=? WHERE scope=? AND unit_id=? AND version=?", (state, dumps(data), self.scope.key(), row["unit_id"], row["version"]))
        return {"state": "canceled", "draft_id": draft_id}

    def decorate(self, node):
        with self.engine.db.connect() as conn:
            if node.get("kind") == "share":
                refs = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_share_coverage WHERE scope=? AND share_id=? ORDER BY at DESC LIMIT 40",(self.scope.key(),node["id"]))]
                return {**node, "content_references": refs}
            if node.get("kind") not in {"finding", "work", "artifact", "exploration"}:
                return node
            try:
                node = {**node, "share_coverage": self.coverage(conn, node["id"])}
            except Missing:
                pass
        return node

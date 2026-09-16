"""Versioned event graph. Edges point to evidence, never replace it."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Literal

from pydantic import Field

from eventmem.core.db import Conflict, Missing, digest, dumps, tokenize
from eventmem.core.models import Model

from .state import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_graph_nodes(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,revision INTEGER NOT NULL,
 occurred_at TEXT NOT NULL,updated_at TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_graph_owner ON mind_graph_nodes(scope,kind,json_extract(data,'$.owner_id'),state);
CREATE INDEX IF NOT EXISTS mind_graph_node_scope ON mind_graph_nodes(scope,kind,occurred_at DESC,id);
CREATE TABLE IF NOT EXISTS mind_graph_edges(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,subject TEXT NOT NULL,object TEXT NOT NULL,
 predicate TEXT NOT NULL,layer TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_graph_left ON mind_graph_edges(scope,subject,state);
CREATE INDEX IF NOT EXISTS mind_graph_right ON mind_graph_edges(scope,object,state);
CREATE TABLE IF NOT EXISTS mind_graph_revisions(
 id TEXT NOT NULL,revision INTEGER NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS mind_graph_commands(
 id TEXT NOT NULL,scope TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS mind_graph_identity(
 scope TEXT NOT NULL,identity TEXT NOT NULL,node_id TEXT NOT NULL,PRIMARY KEY(scope,identity));
CREATE VIRTUAL TABLE IF NOT EXISTS mind_graph_search USING fts5(id UNINDEXED,tokens);
"""

RELATIONS = {"participates", "part_of", "continues", "responds_to", "produces", "delivers",
             "shares", "corrects", "resolves", "supports", "refutes", "causes", "association",
             "follows", "about", "related"}


def query_terms(query, limit=40):
    """Retain the user's language instead of taking ASCII-first tokenizer output."""
    words = list(dict.fromkeys(tokenize(query).split()))
    lower = query.casefold()
    stop = {"的","了","是","我","你","在","和","也","有","这","个","一个","我们","他们","可以","进行","相关","已经"}
    meaningful = [w for w in words if w not in stop and not w.isdecimal()]
    selected = meaningful or words
    selected.sort(key=lambda w: (lower.find(w) if w in lower else len(lower), -len(w)))
    return selected[:limit]


class GraphNode(Model):
    key: str = Field(min_length=1, max_length=160)
    id: str | None = None
    kind: Literal["event", "thread", "entity", "finding", "association"]
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(default="", max_length=4000)
    evidence_ids: list[str] = Field(min_length=1, max_length=16)
    basis: Literal["explicit", "documented", "inferred", "internal_thought"] = "inferred"
    confidence: float = Field(default=0.5, ge=0, le=1)
    occurred_at: str | None = None
    entity_type: Literal["person", "agent", "project", "organization", "place", "other"] | None = None
    aliases: list[str] = Field(default_factory=list, max_length=12)
    owner_id: str | None = None
    expected_revision: int | None = None


class GraphEdge(Model):
    subject: str
    object: str
    relation: Literal["participates", "part_of", "continues", "responds_to", "produces", "delivers", "shares", "corrects", "resolves", "supports", "refutes", "causes", "association", "follows", "about", "related"]
    evidence_ids: list[str] = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=1200)
    basis: Literal["explicit", "documented", "inferred", "internal_thought"] = "inferred"
    confidence: float = Field(default=0.5, ge=0, le=1)
    role: str | None = Field(default=None, max_length=160)
    valid_from: str | None = None
    valid_until: str | None = None


class GraphAssessment(Model):
    nodes: list[GraphNode] = Field(default_factory=list, max_length=12)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=24)


class EventGraph:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        with self.engine.db.connect() as conn:
            conn.executescript(SCHEMA)
            from .lifecycle_schema import initialize_graph_refs
            initialize_graph_refs(conn)

    def identifier(self, kind, key):
        return "graph_" + kind + "_" + digest([self.scope.key(), key])[:28]

    def get(self, conn, identifier, *, follow=False):
        for table in ("mind_graph_nodes", "mind_graph_edges"):
            row = conn.execute(f"SELECT data FROM {table} WHERE scope=? AND id=?", (self.scope.key(), identifier)).fetchone()
            if row:
                value = json.loads(row[0])
                if follow and value.get("merged_into"):
                    return self.get(conn, value["merged_into"], follow=True)
                return value
        raise Missing(identifier)

    def _put(self, conn, value, *, edge=False):
        value = dict(value)
        try:
            old = self.get(conn, value["id"])
        except Missing:
            old = None
        strip = lambda v: {k: x for k, x in v.items() if k not in {"revision", "updated_at", "needs_review"}}
        if old and strip(old) == strip(value):
            return old
        value.update(revision=(old or {}).get("revision", 0) + 1, updated_at=self.mind.clock())
        value.setdefault("state", "active")
        value.setdefault("occurred_at", self.mind.clock())
        if edge:
            conn.execute("INSERT OR REPLACE INTO mind_graph_edges VALUES(?,?,?,?,?,?,?,?,?)", (
                value["id"], self.scope.key(), value["subject"], value["object"], value["predicate"], value["layer"], value["revision"], value["state"], dumps(value)))
        else:
            conn.execute("INSERT OR REPLACE INTO mind_graph_nodes VALUES(?,?,?,?,?,?,?,?)", (
                value["id"], self.scope.key(), value["kind"], value["revision"], value["occurred_at"], value["updated_at"], value["state"], dumps(value)))
            if old:
                conn.execute("DELETE FROM mind_graph_search WHERE id=?", (value["id"],))
            conn.execute("INSERT INTO mind_graph_search VALUES(?,?)", (value["id"], tokenize(" ".join(str(value.get(k, "")) for k in ("title", "text", "aliases")))))
            conn.execute("DELETE FROM mind_graph_record_refs WHERE scope=? AND node_id=?", (self.scope.key(), value["id"]))
            record_ids = set(value.get("record_ids", [])) | {r["record_id"] for r in value.get("evidence", [])}
            conn.executemany("INSERT OR IGNORE INTO mind_graph_record_refs VALUES(?,?,?)",
                             [(self.scope.key(), rid, value["id"]) for rid in record_ids])
        conn.execute("INSERT INTO mind_graph_revisions VALUES(?,?,?,?)", (value["id"], value["revision"], "edge" if edge else "node", dumps(value)))
        from .lifecycle import graph_changed
        graph_changed(conn, self.scope.key(), value, old, self.mind.clock())
        return value

    def proof(self, conn, identifiers, allowed=None):
        roots = []
        for identifier in identifiers:
            if identifier.startswith(("src_", "mem_")):
                roots.append(identifier)
            else:
                roots.extend(self.ensure(conn, identifier).get("source_ids", []))
        refs = self.mind._evidence(conn, list(dict.fromkeys(roots)))
        if not refs or not self.mind._fresh(conn, refs):
            raise Conflict("Graph evidence is missing or needs review")
        if allowed is not None and any(r["source_id"] not in allowed and r["record_id"] not in allowed for r in refs):
            raise Conflict("Graph evidence was not part of this evaluation")
        return refs

    def fresh(self, conn, node):
        try:
            if node.get("reference_status") in {"unverified", "archived", "superseded", "deleted"}:
                return False
            if node["id"].startswith("mem_"):
                record = self.engine._get(conn,node["id"])
                if record["revision"] != node.get("reference_revision") or record["status"] != "active":
                    return False
            if node.get("kind") == "edge" and any(not self.fresh(conn,self.get(conn,nid)) for nid in (node["subject"],node["object"])):
                return False
            return node.get("state") == "active" and bool(node.get("evidence")) and self.mind._fresh(conn, node["evidence"])
        except (Missing, Conflict):
            return False

    def ensure(self, conn, identifier):
        """Project canonical domain objects without copying their full contents."""
        try:
            return self.get(conn, identifier, follow=True)
        except Missing:
            pass
        if identifier.startswith("src_"):
            return self.ensure(conn, self.proof(conn, [identifier])[0]["record_id"])
        if identifier.startswith("mem_"):
            record = self.engine._get(conn, identifier)
            if record["scope"] != self.scope.model_dump():
                raise Conflict("Graph reference crossed memory scopes")
            refs = self.available_proof(conn,record["source_ids"])
            return self._put(conn, {"id": identifier, "kind": record["kind"], "title": record["title"],
                "text": "", "record_ids": [identifier], "source_ids": record["source_ids"], "evidence": refs,
                "basis": record["confirmation"], "occurred_at": record["valid_from"], "reference_revision": record["revision"], "reference_status": record["status"]})
        row = conn.execute("SELECT data FROM mind_memory_nodes WHERE scope=? AND id=?", (self.scope.key(), identifier)).fetchone()
        if row:
            value = json.loads(row[0])
            refs = self.proof(conn, value.get("source_ids", []))
            return self._put(conn, {"id": identifier, "kind": value["kind"], "title": value.get("title") or value.get("name") or value.get("topic") or value["kind"],
                "text": value.get("summary", ""), "record_ids": value.get("record_ids", []), "source_ids": value["source_ids"], "evidence": refs,
                "reference_revision": value["revision"], "created_by": value.get("created_by"), "basis": "observed",
                "occurred_at": value.get("first_at") or value.get("last_at") or value["updated_at"]})
        row = conn.execute("SELECT data FROM mind_explorations WHERE scope=? AND id=?", (self.scope.key(), identifier)).fetchone() if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone() else None
        if row:
            value = json.loads(row[0]); refs = self.proof(conn, [value["source_id"]])
            return self._put(conn, {"id": identifier, "kind": "exploration", "title": value.get("topic") or "Exploration", "text": "",
                "source_ids": [value["source_id"]], "evidence": refs, "basis": "documented", "occurred_at": value.get("completed_at") or self.mind.clock()})
        raise Missing(identifier)

    def available_proof(self, conn, identifiers):
        refs = []
        for identifier in identifiers:
            try:
                refs.extend(self.proof(conn, [identifier]))
            except (Missing, Conflict):
                pass
        return refs

    def project_memory(self, conn, value):
        refs = self.available_proof(conn, value.get("source_ids", []))
        try:
            old = self.get(conn, value["id"])
        except Missing:
            old = {}
        return self._put(conn, {**old, "id": value["id"], "kind": value["kind"],
            "title": value.get("title") or value.get("name") or value.get("topic") or value["kind"],
            "text": value.get("summary", ""), "source_ids": value["source_ids"], "record_ids": value.get("record_ids", []),
            "evidence": refs, "basis": "observed", "reference_revision": value["revision"], "created_by": value.get("created_by"),
            "occurred_at": value.get("first_at") or value.get("last_at") or value["updated_at"]})

    def link(self, conn, subject, predicate, object_id, refs, *, basis="observed", reason="", role=None, confidence=1, valid_from=None, valid_until=None, event_id=None):
        if predicate not in RELATIONS:
            raise ValueError("Unknown graph relationship")
        left, right = self.ensure(conn, subject), self.ensure(conn, object_id)
        if left["id"] == right["id"]:
            raise Conflict("An event cannot provide its own relationship evidence")
        if predicate == "causes" and not reason:
            raise Conflict("Causal claims need a stated evidence basis")
        for value in (valid_from, valid_until):
            if value:
                timestamp(value)
        layer = "association" if predicate == "association" or basis == "internal_thought" else "evidence"
        identifier = self.identifier("edge", [left["id"], predicate, right["id"], layer, role])
        try:
            old = self.get(conn, identifier)
        except Missing:
            old = {}
        evidence = {r["record_id"]: r for r in [*old.get("evidence", []), *refs]}
        return self._put(conn, {**old, "id": identifier, "kind": "edge", "subject": left["id"], "object": right["id"], "predicate": predicate,
            "layer": layer, "basis": basis, "confidence": confidence, "reason": reason, "role": role,
            "source_ids": sorted({r["source_id"] for r in evidence.values()}), "evidence": list(evidence.values()),
            "valid_from": valid_from, "valid_until": valid_until, "assessment_event": event_id, "state": "active"}, edge=True)

    def runtime(self, conn, event, receipt, event_id):
        refs = self.proof(conn, [receipt["source_id"]])
        identifier = self.identifier("event", event_id)
        kind = event["kind"]
        node = self._put(conn, {"id": identifier, "kind": "event", "title": {"owner-message":"用户消息","assistant-message":"公开回复","delivery":"发送回执","task-result":"任务结果","artifact-created":"制作文件","artifact-observed":"读取文件","exploration-result":"探索结果"}.get(kind,kind), "text": "", "runtime_event_id": event_id,
            "source_ids": [receipt["source_id"]], "record_ids": [receipt["record_id"]], "evidence": refs,
            "basis": "explicit" if kind == "owner-message" else "inferred" if kind in {"assistant-message", "exploration-result"} else "observed",
            "occurred_at": event["at"], "channel": event.get("channel"), "operation": kind, "historical": bool(event.get("historical"))})
        for key, relation in (("work_id", "produces" if kind == "artifact-created" else "about"), ("artifact_id", "about"), ("share_id", "delivers")):
            if receipt.get(key):
                self.link(conn, identifier, relation, receipt[key], refs, reason="Host operation record")
        actor = event.get("actor") or ("owner" if kind == "owner-message" else "Kin" if kind in {"assistant-message", "artifact-created"} else None)
        if actor:
            actor_id = self.identifier("entity", ["host-actor", actor])
            try:
                self.get(conn, actor_id)
            except Missing:
                self._put(conn, {"id": actor_id, "kind": "entity", "entity_type": "person" if actor == "owner" else "agent", "title": actor,
                    "source_ids": [receipt["source_id"]], "evidence": refs, "basis": "observed", "identity_basis": "host-bound-actor"})
            identity = "host-actor:" + actor
            conn.execute("INSERT OR IGNORE INTO mind_graph_identity VALUES(?,?,?)", (self.scope.key(),identity,actor_id))
            if actor == "owner" and event.get("channel"):
                conn.execute("INSERT OR IGNORE INTO mind_graph_identity VALUES(?,?,?)",(self.scope.key(),"bound-owner:"+event["channel"],actor_id))
            self.link(conn, actor_id, "participates", identifier, refs, role="requester" if kind == "owner-message" else "creator" if kind == "artifact-created" else "speaker", reason="Host-bound actor")
        if event.get("task_id"):
            tid = self.identifier("thread", ["host-task", event["task_id"]])
            try:
                self.get(conn, tid)
            except Missing:
                self._put(conn, {"id": tid, "kind": "thread", "title": event["task_id"], "source_ids": [receipt["source_id"]], "evidence": refs, "basis": "observed"})
            self.link(conn, identifier, "part_of", tid, refs, reason="Same host task identifier")
        return node

    def apply(self, conn, proposal, refs, event_id, receipt, *, external_aliases=None):
        allowed = {v for r in refs for v in (r["source_id"], r["record_id"])}
        node_aliases = {n.key: n.id or self.identifier(n.kind, [event_id, n.key]) for n in proposal.nodes}
        if len(node_aliases) != len(proposal.nodes) or node_aliases.keys() & (external_aliases or {}).keys():
            raise Conflict("Graph keys must be unique")
        aliases = {**(external_aliases or {}), **node_aliases}
        for item in proposal.nodes:
            evidence = self.proof(conn, item.evidence_ids, allowed)
            identifier = aliases[item.key]
            try:
                previous = self.get(conn, identifier)
            except Missing:
                previous = None
            if previous and item.expected_revision != previous["revision"]:
                raise Conflict("Graph node changed after evaluation")
            if previous and previous["kind"] != item.kind:
                raise Conflict("Graph kind is immutable")
            if item.occurred_at:
                timestamp(item.occurred_at)
            basis = item.basis
            if basis == "explicit" and not any(r.get("authority") == "explicit" for r in evidence):
                basis = "inferred"
            if item.kind == "association":
                basis = "internal_thought"
            value = {**(previous or {}), **item.model_dump(exclude={"id", "key", "expected_revision", "evidence_ids"}), "id": identifier,
                "basis": basis, "source_ids": sorted({r["source_id"] for r in evidence}), "evidence": evidence,
                "occurred_at": item.occurred_at or (previous or {}).get("occurred_at") or self.mind.clock(),
                "assessment_event": event_id, "configuration_version": receipt.get("configuration_version"),
                "model": receipt.get("model"), "state": "active"}
            if item.kind == "finding":
                value["content_version"] = (previous or {}).get("content_version", 1) + int(bool(previous and previous.get("text") != item.text))
            if value.get("owner_id"):
                value["owner_id"] = aliases.get(value["owner_id"],value["owner_id"])
            self._put(conn, value)
        for item in proposal.nodes:
            if item.owner_id:
                node = self.get(conn, aliases[item.key])
                owner = self.ensure(conn, node["owner_id"])
                if owner["id"] == node["id"]:
                    raise Conflict("A graph node cannot own itself")
                self.link(conn,node["id"],"part_of",owner["id"],node["evidence"],basis=node["basis"],reason="Content belongs to the referenced result",event_id=event_id)
        for edge in proposal.edges:
            evidence = self.proof(conn, edge.evidence_ids, allowed)
            basis = edge.basis
            if basis == "explicit" and not any(r.get("authority") == "explicit" for r in evidence):
                basis = "inferred"
            self.link(conn, aliases.get(edge.subject, edge.subject), edge.relation, aliases.get(edge.object, edge.object), evidence,
                basis=basis, reason=edge.reason, role=edge.role, confidence=edge.confidence, valid_from=edge.valid_from, valid_until=edge.valid_until, event_id=event_id)
        return {"node_ids": list(node_aliases.values()), "edge_count": len(proposal.edges)}

    def candidates(self, conn, query="", limit=40):
        limit = min(40, max(1, limit))
        words = query_terms(query)
        found = []
        if words:
            match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
            rows = conn.execute("SELECT n.data FROM mind_graph_nodes n JOIN mind_graph_search f ON f.id=n.id WHERE n.scope=? AND n.state='active' AND mind_graph_search MATCH ? ORDER BY bm25(mind_graph_search),n.occurred_at DESC LIMIT ?", (self.scope.key(), match, limit)).fetchall()
            found = [json.loads(r[0]) for r in rows]
            # Thousands of short legacy summaries otherwise outrank every
            # longer finding. Reserve candidates for the operational objects
            # carrying authorship and delivery coverage, then keep lexical rank.
            units = conn.execute("SELECT n.data FROM mind_graph_nodes n JOIN mind_graph_search f ON f.id=n.id WHERE n.scope=? AND n.state='active' AND n.kind IN ('finding','exploration','work','artifact','entity','thread') AND mind_graph_search MATCH ? ORDER BY bm25(mind_graph_search),n.occurred_at DESC LIMIT ?", (self.scope.key(), match, min(16, max(1, limit//2)))).fetchall()
            found = list({n["id"]:n for n in [*[json.loads(r[0]) for r in units], *found]}.values())
        since = (timestamp(self.mind.clock()) - timedelta(hours=72)).isoformat()
        rows = conn.execute("SELECT data FROM mind_graph_nodes WHERE scope=? AND state='active' AND (occurred_at>=? OR kind='thread') ORDER BY occurred_at DESC LIMIT ?", (self.scope.key(), since, limit)).fetchall()
        return list({n["id"]: n for n in [*found, *[json.loads(r[0]) for r in rows]]}.values())[:limit]

    def read(self, *, focus=None, query="", since=None, until=None, layer=None, kind=None, cursor=0, limit=150, hops=1):
        limit, cursor, hops = min(300, max(1, int(limit))), max(0, int(cursor)), min(3, max(0, int(hops)))
        if layer not in {None, "evidence", "association"}:
            raise ValueError("Unknown graph layer")
        with self.engine.db.connect() as conn:
            if focus:
                anchor = self.ensure(conn, focus)
                ids, frontier = {anchor["id"]}, {anchor["id"]}
                for _ in range(hops):
                    next_ids = set()
                    for identifier in sorted(frontier):
                        for row in conn.execute("SELECT subject,object FROM mind_graph_edges WHERE scope=? AND state='active' AND (subject=? OR object=?) AND (? IS NULL OR layer=?) LIMIT 300", (self.scope.key(), identifier, identifier, layer, layer)):
                            next_ids.update(row)
                    frontier = next_ids - ids; ids.update(frontier)
                    if len(ids) >= 1200:
                        break
                values = [self.get(conn, i) for i in sorted(ids)]
            elif query:
                values = self.candidates(conn, query)
                known = {n["id"] for n in values}
                frontier = set(known)
                for _ in range(hops):
                    additions = set()
                    for identifier in sorted(frontier):
                        for row in conn.execute("SELECT subject,object FROM mind_graph_edges WHERE scope=? AND state='active' AND (subject=? OR object=?) AND (? IS NULL OR layer=?) LIMIT 40", (self.scope.key(), identifier, identifier, layer, layer)):
                            additions.update(row)
                    frontier = set(sorted(additions-known)[:40]); known.update(frontier)
                    values.extend(self.get(conn, i) for i in sorted(frontier))
            else:
                rows = conn.execute("SELECT data FROM mind_graph_nodes WHERE scope=? AND state='active' AND (? IS NULL OR occurred_at>=?) AND (? IS NULL OR occurred_at<=?) AND (? IS NULL OR kind=?) ORDER BY occurred_at DESC,id LIMIT ? OFFSET ?", (self.scope.key(), since, since, until, until, kind, kind, limit+1, cursor)).fetchall()
                values = [json.loads(r[0]) for r in rows]
            values = [n for n in values if (not since or n["occurred_at"] >= since) and (not until or n["occurred_at"] <= until) and (not kind or n["kind"] == kind)]
            # Query order comes from lexical relevance, followed by graph
            # neighbors. Sorting by time here hid old, exact matches.
            if not query:
                values.sort(key=lambda n: (n["occurred_at"], n["id"]), reverse=True)
            more = len(values) > (cursor + limit if focus or query else limit)
            nodes = values[cursor:cursor+limit] if focus or query else values[:limit]
            allowed, edges = {n["id"] for n in nodes}, {}
            for node in nodes:
                node["needs_review"] = not self.fresh(conn, node)
                node["instruction_authority"] = "data"
                for row in conn.execute("SELECT data FROM mind_graph_edges WHERE scope=? AND state='active' AND (subject=? OR object=?) AND (? IS NULL OR layer=?)", (self.scope.key(), node["id"], node["id"], layer, layer)):
                    edge = json.loads(row[0])
                    if edge["subject"] in allowed and edge["object"] in allowed:
                        edge["needs_review"] = not self.fresh(conn, edge)
                        edges[edge["id"]] = edge
            return {"nodes": nodes, "edges": list(edges.values()), "cursor": cursor+limit if more else None, "limit": limit,
                "focus": focus, "layout": "deterministic", "instruction_authority": "data"}

    def detail(self, identifier):
        with self.engine.db.connect() as conn:
            value = self.get(conn, identifier)
            value["needs_review"] = not self.fresh(conn, value)
            value["history"] = [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_graph_revisions WHERE id=? ORDER BY revision", (identifier,))]
            if value["kind"] in {"share", "work", "artifact"}:
                original = conn.execute("SELECT data FROM mind_memory_nodes WHERE scope=? AND id=?", (self.scope.key(), identifier)).fetchone()
                if original:
                    value["record"] = json.loads(original[0])
            return value

    def revise(self, request):
        """Commands and inverse patches are durable; evidence stays immutable."""
        if not request.get("command_id") or not request.get("reason"):
            raise ValueError("Graph revisions need command_id and reason")
        command_hash = digest(request)
        with self.engine.db.connect(write=True) as conn:
            prior = conn.execute("SELECT digest,data FROM mind_graph_commands WHERE scope=? AND id=?", (self.scope.key(), request["command_id"])).fetchone()
            if prior:
                if prior[0] != command_hash:
                    raise Conflict("Graph command ID reused with other data")
                return json.loads(prior[1])
            proof = self.proof(conn, request.get("evidence_ids", []))
            identifier = request["id"]; current = self.get(conn, identifier)
            if current["revision"] != request.get("expected_revision"):
                raise Conflict("Graph revision changed")
            before, changed = {identifier: current}, []
            action = request["action"]
            if action == "merge":
                target = self.get(conn, request["target_id"])
                if target["kind"] != current["kind"] or target.get("merged_into") or current.get("merged_into") or target["id"] == identifier:
                    raise Conflict("Invalid graph merge")
                if target["revision"] != request.get("target_revision"):
                    raise Conflict("Merge target changed")
                before[target["id"]] = target
                current = {**current, "state": "merged", "merged_into": target["id"]}
                target = self._put(conn, {**target, "aliases": list(dict.fromkeys([*target.get("aliases", []), current["title"], *current.get("aliases", [])])), "merge_sources": list(dict.fromkeys([*target.get("merge_sources", []), identifier]))})
                changed.append(target)
                for row in conn.execute("SELECT data FROM mind_graph_edges WHERE scope=? AND (subject=? OR object=?) AND state='active'", (self.scope.key(), identifier, identifier)).fetchall():
                    edge = json.loads(row[0]); before[edge["id"]] = edge
                    edge = {**edge, "subject": target["id"] if edge["subject"] == identifier else edge["subject"], "object": target["id"] if edge["object"] == identifier else edge["object"]}
                    if edge["subject"] == edge["object"]:
                        edge["state"] = "retracted"
                    changed.append(self._put(conn, edge, edge=True))
            elif action == "split_event":
                if current["kind"] != "event" or current["state"] != "active":
                    raise Conflict("Only event membership can be split")
                selected = request.get("member_ids", [])
                if not selected or len(selected) != len(set(selected)) or not request.get("title"):
                    raise ValueError("Event split requires unique members and a title")
                all_edges = [json.loads(r[0]) for r in conn.execute(
                    "SELECT data FROM mind_graph_edges WHERE scope=? AND object=? AND predicate='part_of' AND state='active'",
                    (self.scope.key(), identifier))]
                if not set(selected) < {e["subject"] for e in all_edges}:
                    raise Conflict("Split members must be a proper subset")
                split_id = self.identifier("event", ["split", request["command_id"]])
                try:
                    self.get(conn, split_id)
                    raise Conflict("Split identity already exists")
                except Missing:
                    pass
                before[split_id] = None
                split = self._put(conn, {"id": split_id, "kind": "event", "title": request["title"],
                    "basis": current.get("basis", "inferred"), "lifecycle": current.get("lifecycle", "open"),
                    "occurred_at": min(self.get(conn, i)["occurred_at"] for i in selected),
                    "text": "", "split_from": identifier, "membership_authority": "graph", "record_ids": [],
                    "merge_sources": [], "aliases": [], "source_ids": sorted({r["source_id"] for r in proof}), "evidence": proof})
                changed.append(split)
                for edge in all_edges:
                    if edge["subject"] in selected:
                        before[edge["id"]] = edge
                        changed.append(self._put(conn, {**edge, "object": split_id}, edge=True))
                current = {**current, "membership_command": request["command_id"]}
            elif action in {"undo", "split"}:
                prior = conn.execute("SELECT data FROM mind_graph_commands WHERE scope=? AND id=?", (self.scope.key(), request["previous_command_id"])).fetchone()
                if not prior:
                    raise Missing(request["previous_command_id"])
                old = json.loads(prior[0])
                for nid, revision in old["after_revisions"].items():
                    if self.get(conn, nid)["revision"] != revision:
                        raise Conflict("Affected graph objects changed since the command")
                for nid, value in old["before"].items():
                    before[nid] = self.get(conn, nid)
                    restored = value if value is not None else {**before[nid], "state": "retracted"}
                    changed.append(self._put(conn, {**restored, "revision_reason": request["reason"]}, edge=restored.get("kind") == "edge"))
                current = None
            elif action in {"correct", "retract", "restore"}:
                allowed = {"title", "text", "reason", "role", "basis", "confidence", "valid_from", "valid_until", "occurred_at", "aliases"}
                changes = request.get("changes", {})
                if set(changes) - allowed:
                    raise ValueError("Unsupported graph correction field")
                for key in ("title","text","reason","role"):
                    if key in changes and (not isinstance(changes[key],str) or len(changes[key])>8000):
                        raise ValueError("Graph correction requires complete bounded text")
                if "basis" in changes:
                    if changes["basis"] not in {"observed","explicit","documented","inferred","internal_thought"}:
                        raise ValueError("Unknown evidence basis")
                    if changes["basis"]=="explicit" and not any(r["authority"]=="explicit" for r in proof):
                        raise Conflict("Explicit claims need owner evidence")
                if "confidence" in changes and (type(changes["confidence"]) not in {int,float} or not 0<=changes["confidence"]<=1):
                    raise ValueError("Confidence must be within 0..1")
                for key in ("valid_from","valid_until","occurred_at"):
                    if changes.get(key):
                        timestamp(changes[key])
                if "aliases" in changes and (not isinstance(changes["aliases"],list) or len(changes["aliases"])>100 or any(not isinstance(a,str) for a in changes["aliases"])):
                    raise ValueError("Aliases must be complete names")
                version = current.get("content_version", 1)
                if current["kind"] == "finding" and "text" in changes and changes["text"] != current.get("text"):
                    version += 1
                current = {**current, **changes, "state": "retracted" if action == "retract" else "active"}
                if current["kind"] == "finding":
                    current["content_version"] = version
            else:
                raise ValueError("Unknown graph revision action")
            if current:
                changed.append(self._put(conn, {**current, "revision_reason": request["reason"], "evidence": proof, "source_ids": sorted({r["source_id"] for r in proof})}, edge=current.get("kind") == "edge"))
            result = {"state": "applied", "action": action, "command_id": request["command_id"], "before": before, "after_revisions": {v["id"]: v["revision"] for v in changed}}
            conn.execute("INSERT INTO mind_graph_commands VALUES(?,?,?,?)", (request["command_id"], self.scope.key(), command_hash, dumps(result)))
            return result

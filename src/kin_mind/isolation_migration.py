"""Applying the evidence classification to a store that predates it, once, and taking it back.

The reading side already knows what is not experience. This is the writing side: it fills
`source_evidence_class` for what is already stored, repairs the self-claim supersede chains
nobody ever linked, and moves the configuration references out of the graph nodes that mix
them with lived evidence. Nothing an older revision proves is rewritten: a legacy record keeps
its attributes and its revision, because evidence freshness is pinned to that revision and
bumping it would stale every claim, node, habit and plan that cites it.

Progress is one row of `mind_memory_migrations`, `evidence-isolation-v1`, whose `data.state`
runs pending -> classified -> chains -> invalidated -> complete. While that row exists and is
not settled the read policy is strict, so a half-applied store answers from records rather
than from any cache, summary or receipt it has not re-derived. Every step is idempotent and
resumable: it asks what is already done instead of counting what it did, and it keeps its
write transactions small, because production has other writers.

`--dry-run` is the default and writes nothing at all. Its impact list carries identifiers and
counts, never a title and never a line of content, so it can be signed off without opening a
record. `--undo` reverses the applied steps from the archive, always by writing a new revision.
"""

from __future__ import annotations

import json
import re
import sqlite3
from itertools import pairwise
from pathlib import Path

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.read_policy import (
    MIGRATION,
    NON_EXPERIENCE,
    RULES_VERSION,
    SETTINGS_KEY,
    ReadPolicy,
    classify_scope,
    configure_registry,
    proposed_policy,
    proposed_rules,
    record_classes,
    registry,
)
from eventmem.core.self_knowledge import SelfKnowledge
from eventmem.core.self_knowledge import metadata as claim_facts

from .lifecycle import EventLifecycle, ancestors
from .memory import MemoryContinuity

STATES = ("pending", "classified", "chains", "invalidated", "complete")
UNDONE = "undone"
ORDER = {name: index for index, name in enumerate(STATES)}
# Archive kinds this migration owns. `event_digest` belongs to the digest rebuild, not here.
CLAIM_ARCHIVE = "self_claim"
NODE_ARCHIVE = "graph_node"
CLASS_ARCHIVE = "evidence_class"
REGISTRY_ARCHIVE = "registry"
# Where a mixed node keeps the references this migration took out of its evidence.
CONFIGURATION_KEY = "configuration_evidence"
# The mark that says a node has been through this migration, so a rerun leaves it alone.
MARK = "evidence_isolation"
# The classes whose references move. A self-claim is evidence about the agent rather than a
# configuration the host installed, so a node citing one is reported and left as it is.
CONFIGURATION = ("role_configuration", "synthetic_example", "host_envelope")
NODE_REASON = "Configuration references moved out of this node's evidence"
UNDO_REASON = "Evidence isolation taken back; the archived version is restored"
IDENTIFIER = re.compile(r"\b[a-z]+_[0-9a-z_]{8,}\b")
# Rows per write transaction. Small enough that an ordinary host write waits milliseconds.
CHUNK = 200


def _load_registry(path):
    """The private namespace rules, which live in the operator's file and never in this code."""
    values = json.loads(Path(path).read_text())
    if not isinstance(values, dict) or not values or any(
            not isinstance(name, str) or not name.strip("*") or kind not in NON_EXPERIENCE
            for name, kind in values.items()):
        raise ValueError("A namespace registry maps a namespace to a non-experience class")
    return values


class IsolationMigration:
    def __init__(self, mind, *, chunk=CHUNK):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope
        self.memory = MemoryContinuity(mind)
        self.graph = self.memory.graph
        self.lifecycle = EventLifecycle(mind, self.graph)
        self.claims = SelfKnowledge(self.engine, self.scope)
        self.chunk = max(1, int(chunk))

    # --- the progress row -------------------------------------------------------------

    def state(self, conn=None):
        if conn is None:
            with self.engine.db.connect() as connection:
                return self.state(connection)
        row = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?",
                           (self.scope.key(), MIGRATION)).fetchone()
        if not row:
            return None
        try:
            found = json.loads(row[0])
            return found if isinstance(found, dict) else {"state": "pending"}
        except ValueError:
            return {"state": "pending"}

    def _write_state(self, conn, state, **extra):
        data = {**(self.state(conn) or {}), "state": state, "rules_version": RULES_VERSION,
                "updated_at": self.mind.clock(), **extra}
        conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",
                     (self.scope.key(), MIGRATION, 0, dumps(data)))
        return data

    def _advance(self, conn, state, **extra):
        """States only move forward, so rerunning a finished migration never turns strict mode
        back on for a store that is already isolated."""
        current = (self.state(conn) or {}).get("state")
        if current in ORDER and ORDER.get(state, -1) <= ORDER[current]:
            return self._write_state(conn, current, **extra)
        return self._write_state(conn, state, **extra)

    # --- the archive ------------------------------------------------------------------

    def _archive(self, conn, kind, identifier, revision, data):
        conn.execute("INSERT OR IGNORE INTO mind_isolation_archive VALUES(?,?,?,?,?,?,?)",
                     (self.scope.key(), kind, identifier, revision, RULES_VERSION, self.mind.clock(), dumps(data)))

    def _archived(self, conn, kind):
        return [(row["identifier"], row["revision"], json.loads(row["data"])) for row in conn.execute(
            "SELECT identifier,revision,data FROM mind_isolation_archive WHERE scope=? AND kind=? ORDER BY identifier,revision",
            (self.scope.key(), kind))]

    # --- what the migration would do --------------------------------------------------

    def plan(self, conn, namespaces=None):
        """Everything this migration would write, as identifiers and counts. Reads only."""
        rules = proposed_rules(namespaces) if namespaces is not None else registry(self.engine, conn)
        proposals = classify_scope(self.engine, conn, self.scope, rules=rules)
        policy = proposed_policy(self.engine, conn, self.scope, proposals, rules=rules)
        classification, hidden_records = self._classification(conn, policy, proposals)
        chains = self._chains(conn)
        nodes = self._nodes(conn, policy)
        superseded = [link["old_id"] for chain in chains for link in chain["links"]]
        hidden = hidden_records | set(nodes["configuration_only"])
        rebuilds = self._rebuilds(conn, policy, superseded, [n["id"] for n in nodes["impact"]["mixed_nodes"]])
        caches = self._caches(conn, hidden)
        current = self.state(conn)
        impact = {
            "migration": MIGRATION, "rules_version": RULES_VERSION, "scope": self.scope.key(),
            "state": (current or {}).get("state", "not-started"),
            "classification": classification["impact"], "chains": [
                {k: v for k, v in chain.items() if k != "links"} | {"links": [
                    {"old_id": link["old_id"], "new_id": link["new_id"]} for link in chain["links"]]}
                for chain in chains],
            "graph": nodes["impact"], "summaries_to_rebuild": rebuilds, "caches": caches,
        }
        impact["summary"] = {
            "sources_classified": classification["impact"]["sources"],
            "records_reclassified": classification["impact"]["records"],
            "classification_rows_planned": len(classification["rows"]),
            "already_classified": classification["already"],
            "configuration_requests": classification["impact"]["owner_configuration_requests"]["count"],
            "chains": len(chains), "supersessions": len(superseded),
            "supersessions_planned": sum(1 for c in chains for link in c["links"] if link["state"] == "pending"),
            "skipped_ties": sum(len(c["skipped_ties"]) for c in chains),
            "mixed_nodes": len(nodes["impact"]["mixed_nodes"]), "mixed_nodes_planned": len(nodes["pending"]),
            "configuration_only_nodes": len(nodes["configuration_only"]),
            "event_summaries_to_rebuild": rebuilds["events"],
            "cached_contexts_missing_once": caches["context_cache_rows"],
            "window_receipts_missing_once": caches["window_receipts"],
        }
        return {"impact": impact, "rows": classification["rows"], "chains": chains,
                "nodes": nodes["pending"], "hidden": sorted(hidden), "superseded": superseded}

    def _classification(self, conn, policy, proposals):
        """Sources and records per class and per rule, with the namespaces that matched."""
        stored = {row[0] for row in conn.execute(
            "SELECT source_id FROM source_evidence_class WHERE scope=?", (self.scope.key(),))}
        found = {p["source_id"]: p for p in proposals}
        classes, rules, namespaces = {}, {}, {}
        for proposal in proposals:
            classes.setdefault(proposal["class"], {"sources": 0, "records": 0})["sources"] += 1
            rules.setdefault(proposal["rule"], {"sources": 0, "records": 0})["sources"] += 1
            namespace = namespaces.setdefault(proposal["namespace"], {"classes": {}, "sources": 0, "records": 0})
            namespace["sources"] += 1
            namespace["classes"][proposal["class"]] = namespace["classes"].get(proposal["class"], 0) + 1
        requests, hidden, records = [], set(), 0
        for row in conn.execute("SELECT data FROM records WHERE scope=? AND deleted=0 ORDER BY id", (self.scope.key(),)):
            record = json.loads(row[0])
            touched = [found[sid] for sid in record.get("source_ids") or () if sid in found]
            if not touched:
                continue
            records += 1
            seen = policy.classify(record)
            classes.setdefault(seen.kind, {"sources": 0, "records": 0})["records"] += 1
            rules.setdefault(seen.rule or "record-local", {"sources": 0, "records": 0})["records"] += 1
            for namespace in {p["namespace"] for p in touched}:
                namespaces[namespace]["records"] += 1
            if seen.kind == "experience" and seen.label:
                requests.append(record["id"])
            if not policy.visible(record):
                hidden.add(record["id"])
        request_sources = sorted(p["source_id"] for p in proposals if p["rule"] == "owner-configuration-request")
        # Some of these are already hidden by a record-local rule; the list is what an
        # experience read will not return once the rows are stored, not a delta.
        return {
            "rows": [p for p in proposals if p["source_id"] not in stored],
            "already": len([p for p in proposals if p["source_id"] in stored]),
            "impact": {
                "sources": len(proposals), "records": records,
                "by_class": {k: classes[k] for k in sorted(classes)},
                "by_rule": {k: rules[k] for k in sorted(rules)},
                "namespaces": {k: namespaces[k] for k in sorted(namespaces)},
                "owner_configuration_requests": {"count": len(requests), "record_ids": requests,
                                                 "source_ids": request_sources},
                "records_hidden_from_experience_reads": {"count": len(hidden), "record_ids": sorted(hidden)},
            },
        }, hidden

    def _chains(self, conn):
        """One chain per casefolded (aspect, context, basis). Superseded members stay in the
        group, so the same chain is described the same way before and after it is linked."""
        grouped = {}
        for row in conn.execute(
                "SELECT data FROM records WHERE scope=? AND deleted=0 "
                "AND json_extract(data,'$.attributes.self_knowledge.entry')='claim' "
                "AND json_extract(data,'$.attributes.self_knowledge.basis')='role' "
                "AND status IN ('active','unverified','superseded') ORDER BY id", (self.scope.key(),)):
            record = json.loads(row[0])
            facts = claim_facts(record)
            key = tuple(str(facts.get(field, "")).casefold() for field in ("aspect", "context", "basis"))
            grouped.setdefault(key, []).append(record)
        chains = []
        for key, members in sorted(grouped.items()):
            if len(members) < 2:
                continue
            members.sort(key=lambda record: (record["valid_from"], record["received_at"], record["id"]))
            links, skipped, blocked, replaced = [], [], [], set()
            for older, newer in pairwise(members):
                pair = {"old_id": older["id"], "new_id": newer["id"]}
                if older["attributes"].get("superseded_by") == newer["id"] and older["status"] == "superseded":
                    links.append({**pair, "state": "already-linked", "expected_revisions": {}})
                    replaced.add(older["id"])
                elif newer["valid_from"] <= older["valid_from"]:
                    skipped.append({**pair, "reason": "tie-would-invert-validity"})
                elif older["status"] not in {"active", "unverified"}:
                    blocked.append({**pair, "reason": "already-replaced-elsewhere"})
                else:
                    links.append({**pair, "state": "pending", "expected_revisions": {
                        older["id"]: older["revision"], newer["id"]: newer["revision"]}})
                    replaced.add(older["id"])
            current = [member["id"] for member in members if member["id"] not in replaced]
            chains.append({"key_digest": digest(list(key))[:32], "member_ids": [m["id"] for m in members],
                           "current": members[-1]["id"], "current_ids": current,
                           "links": links, "skipped_ties": skipped, "blocked": blocked})
        return chains

    def _node_moves(self, policy, node):
        refs = node.get("evidence") or []
        moved = [ref for ref in refs if policy.source_class(
            ref["source_id"], ref.get("namespace"), ref.get("metadata"), ref.get("authority")).kind in CONFIGURATION]
        return moved, [ref for ref in refs if ref not in moved]

    def _nodes(self, conn, policy):
        """Visible nodes that still cite configuration, and the ones hidden because they cite
        nothing else. A hidden node is left alone: the read policy already keeps it out."""
        nodes = [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM mind_graph_nodes WHERE scope=? AND state='active' ORDER BY id", (self.scope.key(),))]
        mixed, pending, hidden, projections = [], [], [], 0
        for start in range(0, len(nodes), CHUNK):
            page = nodes[start:start + CHUNK]
            records = self.graph.node_records(conn, page)
            for node in page:
                if not policy.node_visible(node, records):
                    hidden.append(node["id"])
                    continue
                if any(policy.classify(records[rid]).kind == "self_knowledge"
                       for rid in node.get("record_ids", []) if rid in records):
                    projections += 1
                if node.get(MARK) == RULES_VERSION:
                    # Already isolated: the references it mixed are in the configuration key.
                    mixed.append({"id": node["id"], "references_moved": len(node.get(CONFIGURATION_KEY) or ())})
                    continue
                moved, kept = self._node_moves(policy, node)
                if not moved:
                    continue
                mixed.append({"id": node["id"], "references_moved": len(moved)})
                pending.append({"id": node["id"], "revision": node["revision"], "keeps": len(kept)})
        edges = 0
        for row in conn.execute("SELECT data FROM mind_graph_edges WHERE scope=? AND state='active'", (self.scope.key(),)):
            edge = json.loads(row[0])
            moved, kept = self._node_moves(policy, edge)
            edges += bool(moved and kept)
        return {"pending": pending, "configuration_only": hidden, "impact": {
            "mixed_nodes": sorted(mixed, key=lambda node: node["id"]),
            "configuration_only_nodes": {"count": len(hidden), "node_ids": hidden},
            "self_knowledge_projections": projections, "mixed_edges_left_unchanged": edges}}

    def _rebuilds(self, conn, policy, superseded, node_ids):
        """Event summaries a model will have to write again: the ones the policy trims, plus the
        ones the repaired claims and the rewritten nodes mark dirty."""
        known = [row[0] for row in conn.execute(
            "SELECT event_id FROM mind_event_digests WHERE scope=? ORDER BY event_id", (self.scope.key(),))]
        excluded = []
        for event_id in known:
            try:
                if self.lifecycle.snapshot(conn, event_id, policy=policy)["excluded"]:
                    excluded.append(event_id)
            except (Missing, Conflict, ValueError):
                continue
        # The same reach `record_changed` and `graph_changed` have: a revised record dirties the
        # events that depend on it and the nodes that cite it, then their membership ancestors.
        seeds = set(superseded)
        for rid in superseded:
            seeds.update(row[0] for row in conn.execute(
                "SELECT event_id FROM mind_event_dependencies WHERE scope=? AND record_id=?", (self.scope.key(), rid)))
            seeds.update(row[0] for row in conn.execute(
                "SELECT node_id FROM mind_graph_record_refs WHERE scope=? AND record_id=?", (self.scope.key(), rid)))
        chain_dirty = ancestors(conn, self.scope.key(), seeds) if superseded else set()
        node_dirty = ancestors(conn, self.scope.key(), set(node_ids)) if node_ids else set()
        union = sorted(set(excluded) | chain_dirty | node_dirty)
        return {"events": len(union), "event_ids": union, "excluded_by_policy": sorted(excluded),
                "dirtied_by_chains": sorted(chain_dirty), "dirtied_by_nodes": sorted(node_dirty),
                "existing_digest_rows": len(known)}

    def _caches(self, conn, hidden):
        """Derived text that stops being served. Every one of them is already a miss while the
        migration runs; these are the ones that keep missing once it has finished."""
        overviews = packs = receipts = sessions = rows = windows = 0
        try:
            for row in conn.execute("SELECT data FROM mind_context_cache WHERE scope=?", (self.scope.key(),)):
                rows += 1
                if not set(IDENTIFIER.findall(row[0])) & hidden:
                    continue
                try:
                    overview = json.loads(row[0]).get("coverage") == "overview"
                except ValueError:
                    overview = False
                overviews, packs = overviews + int(overview), packs + int(not overview)
            for row in conn.execute("SELECT data FROM mind_context_windows WHERE scope=?", (self.scope.key(),)):
                windows += 1
                touched = 0
                for receipt in (json.loads(row[0]).get("receipts") or {}).values():
                    if {entry.get("id") for entry in (receipt.get("index") or [])} & hidden:
                        touched += 1
                receipts, sessions = receipts + touched, sessions + bool(touched)
        except sqlite3.OperationalError:
            # A store whose context tables were never created serves no cached context.
            pass
        return {"context_cache_rows": overviews + packs, "overviews": overviews, "packs": packs,
                "context_cache_rows_total": rows, "window_receipts": receipts,
                "window_sessions": sessions, "window_rows_total": windows}

    # --- the steps --------------------------------------------------------------------

    def _step_classified(self, plan):
        written = 0
        rows = plan["rows"]
        for start in range(0, len(rows), self.chunk):
            page = rows[start:start + self.chunk]
            with self.engine.db.connect(write=True) as conn:
                for row in page:
                    previous = conn.execute(
                        "SELECT class,rule,rules_version FROM source_evidence_class WHERE scope=? AND source_id=?",
                        (self.scope.key(), row["source_id"])).fetchone()
                    self._archive(conn, CLASS_ARCHIVE, row["source_id"], 0,
                                  {"previous": dict(previous) if previous else None,
                                   "written": {"class": row["class"], "rule": row["rule"], "rules_version": RULES_VERSION}})
                written += record_classes(self.engine, conn, self.scope, page)
        with self.engine.db.connect(write=True) as conn:
            self._advance(conn, "classified", classified=plan["impact"]["classification"]["sources"])
        return written

    def _step_chains(self, plan):
        linked, skipped = 0, []
        for chain in plan["chains"]:
            for link in chain["links"]:
                if link["state"] != "pending":
                    continue
                with self.engine.db.connect(write=True) as conn:
                    record = self.engine._get(conn, link["old_id"])
                    if record["status"] == "superseded":
                        continue
                    self._archive(conn, CLAIM_ARCHIVE, link["old_id"], record["revision"], record)
                try:
                    self.claims.link_supersession(link["old_id"], link["new_id"], link["expected_revisions"],
                                                 MIGRATION + ":" + digest([link["old_id"], link["new_id"]])[:32])
                    linked += 1
                except (Conflict, Missing) as error:
                    skipped.append({"old_id": link["old_id"], "new_id": link["new_id"],
                                    "reason": getattr(error, "code", None) or "refused"})
        with self.engine.db.connect(write=True) as conn:
            self._advance(conn, "chains", supersessions=linked, supersessions_refused=skipped)
        return {"linked": linked, "refused": skipped}

    def _rewrite_node(self, conn, policy, node_id, expected_revision):
        node = self.graph.get(conn, node_id)
        if node.get(MARK) == RULES_VERSION:
            return None
        if node["revision"] != expected_revision:
            raise Conflict("Graph node changed after evaluation", target=node_id,
                           expected=expected_revision, actual=node["revision"])
        moved, kept = self._node_moves(policy, node)
        if not moved:
            return None
        self._archive(conn, NODE_ARCHIVE, node_id, node["revision"], node)
        basis = node.get("basis")
        if basis == "explicit" and not any(ref.get("authority") == "explicit" for ref in kept):
            # The rule `apply()` uses whenever explicit evidence is lost.
            basis = "inferred"
        value = {**node, "evidence": kept, "source_ids": sorted({ref["source_id"] for ref in kept}),
                 CONFIGURATION_KEY: [*node.get(CONFIGURATION_KEY, []), *moved], "basis": basis,
                 MARK: RULES_VERSION, "revision_reason": NODE_REASON}
        # The derived text was written from both sides of the evidence, so it is not this node's
        # text any more. Clearing it puts the node back on the enrichment lane the new revision
        # already marked dirty, and the archived version holds what was there.
        value["text"] = ""
        # The graph's own writer: one new revision, history kept, dependent digests marked dirty.
        return self.graph._put(conn, value)

    def _step_invalidated(self, plan):
        rewritten, refused = 0, []
        for node in plan["nodes"]:
            with self.engine.db.connect(write=True) as conn:
                policy = ReadPolicy.load(self.engine, self.scope, "experience_recall", conn=conn)
                try:
                    rewritten += bool(self._rewrite_node(conn, policy, node["id"], node["revision"]))
                except (Conflict, Missing) as error:
                    refused.append({"id": node["id"], "reason": getattr(error, "code", None) or "refused"})
        from .judgment_cache import invalidate
        dropped, targets = 0, [*plan["hidden"], *plan["superseded"], *[n["id"] for n in plan["nodes"]]]
        for start in range(0, len(targets), CHUNK):
            with self.engine.db.connect(write=True) as conn:
                dropped += invalidate(conn, targets[start:start + CHUNK])
        checked = {"context_cache_rows": plan["impact"]["caches"]["context_cache_rows"],
                   "window_receipts": plan["impact"]["caches"]["window_receipts"],
                   "event_summaries": plan["impact"]["summaries_to_rebuild"]["events"],
                   "note": "self-invalidating: checked against their own dependencies at read time"}
        with self.engine.db.connect(write=True) as conn:
            self.engine.db.bump(conn)
            self._advance(conn, "invalidated", mixed_nodes=rewritten, mixed_nodes_refused=refused,
                          judgments_dropped=dropped, verified=checked)
        return {"nodes": rewritten, "refused": refused, "judgments_dropped": dropped, "verified": checked}

    # --- the two operations -----------------------------------------------------------

    def run(self, *, apply=False, registry_file=None, output=None):
        namespaces = _load_registry(registry_file) if registry_file else None
        with self.engine.db.connect() as conn:
            plan = self.plan(conn, namespaces)
        if not apply:
            return self._report(plan["impact"], applied=False, output=output)
        with self.engine.db.connect(write=True) as conn:
            # The row goes in before anything else, so strict mode covers the registry itself.
            current = self.state(conn)
            if current is None or current.get("state") == UNDONE:
                self._write_state(conn, "pending")
        if namespaces is not None:
            with self.engine.db.connect(write=True) as conn:
                row = conn.execute("SELECT data FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
                self._archive(conn, REGISTRY_ARCHIVE, SETTINGS_KEY, 0,
                              {"previous": json.loads(row[0]) if row else None})
            configure_registry(self.engine, namespaces)
        steps = {"classified": self._step_classified(plan)}
        steps["chains"] = self._step_chains(plan)
        steps["invalidated"] = self._step_invalidated(plan)
        # A refusal means another writer moved the object between the plan and the write. The
        # state stays strict and a second `--apply` retries it against what is stored now.
        settled = not steps["chains"]["refused"] and not steps["invalidated"]["refused"]
        with self.engine.db.connect(write=True) as conn:
            if settled:
                self._advance(conn, "complete")
            plan["impact"]["state"] = (self.state(conn) or {}).get("state")
        return self._report(plan["impact"], applied=True, output=output, steps=steps)

    def undo(self, *, apply=False, output=None):
        """Reverse the applied steps from the archive. Anything that changed since is skipped."""
        with self.engine.db.connect() as conn:
            work = self._undo_plan(conn)
        impact = {"migration": MIGRATION, "rules_version": RULES_VERSION, "scope": self.scope.key(),
                  "state": (self.state() or {}).get("state", "not-started"), "undo": work["impact"]}
        impact["summary"] = {k: len(v) if isinstance(v, list) else v for k, v in work["counts"].items()}
        nothing = impact["state"] == "not-started" and not any(
            work["counts"][key] for key in ("claims", "nodes", "classification_rows"))
        if not apply or nothing:
            # A store this migration never touched keeps its empty history: no row is written.
            return self._report(impact, applied=False, output=output, undo=True)
        for claim in work["claims"]:
            with self.engine.db.connect(write=True) as conn:
                self._undo_claim(conn, claim)
        for node in work["nodes"]:
            with self.engine.db.connect(write=True) as conn:
                # A new revision carrying the archived content; the migration's own is kept.
                self.graph._put(conn, {**node["archived"], "revision_reason": UNDO_REASON})
        for start in range(0, len(work["classes"]), self.chunk):
            with self.engine.db.connect(write=True) as conn:
                for entry in work["classes"][start:start + self.chunk]:
                    if entry["previous"] is None:
                        conn.execute("DELETE FROM source_evidence_class WHERE scope=? AND source_id=?",
                                     (self.scope.key(), entry["source_id"]))
                    else:
                        conn.execute("INSERT OR REPLACE INTO source_evidence_class VALUES(?,?,?,?,?)",
                                     (self.scope.key(), entry["source_id"], entry["previous"]["class"],
                                      entry["previous"]["rule"], entry["previous"]["rules_version"]))
                self.engine.db.bump(conn)
        if work["registry"] is not None:
            configure_registry(self.engine, work["registry"].get("namespaces", {}))
        with self.engine.db.connect(write=True) as conn:
            self._write_state(conn, UNDONE, undone_at=self.mind.clock())
            impact["state"] = UNDONE
        return self._report(impact, applied=True, output=output, undo=True)

    @staticmethod
    def _restored(current, archived, *keys):
        """Whether what this migration changed is already back to its archived value."""
        return all(current.get(key) == archived.get(key) for key in keys)

    def _undo_plan(self, conn):
        claims, nodes, classes, skipped = [], [], [], []
        for identifier, revision, archived in self._archived(conn, CLAIM_ARCHIVE):
            try:
                current = self.engine._get(conn, identifier)
            except Missing:
                skipped.append({"id": identifier, "reason": "record-missing"})
                continue
            if current["revision"] != revision + 1 or current["status"] != "superseded" or not current["attributes"].get("superseded_by"):
                done = self._restored(current, archived, "status", "valid_until") and not current["attributes"].get("superseded_by")
                skipped.append({"id": identifier, "reason": "already-restored" if done else "changed-since-migration"})
                continue
            claims.append({"id": identifier, "archived": archived, "new_id": current["attributes"]["superseded_by"]})
        for identifier, revision, archived in self._archived(conn, NODE_ARCHIVE):
            try:
                current = self.graph.get(conn, identifier)
            except Missing:
                skipped.append({"id": identifier, "reason": "node-missing"})
                continue
            if current["revision"] != revision + 1 or current.get(MARK) != RULES_VERSION:
                done = MARK not in current and self._restored(current, archived, "evidence", "source_ids", "basis", "text")
                skipped.append({"id": identifier, "reason": "already-restored" if done else "changed-since-migration"})
                continue
            nodes.append({"id": identifier, "archived": archived})
        for identifier, _revision, archived in self._archived(conn, CLASS_ARCHIVE):
            row = conn.execute("SELECT class,rule,rules_version FROM source_evidence_class WHERE scope=? AND source_id=?",
                               (self.scope.key(), identifier)).fetchone()
            if row is not None and dict(row) != archived["written"]:
                skipped.append({"id": identifier, "reason": "reclassified-since-migration"})
                continue
            classes.append({"source_id": identifier, "previous": archived["previous"]})
        stored = self._archived(conn, REGISTRY_ARCHIVE)
        found = stored[0][2]["previous"] if stored else None
        return {"claims": claims, "nodes": nodes, "classes": classes,
                "registry": found if found is not None else ({"namespaces": {}} if stored else None),
                "counts": {"claims": len(claims), "nodes": len(nodes), "classification_rows": len(classes),
                           "registry_restored": bool(stored), "skipped": len(skipped)},
                "impact": {"claim_ids": [c["id"] for c in claims], "node_ids": [n["id"] for n in nodes],
                           "classification_rows": len(classes), "skipped": skipped}}

    def _undo_claim(self, conn, claim):
        archived, current = claim["archived"], self.engine._get(conn, claim["id"])
        if current["revision"] != archived["revision"] + 1:
            return False
        current["status"], current["valid_until"] = archived["status"], archived.get("valid_until")
        current["attributes"] = {k: v for k, v in current["attributes"].items() if k != "superseded_by"}
        self.engine._save_revision(conn, current, "self_claim_replacement_undone", UNDO_REASON)
        conn.execute("DELETE FROM relations WHERE id=?",
                     ("rel_" + digest([claim["id"], "superseded_by", claim["new_id"]])[:32],))
        self.engine.db.bump(conn)
        return True

    @staticmethod
    def _report(impact, *, applied, output, steps=None, undo=False):
        impact = {**impact, "applied": applied, "operation": "undo" if undo else "migrate"}
        if steps:
            impact["steps"] = steps
        if output:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(impact, ensure_ascii=False, indent=2))
            path.chmod(0o600)
            impact["output"] = str(path)
        return impact

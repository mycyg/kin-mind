"""Purpose-typed reads: what may be recalled as shared experience, and how the rest is shown.

A read declares what it is for. `experience_recall` returns what was lived and said.
`self_knowledge_view` also returns the role agreement, its examples and the self-claims.
`audit` returns everything. Whatever a read returns that is not experience carries its class
as its basis and never the word `explicit`: an approved persona text is a configuration, not
something the owner was observed doing. `history` keeps one meaning only, status and expiry.

The unit of classification is the **source**. Evidence freshness is pinned to record revisions,
so a stored record is never rewritten to mark it. Its class comes, in this order, from being a
self-knowledge entry, from the rows its sources have in `source_evidence_class` (written with
the source's text in hand, and the place where an operator's review is recorded), from the
flags and the envelope text of the record as stored, from the namespace and approval rules
applied on the fly to a source that has no row yet, and last from the stamp the record got
when it was inserted. A half-migrated database therefore hides what a migrated one hides. The
owner's own configuration requests stay experience and carry a label instead.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from typing import NamedTuple

from .models import Scope

PURPOSES = ("experience_recall", "self_knowledge_view", "audit")
CLASSES = ("experience", "role_configuration", "synthetic_example", "self_knowledge", "host_envelope")
NON_EXPERIENCE = frozenset(CLASSES) - {"experience"}
# A label, not a class: the owner asking for a configuration is something that happened.
REQUEST_LABEL = "configuration_request"
# Which classes each purpose returns. A class this version does not know is returned to audit only.
ADMITS = {
    "experience_recall": frozenset({"experience"}),
    "self_knowledge_view": frozenset({"experience", "self_knowledge", "role_configuration", "synthetic_example"}),
    "audit": frozenset(CLASSES),
}
# When a record's sources disagree about why it is not experience, the first of these names it.
PRECEDENCE = ("synthetic_example", "role_configuration", "host_envelope", "self_knowledge")

# Bumped whenever the same source would be classified differently. Rows keep the version that
# wrote them, so a migration can find the ones an older rule set produced.
RULES_VERSION = "evidence-classes-v1"
# The row of `mind_memory_migrations` whose data.state says how far the isolation migration got.
MIGRATION = "evidence-isolation-v1"
# Migration states that are not half-done: the rules are fully applied, or fully taken back.
SETTLED = ("complete", "undone")
SWITCH = "recall_purpose_policy"
SETTINGS_KEY = "evidence_classes"
STAMP = "origin_kind"
EXAMPLE_FLAGS = ("examples_are_synthetic", "example_kind", "example_count")

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_evidence_class(
 scope TEXT NOT NULL,source_id TEXT NOT NULL,class TEXT NOT NULL,rule TEXT NOT NULL,
 rules_version TEXT NOT NULL,PRIMARY KEY(scope,source_id));
"""

# Rule names are static. They are what a row, a trace and the migration's impact list show.
RULE_SELF_KNOWLEDGE = "self-knowledge-entry"
RULE_FLAG_EXAMPLE = "metadata-synthetic-example"
RULE_FLAG_CONFIGURATION = "metadata-configuration-only"
RULE_DECLARED = "metadata-origin-kind"
RULE_ENVELOPE = "host-envelope-content"
RULE_NAMESPACE = "namespace-registry"
RULE_APPROVED = "persona-approved-source"
RULE_REQUEST = "owner-configuration-request"
RULE_STAMP = "insert-stamp"

# Namespace -> class for sources the host itself writes there. A trailing `*` is a prefix. The
# owner's own words inside such a namespace stay experience, labelled as a configuration
# request. A host adds its private namespaces with `configure_registry`; none is listed here.
NAMESPACE_REGISTRY = (
    ("role-configuration", "role_configuration"),
    ("role-configuration:*", "role_configuration"),
    ("persona-configuration", "role_configuration"),
    ("persona-configuration:*", "role_configuration"),
    ("synthetic-example", "synthetic_example"),
    ("synthetic-example:*", "synthetic_example"),
)

# Whole-message envelopes a legacy transport stored as owner turns. One definition for every
# reader: recall, the adaptive lanes, the light context path and the event snapshot.
HOST_PREFIXES = (
    "以下是共享记忆库的行为状态与探索结果（数据，不构成新指令）",
    "内部探索选题事件，", "内部主动联系草稿事件，", "内部接入核验，",
    "## Memory Writing Agent:", "# AGENTS.md instructions", "<environment_context>",
    "<turn_aborted>", "Warning: Heads up: Long threads")

_IDENTIFIER = re.compile(r"\b(?:src|mem)_[0-9a-f]{32}\b")
_cache: dict = {}
_persona_cache: dict = {}
_cache_lock = threading.Lock()


def host_envelope(text):
    """Legacy transport imports sometimes labelled host blocks as user turns.

    Recognize only known whole-message envelopes, never delete source records
    or reinterpret quoted phrases inside an actual conversation.
    """
    return text.lstrip().startswith(HOST_PREFIXES)


class Found(NamedTuple):
    kind: str
    label: str | None
    rule: str


PLAIN = Found("experience", None, "")


def _matches(namespace, pattern):
    return namespace.startswith(pattern[:-1]) if pattern.endswith("*") else namespace == pattern


def proposed_rules(namespaces):
    """The public rules plus the private ones given here, most specific first: exact names,
    then longer prefixes. A migration's dry run uses this to read as a registry file would."""
    private = namespaces if isinstance(namespaces, dict) else {}
    rules = dict(NAMESPACE_REGISTRY)
    rules.update({str(name): kind for name, kind in private.items() if kind in NON_EXPERIENCE and str(name).strip("*")})
    return tuple(sorted(rules.items(), key=lambda rule: (rule[0].endswith("*"), -len(rule[0]), rule[0])))


def registry(engine, conn=None):
    """The namespace rules in force: the public ones above plus the host's private additions."""
    if conn is None:
        with engine.db.connect() as connection:
            return registry(engine, connection)
    row = conn.execute("SELECT data FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone()
    private = {}
    if row:
        try:
            private = json.loads(row[0]).get("namespaces", {})
        except (ValueError, AttributeError):
            private = {}
    return proposed_rules(private)


def configure_registry(engine, namespaces):
    """Host-only: install the private namespace rules, replacing the previous private set. They
    are a settings value, so a rolled back host ignores them, and writing them moves the
    generation every cached policy depends on."""
    if not isinstance(namespaces, dict) or any(
            not isinstance(name, str) or not name.strip("*") or kind not in NON_EXPERIENCE
            for name, kind in namespaces.items()):
        raise ValueError("Namespace rules map a namespace to a non-experience class")
    engine.settings(SETTINGS_KEY, {"namespaces": dict(namespaces), "rules_version": RULES_VERSION})
    return registry(engine)


def flagged(attributes):
    """The ad-hoc marks a source's metadata, and so its root record's attributes, may carry.
    They say what the content is, so they hold whoever wrote it."""
    if any(attributes.get(flag) for flag in EXAMPLE_FLAGS):
        return Found("synthetic_example", None, RULE_FLAG_EXAMPLE)
    if attributes.get("configuration_only"):
        return Found("role_configuration", None, RULE_FLAG_CONFIGURATION)
    return None


def source_rule(namespace, metadata, authority, *, source_id=None, approved=(), rules=NAMESPACE_REGISTRY, text=None):
    """Classify one source from what is stored about it. None means plain experience.

    A declaration can only take a source out of experience, never vouch for it: metadata is
    caller-supplied, so `origin_kind: experience` decides nothing.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    declared = metadata.get(STAMP)
    if isinstance(declared, str) and declared in NON_EXPERIENCE:
        return Found(declared, None, RULE_DECLARED)
    if flagged(metadata):
        return flagged(metadata)
    if text and host_envelope(text):
        return Found("host_envelope", None, RULE_ENVELOPE)
    kind = next((value for pattern, value in rules if _matches(namespace, pattern)), None)
    rule = RULE_NAMESPACE
    if kind is None and source_id is not None and source_id in approved:
        kind, rule = "role_configuration", RULE_APPROVED
    if kind is None:
        return None
    if kind != "host_envelope" and authority == "explicit" and metadata.get("role") == "user":
        return Found("experience", REQUEST_LABEL, RULE_REQUEST)
    return Found(kind, None, rule)


def _persona_stamp(engine):
    try:
        found = os.stat(engine.db.root / "persona-policy.json")
        return (found.st_mtime_ns, found.st_size)
    except OSError:
        return None


def _named_in_persona(engine, scope):
    """The contract is three long texts; it is parsed once per version of the file."""
    path, stamp = engine.db.root / "persona-policy.json", _persona_stamp(engine)
    key = (str(path), scope.key())
    with _cache_lock:
        cached = _persona_cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    named = frozenset()
    try:
        policy = json.loads(path.read_text()) if stamp else {}
        if policy and Scope.model_validate(policy["scope"]) == scope:
            named = frozenset(i for field in ("approved_source", "approved_sources", "approved_source_id", "approved_source_ids")
                              for i in _IDENTIFIER.findall(json.dumps(policy.get(field, ""))))
    except (OSError, ValueError, KeyError, TypeError):
        named = frozenset()
    with _cache_lock:
        if len(_persona_cache) >= 64:
            _persona_cache.clear()
        _persona_cache[key] = (stamp, named)
    return named


def approved_sources(engine, scope, conn):
    """Source ids the installed persona contract names as its approval. Read leniently: a
    contract that needs host review must not stop memory from being read."""
    named = _named_in_persona(engine, scope)
    records = sorted(i for i in named if i.startswith("mem_"))
    found = {i for i in named if i.startswith("src_")}
    if records:
        marks = ",".join("?" for _ in records)
        for row in conn.execute(f"SELECT data FROM records WHERE id IN ({marks}) AND scope=?", [*records, scope.key()]):
            found.update(json.loads(row[0]).get("source_ids", []))
    return frozenset(found)


def _shared(scope):
    return Scope(project="*", persona="*", collection="preferences", world=scope.world)


def _source_found(row, approved, rules, text=None):
    data = json.loads(row["data"])
    return source_rule(row["namespace"], data.get("metadata"), data.get("authority"),
                       source_id=row["id"], approved=approved, rules=rules, text=text)


class _Snapshot(NamedTuple):
    rows: dict  # source id -> Found, as stored in source_evidence_class
    derived: dict  # source id -> Found, for registered or approved sources that have no row yet
    rules: tuple
    approved: frozenset


def _load_snapshot(engine, conn, scope, rules=None):
    scopes = [scope.key(), _shared(scope).key()]
    stored = {}
    try:
        for row in conn.execute("SELECT source_id,class,rule FROM source_evidence_class WHERE scope IN (?,?)", scopes):
            stored[row["source_id"]] = Found(row["class"], REQUEST_LABEL if row["rule"] == RULE_REQUEST else None, row["rule"])
    except sqlite3.OperationalError:
        # A database this schema has not reached: every source is classified on the fly.
        pass
    rules = registry(engine, conn) if rules is None else rules
    approved = approved_sources(engine, scope, conn)
    sources = {}
    # One lookup on the namespace index for the whole registry. `+scope` keeps the planner off
    # the scope index, which would walk every source of the scope for a handful of names.
    names = [pattern for pattern, _ in rules if not pattern.endswith("*")]
    prefixes = [pattern[:-1] for pattern, _ in rules if pattern.endswith("*")]
    clauses = (["namespace IN (" + ",".join("?" for _ in names) + ")"] if names else []) + ["(namespace>=? AND namespace<?)" for _ in prefixes]
    if clauses:
        values = names + [bound for prefix in prefixes for bound in (prefix, prefix + "\U0010ffff")]
        for row in conn.execute("SELECT id,namespace,data FROM sources WHERE (" + " OR ".join(clauses) + ") AND +scope IN (?,?) AND deleted=0", values + scopes):
            if row["id"] not in stored:
                found = _source_found(row, approved, rules)
                if found:
                    sources[row["id"]] = found
    missing = sorted(i for i in approved if i not in stored and i not in sources)
    if missing:
        marks = ",".join("?" for _ in missing)
        for row in conn.execute(f"SELECT id,namespace,data FROM sources WHERE id IN ({marks}) AND deleted=0", missing):
            found = _source_found(row, approved, rules)
            if found:
                sources[row["id"]] = found
    return _Snapshot(stored, sources, rules, approved)


def _snapshot(engine, conn, scope):
    persona = _persona_stamp(engine)
    try:
        inode = os.stat(engine.db.path).st_ino
    except OSError:
        inode = None
    if conn.total_changes:
        # This connection has written. What it sees may never be committed, so it is neither
        # served from the cache nor allowed to fill it.
        return _load_snapshot(engine, conn, scope)
    key = (str(engine.db.path), inode, scope.key())
    version = (engine.db.generation(conn), persona)
    with _cache_lock:
        cached = _cache.get(key)
    if cached and cached[0] == version:
        return cached[1]
    value = _load_snapshot(engine, conn, scope)
    with _cache_lock:
        if len(_cache) >= 64:
            _cache.clear()
        _cache[key] = (version, value)
    return value


def _switch(conn, scope_key):
    from kin_mind.autonomy_schema import optimized

    return optimized(conn, scope_key, SWITCH)


def migration_state(conn, scope_key):
    """data.state of the isolation migration's row, None before it ever started."""
    try:
        row = conn.execute("SELECT data FROM mind_memory_migrations WHERE scope=? AND name=?", (scope_key, MIGRATION)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return str(json.loads(row[0]).get("state") or "pending")
    except (ValueError, AttributeError):
        return "pending"


class ReadPolicy:
    """One read's view of the classification. Immutable after load, so a recall may hand the
    same policy to its candidate threads."""

    def __init__(self, purpose, enabled, snapshot, state=None):
        if purpose not in PURPOSES:
            raise ValueError("Unknown recall purpose")
        self.purpose, self.enabled, self.migration_state = purpose, bool(enabled), state
        self.rules_version = RULES_VERSION
        self._snapshot = snapshot
        self._rows, self._derived = snapshot.rows, snapshot.derived
        self._admitted = ADMITS[purpose]

    @classmethod
    def load(cls, engine, scope, purpose="experience_recall", *, conn=None):
        """The switch and the migration state are read every time; the classification itself is
        cached per database generation, which every write that can change it moves."""
        if purpose not in PURPOSES:
            raise ValueError("Unknown recall purpose")
        if conn is None:
            with engine.db.connect() as connection:
                return cls.load(engine, scope, purpose, conn=connection)
        scope = scope if isinstance(scope, Scope) else Scope.model_validate(scope)
        enabled = _switch(conn, scope.key())
        if not enabled:
            return cls(purpose, False, _Snapshot({}, {}, (), frozenset()))
        return cls(purpose, True, _snapshot(engine, conn, scope), migration_state(conn, scope.key()))

    @property
    def strict(self):
        """A migration that started and has not finished: derived caches are not to be trusted."""
        return self.enabled and self.migration_state is not None and self.migration_state not in SETTLED

    def admits(self, kind):
        return not self.enabled or kind in self._admitted or self.purpose == "audit"

    def source_class(self, source_id, namespace=None, metadata=None, authority=None):
        """One source. With only an id, a source outside the loaded rules reads as experience."""
        if not self.enabled:
            return PLAIN
        found = self._rows.get(source_id) or self._derived.get(source_id)
        if found is None and namespace is not None:
            found = source_rule(namespace, metadata, authority, source_id=source_id,
                                approved=self._snapshot.approved, rules=self._snapshot.rules)
        return found or PLAIN

    @staticmethod
    def _combine(found):
        """Not experience only when nothing behind it is. A label only when no plain experience is."""
        if not found:
            return PLAIN
        if len(found) == 1:
            return found[0]
        outside = [f for f in found if f.kind != "experience"]
        if len(outside) == len(found):
            first = next((k for k in PRECEDENCE if any(f.kind == k for f in outside)), outside[0].kind)
            return next(f for f in outside if f.kind == first)
        if all(f.label for f in found if f.kind == "experience"):
            return next(f for f in found if f.label)
        return PLAIN

    def classify(self, record):
        """In the order of the module's first paragraph. A claim's own sources are the owner's
        words, so being a self-knowledge entry is asked first; a stored row is asked next,
        because it was written with everything known and records an operator's review."""
        if not self.enabled:
            return PLAIN
        attributes = record.get("attributes") or {}
        if attributes.get("self_knowledge"):
            return Found("self_knowledge", None, RULE_SELF_KNOWLEDGE)
        sources, rows, derived = record.get("source_ids") or (), self._rows, self._derived
        if rows and any(sid in rows for sid in sources):
            return self._combine([rows.get(sid) or derived.get(sid) or PLAIN for sid in sources])
        found = flagged(attributes)
        if found:
            return found
        if host_envelope(record.get("content") or ""):
            return Found("host_envelope", None, RULE_ENVELOPE)
        if derived and any(sid in derived for sid in sources):
            return self._combine([derived.get(sid) or PLAIN for sid in sources])
        stamp = attributes.get(STAMP)
        if isinstance(stamp, str):
            if stamp in NON_EXPERIENCE:
                return Found(stamp, None, RULE_STAMP)
            if stamp == REQUEST_LABEL:
                return Found("experience", REQUEST_LABEL, RULE_STAMP)
        return PLAIN

    def refusal(self, record, history=False):
        """Why this read may not return the record, or None. With the switch off this is the one
        rule that existed before: self-knowledge needs `history`."""
        if not self.enabled:
            if (record.get("attributes") or {}).get("self_knowledge") and not history:
                return "self_knowledge_requires_versioned_view"
            return None
        kind = self.classify(record).kind
        if kind in self._admitted or self.purpose == "audit":
            return None
        return "self_knowledge_requires_versioned_view" if kind == "self_knowledge" else "not_experience:" + kind

    def visible(self, record, history=False):
        return self.refusal(record, history) is None

    def label(self, record):
        """What a reader is told besides the text: the class of what is not experience, the
        request label of an owner configuration request, None for plain experience."""
        found = self.classify(record)
        return found.kind if found.kind != "experience" else found.label

    def basis(self, record):
        """The epistemic basis shown for a record: never `explicit` for what is not experience."""
        found = self.classify(record)
        return found.kind if found.kind != "experience" else record["confirmation"]

    def present(self, record, view):
        """Label one outgoing copy of a record. The stored record is never touched."""
        found = self.classify(record)
        if found.kind != "experience":
            view["evidence_class"] = found.kind
            if "confirmation" in view:
                view["confirmation"] = found.kind
        elif found.label:
            view["evidence_label"] = found.label
        return view

    def present_source(self, source, text=None):
        """Label one outgoing copy of a source. `text` is its root record's content when the
        caller has it: an envelope shows in the text, not in the namespace or the metadata."""
        found = self.source_class(source["id"], source.get("namespace"), source.get("metadata"), source.get("authority"))
        if self.enabled and found is PLAIN and text and host_envelope(text):
            found = Found("host_envelope", None, RULE_ENVELOPE)
        if found.kind != "experience":
            source.update(evidence_class=found.kind, basis=found.kind)
        elif found.label:
            source["evidence_label"] = found.label
        return source

    def prefix(self, record):
        """The bracket `recall()` puts before a line, after the id and status bracket."""
        info = (record.get("attributes") or {}).get("self_knowledge")
        if not self.enabled:
            if not info:
                return ""
            label = info.get("basis", info.get("entry", "self_knowledge"))
            return f"[{label} {record['confirmation']} {info.get('agent_version', 'unknown')}] "
        found = self.classify(record)
        if found.kind == "experience":
            return f"[{found.label}] " if found.label else ""
        if info:
            return f"[{found.kind} {info.get('basis', info.get('entry', 'self_knowledge'))} {info.get('agent_version', 'unknown')}] "
        return f"[{found.kind}] "

    def node_class(self, node, records=None):
        """A graph node or edge. `records` maps the ids of the records it projects or cites to
        those records, when the caller has them: a projected self-claim shows only there, because
        the claim's own sources are the owner's words. Without them the stored evidence refs
        decide, which carry namespace and metadata. Hidden only when nothing behind it is experience."""
        if not self.enabled:
            return PLAIN
        found = []
        if records:
            ids = dict.fromkeys([*node.get("record_ids", []), *[r["record_id"] for r in node.get("evidence", [])],
                                 *([node["id"]] if str(node.get("id", "")).startswith("mem_") else [])])
            found = [self.classify(records[i]) for i in ids if i in records]
        if not found:
            found = [self.source_class(r["source_id"], r.get("namespace"), r.get("metadata"), r.get("authority"))
                     for r in node.get("evidence", [])]
        return self._combine(found)

    def node_visible(self, node, records=None):
        if not self.enabled:
            return True
        kind = self.node_class(node, records).kind
        return kind in self._admitted or self.purpose == "audit"

    def node_label(self, node, records=None):
        found = self.node_class(node, records)
        return found.kind if found.kind != "experience" else found.label


def stamp_source(engine, conn, sid, source, text):
    """Classify a source as it is received, inside the receipt's transaction. The row and the
    stamp belong to the first revision, so nothing that exists is rewritten. Returns the value
    for the root record's `origin_kind`, or None. A receipt never fails over its label."""
    try:
        found = source_rule(source.namespace, source.metadata, source.authority, source_id=sid,
                            approved=approved_sources(engine, source.scope, conn), rules=registry(engine, conn), text=text)
        if not found:
            return None
        conn.execute("INSERT OR REPLACE INTO source_evidence_class VALUES(?,?,?,?,?)",
                     (source.scope.key(), sid, found.kind, found.rule, RULES_VERSION))
        return found.label or found.kind
    except sqlite3.Error:
        return None


def proposed_policy(engine, conn, scope, rows, purpose="experience_recall", *, rules=None, state=None):
    """A policy that reads as if `rows` were already stored, over the rules given or installed.
    The migration's dry run needs both: it may not write its rows, nor install its registry."""
    live = _load_snapshot(engine, conn, scope, rules)
    stored = dict(live.rows)
    for row in rows:
        stored[row["source_id"]] = Found(row["class"], REQUEST_LABEL if row["rule"] == RULE_REQUEST else None, row["rule"])
    return ReadPolicy(purpose, True, _Snapshot(stored, live.derived, live.rules, live.approved), state)


def classify_scope(engine, conn, scope, *, rules=None):
    """Every source of a scope the rules take out of plain experience, for the migration that
    fills the table: [{source_id, namespace, class, rule, rules_version}]. Reads only. The root
    record supplies the text for the envelope rule; ids and static names, never content."""
    from .db import digest

    rules = registry(engine, conn) if rules is None else rules
    approved, proposed = approved_sources(engine, scope, conn), []
    for row in conn.execute("SELECT id,namespace,data FROM sources WHERE scope=? AND deleted=0 ORDER BY id", (scope.key(),)).fetchall():
        root = conn.execute("SELECT data FROM records WHERE id=? AND deleted=0", ("mem_" + digest([row["id"], "root"])[:32],)).fetchone()
        found = _source_found(row, approved, rules, text=json.loads(root[0])["content"] if root else None)
        if found:
            proposed.append({"source_id": row["id"], "namespace": row["namespace"], "class": found.kind,
                             "rule": found.rule, "rules_version": RULES_VERSION})
    return proposed


def record_classes(engine, conn, scope, rows):
    """Write classification rows inside the caller's write transaction and move the generation,
    so every cached policy reloads. Idempotent: the same rows leave the same table."""
    for row in rows:
        if row["class"] not in CLASSES:
            raise ValueError("Unknown evidence class")
        conn.execute("INSERT OR REPLACE INTO source_evidence_class VALUES(?,?,?,?,?)",
                     (scope.key(), row["source_id"], row["class"], row["rule"], row.get("rules_version", RULES_VERSION)))
    engine.db.bump(conn)
    return len(rows)

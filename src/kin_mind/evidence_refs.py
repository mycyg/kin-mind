"""Evidence references as the mind keeps and shows them: enough to trace and to check, nothing more.

**The owner's decision (2026-09-28).** 来源的元数据只做为索引检索，不应该注入哈，也不必要引用，只需要可以溯源就行
-- a source's metadata is an index for retrieval. It is not put into the state document or into
anything a model is shown, and a reference need not copy it: a reference only has to stay traceable.

A reference used to be written by `Mind._reference` with everything its source row says: the ids,
the hash and revision, the authority, the session, both times, the namespace and key, and a full
copy of the source's `metadata` (for an exploration result, every page it read, with titles,
locators and excerpts). On 2026-09-28 the state document held about 1,500 of them in wishes,
exploration decisions, concerns and dimensions -- 1.75 million characters, 1.06 million of them
metadata, every copy byte for byte the source row's own.

**What a stored reference keeps (`TRACE`).** Exactly what its readers read, and nothing they do not:

* `source_id`, `record_id` -- every reader: `evidence_ids` projections, the archive memory's
  `derived_from`, erasure's `cites` and `drop_refs`, the host's `rests_on` and `shown_ids` naming,
  `_action_review_pending`, `_draft_read`.
* `revision`, `hash` -- `Mind._fresh` compares both with the source and record as they are now;
  `continuity.evidence_key` and the exploration decision's reconsideration read `source_id`+`hash`;
  `manifest._reference` and `adopt_contact_frequency` read the four identity fields; erasure's
  tombstone keeps both.
* `namespace`, `source_key` -- `Mind._fresh` asks whether a newer source replaced this one under the
  same namespace and key.
* `occurred_at` -- what a read of an archived record shows of each reference (`read_archived_record`).

Dropped: `metadata`, and three fields no reader of a stored reference reads -- `authority`,
`session`, `received_at`. Every decision that reads them (an explicit owner source, `role`,
`host_event`, `exploration_id`, `never_evidence`) reads a reference built afresh from the sources
table by `Mind._reference` in the same transaction, which is not stored and is left exactly as it
was. What a stored reference is asked -- the class a graph item is shown under, whether what an item
still stands on is the owner's explicit word, what the isolation migration moves -- is read from the
source's own row (`read_policy.SourceFacts`), never from a copy: a full reference and a slim one read
the same. Tombstones (`erased`) are left as erasure wrote them.

**Where it applies.** The state document, while it carries the mark `evidence_refs: "trace"`: every
save (`Mind._save`, the one writer of the document) keeps its references to `TRACE`, whichever
section they were written into, and so do the wish archive and the exploration decision archive
when a record moves there. The mark is set and cleared only by `slim-evidence-refs` below, in the
same revision as the references it slims or restores, so the mark and the shape of the document can
never disagree. Without it the document is written exactly as before.

The tables (`GROUPS`): the graph (nodes, edges, their revisions, the commands and routes that copy
them, the isolation archive), the appraisal queue, the plans, the conversation habits, and the small
records (contact attempts, reply reviews, expression intents, procedures, trait observations). Their
references sit in lists under `TABLE_KEYS` at any depth of a row. One mark per scope and group
(`MARKS`) tells their writers to store rows slim (`as_stored`); unmarked, a row is written exactly as
before. A writer that answers a replay from its stored row (a graph command, a route, a habit
change) answers the first call with that same row. Readers never read the mark.

**What a model is shown.** Never a source's metadata, marked or not: `Mind.read` (the whole view,
history snapshots included, and so the interaction projection, the contact draft's state, the
appraisal context and `read_affective_state`) and `Mind._view` (the wishes a contact attempt is
offered) give every reference as `TRACE`. The archive reads already showed ids only.

**The migration.** `slim-evidence-refs` (host operator action) reads by default and writes nothing:
how many references the document, the two archive tables and every table of `GROUPS` hold, how many
would be slimmed, what the undo would have to keep, and their size before and after. `--apply`
slims the document and both archives in one recorded revision (history kind `slim-evidence-refs`)
and sets the document's mark; then, group by group, it sets the group's mark in the first write
transaction of that group and slims its rows a batch at a time (`BATCH_ROWS`, `BATCH_CHARS`: well
under a second each, so the host's writers are never kept waiting long). A batch is idempotent and
the walk re-runnable: an interrupted apply is finished by the next, and a second apply finds nothing
to do and writes nothing. `--undo --apply` puts every reference back as `Mind._reference` would write
it from the sources table -- the document in one revision of its own (`slim-evidence-refs-undo`), the
tables a batch at a time, pass after pass until one finds nothing left (a writer unmarked by then may
have copied a slim reference into a row already done) -- and clears every mark: it is what runs
before a rollback to a release that expects the full references. Undone straight after an apply,
the document, the archive rows and every table row are what they were byte for byte, apart from the
document's revision and its time. A reference whose original cannot be rebuilt from its source row --
one written before `received_at` was kept, one whose copied metadata an erase had already blanked,
one whose source is gone -- has what differs kept in `mind_evidence_ref_originals`: for the document
by where it sits (the wish or decision it belongs to, which moves with it between the document and
its archive, and the path inside it); for a table, one row per table row (`path` ""), keyed by the
list and the reference it is, and holding only what differs (`_lean_difference`: fields it lacked,
fields that differ, a patch of the copied metadata). An erase reaches that table as it reaches every
mind table with a `data` column; what a table's row kept of an erased source goes whole
(`erase_kept`). A reference no undo can rebuild -- its source erased and nothing kept -- stays slim
and is counted as `unexpandable`. The undo empties the table.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import NamedTuple

from eventmem.core.db import Conflict, digest, dumps

# What a stored or shown reference keeps. See the module docstring for who reads each.
TRACE = ("source_id", "record_id", "revision", "hash", "namespace", "source_key", "occurred_at")
# What it no longer carries. Anything else a reference holds (a tombstone's `erased`) is left to it.
DROPPED = ("metadata", "authority", "session", "received_at")
# Fields that make a dict a reference to a source (a tombstone has them too).
IDENTITY = ("source_id", "record_id", "hash")
# The lists a reference is stored in, in the document and in the archived records.
REF_KEYS = ("evidence", "resolution_evidence")
# The document's own mark: present, and "trace", once its references are slim.
MARK, SHAPE = "evidence_refs", "trace"
KIND, UNDO_KIND = "slim-evidence-refs", "slim-evidence-refs-undo"
UNCHANGED = "slim-evidence-refs-unchanged"
TABLE = "mind_evidence_ref_originals"
# The tables' marks (below): one row per scope and group of tables whose references are kept slim.
MARKS = "mind_evidence_ref_marks"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE}(
 scope TEXT NOT NULL,container TEXT NOT NULL,entry TEXT NOT NULL,path TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,container,entry,path));
CREATE TABLE IF NOT EXISTS {MARKS}(
 scope TEXT NOT NULL,container TEXT NOT NULL,shape TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(scope,container));
"""
# Where an entry of the document goes when it is archived: the section, the table and its container
# name, which is how a kept original follows the entry between the two.
SECTIONS = {"desires": ("mind_desire_archive", "desire"),
            "exploration_decisions": ("mind_exploration_decision_archive", "exploration-decision")}
DOCUMENT = "state"
# What the document and its two archives keep in the originals table; the tables below keep theirs
# under their own names.
DOCUMENT_CONTAINERS = (DOCUMENT, *(container for _, container in SECTIONS.values()))


# --- what a reference is -------------------------------------------------------------------------

def is_ref(value):
    return isinstance(value, dict) and all(isinstance(value.get(key), str) for key in IDENTITY)


def is_full(value):
    """A stored reference that still carries what `trace` drops. A tombstone is erasure's, and is
    left as erasure wrote it."""
    return is_ref(value) and not value.get("erased") and any(key in value for key in DROPPED)


def is_slim(value):
    return is_ref(value) and not value.get("erased") and not any(key in value for key in DROPPED)


def trace(ref):
    """The reference as it is kept and shown: everything but `DROPPED`, which for a reference
    `Mind._reference` wrote is `TRACE`."""
    return {key: value for key, value in ref.items() if key not in DROPPED}


def marked(state):
    return isinstance(state, dict) and state.get(MARK) == SHAPE


# --- walking a document ----------------------------------------------------------------------------

def _refs(value, path=(), keys=REF_KEYS):
    """Every reference in a stored list of references inside `value`: (path, list, index, ref)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, list):
                for index, ref in enumerate(item):
                    if is_ref(ref):
                        yield (*path, key, index), item, index, ref
                    else:
                        yield from _refs(ref, (*path, key, index), keys)
            else:
                yield from _refs(item, (*path, key), keys)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _refs(item, (*path, index), keys)


def slim(value):
    """Keep every stored reference in `value` to `TRACE`, in place. Returns how many were slimmed."""
    count = 0
    for _, items, index, ref in list(_refs(value)):
        if is_full(ref):
            items[index] = trace(ref)
            count += 1
    return count


def slimmed(record):
    """A copy of an archived record with its references kept to `TRACE`."""
    record = json.loads(dumps(record))
    slim(record)
    return record


def shown(value):
    """`value` as a model may be shown it: every reference anywhere in it kept to `TRACE`, whatever
    list it sits in. A copy; the original is left as it is."""
    if isinstance(value, dict):
        if is_ref(value):
            return trace(value)
        return {key: shown(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shown(item) for item in value]
    return value


# --- rebuilding a full reference -------------------------------------------------------------------

def _source(conn, scope, source_id, cache):
    if source_id not in cache:
        row = conn.execute("SELECT data,session,received_at FROM sources WHERE id=? AND scope=? AND deleted=0",
                           (source_id, scope)).fetchone()
        cache[source_id] = (json.loads(row[0]), row[1], row[2]) if row else None
    return cache[source_id]


def expand(conn, scope, ref, cache):
    """The full reference `Mind._reference` wrote for this slim one: its own fields, and the rest from
    its source row. None when the source is gone."""
    found = _source(conn, scope, ref["source_id"], cache)
    if found is None:
        return None
    data, session, received_at = found
    full = {**ref, "authority": data.get("authority"), "session": session, "received_at": received_at,
            "metadata": data.get("metadata", {})}
    return full


def _difference(original, rebuilt):
    """What an undo has to put back that the source row cannot give: the fields that differ or are
    missing (`set`), and those the rebuilt reference has that the original did not (`unset`)."""
    rebuilt = rebuilt or {}
    changed = {key: value for key, value in original.items() if key not in rebuilt or rebuilt[key] != value}
    unset = sorted(key for key in rebuilt if key not in original)
    if not changed and not unset:
        return None
    return {**{key: original[key] for key in IDENTITY + ("revision",) if key in original},
            **({"set": changed} if changed else {}), **({"unset": unset} if unset else {})}


def _restore(ref, rebuilt, kept):
    if kept and all(kept.get(key) == ref.get(key) for key in IDENTITY + ("revision",)):
        rebuilt = {**(rebuilt or ref), **kept.get("set", {})}
        for key in kept.get("unset", ()):
            rebuilt.pop(key, None)
    return rebuilt


def _place(path):
    """Where a reference of the document sits, as (container, entry, path inside the entry): an entry
    of a section that is archived is named by its id, so what is kept of it follows it to its table."""
    if len(path) > 2 and path[0] in SECTIONS and isinstance(path[1], str):
        return SECTIONS[path[0]][1], path[1], dumps(list(path[2:]))
    return DOCUMENT, "", dumps(list(path))


# --- the migration ---------------------------------------------------------------------------------

def _archives(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [(table, container) for table, container in SECTIONS.values() if table in names]


def _installed(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone())


def _plan(conn, scope, state, *, undo):
    """Every change the migration would make, decided against what the transaction holds:
    `document` (path -> new reference), `rows` ({table: {id: new record}}), `kept` (the originals an
    undo would need), and the counts and sizes a report gives."""
    cache, kept_rows = {}, {}
    if undo and _installed(conn):
        kept_rows = {(row[0], row[1], row[2]): json.loads(row[3]) for row in conn.execute(
            f"SELECT container,entry,path,data FROM {TABLE} WHERE scope=? AND container IN (?,?,?)",
            (scope, *DOCUMENT_CONTAINERS))}
    counts = {"refs": 0, "changed": 0, "unexpandable": 0, "kept": 0}

    def change(ref, place):
        counts["refs"] += 1
        if undo:
            if not is_slim(ref):
                return None
            rebuilt = _restore(ref, expand(conn, scope, ref, cache), kept_rows.get(place))
            if rebuilt is None:
                counts["unexpandable"] += 1
                return None
            counts["changed"] += 1
            return rebuilt
        if not is_full(ref):
            return None
        new = trace(ref)
        difference = _difference(ref, expand(conn, scope, new, cache))
        if difference is not None:
            kept[place] = difference
            counts["kept"] += 1
        counts["changed"] += 1
        return new

    kept, document = {}, {}
    for path, _, _, ref in _refs(state):
        new = change(ref, _place(path))
        if new is not None:
            document[path] = new
    rows, sizes = {}, {}
    for table, container in _archives(conn):
        before = after = number = 0
        for identifier, data in conn.execute(f"SELECT id,data FROM {table} WHERE scope=? ORDER BY id", (scope,)):
            record, number = json.loads(data), number + 1
            before += len(data)
            touched = False
            for path, items, index, ref in list(_refs(record)):
                new = change(ref, (container, identifier, dumps(list(path))))
                if new is not None:
                    items[index], touched = new, True
            if touched:
                rows.setdefault(table, {})[identifier] = record
                after += len(dumps(record))
            else:
                after += len(data)
        sizes[table] = {"rows": number, "chars": before, "chars_after": after, "changed_rows": len(rows.get(table, {}))}
    return {"document": document, "rows": rows, "kept": kept, "counts": counts, "archives": sizes}


def _put(state, path, value):
    target = state
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _sizes(state, plan, *, undo):
    before = len(dumps(state))
    after = json.loads(dumps(state))
    for path, value in plan["document"].items():
        _put(after, path, value)
    if undo:
        after.pop(MARK, None)
    else:
        after[MARK] = SHAPE
    return {"chars": before, "chars_after": len(dumps(after))}


def _report(mind, document, plan, *, undo, **extra):
    counts = plan["counts"]
    return {"scope": mind.scope.key(), "operation": "undo" if undo else "slim", "revision": document["revision"],
            "marked": marked(document), "fields": list(TRACE),
            "references": counts["refs"], ("would_expand" if undo else "would_slim"): counts["changed"],
            **({"unexpandable": counts["unexpandable"]} if undo else {"originals_kept": counts["kept"]}),
            "document": {**_sizes(document, plan, undo=undo),
                         "references_changed": len(plan["document"])},
            "archives": plan["archives"], **extra}


def _run_document(mind, *, apply=False, undo=False):
    """The document and its two archives: the dry run by default; `apply` slims and marks the
    document, `undo` expands and unmarks it. Either writes one recorded revision, or nothing when
    nothing is left to change."""
    scope = mind.scope.key()
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        plan = _plan(conn, scope, state, undo=undo)
    pending = plan["counts"]["changed"] or (marked(state) if undo else not marked(state))
    if not apply or not pending:
        return _report(mind, state, plan, undo=undo,
                       state="dry-run" if not apply else ("expanded" if undo else "slimmed"),
                       **({"changed": 0, "event_id": None} if apply else {}))

    def migrate(conn, current, event_id):
        # Decided again against the revision this transaction holds.
        again = _plan(conn, scope, current, undo=undo)
        if not again["counts"]["changed"] and (not marked(current) if undo else marked(current)):
            raise Conflict("No evidence reference is left to change", kind="runtime", code=UNCHANGED)
        for path, value in again["document"].items():
            _put(current, path, value)
        for table, records in again["rows"].items():
            for identifier, record in records.items():
                conn.execute(f"UPDATE {table} SET data=? WHERE scope=? AND id=?", (dumps(record), scope, identifier))
        if undo:
            current.pop(MARK, None)
            conn.execute(f"DELETE FROM {TABLE} WHERE scope=? AND container IN (?,?,?)", (scope, *DOCUMENT_CONTAINERS))
        else:
            current[MARK] = SHAPE
            conn.executemany(f"INSERT OR REPLACE INTO {TABLE} VALUES(?,?,?,?,?)",
                             [(scope, *place, dumps(value)) for place, value in again["kept"].items()])
        return {"changed": again["counts"]["changed"], "document_changed": len(again["document"]),
                "rows_changed": {table: len(records) for table, records in again["rows"].items()},
                "originals_kept": again["counts"]["kept"], "unexpandable": again["counts"]["unexpandable"]}

    kind = UNDO_KIND if undo else KIND
    payload = {"command_id": kind + ":" + digest([scope, state["revision"]])[:32],
               "agent_version": state["agent_version"], "expected_revision": state["revision"]}
    try:
        result = mind._mutate(payload, kind, migrate, rebase=lambda conn, current: True)
    except Conflict as error:
        if getattr(error, "code", None) != UNCHANGED:
            raise
        result = {"changed": 0, "event_id": None}
    with mind.engine.db.connect() as conn:
        current = mind._load(conn)
        after = _plan(conn, scope, current, undo=undo)
        return _report(mind, current, after, undo=undo, before=_sizes(state, plan, undo=undo),
                       state="expanded" if undo else "slimmed",
                       changed=result["changed"], event_id=result["event_id"],
                       **{key: result[key] for key in ("document_changed", "rows_changed", "originals_kept", "unexpandable")
                          if key in result},
                       **({"revision_written": result["revision"]} if result.get("revision") else {}))


# --- the tables ------------------------------------------------------------------------------------
#
# Besides the document, references are stored in the rows of the tables below, in a list under one of
# `TABLE_KEYS` at any depth of a row's `data`. Their writers read one mark per scope and group of
# tables (`MARKS`): marked, every row they write keeps its references to `TRACE` (`as_stored`);
# unmarked, the row is written exactly as before. Their readers never read the mark: whatever they
# need of a source they read from the source's row (`read_policy.SourceFacts`), so a full reference
# and a slim one read the same, and rows of both shapes may stand side by side for as long as they
# like -- during a batched migration, after an interrupted one, after an undo.

# Where a table's row keeps references: every list under one of these keys, at any depth. `sources`
# is a stored proposal's (`reuse.sources`); its other lists name sources by `id` and are not
# references, so nothing in them is touched.
TABLE_KEYS = REF_KEYS + ("configuration_evidence", "evaluated_sources", "seed_sources", "exploration_targets", "sources")


class Table(NamedTuple):
    name: str
    # The SQL naming a row within its scope: what the undo's kept originals are filed under.
    entry: str
    # The SQL choosing a scope's rows (`:scope`); a table without a scope column follows its owner's.
    scope: str = "scope=:scope"


# The groups of tables one mark covers, in the order the migration walks them. A group is what one
# writer keeps consistent: a graph node and its revisions, and the command that copied it before a
# change, are marked and slimmed together.
GROUPS = {
    "graph": (Table("mind_graph_nodes", "id"), Table("mind_graph_edges", "id"),
              Table("mind_graph_revisions", "id||'@'||revision",
                    "id IN (SELECT id FROM mind_graph_nodes WHERE scope=:scope) "
                    "OR id IN (SELECT id FROM mind_graph_edges WHERE scope=:scope)"),
              Table("mind_graph_commands", "id"), Table("mind_event_routes", "id"),
              Table("mind_isolation_archive", "kind||':'||identifier||'@'||revision")),
    "appraisals": (Table("mind_appraisals", "id"),),
    "plans": (Table("mind_plans", "id"),
              Table("mind_plan_history", "id||'@'||revision", "id IN (SELECT id FROM mind_plans WHERE scope=:scope)"),
              Table("mind_plan_reviews", "plan_id||':'||command_id"), Table("mind_plan_runs", "id")),
    "habits": (Table("mind_conversation_habits", "'habits'"), Table("mind_habit_commands", "id"),
               Table("mind_habit_revisions", "CAST(revision AS TEXT)")),
    "records": (Table("mind_contacts", "id"), Table("mind_reply_reviews", "id"),
                Table("mind_expression_intent_log", "id"), Table("mind_expression_intents", "id"),
                Table("mind_procedures", "id"),
                Table("mind_procedure_history", "id||'@'||revision",
                      "id IN (SELECT id FROM mind_procedures WHERE scope=:scope)"),
                Table("mind_trait_observations", "id")),
}
TABLES = {table.name: (group, table) for group, tables in GROUPS.items() for table in tables}
# One write transaction of the migration: this many rows at most, and about this many characters of
# them -- well under a second, so the host's own writers wait for it no longer than for one of theirs.
BATCH_ROWS = 400
BATCH_CHARS = 4_000_000
# Passes the undo makes at most: a writer that ran while it did (unmarked by then) may have copied a
# slim reference from a row not yet reached into one already done; the next pass finds it.
UNDO_PASSES = 3
# The sources read while one group is walked, kept for the next rows: at most this many.
SOURCE_CACHE = 20_000


def lean(value):
    """`value` with every full reference in a list under `TABLE_KEYS` kept to `TRACE`: a copy of
    what changes, the same objects where nothing does (so `lean(v) is v` when there is nothing)."""
    if isinstance(value, dict):
        out, changed = {}, False
        for key, item in value.items():
            if key in TABLE_KEYS and isinstance(item, list):
                new = [trace(ref) if is_full(ref) else lean(ref) for ref in item]
                new = item if all(a is b for a, b in zip(new, item)) else new
            else:
                new = lean(item)
            changed = changed or new is not item
            out[key] = new
        return out if changed else value
    if isinstance(value, list):
        new = [lean(item) for item in value]
        return value if all(a is b for a, b in zip(new, value)) else new
    return value


def has_slim(value):
    """Whether `value` stores any reference already kept to `TRACE`."""
    return any(is_slim(ref) for _, _, _, ref in _refs(value, keys=TABLE_KEYS))


def table_marked(conn, scope, table):
    """Whether the writers of `table` keep its references slim in `scope`: its group is marked. Read
    inside the writer's own transaction, so a row is written in the shape the mark it sees asks for."""
    group = TABLES[table][0]
    try:
        row = conn.execute(f"SELECT shape FROM {MARKS} WHERE scope=? AND container=?", (scope, group)).fetchone()
    except sqlite3.OperationalError:
        # A store the mind's schema has not reached: nothing is marked.
        return False
    return bool(row) and row[0] == SHAPE


def as_stored(conn, scope, table, value):
    """`value` as a writer of `table` stores it now: `lean(value)` once the table's group is marked,
    `value` itself otherwise -- unmarked, a row is written exactly as before."""
    return lean(value) if table_marked(conn, scope, table) else value


def _mark(conn, scope, group, on, at):
    """Set or clear a group's mark, inside the migration's first transaction for it. Returns whether
    anything was written: a second apply, or a second undo, writes nothing."""
    current = conn.execute(f"SELECT shape FROM {MARKS} WHERE scope=? AND container=?", (scope, group)).fetchone()
    if on and not (current and current[0] == SHAPE):
        conn.execute(f"INSERT OR REPLACE INTO {MARKS} VALUES(?,?,?,?)", (scope, group, SHAPE, at))
        return True
    if not on and current:
        conn.execute(f"DELETE FROM {MARKS} WHERE scope=? AND container=?", (scope, group))
        return True
    return False


def _marks(conn, scope):
    try:
        return {row[0]: row[1] == SHAPE for row in conn.execute(f"SELECT container,shape FROM {MARKS} WHERE scope=?", (scope,))}
    except sqlite3.OperationalError:
        return {}


def _existing(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _key(path, ref, seen):
    """Where a table's kept original is filed inside its row: the list it sits in and the reference
    it is, so it follows the reference however the list moves around it -- and, for the second copy
    of one reference in the same list, which copy (`seen` counts them, in list order)."""
    named = [ref.get(key) for key in ("source_id", "record_id", "revision")]
    at = dumps([*path[:-1], named])
    seen[at] = seen.get(at, -1) + 1
    return dumps([*path[:-1], named + ([seen[at]] if seen[at] else [])])


def _patch(original, rebuilt, path=()):
    """The operations that turn `rebuilt` into `original`: `[path, value]` sets one value, `[path]`
    removes one key. Only what differs, so a copied metadata an erase blanked a few words of is kept
    as those few words, not whole."""
    if isinstance(original, dict) and isinstance(rebuilt, dict):
        ops = []
        for key, value in original.items():
            if key not in rebuilt:
                ops.append([[*path, key], value])
            elif value != rebuilt[key]:
                ops.extend(_patch(value, rebuilt[key], (*path, key)))
        ops.extend([[*path, key]] for key in rebuilt if key not in original)
        return ops
    if isinstance(original, list) and isinstance(rebuilt, list) and len(original) == len(rebuilt):
        return [op for index, (a, b) in enumerate(zip(original, rebuilt)) if a != b for op in _patch(a, b, (*path, index))]
    return [[list(path), original]]


def _patched(value, ops):
    value = json.loads(dumps(value))
    for op in ops:
        path = op[0]
        if not path:
            value = op[1]
            continue
        target = value
        for key in path[:-1]:
            target = target[key]
        if len(op) == 2:
            target[path[-1]] = op[1]
        else:
            target.pop(path[-1], None)
    return value


def _lean_difference(original, rebuilt):
    """What an undo needs of a table's reference beyond what its source row gives back, compactly:
    `unset` (fields it did not have -- a reference written before `received_at` was kept), `set`
    (fields that differ, or all it carried when its source is gone), `metadata` (a `_patch` of the
    copied metadata). One that keeps any value names its source and record, so an erase of either
    finds it. None when the source row gives it back whole."""
    if rebuilt is None:
        out = {"set": {key: original[key] for key in DROPPED if key in original}}
    else:
        out = {}
        for key in DROPPED:
            if key not in original:
                if key in rebuilt:
                    out.setdefault("unset", []).append(key)
            elif key not in rebuilt:
                out.setdefault("set", {})[key] = original[key]
            elif original[key] != rebuilt[key]:
                if key == "metadata" and isinstance(original[key], dict) and isinstance(rebuilt[key], dict):
                    out["metadata"] = _patch(original[key], rebuilt[key])
                else:
                    out.setdefault("set", {})[key] = original[key]
    if not out:
        return None
    if "set" in out or "metadata" in out:
        out.update(source_id=original["source_id"], record_id=original["record_id"])
    return out


def _lean_restore(ref, rebuilt, kept):
    if not kept:
        return rebuilt
    full = dict(rebuilt or ref)
    full.update(kept.get("set", {}))
    for key in kept.get("unset", ()):
        full.pop(key, None)
    if "metadata" in kept:
        full["metadata"] = _patched(full.get("metadata", {}), kept["metadata"])
    return full


def _row(conn, scope, record, *, undo, cache, kept):
    """One row: its references changed in place in `record`; `kept` is what the undo needs of this
    row, keyed by `_key`, read by an undo and added to by an apply. Returns the counts' changes."""
    found = {"refs": 0, "changed": 0, "originals_kept": 0, "originals_chars": 0, "unexpandable": 0}
    seen = {}
    for path, items, index, ref in list(_refs(record, keys=TABLE_KEYS)):
        found["refs"] += 1
        key = _key(path, ref, seen)
        if undo:
            if not is_slim(ref):
                continue
            rebuilt = _lean_restore(ref, expand(conn, scope, ref, cache), kept.get(key))
            if rebuilt is None:
                found["unexpandable"] += 1
                continue
            items[index] = rebuilt
            found["changed"] += 1
            continue
        if not is_full(ref):
            continue
        new = trace(ref)
        difference = _lean_difference(ref, expand(conn, scope, new, cache))
        if difference is not None:
            kept[key] = difference
            found["originals_kept"] += 1
            found["originals_chars"] += len(key) + len(dumps(difference))
        items[index] = new
        found["changed"] += 1
    return found


def erase_kept(data, ids):
    """A table's kept originals (one row per table row, path "") without what they keep of `ids`: an
    erased source's or record's reference is a tombstone in its row, which no undo rebuilds, so what
    was kept of it -- its copied words among it -- goes. None when nothing of it was there."""
    if not isinstance(data, dict):
        return None
    left = {}
    for key, value in data.items():
        try:
            named = json.loads(key)[-1]
        except (ValueError, IndexError, TypeError):
            named = []
        if any(isinstance(i, str) and i in ids for i in named) or (
                isinstance(value, dict) and (value.get("source_id") in ids or value.get("record_id") in ids)):
            continue
        left[key] = value
    return left if len(left) != len(data) else None


def _walk(mind, group, table, *, write, undo, first, cache, clock):
    """One pass over one table, a batch per transaction; its counts. `first`: a one-item list, true
    until this group's first transaction has run, which is where the mark is set or cleared."""
    scope, where = mind.scope.key(), f"({table.scope})"
    counts = {"rows": 0, "chars": 0, "delta": 0, "rows_with_refs": 0, "refs": 0, "changed": 0, "changed_rows": 0,
              "originals_kept": 0, "originals_chars": 0, "unexpandable": 0, "unreadable": 0, "batches": 0, "seconds": 0.0,
              "longest_batch_seconds": 0.0}
    with mind.engine.db.connect() as conn:
        counts["rows"], counts["chars"] = conn.execute(
            f"SELECT COUNT(*),COALESCE(SUM(length(data)),0) FROM {table.name} WHERE {where}", {"scope": scope}).fetchone()
    after = 0
    while True:
        started = time.perf_counter()
        with mind.engine.db.connect(write=write) as conn:
            if write and first[0]:
                _mark(conn, scope, group, not undo, clock())
                first[0] = False
            rows, size = [], 0
            # Only rows that hold a reference at all: every reference, full or slim, names its key.
            for row in conn.execute(
                    f"SELECT rowid,{table.entry},data FROM {table.name} WHERE {where} AND rowid>:after "
                    "AND instr(data,'\"source_key\":')>0 ORDER BY rowid LIMIT :limit",
                    {"scope": scope, "after": after, "limit": BATCH_ROWS}):
                rows.append((row[0], str(row[1]), row[2]))
                size += len(row[2])
                if size >= BATCH_CHARS:
                    break
            if not rows:
                break
            after = rows[-1][0]
            entries = sorted({entry for _, entry, _ in rows})
            # What an undo needs of a row is kept in one row of its own, beside the rows of the
            # document's originals: (container = this table, entry, path "").
            kept = {}
            for start in range(0, len(entries), 400):
                page = entries[start:start + 400]
                for entry, data in conn.execute(
                        f"SELECT entry,data FROM {TABLE} WHERE scope=? AND container=? AND path='' AND entry IN ("
                        + ",".join("?" for _ in page) + ")", (scope, table.name, *page)):
                    kept[entry] = json.loads(data)
            if len(cache) > SOURCE_CACHE:
                cache.clear()
            changed_entries = set()
            for rowid, entry, data in rows:
                try:
                    record = json.loads(data)
                except ValueError:
                    counts["unreadable"] += 1
                    continue
                found = _row(conn, scope, record, undo=undo, cache=cache, kept=kept.setdefault(entry, {}))
                for key, value in found.items():
                    counts[key] += value
                counts["rows_with_refs"] += bool(found["refs"])
                if found["originals_kept"]:
                    changed_entries.add(entry)
                if not found["changed"]:
                    continue
                new = dumps(record)
                counts["changed_rows"] += 1
                counts["delta"] += len(new) - len(data)
                if write:
                    conn.execute(f"UPDATE {table.name} SET data=? WHERE rowid=?", (new, rowid))
            if write and undo:
                # What an undo needed of these rows it has used: they are as they were.
                conn.executemany(f"DELETE FROM {TABLE} WHERE scope=? AND container=? AND entry=? AND path=''",
                                 [(scope, table.name, entry) for entry in entries if kept.get(entry)])
            if write and changed_entries:
                conn.executemany(f"INSERT OR REPLACE INTO {TABLE} VALUES(?,?,?,'',?)",
                                 [(scope, table.name, entry, dumps(kept[entry])) for entry in sorted(changed_entries)])
        elapsed = time.perf_counter() - started
        counts["batches"] += 1
        counts["seconds"] += elapsed
        counts["longest_batch_seconds"] = max(counts["longest_batch_seconds"], elapsed)
    return counts


SUMMED = ("changed", "changed_rows", "originals_kept", "originals_chars", "delta", "batches", "seconds")


def _run_tables(mind, *, apply=False, undo=False):
    """Every table of `GROUPS`, group by group, table by table, a batch per transaction. The dry run
    reads only. `apply` marks each group in its first transaction and slims its rows; `undo` clears
    the mark in its first transaction and rebuilds its rows from their sources and the kept
    originals, pass after pass until one finds nothing left to rebuild (at most `UNDO_PASSES`).
    Idempotent per batch and re-runnable after an interruption: a row already in the shape asked for
    is left as it is, and a second apply or undo writes nothing."""
    scope, write = mind.scope.key(), bool(apply)
    started = time.perf_counter()
    with mind.engine.db.connect() as conn:
        existing = _existing(conn)
    if write and not {TABLE, MARKS} <= existing:
        with mind.engine.db.connect(write=True) as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
    stats, passes = {}, 0
    for number in range(UNDO_PASSES if undo and write else 1):
        passes, changed = number + 1, 0
        for group, tables in GROUPS.items():
            cache, first = {}, [write and number == 0]
            for table in tables:
                if table.name not in existing:
                    continue
                found = _walk(mind, group, table, write=write, undo=undo, first=first, cache=cache, clock=mind.clock)
                changed += found["changed"]
                if table.name not in stats:
                    stats[table.name] = {"group": group, **found, "passes": 1}
                    continue
                counts = stats[table.name]
                counts.update({key: counts[key] + found[key] for key in SUMMED}, passes=counts["passes"] + 1,
                              unexpandable=found["unexpandable"], unreadable=found["unreadable"],
                              longest_batch_seconds=max(counts["longest_batch_seconds"], found["longest_batch_seconds"]))
            if first[0]:
                # A group none of whose tables exists yet is still marked, or cleared, for its writers.
                with mind.engine.db.connect(write=True) as conn:
                    _mark(conn, scope, group, not undo, mind.clock())
        if not changed:
            break
    if write and undo:
        # The originals of rows no longer there go with them: nothing is left for them to rebuild.
        names = [table.name for tables in GROUPS.values() for table in tables]
        with mind.engine.db.connect(write=True) as conn:
            if conn.execute(f"SELECT 1 FROM {TABLE} WHERE scope=? AND container IN ({','.join('?' for _ in names)}) LIMIT 1",
                            (scope, *names)).fetchone():
                conn.execute(f"DELETE FROM {TABLE} WHERE scope=? AND container IN ({','.join('?' for _ in names)})",
                             (scope, *names))
    label = ("expanded" if undo else "slimmed") if write else ("would_expand" if undo else "would_slim")
    tables = {}
    for name, counts in stats.items():
        counts = dict(counts)
        counts["chars_after"] = counts["chars"] + counts.pop("delta")
        counts["seconds"] = round(counts["seconds"], 3)
        counts["longest_batch_seconds"] = round(counts["longest_batch_seconds"], 3)
        counts[label] = counts.pop("changed")
        tables[name] = counts
    with mind.engine.db.connect() as conn:
        marks = _marks(conn, scope)
    total = {key: sum(counts[key] for counts in tables.values())
             for key in ("rows", "chars", "chars_after", "refs", label, "changed_rows", "originals_kept", "originals_chars",
                         "unexpandable")}
    if write and total[label]:
        mind.engine.db.metric("evidence_refs_" + label, total[label],
                              {"scope": scope, "tables": {name: counts[label] for name, counts in tables.items() if counts[label]}})
    return {"tables": tables, "table_marks": {group: marks.get(group, False) for group in GROUPS},
            "tables_total": {**total, "passes": passes, "seconds": round(time.perf_counter() - started, 3),
                             "longest_batch_seconds": max([c["longest_batch_seconds"] for c in tables.values()] or [0.0])}}


def run(mind, *, apply=False, undo=False):
    """`slim-evidence-refs`: the dry run by default; `apply` slims and marks the document and every
    table, `undo` expands and unmarks them. The document is one recorded revision (or nothing when
    nothing is left to change); the tables are written a batch per transaction. Neither is gated:
    the dry run writes nothing, and the undo is what runs before a rollback."""
    report = _run_document(mind, apply=apply, undo=undo)
    report.update(_run_tables(mind, apply=apply, undo=undo))
    return report

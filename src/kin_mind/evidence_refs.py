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
`host_event`, `exploration_id`, `never_evidence`, the graph's classification) reads a reference
built afresh from the sources table by `Mind._reference` in the same transaction, which is not
stored and is left exactly as it was. Tombstones (`erased`) are left as erasure wrote them.

**Where it applies.** The state document, while it carries the mark `evidence_refs: "trace"`: every
save (`Mind._save`, the one writer of the document) keeps its references to `TRACE`, whichever
section they were written into, and so do the wish archive and the exploration decision archive
when a record moves there. The mark is set and cleared only by `slim-evidence-refs` below, in the
same revision as the references it slims or restores, so the mark and the shape of the document can
never disagree. Without it the document is written exactly as before.

**What a model is shown.** Never a source's metadata, marked or not: `Mind.read` (the whole view,
history snapshots included, and so the interaction projection, the contact draft's state, the
appraisal context and `read_affective_state`) and `Mind._view` (the wishes a contact attempt is
offered) give every reference as `TRACE`. The archive reads already showed ids only.

**The migration.** `slim-evidence-refs` (host operator action) reads by default and writes nothing:
how many references the document and the two archive tables hold, how many would be slimmed, and
their size before and after. `--apply` slims them all in one recorded revision (history kind
`slim-evidence-refs`) and sets the mark; a second apply finds nothing to do and writes nothing.
`--undo --apply` puts every reference back as it was, from the sources table, in one revision of its
own (`slim-evidence-refs-undo`), and clears the mark: it is what runs before a rollback to a release
that expects the full references. Undone straight after an apply, the document and the archive rows
are what they were byte for byte, apart from the revision and its time. A reference whose original
cannot be rebuilt from its source row -- one written before `received_at` was kept, one whose copied
metadata an erase had already blanked, one whose source is gone -- has what differs kept in
`mind_evidence_ref_originals` by where it sits (the wish or decision it belongs to, which moves with
it between the document and its archive, and the path inside it). An erase reaches that table as it
reaches every mind table with a `data` column, and the undo empties it.
"""

from __future__ import annotations

import json

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
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE}(
 scope TEXT NOT NULL,container TEXT NOT NULL,entry TEXT NOT NULL,path TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,container,entry,path));
"""
# Where an entry of the document goes when it is archived: the section, the table and its container
# name, which is how a kept original follows the entry between the two.
SECTIONS = {"desires": ("mind_desire_archive", "desire"),
            "exploration_decisions": ("mind_exploration_decision_archive", "exploration-decision")}
DOCUMENT = "state"


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

def _refs(value, path=()):
    """Every reference in a stored list of references inside `value`: (path, list, index, ref)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in REF_KEYS and isinstance(item, list):
                for index, ref in enumerate(item):
                    if is_ref(ref):
                        yield (*path, key, index), item, index, ref
                    else:
                        yield from _refs(ref, (*path, key, index))
            else:
                yield from _refs(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _refs(item, (*path, index))


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
            f"SELECT container,entry,path,data FROM {TABLE} WHERE scope=?", (scope,))}
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


def run(mind, *, apply=False, undo=False):
    """`slim-evidence-refs`: the dry run by default; `apply` slims and marks the document, `undo`
    expands and unmarks it. Either writes one recorded revision, or nothing when nothing is left to
    change. Neither is gated: the dry run writes nothing, and the undo is what runs before a rollback."""
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
            conn.execute(f"DELETE FROM {TABLE} WHERE scope=?", (scope,))
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

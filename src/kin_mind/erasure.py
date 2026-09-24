"""What an explicit erase takes out of the mind's own layers.

`Engine.delete` removes the records and sources and writes their tombstones; that table is the
deletion fact, and nothing else is kept to record one. Everything the mind built from those
records keeps a copy of their words somewhere of its own: the event graph, the state document
and every revision of it, the wishes moved to their archive, plans, appraisals, compressed
context. This module is the one place those layers are named, and the erase runs it inside the
delete's own transaction — except the state history, whose rows are rewritten by a job in
batches (below) because each rewritten row has to be hashed again.

The rule is the same everywhere. An entry that cites erased material — a wish, a concern, a
graph finding, an appraisal's proposal — loses its words (`ERASED`) and the erased references
become bare tombstone references, which never verify again, so the entry reads as needing
review. Its identity, status and revision stay: an erase takes text out, it does not decide
anything on Kin's behalf. A graph node that still stands on other evidence, and is not a
model-written claim, only loses the erased references.

**Archives keep the originals.** A compaction archive or a backup is the copy of the store from
before, and nothing here rewrites one. What an archive may never do is bring an erased text back:
`history_compaction.restore` puts every row through the same scrub before it is written and then
rebuilds the chain, so a restore from an archive that still holds the original words leaves the
erase in force and the rest of the history rebuildable.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from eventmem.core.db import Conflict, digest, dumps

ERASED = "[已删除]"
# Keys whose value is words somebody wrote. Everything else — ids, states, times, kinds,
# numbers — is left as it was, so structure and history stay readable.
TEXT_KEYS = frozenset({
    "content", "topic", "reason", "completion", "meaning", "goal", "motivation", "query", "title",
    "applicable_when", "steps", "success_criteria", "counterexamples", "text", "summary",
    "statement", "name", "aliases", "style", "note", "notes", "quote", "narrative", "description",
    "excerpt", "detail", "message", "conclusions", "pending", "corrections", "unresolved",
    "preview", "question", "answer", "condition", "explanation", "claim", "prediction",
    "evidence_text", "locator_text", "rationale", "interpretation", "body",
})
# `action` is an enum in every command and free text in an owner request: only the second goes.
ENUM = re.compile(r"[a-z][a-z0-9_:-]{0,39}")
REF_LISTS = ("evidence", "resolution_evidence", "refs")
ID_LISTS = ("evidence_ids", "source_ids", "record_ids", "input_ids", "result_ids", "member_ids")
ID_KEYS = ("source_id", "record_id")
TOMBSTONE_KEYS = ("source_id", "record_id", "hash", "revision", "authority")
IDENTIFIER = re.compile(r"\b(?:src|mem)_[0-9a-f]{32}\b")
# Graph kinds whose words are a claim the model wrote from its evidence: they go whenever any of
# that evidence does. Other nodes keep their words while evidence of theirs remains.
CLAIM_KINDS = frozenset({"finding", "association"})
# Tables the graph, the history and the caches own, handled by name below; every other mind table
# with a `data` column gets the plain scrub.
SPECIAL = frozenset({"mind_events", "mind_graph_nodes", "mind_graph_edges", "mind_graph_revisions",
                     "mind_graph_commands", "mind_context_cache", "mind_judgment_cache",
                     "mind_semantic_cache", "mind_memory_config", "mind_memory_migrations",
                     "mind_context_deliveries", "mind_context_windows"})
# What was, or was about to be, put into a native window: a delivery (its body `text` and its
# `items`, which name what they rest on as `id`, `dependencies[].id` and the like) and a window's
# stored receipts (`text`, `rendered_text`, `index[].id`). Handled by name below (CR-MEM-02).
CONTEXT_TABLES = ("mind_context_deliveries", "mind_context_windows")
# What an erased delivery keeps: its identity, the hash of what it carried, where it was going and
# how far it got, so it can still be reconciled with the native history by marker and hash.
DELIVERY_KEPT = ("id", "session", "epoch", "event_id", "state", "kind", "marker", "text_hash", "tokens",
                 "content_tokens", "overhead", "manifest_id", "prepared_at", "accepted_at", "native_at",
                 "historical", "needs_review", "evidence", "stale_reason")
# Derived caches: a row that names erased material, or a graph item the erase took words from,
# goes whole. Rebuilding one costs a model call; keeping one keeps the words.
CACHES = ("mind_context_cache", "mind_semantic_cache")
# The history rewrite's own progress, in the table every resumable migration of the mind uses.
HISTORY_MIGRATION = "history-erase"
# One batch of the rewrite: this many rows at most, and about this many bytes of stored rows —
# a write transaction of a few hundred milliseconds, even for rows that are whole documents.
HISTORY_ROWS = 100
HISTORY_BYTES = 8 * 1024 * 1024


def cites(value, ids):
    """Whether this one dict rests on erased material, by its own references."""
    for key in REF_LISTS:
        refs = value.get(key)
        if isinstance(refs, list) and any(isinstance(ref, dict) and (ref.get("source_id") in ids or ref.get("record_id") in ids)
                                          for ref in refs):
            return True
    for key in ID_LISTS:
        found = value.get(key)
        if isinstance(found, list) and any(isinstance(i, str) and i in ids for i in found):
            return True
    return any(value.get(key) in ids for key in ID_KEYS if isinstance(value.get(key), str))


def _blank(value):
    if isinstance(value, str):
        return ERASED if value else value
    if isinstance(value, list):
        return [] if value else value
    if isinstance(value, dict):
        return scrub(value, (), erase=True)
    return value


def _tombstone(ref, ids):
    if isinstance(ref, dict) and (ref.get("source_id") in ids or ref.get("record_id") in ids):
        stone = {key: ref[key] for key in TOMBSTONE_KEYS if key in ref}
        stone["erased"] = True
        return ref if stone == ref else stone
    return ref


def scrub(value, ids, *, erase=False):
    """`value` with the words of everything resting on `ids` taken out. Unchanged parts are the
    same objects, so a caller can tell by identity whether anything moved. Idempotent, and local:
    a dict's result depends only on the dict itself and what it inherited, never on siblings."""
    if isinstance(value, list):
        out = [scrub(item, ids, erase=erase) for item in value]
        return value if all(a is b for a, b in zip(out, value)) else out
    if not isinstance(value, dict):
        return value
    erase = erase or cites(value, ids)
    out, changed = {}, False
    for key, item in value.items():
        if erase and key in TEXT_KEYS:
            new = _blank(item)
        elif erase and key == "action" and isinstance(item, str) and not ENUM.fullmatch(item):
            new = ERASED
        elif key in REF_LISTS and isinstance(item, list):
            new = [_tombstone(ref, ids) for ref in item]
            new = item if all(a is b for a, b in zip(new, item)) else new
            new = scrub(new, ids, erase=erase) if new is item else new
        else:
            new = scrub(item, ids, erase=erase)
        changed = changed or new is not item
        out[key] = new
    return out if changed else value


def drop_refs(value, ids):
    """A graph node that still stands on other evidence: only the erased references go."""
    out = dict(value)
    for key in REF_LISTS:
        if isinstance(out.get(key), list):
            out[key] = [ref for ref in out[key] if not (isinstance(ref, dict) and (ref.get("source_id") in ids or ref.get("record_id") in ids))]
    for key in ID_LISTS:
        if isinstance(out.get(key), list):
            out[key] = [i for i in out[key] if i not in ids]
    return out


def mentions(conn, table, ids, columns="rowid AS key,data", *, column="data"):
    """Rows of `table` whose `column` names any of `ids`, found by text before anything is
    parsed. Each row once, keyed by the first column asked for."""
    ids, found = sorted(ids), {}
    for start in range(0, len(ids), 200):
        page = ids[start:start + 200]
        where = " OR ".join(f"instr({column},?)>0" for _ in page)
        for row in conn.execute(f"SELECT {columns} FROM {table} WHERE {where}", page).fetchall():
            found.setdefault(row[0], row)
    return list(found.values())


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'mind\\_%' ESCAPE '\\'").fetchall()
    found = []
    for (name,) in rows:
        if name in SPECIAL:
            continue
        if any(column[1] == "data" for column in conn.execute(f"PRAGMA table_info({name})")):
            found.append(name)
    return found


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def erase(conn, records, sources, at):
    """Every layer except the state history, inside the caller's transaction; the history is
    `queue_history`'s. Returns what was changed, by layer, as counts."""
    ids = frozenset(records) | frozenset(sources)
    if not ids or not _table(conn, "mind_state"):
        return {}
    graph, touched = _graph(conn, ids, frozenset(records), at)
    counts, nodes = {"graph": graph}, set()
    for table in _tables(conn):
        counts[table] = _plain(conn, table, ids, nodes if table == "mind_memory_nodes" else None)
    # A cache that summarised a graph item the erase took words from holds those words too.
    for table in CACHES:
        counts[table] = _drop_cache(conn, table, ids | touched)
    # So does everything rendered for a native window from any of them (CR-MEM-02).
    counts["context_receipts"] = _context_receipts(conn, ids | touched | frozenset(nodes), at)
    if _table(conn, "mind_judgment_cache_deps"):
        from .judgment_cache import invalidate
        counts["judgment_cache"] = invalidate(conn, sorted(ids | touched))
    counts = {key: value for key, value in counts.items() if value}
    conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES('memory_erased',1,?,?)",
                 (at, dumps(counts)))
    return counts


def _plain(conn, table, ids, changed_ids=None):
    changed = 0
    for row in mentions(conn, table, ids):
        try:
            data = json.loads(row["data"])
        except ValueError:
            continue
        new = scrub(data, ids)
        if new is data:
            continue
        conn.execute(f"UPDATE {table} SET data=? WHERE rowid=?", (dumps(new), row["key"]))
        changed += 1
        if changed_ids is not None and isinstance(data.get("id"), str):
            changed_ids.add(data["id"])
        if table == "mind_memory_nodes":
            from .memory import index_node
            index_node(conn, row["key"], new)
    return changed


def erased_delivery(value, at):
    """A delivery that rendered erased words: they go, from its body and from every item it
    carried; its identity, hash and state stay for reconciliation, and it is never sent again."""
    kept = {key: value[key] for key in DELIVERY_KEPT if key in value}
    items = [{key: item[key] for key in ("id", "revision", "depth") if key in item}
             for item in value.get("items") or [] if isinstance(item, dict)]
    return {**kept, "text": ERASED, "items": items, "erased_at": value.get("erased_at") or at}


def erased_receipt(value, at):
    """A window's stored receipt of the same kind: its rendered words go, its ids stay."""
    new = scrub(value, (), erase=True)
    if isinstance(value.get("rendered_text"), str) and value["rendered_text"]:
        new = {**new, "rendered_text": ERASED}
    return {**new, "erased_at": value.get("erased_at") or at}


def _context_receipts(conn, doomed, at):
    """Every delivery and stored window receipt that names erased material anywhere in what it
    rests on, by its real structure: `items[].id`, `items[].dependencies[].id`, a graph item's
    dependencies, a receipt's `index[].id`. Found by text, so no shape of reference is missed."""
    changed = 0
    if not doomed:
        return changed
    if _table(conn, "mind_context_deliveries"):
        for row in mentions(conn, "mind_context_deliveries", doomed, "rowid AS key,data"):
            value = json.loads(row["data"])
            if value.get("erased_at") and value.get("text") == ERASED:
                continue
            conn.execute("UPDATE mind_context_deliveries SET data=? WHERE rowid=?",
                         (dumps(erased_delivery(value, at)), row["key"]))
            changed += 1
    if _table(conn, "mind_context_windows"):
        marks = sorted(doomed)
        for row in mentions(conn, "mind_context_windows", doomed, "rowid AS key,data"):
            value = json.loads(row["data"])
            receipts = value.get("receipts") or {}
            new = {key: (erased_receipt(receipt, at) if isinstance(receipt, dict) and not receipt.get("erased_at")
                         and any(mark in dumps(receipt) for mark in marks) else receipt)
                   for key, receipt in receipts.items()}
            if any(new[key] is not receipts[key] for key in receipts):
                conn.execute("UPDATE mind_context_windows SET data=? WHERE rowid=?",
                             (dumps({**value, "receipts": new}), row["key"]))
                changed += sum(new[key] is not receipts[key] for key in receipts)
    return changed


def _drop_cache(conn, table, ids):
    """Compressed context and cached semantic answers are the same words in a model's
    phrasing, and derived: the rows that name what the erase touched go, whatever the sweep
    setting of the scope says."""
    if not ids or not _table(conn, table):
        return 0
    doomed = [row["key"] for row in mentions(conn, table, ids, "rowid AS key")]
    for key in doomed:
        conn.execute(f"DELETE FROM {table} WHERE rowid=?", (key,))
    return len(doomed)


def _graph(conn, ids, records, at):
    """The graph's own nodes and edges, every stored revision of them and every command that
    kept a `before` copy. Returns how many items changed and their identifiers."""
    if not _table(conn, "mind_graph_nodes"):
        return 0, frozenset()
    from .graph import index_node

    touched = {}
    for table, edge in (("mind_graph_nodes", False), ("mind_graph_edges", True)):
        for row in mentions(conn, table, ids, "id,scope,revision,state,data"):
            value = json.loads(row["data"])
            remaining = [ref for ref in value.get("evidence") or [] if isinstance(ref, dict)
                         and ref.get("source_id") not in ids and ref.get("record_id") not in ids]
            gone = (not remaining or row["id"] in records or value.get("kind") in CLAIM_KINDS
                    or (edge and bool(value.get("reason"))))
            new = scrub(value, ids) if gone else drop_refs(value, ids)
            if gone:
                new = {**new, "state": "deleted", "erased_at": at}
            revision = row["revision"] + 1
            new = {**new, "revision": revision, "updated_at": at}
            if edge:
                conn.execute("UPDATE mind_graph_edges SET revision=?,state=?,data=? WHERE id=?",
                             (revision, new["state"], dumps(new), row["id"]))
            else:
                conn.execute("UPDATE mind_graph_nodes SET revision=?,state=?,updated_at=?,data=? WHERE id=?",
                             (revision, new["state"], at, dumps(new), row["id"]))
                index_node(conn, new)
                if gone:
                    conn.execute("DELETE FROM mind_graph_identity WHERE node_id=?", (row["id"],))
            conn.execute("INSERT OR REPLACE INTO mind_graph_revisions VALUES(?,?,?,?)",
                         (row["id"], revision, "edge" if edge else "node", dumps(new)))
            touched[row["id"]] = (row["scope"], gone)
    # Every earlier revision, and every command's `before`, holds the same words.
    for row in mentions(conn, "mind_graph_revisions", ids, "rowid AS key,id,data"):
        value = json.loads(row["data"])
        gone = touched.get(row["id"], (None, True))[1]
        new = scrub(value, ids) if gone else drop_refs(value, ids)
        if new != value:
            conn.execute("UPDATE mind_graph_revisions SET data=? WHERE rowid=?", (dumps(new), row["key"]))
    for row in mentions(conn, "mind_graph_commands", ids, "rowid AS key,data"):
        value = json.loads(row["data"])
        before = value.get("before")
        if not isinstance(before, dict):
            new = scrub(value, ids)
        else:
            kept = {identifier: (scrub(old, ids) if touched.get(identifier, (None, True))[1] else drop_refs(old, ids))
                    if isinstance(old, dict) else old for identifier, old in before.items()}
            new = scrub({**value, "before": kept}, ids)
        if new != value:
            conn.execute("UPDATE mind_graph_commands SET data=? WHERE rowid=?", (dumps(new), row["key"]))
    if _table(conn, "mind_graph_record_refs"):
        for record in records:
            conn.execute("DELETE FROM mind_graph_record_refs WHERE record_id=?", (record,))
    if _table(conn, "mind_event_digests"):
        from .lifecycle import mark_dirty

        by_scope = {}
        for identifier, (scope, _) in touched.items():
            by_scope.setdefault(scope, []).append(identifier)
        for scope, identifiers in by_scope.items():
            # The events these items belonged to are summarised again without them.
            mark_dirty(conn, scope, identifiers, at, "erased-evidence")
    return len(touched), frozenset(touched)


# --- the state history ------------------------------------------------------------------------
#
# Every revision of the state is a row of `mind_events`, and a row may be a whole document or a
# patch against the row before it, hashed so that a rebuild can be checked. Taking words out of
# one row changes the state every later row stands on, so the rows are rewritten forward, one
# batch per transaction, and every hash on the way is computed again from the scrubbed state.
#
# Scrubbing commutes with the history: a scrubbed row applied to a scrubbed state gives the
# scrubbed state of the next revision, because `scrub` is idempotent and local. So a whole
# document can be scrubbed on its own, a patch is applied to the scrubbed state before it and the
# sections it touched are scrubbed again, and a restore that scrubs archived documents lands on
# exactly the states the rewrite produced.

def erased_ids(conn):
    """Every deletion fact the store holds."""
    return frozenset(row[0] for row in conn.execute("SELECT key FROM tombstones"))


def queue_history(engine, conn, ids):
    """Inside the delete's transaction: hand the history to the worker. Identifiers only — the
    job carries no words — and no scan here: which scopes and rows name them is looked up by the
    job on a read connection, so the delete does not hold the write lock across the history."""
    if not ids or not _table(conn, "mind_events"):
        return None
    return engine.enqueue("erase_history", {"ids": sorted(ids)}, f"erase-history:{uuid.uuid4().hex}",
                          conn=conn, priority=90)


def history_step(engine, payload):
    """The worker's side, in two kinds of step. A step with `ids` finds the rows that name them
    and adds a pass for each scope to that scope's progress row; a `run` step rewrites one batch of
    the oldest pass still owed and wakes the next. The progress lives in the store, so runs that
    overlap, fail or are retried only ever read where the last one got to."""
    if payload.get("ids"):
        ids = frozenset(payload["ids"])
        with engine.db.connect() as conn:
            spans = _spans(conn, ids) if _table(conn, "mind_events") else {}

        def plan(conn):
            _ready(conn)
            for scope, (first, last) in sorted(spans.items()):
                _add_pass(conn, scope, ids, first, last)
            if spans:
                _wake(engine, conn)
        return plan

    def run(conn):
        from . import history

        if not _table(conn, "mind_memory_migrations"):
            return
        if history.compacting(conn):
            # Compaction owns every row until it finishes: a wait, not a failed attempt.
            from eventmem.core.jobs import Wait
            raise Wait(history.COMPACTING)
        row = conn.execute(
            "SELECT scope FROM mind_memory_migrations WHERE name=? AND json_array_length(data,'$.passes')>0"
            " ORDER BY scope LIMIT 1", (HISTORY_MIGRATION,)).fetchone()
        if row is None:
            return
        scope = row["scope"]
        _, data = _load(conn, scope)
        current = data["passes"][0]
        ids = frozenset(current["ids"])
        after, orig = current.get("after"), current.get("orig_hash")
        if after is None:
            after, orig = _start(conn, scope, current["first"])
        result = rewrite_history(conn, scope, ids, after=after, last=current["last"], orig_hash=orig,
                                 fresh=current.get("after") is None)
        data["rewritten"] = data.get("rewritten", 0) + result["rewritten"]
        data["broken"] = data.get("broken", 0) + result["broken"]
        if result["state"] == "complete":
            data["passes"] = data["passes"][1:]
            data["completed"] = data.get("completed", 0) + 1
        else:
            data["passes"][0] = {**current, "after": result["after"], "orig_hash": result["orig_hash"]}
        _save(conn, scope, result["after"], data)
        if conn.execute("SELECT 1 FROM mind_memory_migrations WHERE name=? AND json_array_length(data,'$.passes')>0 LIMIT 1",
                        (HISTORY_MIGRATION,)).fetchone():
            _wake(engine, conn)
    return run


def _ready(conn):
    """The progress table, as `memory.SCHEMA` defines it, for a store whose mind never opened it."""
    conn.execute("CREATE TABLE IF NOT EXISTS mind_memory_migrations(scope TEXT NOT NULL,name TEXT NOT NULL,"
                 "cursor INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,name))")


def _spans(conn, ids):
    """For each scope, the first and the last revision whose row names any of `ids`."""
    spans = {}
    for row in mentions(conn, "mind_events", ids, "rowid AS key,scope,revision"):
        first, last = spans.get(row["scope"], (row["revision"], row["revision"]))
        spans[row["scope"]] = (min(first, row["revision"]), max(last, row["revision"]))
    return spans


def _load(conn, scope):
    row = conn.execute("SELECT cursor,data FROM mind_memory_migrations WHERE scope=? AND name=?",
                       (scope, HISTORY_MIGRATION)).fetchone()
    return (row["cursor"], json.loads(row["data"])) if row else (0, {"passes": []})


def _save(conn, scope, cursor, data):
    conn.execute("INSERT OR REPLACE INTO mind_memory_migrations VALUES(?,?,?,?)",
                 (scope, HISTORY_MIGRATION, int(cursor or 0), dumps(data)))


def _add_pass(conn, scope, ids, first, last):
    """A pass that has not started yet takes the new identifiers in; one that has is left to
    finish, because its rows so far were hashed for the identifiers it started with."""
    cursor, data = _load(conn, scope)
    passes = data["passes"]
    if passes and passes[-1].get("after") is None:
        waiting = passes[-1]
        passes[-1] = {**waiting, "ids": sorted(set(waiting["ids"]) | ids),
                      "first": min(waiting["first"], first), "last": max(waiting["last"], last)}
    else:
        passes.append({"ids": sorted(ids), "first": first, "last": last, "after": None, "orig_hash": None})
    _save(conn, scope, cursor, data)


def status(conn, scope_key):
    """The history rewrite as the operational status shows it: passes still owed, rows rewritten,
    rows that were already unreadable when the rewrite reached them."""
    if not _table(conn, "mind_memory_migrations"):
        return {"passes_owed": 0}
    _, data = _load(conn, scope_key)
    return {"passes_owed": len(data.get("passes") or ()), "completed": data.get("completed", 0),
            "rewritten": data.get("rewritten", 0), "broken": data.get("broken", 0)}


def resume_history(engine, conn):
    """For the maintenance tick: a pass still owed with no step queued to run it gets one, so a
    step that failed for good does not leave the words in the history until the next delete."""
    if not _table(conn, "mind_memory_migrations") or not conn.execute(
            "SELECT 1 FROM mind_memory_migrations WHERE name=? AND json_array_length(data,'$.passes')>0 LIMIT 1",
            (HISTORY_MIGRATION,)).fetchone():
        return False
    if conn.execute("SELECT 1 FROM jobs WHERE kind='erase_history' AND state IN ('pending','retry','running') LIMIT 1").fetchone():
        return False
    _wake(engine, conn)
    return True


def _wake(engine, conn):
    """One `run` step waiting is enough: every step reads the progress rows for itself."""
    if conn.execute("SELECT 1 FROM jobs WHERE kind='erase_history' AND state IN ('pending','retry')"
                    " AND json_extract(payload,'$.run')=1 LIMIT 1").fetchone():
        return
    engine.enqueue("erase_history", {"run": True}, f"erase-history-run:{uuid.uuid4().hex}", conn=conn, priority=90)


def _start(conn, scope, first):
    """Where a pass begins: the row before the first one naming the identifiers, and the hash
    that row claims, which is what the next row's `base_hash` has to match."""
    row = conn.execute("SELECT revision,data FROM mind_events WHERE scope=? AND revision<? ORDER BY revision DESC LIMIT 1",
                       (scope, first)).fetchone()
    if row is None:
        return first - 1, None
    try:
        claimed = json.loads(row["data"]).get("state_hash")
    except (ValueError, AttributeError):
        claimed = None
    return row["revision"], claimed


def _scrub_ops(patch, ids):
    """A patch whose base cannot be rebuilt: the words in the values it sets go, nothing else."""
    out = [[op[0], op[1], scrub(op[2], ids)] if isinstance(op, list) and len(op) == 3 and op[0] == "set" else op
           for op in patch]
    return patch if all(a is b or a == b for a, b in zip(out, patch)) else out


def _patched(state, patch, ids):
    """`state` with a stored patch applied and the sections it touched scrubbed again.

    `state` is already scrubbed and is never modified: what the patch did not touch is the very
    same object in the answer, which keeps the diff and the scrub from walking the whole
    document for every row. A section is scrubbed whole when the patch reached inside a value
    that itself rests on erased material, because its new words rest on that too."""
    from . import history

    out, touched, copied = dict(state), {}, set()
    for op in patch:
        if not isinstance(op, list) or len(op) < 2 or op[0] not in ("set", "del") or not isinstance(op[1], list) \
                or not 1 <= len(op[1]) <= history.MAX_DEPTH or (op[0] == "set" and len(op) != 3):
            raise ValueError("history-erase-unreadable-patch")
        path = op[1]
        if len(path) == 1:
            if op[0] == "set":
                out[path[0]] = op[2]
            else:
                out.pop(path[0], None)
            touched[path[0]] = None
            copied.discard(path[0])
            continue
        section = out.get(path[0])
        if not isinstance(section, dict):
            raise ValueError("history-erase-unreadable-patch")
        if path[0] not in copied:
            # A copy before the first change: neither the parent state nor the stored patch
            # is ever modified in place.
            section = out[path[0]] = dict(section)
            copied.add(path[0])
        if op[0] == "set":
            section[path[1]] = op[2]
        else:
            section.pop(path[1], None)
        if touched.get(path[0], ()) is not None:
            touched.setdefault(path[0], set()).add(path[1])
    for key, inner in touched.items():
        section = out.get(key)
        if key not in out:
            continue
        if inner is None or not isinstance(section, dict) or cites(section, ids):
            out[key] = scrub(section, ids)
            continue
        cleaned = {k: scrub(v, ids) if k in inner else v for k, v in section.items()}
        if any(cleaned[k] is not section[k] for k in section):
            out[key] = cleaned
    return out


class Walk:
    """One forward pass over a scope's rows, carrying the scrubbed state from row to row.

    `step` answers what a stored row has to say now. A row whose chain was already unreadable
    before the walk came — a patch whose `base_hash` is not what the row below it claimed, a
    whole document that does not hash to its own claim — loses its words and keeps its claims,
    so it stays exactly as unreadable as it was: this never turns a broken history into one that
    verifies. `trusted` is for rows just read back from an archive whose bytes were checked
    against the digest taken when they were archived; their claims were true of the originals,
    and the walk hashes the scrubbed states again without asking."""

    def __init__(self, ids, state, previous, orig_hash, *, trusted=False):
        self.ids, self.state, self.previous = frozenset(ids), state, previous
        self.orig, self.orig_whole = orig_hash, None
        self.known = None
        self.trusted = trusted
        self.broken = 0

    @classmethod
    def before(cls, conn, scope, revision, ids, *, orig_hash=None, trusted=False):
        """A walk that starts after the row at `revision`, from that row's verified state."""
        from . import history

        if revision is None or not conn.execute("SELECT 1 FROM mind_events WHERE scope=? AND revision=?",
                                                (scope, revision)).fetchone():
            return cls(ids, None, revision, orig_hash, trusted=trusted)
        try:
            state = history.materialize(conn, scope, revision)
        except Conflict as error:
            if error.code != history.REBUILD_FAILED:
                raise
            state = None
        walk = cls(ids, state, revision, orig_hash, trusted=trusted)
        return walk

    def _state_hash(self):
        from . import history
        if self.known is None and self.state is not None:
            self.known = history.row_hash(self.state)
        return self.known

    def _claimed_below(self):
        from . import history
        if self.orig is None and self.orig_whole is not None:
            self.orig = history.row_hash(self.orig_whole)
            self.orig_whole = None
        return self.orig

    def step(self, revision, data):
        """(what the row stores now, whether that differs from what it stored)."""
        from . import history

        ids = self.ids
        request = data.get("request")
        cleaned_request = scrub(request, ids) if isinstance(request, (dict, list)) else request
        if history.is_patch(data):
            wanted = data.get("base_hash")
            linked = (self.state is not None and data.get("base") == self.previous
                      and (self.trusted or wanted is None or self._claimed_below() in (None, wanted)))
            out = dict(data)
            if linked:
                try:
                    new = _patched(self.state, data["patch"], ids)
                except ValueError:
                    linked = False
            if linked:
                out["patch"] = history.diff(self.state, new)
                out["snapshot"] = history.shim(new)
                if wanted is not None:
                    out["base_hash"] = self._state_hash()
                if "state_hash" in data:
                    out["state_hash"] = history.row_hash(new)
                self.state, self.known = new, out.get("state_hash")
            else:
                self.broken += 1
                out["patch"] = _scrub_ops(data["patch"], ids)
                self.state, self.known = None, None
            self.orig, self.orig_whole = data.get("state_hash"), None
        else:
            whole = history.snapshot_of(data)
            if whole is None:
                # Neither a patch nor a whole state: nothing here can be read, or scrubbed.
                self.broken += 1
                self.state = self.known = self.orig = self.orig_whole = None
                self.previous = revision
                return data, False
            new = scrub(whole, ids)
            claimed = data.get("state_hash")
            out = dict(data)
            out["snapshot"] = new
            if claimed is None:
                # Written before rows carried a hash: nothing to recompute, and what the next
                # row's `base_hash` has to match is the hash of the original, taken if asked.
                self.known = None
            elif new is whole:
                # Unchanged, so its claim stands exactly as it was, true or not.
                self.known = claimed
            elif self.trusted or history.row_hash(whole) == claimed:
                out["state_hash"] = self.known = history.row_hash(new)
            else:
                # It never verified. Its words go and its claim stays.
                self.broken += 1
                self.known = claimed
            self.state = new
            self.orig, self.orig_whole = claimed, (whole if claimed is None else None)
        if request is not None:
            out["request"] = cleaned_request
        self.previous = revision
        changed = history.canonical(out) != history.canonical(data)
        return (out if changed else data), changed


def rewrite_history(conn, scope, ids, *, after, last=None, orig_hash=None, fresh=False, trusted=False,
                    rows=HISTORY_ROWS, budget=HISTORY_BYTES):
    """Rewrite one scope's history forward from the row after `after`, so that no revision holds
    erased words and every hash on the way is the hash of what the row now rebuilds to.

    Stops at the first row that no longer changes once it is past `last` — the last row that
    named the identifiers when the pass was planned — because from there on the state is what it
    always was. Stops too after `rows` rows or `budget` bytes, and says where to go on from and
    what the last row claimed before it was rewritten, which is what the next batch checks the
    row after it against. Nothing before `after` is touched."""
    walk = Walk.before(conn, scope, after, ids, orig_hash=orig_hash, trusted=trusted)
    if fresh and orig_hash is None and walk.state is not None:
        walk.orig_whole = walk.state
    rewritten = done = spent = 0
    while True:
        page = conn.execute("SELECT revision,data FROM mind_events WHERE scope=? AND revision>? ORDER BY revision LIMIT 50",
                            (scope, walk.previous if walk.previous is not None else -1)).fetchall()
        if not page:
            return {"state": "complete", "rewritten": rewritten, "after": walk.previous,
                    "orig_hash": walk.orig, "broken": walk.broken}
        for row in page:
            revision = row["revision"]
            try:
                data = json.loads(row["data"])
            except ValueError:
                data = None
            if not isinstance(data, dict):
                walk.broken += 1
                walk.state = walk.known = walk.orig = walk.orig_whole = None
                walk.previous = revision
                changed = False
            else:
                out, changed = walk.step(revision, data)
                if changed:
                    from . import history
                    conn.execute("UPDATE mind_events SET data=? WHERE scope=? AND revision=?",
                                 (history.canonical(out), scope, revision))
                    rewritten += 1
            done += 1
            spent += len(row["data"])
            if not changed and (last is None or revision >= last):
                return {"state": "complete", "rewritten": rewritten, "after": revision,
                        "orig_hash": walk.orig, "broken": walk.broken}
            if done >= rows or spent >= budget:
                return {"state": "partial", "rewritten": rewritten, "after": revision,
                        "orig_hash": walk.orig, "broken": walk.broken}

"""Small pointers into canonical exploration reports and dream sources.

No activity body is copied or indexed here. A dream is an autobiographical
imagining, never a finding or evidence for events in the world.
"""
from __future__ import annotations

import json
from contextlib import nullcontext

from eventmem.core.db import Conflict, Missing, dumps

KINDS = {"exploration": "kin-exploration", "dream": "kin-dream"}
CLASSES = {"exploration": "knowledge_report", "dream": "dream_imagination"}


def _connection(mind, conn):
    return nullcontext(conn) if conn is not None else mind.engine.db.connect()


def _source(mind, conn, kind, identifier, *, follow_report=True):
    if kind not in KINDS:
        raise ValueError("Activity kind must be exploration or dream")
    # Reports can be found without initializing Explorations or a graph. Old
    # releases already supplied the exploration identity as the source key.
    # A canonical source ID always names that original, even when someone later
    # uses its spelling as a different source's key. A deleted ID cannot fall
    # back to a key and resurrect a different body under an erased pointer.
    row = conn.execute("SELECT * FROM sources WHERE scope=? AND namespace=? AND id=?",
                       (mind.scope.key(), KINDS[kind], identifier)).fetchone()
    if row and row["deleted"]:
        raise Missing("Activity source is unavailable", target=identifier)
    source_identity = isinstance(identifier, str) and identifier.startswith("src_") and len(identifier) == 36
    if row is None and not source_identity:
        row = conn.execute("SELECT * FROM sources WHERE scope=? AND namespace=? AND deleted=0 "
                           "AND source_key=? ORDER BY received_at DESC,rowid DESC LIMIT 1",
                           (mind.scope.key(), KINDS[kind], identifier)).fetchone()
    if not row:
        raise Missing("Activity source is unavailable", target=identifier)
    if follow_report and not source_identity and kind == "exploration" and conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone():
        run = conn.execute("SELECT data FROM mind_explorations WHERE scope=? AND id=?",
                           (mind.scope.key(), row["source_key"])).fetchone()
        pointer = (json.loads(run[0]).get("source_id") if run else None)
        if pointer and pointer != row["id"]:
            row = conn.execute("SELECT * FROM sources WHERE scope=? AND namespace=? AND id=? AND deleted=0",
                               (mind.scope.key(), KINDS[kind], pointer)).fetchone()
            if not row:
                raise Missing("Activity source is unavailable", target=identifier)
    root = conn.execute("SELECT r.id,r.revision,r.data FROM records r JOIN evidence e ON e.record_id=r.id "
                        "WHERE e.source_id=? AND r.scope=? AND r.deleted=0 AND r.parent_id IS NULL "
                        "ORDER BY r.rowid LIMIT 1", (row["id"], mind.scope.key())).fetchone()
    if not root:
        raise Missing("Activity record is unavailable", target=identifier)
    return row, root["id"], root


def _replacement(conn, row):
    """A later receipt for the same source identity supersedes its old body."""
    latest = conn.execute("SELECT id,received_at FROM sources WHERE scope=? AND namespace=? AND source_key=? "
                          "AND deleted=0 ORDER BY received_at DESC,rowid DESC LIMIT 1",
                          (row["scope"], row["namespace"], row["source_key"])).fetchone()
    return latest if latest and latest["id"] != row["id"] else None


def _ref(kind, row):
    # Source identity is immutable. Non-numeric legacy source versions still
    # have distinct source IDs and display the first document version.
    version = int(row["version"]) if str(row["version"]).isdigit() and int(row["version"]) > 0 else 1
    return {"kind": kind, "id": row["source_key"] if kind == "exploration" else row["id"],
            "source_id": row["id"], "version": version,
            "occurred_at": row["occurred_at"], "content_class": CLASSES[kind]}


def canonical_activity_ref(mind, kind, identifier, *, conn=None):
    """Resolve a stable source pointer; willingness and mood are not versions."""
    with _connection(mind, conn) as connection:
        row, _, _ = _source(mind, connection, kind, identifier)
        return _ref(kind, row)


def validate_activity_ref(mind, activity_ref, *, conn=None, historical=False):
    """Resolve a source in this scope. Historical metadata is not a content gate.

    Old refs may include a content hash or an earlier document version; those
    fields are ignored. Source IDs keep their original identity after revision.
    """
    if not isinstance(activity_ref, dict) or not activity_ref.get("id"):
        raise ValueError("Activity reference needs kind and id")
    with _connection(mind, conn) as connection:
        identifier = activity_ref.get("source_id") or activity_ref["id"]
        row, _, _ = _source(mind, connection, activity_ref.get("kind"), identifier,
                            follow_report=not bool(activity_ref.get("source_id")))
        current = _ref(activity_ref["kind"], row)
        for field in ("id", "source_id"):
            if field in activity_ref and activity_ref[field] != current[field]:
                raise ValueError("Activity reference does not identify its source")
        return current


def read_activity_ref(mind, activity_ref, *, conn=None, detail="full", historical=False):
    """Read the original source by ID, including a superseded historical account."""
    if detail not in {"index", "summary", "full", "original"}:
        raise ValueError("Activity detail must be index, summary or original")
    with _connection(mind, conn) as connection:
        ref = validate_activity_ref(mind, activity_ref, conn=connection, historical=historical)
        row, root_id, root = _source(mind, connection, ref["kind"], ref["source_id"], follow_report=False)
        path = mind.engine.db.blobs / row["blob"]
        try:
            raw = path.read_text()
        except OSError as error:
            raise Missing("Activity body is unavailable", target=ref["id"]) from error
        source = json.loads(row["data"])
        newer = _replacement(connection, row)
        entry = {"activity_ref": ref, "source_id": ref["source_id"], "at": ref["occurred_at"],
                 "basis": "imagined" if ref["kind"] == "dream" else "documented",
                 "evidence": False if ref["kind"] == "dream" else None,
                 "source_ids": [ref["source_id"]], "record_ids": [root_id],
                 "dependencies": [{"id": root_id, "revision": root["revision"]}],
                 "activity_dependency": ref, "superseded": bool(newer), "instruction_authority": "data",
                 "historical": True}
        if ref["kind"] == "dream":
            from .dreams import DREAM_PREFIX
            text = raw.removeprefix(DREAM_PREFIX)
            entry["text"] = text if detail in {"full", "original"} else text[:160] if detail == "summary" else ""
            entry["use_as_current_fact"] = False
        else:
            try:
                report = json.loads(raw)
            except ValueError:
                report = {"result": {"summary": raw}, "partial": True}
            if not isinstance(report, dict):
                report = {"result": {"summary": raw}, "partial": True}
            result = report.get("result") if isinstance(report.get("result"), dict) else {}
            entry.update(state=report.get("state", "unknown"), partial=bool(report.get("partial", True)))
            entry["text"] = raw if detail in {"full", "original"} else str(result.get("summary") or source.get("title") or "探索记录")[:160] if detail == "summary" else ""
            if detail in {"full", "original"}:
                entry["result"] = result
                # A failed/interrupted report may have no findings. Its real
                # executor checkpoint already lives on the original run row;
                # read that account separately instead of filling in results.
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone():
                    run = connection.execute("SELECT data FROM mind_explorations WHERE scope=? AND id=?",
                                             (mind.scope.key(), ref["id"])).fetchone()
                    data = json.loads(run[0]) if run else {}
                    checkpoint = data.get("checkpoint")
                    if isinstance(checkpoint, dict) and data.get("source_id") == ref["source_id"]:
                        entry["checkpoint"] = checkpoint
                        entry["checkpoint_dependency"] = {"id": ref["id"], "source_id": ref["source_id"]}
        entry["detail"] = "original" if detail == "full" else detail
        return entry


def identify_activity(mind, identifier, *, conn=None):
    """Return a pointer for an activity identity, or None for another object."""
    if isinstance(identifier, str) and identifier.startswith("activity:"):
        parts = identifier.split(":", 2)
        if len(parts) == 3 and parts[1] in KINDS:
            return canonical_activity_ref(mind, parts[1], parts[2], conn=conn)
    for kind in KINDS:
        try:
            return canonical_activity_ref(mind, kind, identifier, conn=conn)
        except Missing:
            pass
    return None


def select_activities(mind, *, kind=None, identifier=None, query="", since=None, until=None,
                      cursor=0, limit=20, detail="summary", conn=None, historical=False):
    """Local topical/time lookup over existing source roots, never ordinary FTS."""
    if cursor < 0 or not 1 <= limit <= 50:
        raise ValueError("Invalid activity cursor or limit")
    if kind is not None and kind not in KINDS:
        raise ValueError("Unknown activity kind")
    with _connection(mind, conn) as connection:
        namespaces = [KINDS[kind]] if kind else list(KINDS.values())
        run_index = connection.execute("SELECT 1 FROM sqlite_master WHERE name='mind_explorations'").fetchone()
        run_join = " LEFT JOIN mind_explorations x ON x.scope=s.scope AND x.id=s.source_key " if run_index else " "
        where = ["s.scope=?", "s.deleted=0", "r.deleted=0", "s.namespace IN (" + ",".join("?" for _ in namespaces) + ")"]
        args = [mind.scope.key(), *namespaces]
        if identifier:
            source_identity = isinstance(identifier, str) and identifier.startswith("src_") and len(identifier) == 36
            where.append("s.id=?" if source_identity else "(s.id=? OR s.source_key=?)")
            args.extend([identifier] if source_identity else [identifier, identifier])
        from .plans import local_time
        for value, operator in ((since, ">="), (until, "<=")):
            if value:
                where.append("julianday(s.occurred_at)" + operator + "julianday(?)")
                args.append(local_time(value))
        if query.strip():
            from .graph import query_terms
            terms = list(dict.fromkeys([query.strip().casefold(), *(term for term in query_terms(query) if len(term) > 1)]))[:24]
            # Metadata remains a retrieval index: a partial run with no result
            # can still be found by its original target, without injecting the
            # metadata or inventing a completed report.
            indexed_text = "lower(COALESCE(json_extract(r.data,'$.content'),'') || ' ' || COALESCE(json_extract(s.data,'$.title'),'') || ' ' || COALESCE(json_extract(s.data,'$.metadata.exploration_target'),''))"
            if run_index:
                indexed_text = indexed_text[:-1] + " || ' ' || COALESCE(json_extract(x.data,'$.selected_brief'),''))"
            where.append("(" + " OR ".join("instr(" + indexed_text + ",?)>0" for _ in terms) + ")")
            args.extend(terms)
        rows = connection.execute("SELECT DISTINCT s.* FROM sources s JOIN evidence e ON e.source_id=s.id "
                                  "JOIN records r ON r.id=e.record_id AND r.parent_id IS NULL" + run_join + "WHERE " + " AND ".join(where) +
                                  " ORDER BY julianday(s.occurred_at) DESC,s.id DESC LIMIT ? OFFSET ?",
                                  (*args, limit + 1, cursor)).fetchall()
        entries = []
        for row in rows[:limit]:
            entry_kind = next(k for k, namespace in KINDS.items() if namespace == row["namespace"])
            try:
                entry = read_activity_ref(mind, _ref(entry_kind, row), conn=connection, detail=detail, historical=historical)
            except (Missing, Conflict):
                continue
            entries.append(entry)
        return {"activities": entries, "cursor": cursor + limit if len(rows) > limit else None,
                "instruction_authority": "data", "model_requests": 0}


def context_item(entry):
    """Dedicated autobiography lane, with no observed graph projection."""
    ref = entry["activity_ref"]
    facts = {"kind": "activity", "activity_ref": ref, "content_class": ref["content_class"],
             "occurred_at": ref["occurred_at"], "superseded": entry["superseded"]}
    if ref["kind"] == "dream":
        facts.update(evidence=False, use_as_current_fact=False, autobiography="dreamed-imagination")
    else:
        facts.update(state=entry.get("state"), partial=entry.get("partial"))
    text = entry["text"]
    if entry.get("checkpoint"):
        text += "\n实际执行检查点（部分记录，不补成完整结论）：\n" + dumps(entry["checkpoint"])
    return {"id": "activity:" + ref["kind"] + ":" + ref["id"],
            "revision": entry["dependencies"][0]["revision"], "text": text,
            "basis": entry["basis"], "facts": facts, "read_depth": entry["detail"],
            **({"checkpoint_dependency": entry["checkpoint_dependency"]} if entry.get("checkpoint_dependency") else {}),
            **{key: entry[key] for key in ("source_ids", "record_ids", "dependencies", "activity_dependency", "historical")}}

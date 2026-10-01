"""Kin's diary as 小光 reads it, and 小光's replies to it (2026-10-01, after Serein's diary comments).

**Where it is read.** A diary entry is a `kin-reflection` source whose root record is an `episode`
(memory.remember_reflection). The console's "日记与自述" group lists the narrative record kinds
(diary, summary, portrait, self narrative, prediction), so not one entry of Kin's own diary was ever
there: they were only to be found among all the episodes of the timeline. `read` is that diary,
newest first, each entry with the replies it has; the console shows it at the head of the group.

**A reply.** With `diary_replies` on, 小光 may answer an entry (`reply`). Her words are an owner
statement like any message of hers -- a source of its own namespace (`kin-diary-reply`), explicit
authority, role user -- and nothing else is in them: the entry is not quoted into the reply. Which
entry it answers is the reply's own `reply_to`, an index in its metadata, never shown to a model. So
the link needs nothing of its own from a delete: deleting the reply takes the link with it, and
deleting the entry leaves a bare id that nothing reads as words -- the reply stays 小光's, without an
entry to stand under. Nothing is sent and nothing comes up in a chat.

**What the next appraisal sees.** The reply is queued for an appraisal through the ordinary path,
as new evidence of an interaction. That appraisal is also shown which entry it answers
(`replies_context`): the entry's own words as Kin's thought, by its source id, checked current at
commit like the latest diaries; an entry deleted since is shown as gone. `REPLY_PROMPT` says what it
is, only to a request that carries one.
"""
from __future__ import annotations

import json

from eventmem.core.db import Conflict, Missing
from eventmem.core.models import SourceInput

from .autonomy_schema import enabled

SWITCH = "diary_replies"
REFLECTIONS = "kin-reflection"
NAMESPACE = "kin-diary-reply"
HOST_EVENT = "diary-reply"
REPLY_LIMIT = 2000
EXCERPT = 400

# --- Model-facing wording (NEEDS 小光 OK) ----------------------------------------------------------
REPLY_PROMPT = ("\ndiary_replies 列出这次新证据里小光对你日记的回复：reply_id 是小光的回复（就在 new_evidence 里，是小光本人的话），"
                "diary 是这条回复所回的那一篇（你当时自己的想法，可以用它的 source_id 引用）；diary 为 null 表示那篇日记已经删掉了。"
                "小光回了你的日记，这是一次真实的新互动，照常评估；小光的回复是小光的原话，日记仍然只是你当时的想法。")


def _thought(text):
    """An entry's own words, without the line that says whose they are (memory.remember_reflection)."""
    return text.split("\n", 1)[1] if "\n" in text else text


def _text(engine, source_id):
    try:
        return engine.source(source_id, content=True).read_text()
    except (Missing, OSError):
        return None


def _entry(engine, row, *, excerpt=None):
    text = _text(engine, row["id"])
    if text is None:
        return None
    metadata = (json.loads(row["data"]) if row["data"] else {}).get("metadata") or {}
    thought = _thought(text).strip()
    return {"source_id": row["id"], "at": row["occurred_at"], "topic": metadata.get("topic"),
            "text": thought[:excerpt] if excerpt else thought}


def read(mind, *, cursor=0, limit=20):
    """Kin's diary entries, newest first, each with 小光's replies (oldest first), and whether 小光 may
    reply now. Every entry is read whatever the switch says: it is the diary itself."""
    if not 1 <= limit <= 100 or int(cursor) < 0:
        raise ValueError("Invalid diary page")
    scope, engine = mind.scope.key(), mind.engine
    with engine.db.connect() as conn:
        rows = conn.execute("SELECT id,occurred_at,data FROM sources WHERE namespace=? AND scope=? AND deleted=0 "
                            "ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?",
                            (REFLECTIONS, scope, limit + 1, int(cursor))).fetchall()
        replying = enabled(conn, scope, SWITCH)
        ids = [row["id"] for row in rows[:limit]]
        answered = conn.execute("SELECT id,occurred_at,json_extract(data,'$.metadata.reply_to') AS reply_to FROM sources "
                                "WHERE namespace=? AND scope=? AND deleted=0 AND json_extract(data,'$.metadata.reply_to') IN ("
                                + ",".join("?" * len(ids)) + ") ORDER BY occurred_at,id", (NAMESPACE, scope, *ids)).fetchall() if ids else []
    replies = {}
    for row in answered:
        text = _text(engine, row["id"])
        if text is not None:
            replies.setdefault(row["reply_to"], []).append({"source_id": row["id"], "at": row["occurred_at"], "text": text})
    entries = []
    for row in rows[:limit]:
        entry = _entry(engine, row)
        if entry:
            entries.append({**entry, "replies": replies.get(row["id"], [])})
    return {"entries": entries, "cursor": int(cursor) + limit if len(rows) > limit else None,
            "replies": "enabled" if replying else "disabled"}


def reply(mind, request, *, agent_version=None):
    """小光's answer to one diary entry: kept as 小光's own statement and queued for an appraisal. A
    repeat of the same `command_id` with the same words is the same reply."""
    from .appraisal import Appraisals
    reflection, text, command = (request.get(key) for key in ("reflection_id", "text", "command_id"))
    if not isinstance(command, str) or not command.strip() or len(command) > 200:
        raise ValueError("A reply needs a command_id")
    if not isinstance(text, str) or not text.strip() or len(text) > REPLY_LIMIT:
        raise ValueError(f"A reply is 1..{REPLY_LIMIT} characters")
    scope, engine = mind.scope.key(), mind.engine
    with engine.db.connect() as conn:
        if not enabled(conn, scope, SWITCH):
            return {"state": "disabled"}
        if not isinstance(reflection, str) or not conn.execute(
                "SELECT 1 FROM sources WHERE id=? AND namespace=? AND scope=? AND deleted=0", (reflection, REFLECTIONS, scope)).fetchone():
            raise Missing("No such diary entry in this scope", code="diary-entry-unknown")
        version = agent_version or mind._load(conn).get("agent_version")
    source = engine.receive(SourceInput(
        namespace=NAMESPACE, key=command, scope=mind.scope, text=text, authority="explicit", occurred_at=mind.clock(),
        metadata={"host_event": HOST_EVENT, "role": "user", "channel": "console", "reply_to": reflection}))
    if (source.get("metadata") or {}).get("reply_to") != reflection:
        raise Conflict("This command already answered another entry", code="diary-reply-moved")
    job = Appraisals(mind).enqueue([source["id"]], version)
    return {"state": "recorded", "source_id": source["id"], "reply_to": reflection, "appraisal": job}


def replies_context(conn, mind, sources):
    """For the appraisal: which entry each reply among its new evidence answers. [] when it has none
    or the switch is off, so a request without one is the request it was."""
    if not enabled(conn, mind.scope.key(), SWITCH):
        return []
    found = []
    for source in sources:
        metadata = source.get("metadata") or {}
        if metadata.get("host_event") != HOST_EVENT or not isinstance(metadata.get("reply_to"), str):
            continue
        row = conn.execute("SELECT id,occurred_at,data FROM sources WHERE id=? AND namespace=? AND scope=? AND deleted=0",
                           (metadata["reply_to"], REFLECTIONS, mind.scope.key())).fetchone()
        found.append({"reply_id": source["id"], "diary": _entry(mind.engine, row, excerpt=EXCERPT) if row else None})
    return found

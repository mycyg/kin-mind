"""Kin's dreams (2026-10-01, after Serein's dream engine, at 小光's request).

**When.** Only an idle review, only while the rhythm is in its resting phase, at most once a night,
and only while the `dreams` setting is on. The host offers the section then and only then; whether
a dream comes at all is Kin's: the review may leave it empty, and most nights it should. A night is
the local day from noon to noon (Asia/Singapore), so 23:00 and 03:00 are the same night.

**From what.** What the last 48 hours left behind (`material`): the newest events the appraisals
recorded in the graph, the newest memory notes and the newest diary entries, at most two of each,
read the way an experience read reads them. The model is shown short excerpts by their ids; the host
keeps their references at the versions shown.

**What it is.** 80 to 220 characters, first person, present tense; dream logic is allowed, advice and
the interpretation of symbols are not (`DREAM_PROMPT`). It is kept as a derived source of its own
namespace (`kin-dream`) that rests on every piece of material it was offered: a delete of any of it
takes the dream along, and one that came in before it was stored keeps it from being stored. The
origin table calls it `kin_dream`: never experience, never evidence (`evidence_classes.never_evidence`),
never in the text index, never recalled; it is read only where dreams are shown -- `recent` for the
next idle reviews, `read` for the console and the tool.

**Telling.** Nothing here sends anything or brings a dream into a chat. The idle reviews of the next
day are shown the latest dream (`recent`); whether 小光 hears about it is the contact wish Kin makes
or does not make, through the ordinary path.
"""
from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from eventmem.core.db import Conflict, Missing, digest
from eventmem.core.engine import DERIVED_CONFLICTS
from eventmem.core.models import SourceInput

from .autonomy_schema import enabled
from .rhythm import REST_PHASE
from .state import timestamp

SECTION = SWITCH = "dreams"
NAMESPACE = "kin-dream"
ZONE = "Asia/Singapore"
# A night runs from one local noon to the next.
NIGHT_TURNS = 12
MATERIAL_HOURS, MATERIAL_EACH, EXCERPT = 48, 2, 200
# What a dream may be, checked when it is committed: a refusal costs the night's dream, never the review.
LENGTH = (80, 220)
# How long the latest dream is shown to the idle reviews after it.
RECENT_HOURS = 24
TITLE = "Kin 的梦"

# --- Model-facing wording (NEEDS 小光 OK) ----------------------------------------------------------
# The prefix every stored dream starts with: whatever shows its raw text says what it is.
DREAM_PREFIX = "Kin 的梦（想象出来的，不是发生过的事）：\n"
# The section's paragraph, appended only to a request that offers it.
DREAM_PROMPT = ("dream 只在休息相位的空闲评估里出现：今晚要不要做梦由你决定，不必每晚都有，大多数夜里留空就好。"
                "想做时，只从 dream_material 里取材料（最近 48 小时的经历、记忆和日记片段），写 text：80 到 220 个字，"
                "第一人称、现在时，像刚醒时还记得的那一段；可以不合逻辑、场景跳转、人和事错位。"
                "不给建议，不解释象征，不写成日记或总结。梦不是发生过的事，不能当作任何记忆、心事、特征或判断的依据。")
# Said once beside the latest dream, which the next idle reviews are shown.
RECENT_DREAMS_PROMPT = ("\nrecent_dreams 是你最近做的梦，是想象，不是发生过的事，也不能作为任何证据。"
                        "可以只放在心里；想讲给小光听时，照常提出 contact 愿望，把想讲的梦写进愿望内容里。梦不会自己发出去。")


def night_start(at):
    """The local noon this night began at, as an aware time."""
    local = timestamp(at).astimezone(ZoneInfo(ZONE))
    start = local.replace(hour=NIGHT_TURNS, minute=0, second=0, microsecond=0)
    return start if start <= local else start - timedelta(days=1)


def dreamt_tonight(conn, scope, at):
    since = night_start(at).isoformat()
    return conn.execute("SELECT 1 FROM sources WHERE namespace=? AND scope=? AND deleted=0 "
                        "AND julianday(occurred_at)>=julianday(?) LIMIT 1", (NAMESPACE, scope, since)).fetchone() is not None


def _excerpt(text):
    return " ".join(str(text or "").split())[:EXCERPT]


def material(conn, mind, at):
    """What the last 48 hours left behind, newest first: (what the model is shown, the references
    the dream will rest on). Nothing current is an empty pair."""
    from eventmem.core.read_policy import ReadPolicy

    scope, since = mind.scope.key(), (timestamp(at) - timedelta(hours=MATERIAL_HOURS)).isoformat()
    policy = ReadPolicy.load(mind.engine, mind.scope, "experience_recall", conn=conn)
    shown, refs = [], []

    def visible(record_id):
        try:
            record = mind.engine._get(conn, record_id)
        except Missing:
            return None
        return record if record.get("status") == "active" and policy.visible(record) else None

    def current(identifiers):
        try:
            found = mind._evidence(conn, identifiers)
        except (Missing, Conflict):
            return None
        return found if found and mind._fresh(conn, found) else None

    # Events an appraisal recorded (a runtime event of the host is a message or a receipt, not a moment).
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_graph_nodes'").fetchone():
        taken = 0
        for row in conn.execute("SELECT data FROM mind_graph_nodes WHERE scope=? AND kind='event' AND state='active' "
                                "AND json_extract(data,'$.operation') IS NULL AND json_extract(data,'$.basis') IN ('explicit','inferred','documented') "
                                "AND julianday(occurred_at)>=julianday(?) ORDER BY occurred_at DESC,id DESC LIMIT 12", (scope, since)):
            if taken >= MATERIAL_EACH:
                break
            node = json.loads(row[0])
            found = current([r["record_id"] for r in node.get("evidence") or []])
            if not found or any(visible(r["record_id"]) is None for r in found):
                continue
            text = _excerpt(node.get("text") or node.get("title"))
            if not text:
                continue
            shown.append({"id": node["id"], "kind": "event", "at": node.get("occurred_at"), "text": text,
                          "evidence_ids": sorted({r["record_id"] for r in found})})
            refs += found
            taken += 1
    # Memory notes the appraisals wrote.
    taken = 0
    for row in conn.execute("SELECT id FROM records WHERE scope=? AND deleted=0 AND status='active' "
                            "AND json_extract(data,'$.attributes.semantic_event') IS NOT NULL AND json_extract(data,'$.generated')=1 "
                            "AND julianday(updated_at)>=julianday(?) ORDER BY updated_at DESC,id DESC LIMIT 12", (scope, since)):
        if taken >= MATERIAL_EACH:
            break
        record = visible(row[0])
        found = current([row[0]]) if record else None
        if not found:
            continue
        shown.append({"id": record["id"], "kind": "note", "at": record.get("valid_from"),
                      "text": _excerpt(record.get("title", "") + "：" + record.get("content", ""))})
        refs += found
        taken += 1
    # Kin's own diary.
    taken = 0
    for row in conn.execute("SELECT id,occurred_at FROM sources WHERE namespace='kin-reflection' AND scope=? AND deleted=0 "
                            "AND julianday(occurred_at)>=julianday(?) ORDER BY occurred_at DESC,id DESC LIMIT 12", (scope, since)):
        if taken >= MATERIAL_EACH:
            break
        found = current([row["id"]])
        if not found:
            continue
        try:
            text = mind.engine.source(row["id"], content=True).read_text()
        except (Missing, OSError):
            continue
        # The diary's own words, without the line that says whose they are (memory.recent_reflections).
        shown.append({"id": row["id"], "kind": "diary", "at": row["occurred_at"],
                      "text": _excerpt(text.split("\n", 1)[1] if "\n" in text else text)})
        refs += found
        taken += 1
    shown.sort(key=lambda item: (item.get("at") or "", item["id"]), reverse=True)
    kept = {}
    for ref in refs:
        trace = {key: ref[key] for key in ("source_id", "record_id", "hash", "revision") if ref.get(key) is not None}
        kept[(trace.get("source_id"), trace.get("record_id"))] = trace
    return shown, list(kept.values())


def offer(conn, mind, view, data, *, at):
    """The material a dream may be written from, when this attempt may offer one: an idle review, in
    the resting phase, with no dream yet tonight and something to dream of. Otherwise None."""
    stimuli = set(data.get("stimuli") or [data.get("stimulus")])
    if ("idle-review" not in stimuli or (view.get("rhythm") or {}).get("phase") != REST_PHASE
            or not enabled(conn, mind.scope.key(), SWITCH) or dreamt_tonight(conn, mind.scope.key(), at)):
        return None
    shown, refs = material(conn, mind, at)
    return (shown, refs) if shown and refs else None


def commit_dream(commit):
    """`dream`: the host keeps it to its bounds and to tonight. The dream itself is stored after the
    commit (`remember`), as the diary is: a derived source needs a write of its own."""
    text = commit.value.text.strip()
    if not LENGTH[0] <= len(text) <= LENGTH[1]:
        raise Conflict("A dream is 80 to 220 characters", code="dream-length")
    if dreamt_tonight(commit.conn, commit.mind.scope.key(), commit.mind.clock()):
        raise Conflict("Kin has dreamt tonight already", code="dream-tonight")
    return {"length": len(text)}


def source_id(mind, event_id):
    """The id the dream of this appraisal is stored under (`Engine.receive`)."""
    return "src_" + digest([NAMESPACE, event_id, "1", mind.scope.key()])[:32]


def remember(memory, result):
    """Keep a committed dream. The appraisal event is its identity; a replay returns the source it
    stored. It rests on the material it was offered and was written from everything the appraisal
    was shown: one deleted or changed since, and it is not kept."""
    proposal = (result.get("proposal") or {}).get("dream") or {}
    committed = result.get("dream") or {}
    if not proposal.get("text") or not committed.get("derived_from") or committed.get("source_id") != source_id(memory.mind, result.get("event_id")):
        return None
    engine, scope = memory.engine, memory.scope
    with engine.db.connect() as conn:
        event = conn.execute("SELECT occurred_at FROM mind_events WHERE id=? AND scope=?",
                             (result["event_id"], scope.key())).fetchone()
        kept = conn.execute("SELECT * FROM sources WHERE namespace=? AND source_key=? AND scope=?",
                            (NAMESPACE, result["event_id"], scope.key())).fetchone()
    if not event:
        return None
    if kept:
        return engine._source(kept)
    shown = [ref for ref in result.get("evaluated_sources") or [] if isinstance(ref, dict)]
    shown += [identifier for identifier in result.get("evaluated_ids") or [] if isinstance(identifier, str)]
    try:
        return engine.receive(SourceInput(
            namespace=NAMESPACE, key=result["event_id"], scope=scope, occurred_at=event["occurred_at"],
            authority="model", kind="diary", title=TITLE, text=DREAM_PREFIX + proposal["text"].strip(),
            metadata={"role": "assistant", "basis": "imagined", "internal": True, "host_event": "dream",
                      "appraisal_event_id": result["event_id"]}),
            derived_from=committed["derived_from"], shown=shown)
    except Conflict as error:
        if getattr(error, "code", None) in DERIVED_CONFLICTS:
            return None
        raise


def _entry(engine, row):
    try:
        text = engine.source(row["id"], content=True).read_text()
    except (Missing, OSError):
        return None
    return {"source_id": row["id"], "at": row["occurred_at"], "text": text.split(DREAM_PREFIX, 1)[-1]}


def recent(mind, at=None):
    """The latest dream of the last day, for the idle reviews after it; [] when there is none."""
    at = at or mind.clock()
    since = (timestamp(at) - timedelta(hours=RECENT_HOURS)).isoformat()
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT id,occurred_at FROM sources WHERE namespace=? AND scope=? AND deleted=0 "
                           "AND julianday(occurred_at)>=julianday(?) ORDER BY occurred_at DESC,id DESC LIMIT 1",
                           (NAMESPACE, mind.scope.key(), since)).fetchone()
    entry = _entry(mind.engine, row) if row else None
    return [entry] if entry else []


def read(mind, *, cursor=0, limit=20):
    """Kin's dreams, newest first, for the console and the tool. Off, nothing is read."""
    with mind.engine.db.connect() as conn:
        if not enabled(conn, mind.scope.key(), SWITCH):
            return {"state": "disabled", "dreams": [], "cursor": None}
        rows = conn.execute("SELECT id,occurred_at FROM sources WHERE namespace=? AND scope=? AND deleted=0 "
                            "ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?",
                            (NAMESPACE, mind.scope.key(), limit + 1, cursor)).fetchall()
    dreams = [entry for entry in (_entry(mind.engine, row) for row in rows[:limit]) if entry]
    return {"state": "enabled", "dreams": dreams, "cursor": cursor + limit if len(rows) > limit else None}

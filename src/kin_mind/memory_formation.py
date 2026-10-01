"""How a conversation becomes memory: four rules, each behind a switch of its own, all off by default.

Each switch lives in `mind_memory_config` (memory.DEFAULTS). Off -- unset or false -- is the behaviour
before this module, exactly: no schema field, no prompt line, no table and no check of it runs.

* **`entity_name_check`** -- an entity node the graph writes names who or what its evidence names.
  Its title, or at least one of its aliases, normalized (NFKC, then casefold), appears in the text of a
  record it cites. An alias that appears in none of them is dropped (one the stored node already had
  stays: this write does not speak for it); a node none of whose names appears is refused, alone,
  under the memory section's item isolation (memory_items). The host reads text it holds; no model.
* **`faithful_notes`** -- the memory instructions carry FAITHFUL_NOTE_RULES: nicknames and puns as
  said, asking is not agreeing, one person's thought is not a shared conclusion, state words stay,
  no announcer's wrapper. The event digest prompt (lifecycle.py) is left as it is: changing it makes
  every digest dirty at once.
* **`memory_disposition`** -- every conversation source of a batch has a disposition: a committed
  note or graph node carries it, a committed event route takes it, or the assessment lists it in
  `memory.skipped` with a reason category. A conversation source is the host's public dialogue
  (`dialogue.is_public_dialogue`): the owner's messages, Kin's replies, and Kin's own accepted
  proactive messages. What is left without one is not organised: the host asks once more with only
  those sources (`coverage_of`, a memory-only enrichment, the one-shot follow-up), and what that pass
  leaves too goes to the ledger `queue_unorganized` reads (memory_items). Skip receipts are kept in
  `mind_memory_skips`, ids and a category only.
* **`memorable_marks`** -- the main session may mark sources it wants remembered (`mark_memorable`).
  The host keeps the marks (`mind_memorable_marks`), a quota per rolling window; the next assessment
  that organises a marked source is shown the mark and held to it as to a conversation source. A mark
  is consumed by the commit that gives its source a disposition, or expires.

Both tables are reached by an erase (`erase`, from kin_mind.erasure): a skip receipt naming an erased
source goes, and so does every row of a mark one of whose sources is erased, its reason with it.
Neither keeps an evidence reference, so neither is a table of evidence_refs. Nothing here shows a
model a source's metadata: the host reads it, as an index, to tell conversation from the rest.
"""
from __future__ import annotations

import json
import time
import unicodedata
from copy import deepcopy
from datetime import timedelta
from typing import Literal, NamedTuple

from pydantic import Field

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.models import Model

from .state import timestamp

ENTITY_CHECK = "entity_name_check"
FAITHFUL = "faithful_notes"
DISPOSITION = "memory_disposition"
MARKS = "memorable_marks"
SWITCHES = (ENTITY_CHECK, FAITHFUL, DISPOSITION, MARKS)

SKIP_REASONS = ("greeting", "receipt", "already_kept", "no_lasting_content")
SKIPPED_MAX = 50
# The follow-up that asks again for what a commit left without a disposition, and the field that
# names the commit it answers for. A follow-up never queues another.
FOLLOW_UP_PREFIX = "cover_"
COVERAGE_OF = "coverage_of"
# Lanes whose memory section is not this batch's to judge: a held-sections follow-up restates the
# parent's refused sections (its memory was the parent's), a bootstrap and a session review organise
# nothing. A memory-only pass of the ledger is judged, but asks no follow-up of its own.
NOT_JUDGED = frozenset({"held-sections", "continuity-bootstrap", "session-maintenance"})
NO_FOLLOW_UP = frozenset({"memory-backfill"})
# Public dialogue that is a conversation source: the owner's and Kin's own words. A delivery is the
# receipt of a reply already observed as Kin's message, unless Kin started it (`origin` proactive).
CONVERSATION_KINDS = frozenset({"owner-message", "assistant-message"})
PROACTIVE = "proactive"
METRIC = "memory_disposition"

# Marks: how many sources the main session may mark in a rolling window, how long one waits for the
# assessment that organises its source, and how long a settled one is kept before it is cleared.
MARK_QUOTA = 8
MARK_WINDOW_HOURS = 6
MARK_TTL_HOURS = 48
MARK_IDS_MAX = 8
MARK_REASON_MAX = 300
MARK_KEPT_DAYS = 30

SKIPS, MARK_TABLE = "mind_memory_skips", "mind_memorable_marks"
# Created the first time a switch that writes them is on, so a store with every switch off is the
# store it was. Statement by statement: they run inside the commit's own transaction.
SCHEMA = (
    f"CREATE TABLE IF NOT EXISTS {SKIPS}(scope TEXT NOT NULL,source_id TEXT NOT NULL,event_id TEXT NOT NULL,"
    "reason TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(scope,source_id,event_id))",
    f"CREATE TABLE IF NOT EXISTS {MARK_TABLE}(scope TEXT NOT NULL,mark_id TEXT NOT NULL,source_id TEXT NOT NULL,"
    "state TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,"
    "updated_at TEXT NOT NULL,event_id TEXT,PRIMARY KEY(scope,mark_id,source_id))",
    f"CREATE INDEX IF NOT EXISTS mind_memorable_mark_source ON {MARK_TABLE}(scope,source_id,state)",
)

# --- Model-facing wording. Every constant below NEEDS 小光 OK before its switch is turned on. ---------

# NEEDS 小光 OK. The memory instructions' faithful-note rules (`faithful_notes`).
FAITHFUL_NOTE_RULES = """
忠实记录（memory.notes 与图谱节点的 title、text）：
昵称、谐音、双关和玩笑话按说话人原来的写法保留，不改成正式说法，也不替说话人换成别的词。
追问、反问或复述对方的话不等于同意；只有明确表态才写成赞同、答应或决定。
一方单独展开的想法记成这一方的想法，不写成双方的共同结论或约定。
保留“打算、可能、想、还没定”这类状态词：计划不写成已经做了，猜测不写成事实。
直接写发生了什么，不加“在这段对话中”“用户表示”之类的报幕式开头和总结套话。"""

# NEEDS 小光 OK. What `memory.skipped` is (`memory_disposition` or `memorable_marks`).
SKIPPED_PROMPT = """
memory.skipped 列出本批 new_evidence 里刻意不记的来源，每项写 evidence_id 和 reason：greeting 是寒暄、应答或表情；receipt 是任务请求与完成回执、送达确认这类程序往来；already_kept 是内容已经由另一条记忆完整保存；no_lasting_content 是没有以后值得想起的内容。不要为了通过检查把来源一律写进 skipped，也不要猜测归属；拿不准就记一条简短笔记。
coverage_review 出现时，表示上一次整理没有给它列出的来源任何去向：只为这些来源逐条给出笔记、事件归属或 skipped，已经保存的内容不再重复。"""

# NEEDS 小光 OK. Every conversation source has a disposition (`memory_disposition`).
DISPOSITION_PROMPT = """
new_evidence 里的对话原话——host_event 为 message、owner-message、assistant-message、assistant-result 的来源，以及 Kin 主动发出并已送达的消息（delivery 来源中 origin 为 proactive 的）——每一条都要有去向：写进某条 memory.notes 或 memory.graph.nodes 的 evidence_ids，用 memory.event_routes 归到事件，或列进 memory.skipped。宿主逐条核对，没有去向的来源会单独再整理一次。"""

# NEEDS 小光 OK. The marks the main session made (`memorable_marks`).
MEMORABLE_PROMPT = """
memorable 列出 Kin 在聊天时标记“想记住这一段”的来源和当时写下的理由（理由是 Kin 自己的话，不是用户原话，也不是事实依据）：其中每一条都写成笔记或归到事件；确实不该记时列进 memory.skipped 并给出 reason。"""

# NEEDS 小光 OK. The description of the main session's `mark_memorable` tool.
MARK_TOOL_DESCRIPTION = ("标记“想记住这一段”。evidence_ids 填这段对话的来源编号（src_ 或 mem_ 开头，或当前输入的 input_id），"
                         "一次最多 8 个；reason 用一句话写为什么想记住。宿主只登记标记、不写记忆：之后后台整理这些来源时，"
                         "会为每一条写下笔记或归入事件，确实不该记的会写明原因。每 6 小时最多标记 8 条来源，标记 48 小时后失效；"
                         "已经整理完、不会再被整理的来源标记不上。只在真心想记住时用，不必每轮都标。")


class MemorySkip(Model):
    evidence_id: str = Field(min_length=1, max_length=200)
    reason: Literal["greeting", "receipt", "already_kept", "no_lasting_content"]


def ensure(conn):
    for statement in SCHEMA:
        conn.execute(statement)


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _on(conn, scope, flag):
    from .autonomy_schema import enabled
    return enabled(conn, scope, flag)


def _pages(values, size=400):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


# --- Requests: the switches an attempt is framed by -----------------------------------------------------

def rules(config):
    """The switches of this module that frame an assessment request, from a read of the memory
    settings. Empty: the request as it was."""
    return frozenset(name for name in (FAITHFUL, DISPOSITION, MARKS) if config.get(name) is True)


def offers_skipped(active):
    return bool({DISPOSITION, MARKS} & set(active or ()))


def prompt(active, *, operational=False):
    """The lines these switches add to the memory instructions; nothing on a lane without memory."""
    if operational or not active:
        return ""
    return "".join(text for flag, text in ((FAITHFUL, FAITHFUL_NOTE_RULES), ("skipped", SKIPPED_PROMPT),
                                           (DISPOSITION, DISPOSITION_PROMPT), (MARKS, MEMORABLE_PROMPT))
                   if flag in active or (flag == "skipped" and offers_skipped(active)))


def with_skipped(schema):
    """`schema` with `memory.skipped` in it. The field is left out of every schema pydantic writes, so
    a request that does not offer it is the request as it was (memory.MemoryAssessment)."""
    schema = deepcopy(schema)
    definitions = schema.setdefault("$defs", {})
    memory = definitions.get("MemoryAssessment")
    if memory is None:
        return schema
    definitions["MemorySkip"] = MemorySkip.model_json_schema()
    memory["properties"]["skipped"] = {"items": {"$ref": "#/$defs/MemorySkip"}, "maxItems": SKIPPED_MAX,
                                       "title": "Skipped", "type": "array"}
    return schema


def blank_skipped(proposal, active):
    """A proposal holds `memory.skipped` only where the request offered it."""
    if offers_skipped(active) or not proposal.memory.skipped:
        return proposal
    return proposal.model_copy(update={"memory": proposal.memory.model_copy(update={"skipped": []})})


def context(mind, refs, data, config):
    """What an assessment that organises memory is shown besides its sources: the marks Kin made on
    them, and, for a follow-up, which of them the last pass left without a disposition."""
    active, out = rules(config), {}
    if MARKS in active:
        sources = {ref["source_id"] for ref in refs}
        with mind.engine.db.connect() as conn:
            marks = open_marks(conn, mind.scope.key(), sources, mind.clock())
        if marks:
            out["memorable"] = [{"source_id": sid, "reason": reason} for sid, reason in sorted(marks.items())]
    if data.get(COVERAGE_OF) and offers_skipped(active):
        out["coverage_review"] = {"missing_ids": sorted({ref["source_id"] for ref in refs})}
    return out


# --- Item 1: an entity's name, verbatim in its evidence --------------------------------------------------

def _normal(text):
    return unicodedata.normalize("NFKC", str(text)).casefold().strip()


def verbatim_names(title, aliases, texts, kept=()):
    """(whether the title or one alias appears in `texts`, the aliases kept). Only names this write
    proposes can pass the check; an alias the stored node already had is kept without one."""
    corpus = [_normal(text) for text in texts]

    def found(name):
        name = _normal(name)
        return bool(name) and any(name in text for text in corpus)

    seen = {_normal(alias) for alias in kept}
    passing = [alias for alias in aliases if found(alias)]
    return (found(title) or bool(passing),
            [alias for alias in aliases if alias in passing or _normal(alias) in seen])


def check_entity(conn, engine, scope, item, evidence, previous):
    """The aliases an entity node may be written with, or the item's own refusal (graph.apply)."""
    texts = []
    for ref in evidence:
        try:
            texts.append(engine._get(conn, ref["record_id"])["content"])
        except Missing:
            continue
    passed, aliases = verbatim_names(item.title, item.aliases, texts, (previous or {}).get("aliases") or ())
    if not passed:
        raise Conflict("Entity name is not in its evidence")
    return aliases


# --- Item 3: every conversation source has a disposition -------------------------------------------------

class Formed(NamedTuple):
    missing: frozenset = frozenset()    # no disposition: not organised by this commit
    queued: frozenset = frozenset()     # ...asked again by the follow-up this commit queued
    withheld: frozenset = frozenset()   # ...left to the ledger (memory_items) as withheld
    follow_up: str | None = None


NOTHING = Formed()


def conversation(conn, mind, source_ids):
    """Which of `source_ids` the host keeps as public dialogue: the owner's words and Kin's own."""
    from .dialogue import envelope_prefixes, is_public_dialogue
    if not source_ids:
        return set()
    prefixes, found = envelope_prefixes(mind.engine, conn), set()
    for page in _pages(source_ids):
        for row in conn.execute("SELECT kind,data FROM mind_runtime_events WHERE scope=? AND json_extract(data,'$.source_id') IN ("
                                + ",".join("?" * len(page)) + ")", (mind.scope.key(), *page)):
            event = json.loads(row["data"])
            if not is_public_dialogue(event, prefixes):
                continue
            if row["kind"] in CONVERSATION_KINDS or (row["kind"] == "delivery" and event.get("origin") == PROACTIVE):
                found.add(event["source_id"])
    return found


def open_marks(conn, scope, sources, at):
    """{source_id: reason} of the marks still open on `sources`, the latest reason for each."""
    if not sources or not _table(conn, MARK_TABLE):
        return {}
    found = {}
    for page in _pages(sources):
        for row in conn.execute(f"SELECT source_id,reason FROM {MARK_TABLE} WHERE scope=? AND state='open' "
                                "AND julianday(expires_at)>julianday(?) AND source_id IN (" + ",".join("?" * len(page)) + ") "
                                "ORDER BY created_at,mark_id", (scope, at, *page)):
            found[row["source_id"]] = row["reason"]
    return found


def _carried(conn, items, assessment, graph):
    """The sources whose content a committed note or graph node took into memory."""
    from .memory_items import cited_sources
    if items.enabled:
        return set().union(*items.carriers.values()) if items.carriers else set()
    # Without item isolation the section commits whole or not at all: every proposed carrier stands.
    named = [i for note in assessment.notes for i in note.evidence_ids]
    if graph:
        named += [i for node in assessment.graph.nodes for i in node.evidence_ids]
    return cited_sources(conn, named)


def routed_sources(conn, results):
    """The sources a committed event route took: its evidence and the members it names."""
    from .memory_items import cited_sources
    found = set()
    for result in results or ():
        found.update(ref["source_id"] for ref in result.get("evidence") or () if isinstance(ref, dict) and ref.get("source_id"))
        found |= cited_sources(conn, [i for i in result.get("member_ids") or () if isinstance(i, str)])
    return found


def settle(conn, mind, *, assessment, items, routes, processed, event_id, job):
    """What this commit gives a disposition, inside the commit. Writes the skip receipts, consumes the
    marks it answers and, once, queues the follow-up for what it leaves without one."""
    scope = mind.scope.key()
    disposition, marking = _on(conn, scope, DISPOSITION), _on(conn, scope, MARKS)
    stimulus = (job or {}).get("stimulus")
    if not (disposition or marking) or stimulus in NOT_JUDGED:
        return NOTHING
    from .autonomy_schema import settings
    from .memory_items import cited_sources
    ensure(conn)
    at, graph = mind.clock(), settings(conn, scope).get("graph") is True
    routed = routed_sources(conn, routes)
    roots = {ref["source_id"] for ref in processed}
    required = conversation(conn, mind, roots) if disposition else set()
    marked = set(open_marks(conn, scope, roots, at)) if marking else set()
    required |= marked
    skipped = {}
    for entry in assessment.skipped:
        for sid in cited_sources(conn, [entry.evidence_id]) & roots:
            skipped.setdefault(sid, entry.reason)
    carried = _carried(conn, items, assessment, graph) & roots
    taken = carried | (routed & roots) | set(skipped)
    missing = frozenset(required - taken)
    for sid, reason in sorted(skipped.items()):
        conn.execute(f"INSERT OR IGNORE INTO {SKIPS} VALUES(?,?,?,?,?)", (scope, sid, event_id, reason, at))
    answered = sorted(marked & taken)
    for page in _pages(answered):
        conn.execute(f"UPDATE {MARK_TABLE} SET state='consumed',event_id=?,updated_at=? WHERE scope=? AND state='open' "
                     "AND source_id IN (" + ",".join("?" * len(page)) + ")", (event_id, at, scope, *page))
    follow_up = None
    if missing and job and job.get("id") and stimulus not in NO_FOLLOW_UP and not job.get(COVERAGE_OF):
        from .appraisal import queue_row
        follow_up = FOLLOW_UP_PREFIX + digest([job["id"], "coverage-v1"])[:32]
        conn.execute("INSERT OR IGNORE INTO mind_appraisals(id,scope,state,available,data) VALUES(?,?,?,?,?)",
                     (follow_up, scope, "pending", time.time(), queue_row(conn, scope, {
                         "evidence_ids": sorted(missing), "agent_version": job.get("agent_version"), "origin": "reflection",
                         "stimulus": "memory-enrichment", "parent_id": job["id"], COVERAGE_OF: job["id"]})))
    conn.execute("INSERT INTO metrics(name,value,created_at,data) VALUES(?,?,?,?)", (METRIC, len(missing), at, dumps({
        "event_id": event_id, "required": len(required), "marked": len(marked), "carried": len(carried & required),
        "routed": len(routed & required), "skipped": len(set(skipped) & required), "missing": len(missing),
        "follow_up": bool(follow_up), "coverage_pass": bool((job or {}).get(COVERAGE_OF))})))
    if follow_up:
        return Formed(missing, missing, frozenset(), follow_up)
    return Formed(missing, frozenset(), missing, None)


def queue_ledger(conn, scope, formed, event_id, at):
    """The sources a follow-up asks again, in the ledger as queued for it (memory_items), after the
    commit's own settle so that settle does not take them out as organised."""
    for sid in sorted(formed.queued):
        conn.execute("INSERT INTO mind_memory_unorganized VALUES(?,?,?,?,?,?) ON CONFLICT(scope,source_id) DO UPDATE SET "
                     "state=excluded.state,updated_at=excluded.updated_at,data=excluded.data",
                     (scope, sid, "queued", 0, at, dumps({"event_id": event_id, "job_id": formed.follow_up, "reason": "coverage"})))


# --- Item 4: the main session's marks -------------------------------------------------------------------

def _expire(conn, scope, at):
    conn.execute(f"UPDATE {MARK_TABLE} SET state='expired',updated_at=? WHERE scope=? AND state='open' "
                 "AND julianday(expires_at)<=julianday(?)", (at, scope, at))
    conn.execute(f"DELETE FROM {MARK_TABLE} WHERE scope=? AND state<>'open' AND julianday(updated_at)<julianday(?)",
                 (scope, (timestamp(at) - timedelta(days=MARK_KEPT_DAYS)).isoformat()))


def _resolve(conn, mind, identifier):
    """The sources an id names -- a source, a record (its sources), or a received owner input -- while
    each of them is current; None otherwise."""
    if not identifier.startswith(("src_", "mem_")):
        row = conn.execute("SELECT source_id FROM mind_reply_inputs WHERE scope=? AND id=?",
                           (mind.scope.key(), identifier)).fetchone() if _table(conn, "mind_reply_inputs") else None
        if not row:
            return None
        identifier = row[0]
    try:
        refs = mind._evidence(conn, [identifier])
        current = bool(refs) and mind._fresh(conn, refs)
    except (Missing, Conflict):
        return None
    return [ref["source_id"] for ref in refs] if current else None


def _awaiting(conn, scope, sid):
    """Whether an assessment that will organise this source is still to come."""
    if conn.execute("SELECT 1 FROM mind_appraisals a, json_each(a.data,'$.evidence_ids') e WHERE a.scope=? "
                    "AND a.state IN ('pending','running','batched') AND e.value=? LIMIT 1", (scope, sid)).fetchone():
        return True
    return bool(_table(conn, "mind_memory_unorganized") and conn.execute(
        "SELECT 1 FROM mind_memory_unorganized WHERE scope=? AND source_id=? AND state IN ('pending','queued')",
        (scope, sid)).fetchone())


def mark(mind, evidence_ids, reason):
    """Record a mark on the sources `evidence_ids` name. The host writes no memory here, and asks no
    model: the assessment that organises them is held to the mark."""
    if (type(evidence_ids) is not list or not 1 <= len(evidence_ids) <= MARK_IDS_MAX
            or any(type(i) is not str or not i.strip() or len(i) > 200 for i in evidence_ids)):
        raise ValueError(f"A mark names one to {MARK_IDS_MAX} pieces of evidence")
    if type(reason) is not str or not reason.strip() or len(reason) > MARK_REASON_MAX:
        raise ValueError(f"A mark needs a reason of at most {MARK_REASON_MAX} characters")
    scope, at = mind.scope.key(), mind.clock()
    window = {"limit": MARK_QUOTA, "window_hours": MARK_WINDOW_HOURS}
    with mind.engine.db.connect(write=True) as conn:
        if not _on(conn, scope, MARKS):
            return {"state": "disabled"}
        ensure(conn)
        _expire(conn, scope, at)
        named, refused = {}, []
        for given in dict.fromkeys(evidence_ids):
            sources = _resolve(conn, mind, given)
            if sources is None:
                refused.append({"id": given, "code": "evidence-unavailable"})
            for sid in sources or ():
                named.setdefault(sid, given)
        already = set(open_marks(conn, scope, set(named), at))
        wanted = []
        for sid, given in named.items():
            if sid in already:
                continue
            if not _awaiting(conn, scope, sid):
                refused.append({"id": given, "source_id": sid, "code": "no-pending-assessment"})
                continue
            wanted.append(sid)
        since = (timestamp(at) - timedelta(hours=MARK_WINDOW_HOURS)).isoformat()
        used = conn.execute(f"SELECT COUNT(*) FROM {MARK_TABLE} WHERE scope=? AND julianday(created_at)>julianday(?)",
                            (scope, since)).fetchone()[0]
        room = max(0, MARK_QUOTA - used)
        taken, over = wanted[:room], wanted[room:]
        refused += [{"id": named[sid], "source_id": sid, "code": "quota"} for sid in over]
        mark_id, expires = None, (timestamp(at) + timedelta(hours=MARK_TTL_HOURS)).isoformat()
        if taken:
            mark_id = "mark_" + digest([scope, sorted(taken), reason, at])[:32]
            conn.executemany(f"INSERT OR IGNORE INTO {MARK_TABLE} VALUES(?,?,?,?,?,?,?,?,?)",
                             [(scope, mark_id, sid, "open", reason.strip(), at, expires, at, None) for sid in taken])
    return {"state": "recorded" if taken else "refused" if refused else "already-marked",
            **({"mark_id": mark_id, "expires_at": expires} if taken else {}),
            "marked": taken, "already_marked": sorted(already), "refused": refused,
            "quota": {**window, "used": used + len(taken)}}


# --- Erasure ---------------------------------------------------------------------------------------------

def erase(conn, ids, *, write=True):
    """What an erase takes from these tables: a skip receipt naming an erased source, and every row of a
    mark one of whose sources is erased -- its reason was written about all of them. Idempotent."""
    changed = 0
    if _table(conn, SKIPS):
        for page in _pages(ids, 200):
            where = "source_id IN (" + ",".join("?" * len(page)) + ")"
            changed += conn.execute(f"SELECT COUNT(*) FROM {SKIPS} WHERE {where}", page).fetchone()[0]
            if write:
                conn.execute(f"DELETE FROM {SKIPS} WHERE {where}", page)
    if _table(conn, MARK_TABLE):
        doomed = set()
        for page in _pages(ids, 200):
            doomed.update((row[0], row[1]) for row in conn.execute(
                f"SELECT DISTINCT scope,mark_id FROM {MARK_TABLE} WHERE source_id IN (" + ",".join("?" * len(page)) + ")", page))
        for scope, mark_id in sorted(doomed):
            changed += conn.execute(f"SELECT COUNT(*) FROM {MARK_TABLE} WHERE scope=? AND mark_id=?", (scope, mark_id)).fetchone()[0]
            if write:
                conn.execute(f"DELETE FROM {MARK_TABLE} WHERE scope=? AND mark_id=?", (scope, mark_id))
    return changed

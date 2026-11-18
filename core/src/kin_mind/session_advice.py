"""Structured session judgments, evaluated with the existing appraisal call.

The host owns native receipts, execution locks and final decisions. A judgment
does not claim that compaction, delivery or a handover has happened.

Where a judgment lives: the host's conversation registry (`sessionAdvice`), which
its session manager alone writes. It never belongs in the versioned mind state,
whose every revision is a full snapshot. The appraisal that produced it hands it
over through `submit`, one small row replaced by the next, in the same
transaction as the rest of its commit; the host's minute snapshot carries it
from there.
"""
import json
import sqlite3
from typing import Literal

from pydantic import Field

from eventmem.core.db import dumps
from eventmem.core.models import Model

CARRIER = """CREATE TABLE IF NOT EXISTS mind_session_advice(
 scope TEXT PRIMARY KEY,event_id TEXT NOT NULL,snapshot TEXT NOT NULL,data TEXT NOT NULL,at TEXT NOT NULL)"""


class SessionFinding(Model):
    sourceId: str
    kind: Literal["reference-error", "task-omission", "repeat-share", "context-degradation"]
    quote: str = Field(min_length=2, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)


class SessionAdvice(Model):
    action: Literal["keep", "recall", "compact", "prepare", "rotate", "defer"]
    reason: str = Field(min_length=1, max_length=1000)
    evidenceIds: list[str] = Field(default_factory=list, max_length=12)
    compactionId: str | None = None
    recheckCondition: str = Field(default="new-observation", max_length=500)
    findings: list[SessionFinding] = Field(default_factory=list, max_length=4)


SESSION_ADVICE_PROMPT = """
带着 session_context 时顺便交一份 session_advice，看看对话窗口怎么处理合适：选择 keep / recall / compact / prepare / rotate / defer。事实不够用就 recall，快装不下就 compact；如果要 prepare 或 rotate，带上 lastCompaction.id 和那之后退化的 evidenceIds。findings 用来记用户的具体反馈，写上 sourceId、逐字原文 quote 和说明；reason 写为什么这么判断，recheckCondition 写什么时候再看一遍。
"""


class AdviceRejected(ValueError):
    """The host refuses this advice. Code and message are static: neither quotes the advice or its sources."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


ADVICE_REPAIR_PROMPT = """修正这条没通过校验的 session_advice（problem 是宿主给出的失败原因，不当作用户新消息）：
只能引用 allowed_evidence 里的编号；选 prepare 或 rotate 时需要带上 last_compaction.id，外加至少一条晚于 completedAt、状态正常的新观测。findings 的 sourceId 必须是用户原话来源，quote 得跟原文一字不差，对不上就删掉那条。根据不够就退回 keep、recall、compact 或 defer，别现编观测编号，直接调工具提交结果。"""


def with_dialogue(context, dialogue):
    """Use the same source set for the displayed review and its submitted findings."""
    if not context:
        return context
    recent = {row["id"]: row for row in context.get("recent", [])}
    for row in dialogue:
        if row.get("id") and row.get("role") == "user":
            recent.setdefault(row["id"], {k: row[k] for k in ("id", "role", "text", "at", "source_id") if k in row})
    return {**context, "recent": list(recent.values())}


def advice_record(proposal, context, receipt, event_id, request_id=None):
    """Validates before anything is built: a refusal leaves no partial record behind. The
    host's review attempt, when there was one, is named in the record it answers."""
    if proposal is None or not context:
        return None
    known = {entry["id"] for entry in context.get("evidence", [])}
    if set(proposal.evidenceIds) - known:
        raise AdviceRejected("unknown-observation", "Session advice cites unknown observations")
    recent = {entry["id"]: entry for entry in context.get("recent", [])}
    for finding in proposal.findings:
        source = recent.get(finding.sourceId)
        if not source or source.get("role") != "user" or finding.quote not in source.get("text", ""):
            raise AdviceRejected("finding-source-mismatch", "Session finding needs an exact owner source")
    if proposal.action in {"prepare", "rotate"}:
        compact = context.get("lastCompaction") or {}
        if not compact.get("id") or proposal.compactionId != compact["id"]:
            raise AdviceRejected("compaction-not-cited", "Session advice must prefer completed compaction")
        evidence = [entry for entry in context.get("evidence", []) if entry["id"] in proposal.evidenceIds]
        if not any(entry.get("at", 0) > compact["completedAt"] and not entry.get("needsReview") and not entry.get("resolved") for entry in evidence):
            raise AdviceRejected("post-compaction-evidence-missing", "Session advice needs post-compaction evidence")
    return {"decision": proposal.model_dump(), "snapshotId": context["id"], "generation": context["binding"]["generation"],
            "receipt": receipt, "eventId": event_id, **({"requestId": request_id} if request_id else {})}


def repair_input(proposal, context, problem):
    """What one bounded repair may see: the static refusal, what may be cited, and the advice itself.

    Observation ids, times and flags are host bookkeeping; no observed text is repeated here.
    """
    compact = context.get("lastCompaction") or {}
    return {"problem": {"code": problem.code, "message": str(problem)},
            "allowed_evidence": [{k: entry[k] for k in ("id", "at", "needsReview", "resolved") if k in entry} for entry in context.get("evidence", [])],
            "recent": [{k: entry[k] for k in ("id", "role", "text") if k in entry} for entry in context.get("recent", []) if entry.get("role") == "user"],
            "last_compaction": {k: compact[k] for k in ("id", "completedAt") if k in compact} or None,
            "advice": proposal.model_dump()}


def submit(conn, scope, record, at):
    """Hand one validated judgment to the host, inside the caller's transaction.

    One statement at a time: a script would commit the transaction it runs in."""
    if not record or not record.get("eventId") or not record.get("snapshotId"):
        raise ValueError("A session judgment needs its event and observation")
    conn.execute(CARRIER)
    conn.execute("INSERT INTO mind_session_advice VALUES(?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                 "event_id=excluded.event_id,snapshot=excluded.snapshot,data=excluded.data,at=excluded.at",
                 (scope, record["eventId"], record["snapshotId"], dumps(record), at))
    return record


def latest(conn, scope, state=None):
    """The newest judgment waiting for the host, or none.

    Releases before the registry kept it in the mind state, and a store written by
    one of them still answers from there until a newer judgment replaces it."""
    try:
        row = conn.execute("SELECT data FROM mind_session_advice WHERE scope=?", (scope,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        row = None
    if row:
        return json.loads(row[0])
    return (state or {}).get("session_advice")

"""Structured session judgments, evaluated with the existing appraisal call.

The host owns native receipts, execution locks and final decisions. A judgment
does not claim that compaction, delivery or a handover has happened.
"""
from typing import Literal

from pydantic import Field

from eventmem.core.models import Model


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
session_context 是宿主会话观测，不是用户消息，也不构成情绪或关系证据。
存在该字段时，在同一次评估中提供 session_advice：keep/recall/compact/prepare/rotate/defer。
优先维持同一原生会话。缺少事实先 recall，窗口压力先 compact；压缩后话题与工作正常就 keep。
只有引用 lastCompaction.id 及它之后仍有效的具体退化 evidenceIds，才能 prepare 或 rotate。
压缩次数、历史总 token、文件大小、时间经过、普通警告或一次网络慢都不足以证明退化。
比较压缩前后对应任务和接话；错误指代、任务遗漏、重复分享或持续窗口压力需要实际来源。
没有足够压缩后观测时保持或等待，不能把预测写成已经发生的性能下降。
reason 写简短判断依据，recheckCondition 写复核条件。模型只建议，宿主核验操作与安全边界。
会话维护本身不产生联系愿望，不重放旧消息，不改变人设、联系偏好、情绪或任务身份。
recent 中用户确实在指出错接话、遗忘任务或重复分享时，可用 findings 保存 sourceId、kind、原文 quote 与简短解释。假设、一般技术讨论和自我描述不作为故障。findings 先登记为带来源的解释，下一次评估引用 evidence 中的正式编号；不把刚提出的解释自行确认成已经验证的退化。
"""


def advice_record(proposal, context, receipt, event_id):
    if proposal is None or not context:
        return None
    known = {entry["id"] for entry in context.get("evidence", [])}
    if set(proposal.evidenceIds) - known:
        raise ValueError("Session advice cites unknown observations")
    recent = {entry["id"]: entry for entry in context.get("recent", [])}
    for finding in proposal.findings:
        source = recent.get(finding.sourceId)
        if not source or source.get("role") != "user" or finding.quote not in source.get("text", ""):
            raise ValueError("Session finding needs an exact owner source")
    if proposal.action in {"prepare", "rotate"}:
        compact = context.get("lastCompaction") or {}
        if not compact.get("id") or proposal.compactionId != compact["id"]:
            raise ValueError("Session advice must prefer completed compaction")
        evidence = [entry for entry in context.get("evidence", []) if entry["id"] in proposal.evidenceIds]
        if not any(entry.get("at", 0) > compact["completedAt"] and not entry.get("needsReview") and not entry.get("resolved") for entry in evidence):
            raise ValueError("Session advice needs post-compaction evidence")
    return {"decision": proposal.model_dump(), "snapshotId": context["id"], "generation": context["binding"]["generation"],
            "receipt": receipt, "eventId": event_id}

"""Result-specific communication decisions, committed with the existing appraisal."""

import json
from typing import Literal

from pydantic import Field, model_validator

from eventmem.core.db import Conflict, Missing
from eventmem.core.models import Model

# How many decisions the state view shows, newest first. The decision archive keeps these in the
# document, so the view and the manifest of what an appraisal was shown do not move with it.
VIEW = 12


class SharingDecision(Model):
    exploration_id: str | None = Field(default=None, min_length=1, max_length=100)
    activity_ref: dict | None = None
    decision: Literal["share", "defer", "keep", "private", "abandon"]
    reason: str = ""
    reconsider_when: str | None = None

    @model_validator(mode="after")
    def condition(self):
        if not self.exploration_id and not self.activity_ref:
            raise ValueError("Sharing decisions need an original activity reference")
        if self.activity_ref and self.exploration_id and (self.activity_ref.get("kind") != "exploration" or self.activity_ref.get("id") != self.exploration_id):
            raise ValueError("Exploration and activity references must name the same activity")
        if self.activity_ref and self.activity_ref.get("kind") == "exploration" and not self.exploration_id:
            self.exploration_id = self.activity_ref.get("id")
        return self


def apply_decisions(mind, conn, state, proposals, event, receipt, stimulus):
    roots = mind._evidence(conn, event.evidence_ids)
    for proposal in proposals:
        from .activities import canonical_activity_ref, validate_activity_ref
        activity = (validate_activity_ref(mind, proposal.activity_ref, conn=conn) if proposal.activity_ref
                    else canonical_activity_ref(mind, "exploration", proposal.exploration_id, conn=conn))
        exploration_id = activity["id"] if activity["kind"] == "exploration" else None
        key = exploration_id or "dream:" + activity["id"]
        decisions = state.setdefault("exploration_decisions" if exploration_id else "activity_decisions", {})
        row = conn.execute("SELECT data FROM mind_explorations WHERE id=? AND scope=?",
                           (exploration_id, mind.scope.key())).fetchone() if exploration_id else None
        previous = decisions.get(key)
        moved = False
        if previous is None and exploration_id:
            # A decision that moved to the archive is still the decision this result has: judged
            # against it exactly as in the document, and brought back only to be written again.
            from .exploration_decision_archive import archived
            previous = archived(conn, mind.scope.key(), exploration_id)
            moved = previous is not None
        # A new appraisal records a present subjective choice. The saved activity
        # relationship supplies its content without judging psychological consistency.
        if previous and previous.get("event_id") == event.command_id and previous.get("decision") == proposal.decision:
            continue
        identity = lambda ref: tuple((ref or {}).get(k) for k in ("kind", "id", "source_id"))
        if (previous and (identity(previous.get("activity_ref")) == identity(activity) or not previous.get("activity_ref")
                          and any(r.get("source_id") == activity["source_id"] for r in previous.get("evidence", [])))
                and all(previous.get(key, "" if key == "reason" else None) == getattr(proposal, key)
                        for key in ("decision", "reason", "reconsider_when"))):
            # Re-reading a choice does not invalidate its already pending wish.
            continue
        provenance = roots
        if not previous and row:
            result = json.loads(row["data"])
            # All observed resources remain correction dependencies even when
            # only a small evidence window was sent to the reviewer.
            provenance = mind._evidence(conn, [*event.evidence_ids, *result.get("observation_ids", []),
                                              *([result["source_id"]] if result.get("source_id") else [])])
        # What the decision was written from, beside every copy of its words (CR5-MM-01).
        written_from = sorted({ref[key] for ref in provenance for key in ("source_id", "record_id")})
        if moved:
            from .exploration_decision_archive import revive
            revive(mind, conn, state, exploration_id)
        decisions[key] = {
            **proposal.model_dump(), "exploration_id": exploration_id, "activity_ref": activity, "source_ids": [activity["source_id"]],
            "revision": (previous or {}).get("revision", 0) + 1,
            "updated_at": mind.clock(), "event_id": event.command_id,
            "agent_version": event.agent_version, "evidence": provenance, "runtime": receipt,
        }
        if proposal.decision in {"keep", "private", "abandon", "defer"}:
            for desire in state["desires"].values():
                linked = desire.get("activity_ref") or {"kind": "exploration", "id": desire.get("exploration_id")}
                if (linked.get("kind"), linked.get("id")) != (activity["kind"], activity["id"]) or desire["status"] in {"completed", "abandoned"}:
                    continue
                desire.update(status="waiting" if proposal.decision == "defer" else "abandoned",
                              revision=desire["revision"] + 1, updated_at=mind.clock(), reason=proposal.reason,
                              reason_evidence_ids=written_from)
                if proposal.decision == "defer":
                    desire["contact_wait"] = {"condition": "review", "reason": proposal.reconsider_when or "",
                                              "since": mind.clock(), "reason_evidence_ids": written_from}


def newest(state):
    """The decisions in the order the state view shows them, newest first."""
    return sorted(state.get("exploration_decisions", {}).values(),
                  key=lambda d: d.get("updated_at") or "", reverse=True)


def decision_view(mind, conn, state):
    return [{**{k: v for k, v in entry.items() if k not in {"evidence", "runtime"}},
             "needs_review": not _decision_fresh(mind, conn, entry),
             "evidence_ids": [r["record_id"] for r in entry["evidence"]]}
            for entry in newest(state)[:VIEW]]


def activity_decision_view(mind, conn, state):
    entries = sorted(state.get("activity_decisions", {}).values(), key=lambda d: d.get("updated_at") or "", reverse=True)
    return [{**{k: v for k, v in d.items() if k not in {"evidence", "runtime"}},
             "needs_review": not _decision_fresh(mind, conn, d),
             "evidence_ids": [r["record_id"] for r in d.get("evidence", [])]} for d in entries[:VIEW]]


def _decision_fresh(mind, conn, decision):
    for ref in decision.get("evidence", []):
        if not conn.execute("SELECT 1 FROM sources WHERE id=? AND deleted=0", (ref["source_id"],)).fetchone():
            return False
    if decision.get("activity_ref"):
        from .activities import validate_activity_ref
        try:
            validate_activity_ref(mind, decision["activity_ref"], conn=conn)
        except (Missing, Conflict):
            return False
    return True


def require_activity_share(mind, conn, state, activity_ref):
    from .activities import validate_activity_ref
    activity = validate_activity_ref(mind, activity_ref, conn=conn)
    if activity["kind"] == "exploration":
        return require_share(mind, conn, state, activity["id"])
    decision = state.get("activity_decisions", {}).get("dream:" + activity["id"])
    if not decision or decision["decision"] != "share" or not _decision_fresh(mind, conn, decision):
        raise Conflict("This activity has no current decision to communicate")
    validate_activity_ref(mind, decision["activity_ref"], conn=conn)
    return decision


def require_share(mind, conn, state, exploration_id):
    decision = state.get("exploration_decisions", {}).get(exploration_id)
    if decision is None:
        # One that moved still supplies the activity's current sharing choice.
        from .exploration_decision_archive import archived
        decision = archived(conn, mind.scope.key(), exploration_id)
    if not decision or decision["decision"] != "share" or not _decision_fresh(mind, conn, decision):
        raise Conflict("This result has no current decision to communicate")
    return decision

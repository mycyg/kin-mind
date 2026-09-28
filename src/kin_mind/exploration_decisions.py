"""Result-specific communication decisions, committed with the existing appraisal."""

import json
from typing import Literal

from pydantic import Field, model_validator

from eventmem.core.db import Conflict
from eventmem.core.models import Model

# How many decisions the state view shows, newest first. The decision archive keeps these in the
# document, so the view and the manifest of what an appraisal was shown do not move with it.
VIEW = 12


class SharingDecision(Model):
    exploration_id: str = Field(min_length=1, max_length=100)
    decision: Literal["share", "defer", "keep"]
    reason: str = Field(min_length=1)
    reconsider_when: str | None = None

    @model_validator(mode="after")
    def condition(self):
        if self.decision == "defer" and not self.reconsider_when:
            raise ValueError("Deferred sharing needs a meaningful reconsideration condition")
        return self


def apply_decisions(mind, conn, state, proposals, event, receipt, stimulus):
    decisions = state.setdefault("exploration_decisions", {})
    roots = mind._evidence(conn, event.evidence_ids)
    for proposal in proposals:
        row = conn.execute("SELECT data FROM mind_explorations WHERE id=? AND scope=?",
                           (proposal.exploration_id, mind.scope.key())).fetchone()
        if not row:
            raise Conflict("Sharing decision needs an exploration in this scope")
        previous = decisions.get(proposal.exploration_id)
        moved = False
        if previous is None:
            # A decision that moved to the archive is still the decision this result has: judged
            # against it exactly as in the document, and brought back only to be written again.
            from .exploration_decision_archive import archived
            previous = archived(conn, mind.scope.key(), proposal.exploration_id)
            moved = previous is not None
        if previous:
            if stimulus in {"drive-crossing", "delivery", "wish-review"}:
                if proposal.decision != previous["decision"]:
                    raise Conflict("A clock or delivery event cannot reopen a result decision")
                continue
            old = {(r["source_id"], r["hash"]) for r in previous["evidence"]}
            if not any((r["source_id"], r["hash"]) not in old for r in roots):
                if proposal.decision != previous["decision"]:
                    raise Conflict("Reconsideration requires a new sourced thought or observation")
                continue
        provenance = roots
        if not previous:
            result = json.loads(row["data"])
            # All observed resources remain correction dependencies even when
            # only a small evidence window was sent to the reviewer.
            provenance = mind._evidence(conn, [*event.evidence_ids, *result.get("observation_ids", []),
                                              *([result["source_id"]] if result.get("source_id") else [])])
        # What the decision was written from, beside every copy of its words (CR5-MM-01).
        written_from = sorted({ref[key] for ref in provenance for key in ("source_id", "record_id")})
        if moved:
            from .exploration_decision_archive import revive
            revive(mind, conn, state, proposal.exploration_id)
        decisions[proposal.exploration_id] = {
            **proposal.model_dump(), "revision": (previous or {}).get("revision", 0) + 1,
            "updated_at": mind.clock(), "event_id": event.command_id,
            "agent_version": event.agent_version, "evidence": provenance, "runtime": receipt,
        }
        if proposal.decision in {"keep", "defer"}:
            for desire in state["desires"].values():
                if desire.get("exploration_id") != proposal.exploration_id or desire["status"] in {"completed", "abandoned"}:
                    continue
                desire.update(status="abandoned" if proposal.decision == "keep" else "waiting",
                              revision=desire["revision"] + 1, updated_at=mind.clock(), reason=proposal.reason,
                              reason_evidence_ids=written_from)
                if proposal.decision == "defer":
                    desire["contact_wait"] = {"condition": "new_evidence", "reason": proposal.reconsider_when,
                                              "since": mind.clock(), "reason_evidence_ids": written_from}


def newest(state):
    """The decisions in the order the state view shows them, newest first."""
    return sorted(state.get("exploration_decisions", {}).values(),
                  key=lambda d: d.get("updated_at") or "", reverse=True)


def decision_view(mind, conn, state):
    return [{**{k: v for k, v in entry.items() if k not in {"evidence", "runtime"}},
             "needs_review": not mind._fresh(conn, entry["evidence"]),
             "evidence_ids": [r["record_id"] for r in entry["evidence"]]}
            for entry in newest(state)[:VIEW]]


def require_share(mind, conn, state, exploration_id):
    decision = state.get("exploration_decisions", {}).get(exploration_id)
    if decision is None:
        # One that moved still answers: a result already shared refuses a second intent as before.
        from .exploration_decision_archive import archived
        decision = archived(conn, mind.scope.key(), exploration_id)
    if not decision or decision["decision"] != "share" or not mind._fresh(conn, decision["evidence"]):
        raise Conflict("This result has no current decision to communicate")
    return decision

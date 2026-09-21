"""Result-specific communication decisions, committed with the existing appraisal."""

import json
from typing import Literal

from pydantic import Field, model_validator

from eventmem.core.db import Conflict
from eventmem.core.models import Model


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
                              revision=desire["revision"] + 1, updated_at=mind.clock(), reason=proposal.reason)
                if proposal.decision == "defer":
                    desire["contact_wait"] = {"condition": "new_evidence", "reason": proposal.reconsider_when,
                                              "since": mind.clock()}


def decision_view(mind, conn, state):
    return [{**{k: v for k, v in entry.items() if k not in {"evidence", "runtime"}},
             "needs_review": not mind._fresh(conn, entry["evidence"]),
             "evidence_ids": [r["record_id"] for r in entry["evidence"]]}
            for entry in sorted(state.get("exploration_decisions", {}).values(),
                                key=lambda d: d["updated_at"], reverse=True)[:12]]


def require_share(mind, conn, state, exploration_id):
    decision = state.get("exploration_decisions", {}).get(exploration_id)
    if not decision or decision["decision"] != "share" or not mind._fresh(conn, decision["evidence"]):
        raise Conflict("This result has no current decision to communicate")
    return decision

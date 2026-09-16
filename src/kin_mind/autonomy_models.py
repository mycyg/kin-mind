"""Structured proposals shared by appraisal, tools and durable planning."""
from typing import Literal

from pydantic import Field, model_validator

from eventmem.core.models import Model


class RecallNeed(Model):
    query: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=1000)
    mode: Literal["light", "deep"] = "deep"
    identifiers: list[str] = Field(default_factory=list, max_length=8)


class PlanStep(Model):
    id: str = Field(min_length=1, max_length=100)
    actor: Literal["explore", "create", "contact", "owner"]
    goal: str = Field(min_length=1, max_length=3000)
    completion: str = Field(min_length=1, max_length=1600)
    depends_on: list[str] = Field(default_factory=list, max_length=24)
    preconditions: list[str] = Field(default_factory=list, max_length=12)
    not_before: str | None = None
    not_after: str | None = None
    owner_request_id: str | None = None
    input_ids: list[str] = Field(default_factory=list, max_length=16)


class PlanChange(Model):
    action: Literal["create", "update", "pause", "resume", "reschedule", "cancel"]
    id: str | None = None
    key: str | None = Field(default=None, max_length=160)
    expected_revision: int | None = Field(default=None, ge=1)
    goal: str | None = Field(default=None, min_length=1, max_length=3000)
    motivation: str | None = Field(default=None, min_length=1, max_length=1600)
    reason: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str] = Field(min_length=1, max_length=24)
    next_review_at: str | None = None
    steps: list[PlanStep] | None = Field(default=None, max_length=32)
    desire_id: str | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.action == "create" and not (self.key and self.goal and self.motivation and self.steps):
            raise ValueError("Create requires key, goal, motivation and steps")
        if self.action != "create" and not (self.id and self.expected_revision):
            raise ValueError("Changes require a stable ID and expected revision")
        if self.action == "reschedule" and not self.next_review_at:
            raise ValueError("Reschedule requires a review time")
        if self.steps:
            ids = [s.id for s in self.steps]
            if len(ids) != len(set(ids)):
                raise ValueError("Step IDs must be unique")
            edges = {s.id: s.depends_on for s in self.steps}
            def visit(i, path):
                if i in path or i not in edges:
                    raise ValueError("Dependencies must be an acyclic graph within this plan")
                for dep in edges[i]:
                    visit(dep, path | {i})
            for i in ids:
                visit(i, set())
        return self


class ActionDecision(Model):
    plan_id: str = Field(min_length=1, max_length=160)
    step_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    action: Literal["execute", "wait", "abandon", "owner_accepted", "owner_completed", "owner_declined"]
    reason: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str] = Field(min_length=1, max_length=24)
    next_review_at: str | None = None
    conditions_met: list[str] = Field(default_factory=list, max_length=12)
    procedure_ids: list[str] = Field(default_factory=list, max_length=8)
    artifact_hashes: list[str] = Field(default_factory=list, max_length=24)


class ProcedureCandidate(Model):
    key: str = Field(min_length=1, max_length=160)
    id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=300)
    applicable_when: str = Field(min_length=1, max_length=2000)
    steps: list[str] = Field(min_length=1, max_length=24)
    tools: list[str] = Field(default_factory=list, max_length=24)
    environment: dict[str, str] = Field(default_factory=dict, max_length=24)
    success_criteria: str = Field(min_length=1, max_length=2000)
    counterexamples: list[str] = Field(default_factory=list, max_length=16)
    evidence_ids: list[str] = Field(min_length=1, max_length=24)
    result_ids: list[str] = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=1600)

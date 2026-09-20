"""Bounded CLI exploration. Only validated final results enter memory.

Executor protocol. `Explorations.run` calls its runner as

    runner(executable, payload, workdir, *, budget_seconds, canceled, model=None, **options)

- executable: the configured CLI path for the selected backend; a runner never
  resolves or falls back to another one.
- payload: the reviewed input (question, evidence with ids and versions, previous
  explorations). It is data, never instructions.
- workdir: a private directory the executor creates (mode 0700) and owns.
- budget_seconds: the TOTAL ceiling — waiting, tool calls and bounded format repair
  included; any per-request timeout is bounded by the remaining budget.
- canceled: returns True when the run must stop (owner task, lease loss, stop file).
- options: backend extras (`computer`/`web`/`reasoning`/`provider`/`continuation`).

The runner returns a dict matching `ExecutionReport`: a terminal state
(complete / failed / timed-out / preempted), a validated `Findings` dump or None,
usage separated into not_dispatched / reported / unknown, the native execution id,
the exit code, start/finish stamps and the backend identity (executor vs model
provider). An interrupted run also leaves a `checkpoint` a later attempt can
continue from. `normalize_execution_report` fills contract defaults around the
minimal dicts of older runners. The stock executor is `run_codex`; the kimi
executor was removed (owner directive KIN-ITER-20260919-01) — historical receipts
with provider=kimi-cli stay valid as data, and a failure pauses with a recorded
reason instead of falling back to anything.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import Field, field_validator

from eventmem.core.db import Conflict, digest, dumps
from eventmem.core.models import Model, SourceInput

from . import liveness
from .memory import MemoryContinuity
from .state import DesireChange

EXECUTION_STATES = ("complete", "failed", "timed-out", "preempted")
USAGE_STATUSES = ("not_dispatched", "reported", "unknown")


class UsageReport(Model):
    """What the backend said about model usage. A run that never reached the model
    is `not_dispatched`; one that ran but reported nothing is `unknown`. A missing
    count is never a zero."""

    status: Literal["not_dispatched", "reported", "unknown"] = "unknown"
    per_request: list[dict] = Field(default_factory=list)
    total: dict | None = None


class ExecutionReport(Model):
    """The executor contract; see the module docstring. `result` is a validated
    `Findings` dump and only a `complete` state may carry one."""

    state: Literal["complete", "failed", "timed-out", "preempted"]
    result: dict | None = None
    partial: bool = True
    reason: str | None = None
    executor: str = "codex-cli"
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    executor_version: str | None = None
    config_digest: str | None = None
    native_execution_id: str | None = None
    exit_code: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    seconds: float | None = None
    attempt: int = 1
    workdir: str | None = None
    input_sources: list[dict] = Field(default_factory=list)
    usage: UsageReport = Field(default_factory=UsageReport)
    checkpoint: dict | None = None


class CodexUnavailable(RuntimeError):
    """The codex executor cannot start: CLI missing, too old, or a configured
    credential absent. The exploration pauses with the recorded reason; there is
    never a silent fallback to another backend or to a default model."""

    def __init__(self, reason, detail=None, *, executor="codex-cli", provider=None):
        super().__init__(reason if detail is None else reason + ": " + str(detail))
        self.reason, self.executor, self.provider = reason, executor, provider


def normalize_execution_report(raw):
    """Contract defaults around what a runner returned, keeping its extra keys."""
    known = {key: raw[key] for key in ExecutionReport.model_fields if key in raw}
    report = ExecutionReport.model_validate(known)
    return {**raw, **report.model_dump()}


class Citation(Model):
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=500)

    @field_validator("url")
    @classmethod
    def source_url(cls, value):
        if value == "computer://current-context" or re.fullmatch(
                r"computer://app/[A-Za-z0-9_.:-]{1,300}", value):
            return value
        if value.startswith("memory://"):
            # A host-internal evidence locator: the source id of material the host
            # itself supplied to the run (the ledger's historical layer).
            identifier = value[9:]
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", identifier):
                raise ValueError("A memory citation names one supplied evidence source id")
            return value
        if value.startswith("file://"):
            parsed = urlparse(value)
            if parsed.netloc not in {"", "localhost"}:
                raise ValueError(
                    "A local citation must not point to a remote file host"
                )
            value = unquote(parsed.path)
        if not value.startswith(("https://", "http://", "/")):
            raise ValueError("Use an actual URL or an authorized local document path")
        return value


class AssistanceHint(Model):
    action: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    completion: str = Field(min_length=1, max_length=500)


class Findings(Model):
    summary: str = Field(min_length=1, max_length=6000)
    findings: list[str] = Field(max_length=30)
    sources: list[Citation] = Field(max_length=30)
    open_questions: list[str] = Field(max_length=20)
    suggested_share: str | None = Field(default=None, max_length=2000)
    assistance_needed: AssistanceHint | None = None
    # Backward-compatible: claims keyed by their 1-based finding index, mapped to
    # evidence ids from the run's source ledger (read receipts or memory:// ids).
    # Absent on legacy output; the host maps `sources` strictly instead.
    evidence_map: dict[str, list[str]] | None = Field(
        default=None,
        description=(
            "Optional claim-to-evidence map. Each key is the 1-based decimal index "
            "of an item in findings (for example, '1'). Each value is a non-empty "
            "list containing only exact evidence_id or exact locator strings copied "
            "from citable state=observed tool receipts or supplied/historical sources. "
            "Never put prose, shortened ids, version hashes, review_* ids, or action_* "
            "ids in a value. Use null when no finding-level mapping is needed."
        ),
    )


class Explorations:
    def __init__(self, mind):
        self.mind, self.engine = mind, mind.engine
        with self.engine.db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS mind_exploration_active ON mind_explorations(scope) WHERE state='running'"
            )

    def recent(self, limit=3):
        with self.engine.db.connect() as conn:
            return [
                dict(
                    json.loads(r["data"]),
                    id=r["id"],
                    state=r["state"],
                    created_at=r["created_at"],
                )
                for r in conn.execute(
                    "SELECT * FROM mind_explorations WHERE scope=? ORDER BY created_at DESC LIMIT ?",
                    (self.mind.scope.key(), limit),
                )
            ]

    def run(
        self, executable, directory, agent_version, *, canceled=lambda: False,
        model=None, runner=None, brief=None, budget_seconds=1200, desire_id=None, computer=None, web=None,
    ):
        if runner is None:
            # The stock executor. Unconfigured codex pauses with a recorded reason;
            # there is no other executor to fall back to.
            from .codex_executor import run_codex
            runner = run_codex
        from .actions import ActionEvents
        actions = ActionEvents(self.mind)
        candidate = actions.exploration_candidate()
        if candidate["state"] != "ready":
            return candidate
        if canceled():
            return {"state": "waiting", "reason": "owner-task"}
        desire = candidate["desire"]
        target = desire.get("exploration_target", "knowledge")
        if target == "computer" and not (computer or {}).get("enabled"):
            return {"state": "waiting", "reason": "computer-exploration-disabled"}
        if desire_id and desire_id != desire["id"]:
            return {"state": "waiting", "reason": "exploration-intent-changed"}
        # The selected wish is the reviewed brief; an external wake cannot replace it.
        budget_seconds = min(1200, max(1, int(budget_seconds)))
        at = self.mind.clock()
        eid = "explore_" + digest([self.mind.scope.key(), desire["id"], desire["revision"]])[:32]
        data = {"desire_id": desire["id"], "selected_brief": desire["content"], "exploration_target": target,
                "topic_selected_by": "deepseek-appraisal", "agent_version": agent_version,
                "evidence_ids": [r["record_id"] for r in desire["evidence"]],
                # Who is running this, so that a later start can tell an exploration
                # that is still working from one whose process died with the host.
                # The row is the only place there is: `mind_explorations` gains no
                # column for it, and `data` is already ours to shape.
                "liveness": liveness.self_record(deadline=time.time() + budget_seconds + liveness.EXPLORATION_MARGIN_SECONDS)}
        request = DesireChange(command_id=eid+":start", agent_version=agent_version,
            expected_revision=candidate["revision"], evidence_ids=data["evidence_ids"],
            action="start", desire_id=desire["id"], reason="Host claimed the reviewed exploration intent")

        def claim(conn, current, event_id):
            from .plans import AutonomousPlans
            if not AutonomousPlans(self.mind).linked_ready(conn, current["desires"][desire["id"]]):
                raise Conflict("Exploration plan changed before claim")
            if conn.execute("SELECT 1 FROM mind_explorations WHERE scope=? AND state='running'", (self.mind.scope.key(),)).fetchone():
                raise Conflict("Exploration worker already active")
            result = self.mind._apply_desire(conn, current, request, event_id)
            data["desire_revision"] = result["desire_revision"]
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (eid, self.mind.scope.key(), "running", at, dumps(data)))
            return result
        try:
            # Unlike a replayable command, a worker lease must never return a
            # previous success to a second executor that would run Kimi again.
            with self.engine.db.connect(write=True) as conn:
                current = self.mind._load(conn)
                if current["revision"] != candidate["revision"] or current["desires"][desire["id"]]["status"] != "wanted":
                    raise Conflict("Exploration intent changed")
                event_id = "mind_" + digest([eid, "start"])[:32]
                claim(conn, current, event_id)
                current["revision"] += 1
                current["updated_at"] = self.mind.clock()
                self.mind._save(conn, current)
                self.mind._history(conn, event_id, current, "exploration-start", request.model_dump())
        except Conflict:
            return {"state": "waiting", "reason": "exploration-claim-changed"}
        try:
            options = {}
            ui_enabled = bool((computer or {}).get("enabled")
                              and ((computer or {}).get("ui") or {}).get("enabled"))
            if target == "computer" or ui_enabled:
                previous = [v for item in self.recent(8) for v in item.get("observations", [])][-60:]
                options["computer"] = {**computer, "file_reader_enabled": target == "computer", "previous": [
                    {k: v[k] for k in ("id", "locator", "version", "title", "observed_at")} for v in previous]}
            if web:
                options["web"] = web
            if getattr(runner, "wants_continuation", False):
                # Explicit checkpoint continuation: a new attempt reads the previous
                # one's recorded sources, gaps and partial findings. There is no
                # native session resume to claim.
                for prior in self.recent(8):
                    if prior.get("desire_id") == desire["id"] and isinstance(prior.get("checkpoint"), dict):
                        options["continuation"] = prior["checkpoint"]
                        break
            from .decision_context import execution_brief
            reviewed_brief = execution_brief(self.mind, question=desire["content"], evidence_ids=data["evidence_ids"])
            output = runner(executable, {**reviewed_brief, "topic": desire["topic"],
                "source_ids": data["evidence_ids"]}, Path(directory)/eid,
                budget_seconds=budget_seconds, canceled=canceled, model=model, **options)
            output = normalize_execution_report(output)
            data.update(output)
            state = output["state"]
        except CodexUnavailable as error:
            # Pause with the reason recorded; never a silent fallback to kimi or a default model.
            state = "failed"
            data.update(error="exploration-executor-unavailable", waiting_reason=error.reason,
                        executor=error.executor, provider=error.provider, partial=True, result=None)
        except Exception as error:  # noqa: BLE001 - owned helper boundary; retain a redacted failure receipt
            state = "failed"
            data.update(error=type(error).__name__, partial=True, result=None)
        # A result without a definitive answer still supports reflection and sharing.
        # Only final reports and execution receipts enter memory, never tool traces.
        observation_ids = []
        from .source_ledger import (
            valid_computer_receipt,
            valid_web_receipt,
            web_delivery,
        )
        observations = [observation for observation in data.get("observations", [])
                        if valid_computer_receipt(observation, execution_id=eid,
                                                  attempt=data.get("attempt", 1))]
        data["observations"] = observations
        for observation in observations:
            observed = self.engine.receive(SourceInput(namespace="kin-computer-observation", key=observation["id"],
                scope=self.mind.scope, authority="document", kind="observation", session=eid,
                text=dumps({k:v for k,v in observation.items() if k not in {"observed_at", "first_observed_at", "changed_since_last_observation"}}),
                occurred_at=observation["observed_at"], extract=False,
                metadata={"host_event": "computer-observation", "actor": observation["actor"],
                          "locator": observation["locator"], "resource_version": observation["version"]}))
            observation_ids.append(observed["id"])
        web_observations = [receipt for receipt in data.get("web_observations", [])
                            if valid_web_receipt(receipt, execution_id=eid,
                                                 attempt=data.get("attempt", 1))]
        data["web_observations"] = web_observations
        for receipt in web_observations:
            # Observed pages are this run's evidence; a search_result or a failed
            # read is operational record, never content for memory.
            if receipt.get("state") != "observed":
                continue
            observed = self.engine.receive(SourceInput(namespace="kin-web-observation", key=receipt["evidence_id"],
                scope=self.mind.scope, authority="document", kind="observation", session=eid,
                text=dumps({k: v for k, v in receipt.items() if k in {
                    "locator", "requested_locator", "title", "version", "excerpt", "content_type",
                    "truncated", "receipt_format", "content_sha256", "content_chars",
                    "raw_body_sha256", "raw_body_bytes", "delivered_ranges", "http_status",
                    "semantic_classification",
                }}),
                occurred_at=receipt.get("read_at") or self.mind.clock(), extract=False,
                metadata={"host_event": "web-observation", "executor": "codex-cli",
                          "locator": receipt["locator"], "resource_version": receipt["version"],
                          "stored_content": "excerpt", "delivery": web_delivery(receipt)}))
            observation_ids.append(observed["id"])
        executor = data.get("executor") or "codex-cli"
        source = self.engine.receive(SourceInput(namespace="kin-exploration", key=eid,
            scope=self.mind.scope, authority="model", kind="observation", session=eid,
            text=dumps({"state": state, "result": data.get("result"), "partial": data.get("partial", True)}),
            occurred_at=self.mind.clock(), extract=not MemoryContinuity(self.mind).settings()['semantic'],
            # Executor (the CLI that ran) and provider (the model behind it) are
            # distinct facts; legacy rows come from kimi-cli running kimi.
            metadata={"host_event": "exploration-result", "executor": executor,
                      "provider": data.get("provider"),
                      "model": data.get("model"), "reasoning": data.get("reasoning"),
                      "exploration_id": eid,
                      "exploration_target": target, "observation_ids": observation_ids,
                      "partial": data.get("partial", True), "sources": (data.get("result") or {}).get("sources", [])}))
        data["source_id"] = source["id"]
        data["observation_ids"] = observation_ids
        with self.engine.db.connect(write=True) as conn:
            current = self.mind._load(conn)
            active = current["desires"].get(desire["id"])
            if active and active["revision"] == data["desire_revision"] and active["status"] == "in_progress":
                event_id = "mind_" + digest([eid, "settled"])[:32]
                needs_condition = bool((data.get("result") or {}).get("assistance_needed"))
                action = "complete" if state == "complete" and data.get("result") and not needs_condition else "resume" if state == "preempted" else "wait"
                update = DesireChange(command_id=eid+":settled", agent_version=agent_version,
                    expected_revision=current["revision"], evidence_ids=[source["id"], *data["evidence_ids"]],
                    action=action, desire_id=desire["id"], reason="Exploration awaits a reported condition" if needs_condition else "Exploration execution: "+state)
                self.mind._apply_desire(conn, current, update, event_id)
                from .plans import AutonomousPlans
                AutonomousPlans(self.mind).settle_linked(conn, active, {"id": eid, "kind": "exploration-result",
                    "complete": action == "complete", "source_id": source["id"], "state": state})
                current["revision"] += 1
                current["updated_at"] = self.mind.clock()
                self.mind._save(conn, current)
                self.mind._history(conn, event_id, current, "exploration-result", data)
            conn.execute("UPDATE mind_explorations SET state=?,data=? WHERE id=?", (state, dumps(data), eid))
            memory = MemoryContinuity.__new__(MemoryContinuity)
            memory.mind, memory.engine, memory.scope = self.mind, self.engine, self.mind.scope
            if memory.settings(conn).get("sharing") or memory.settings(conn).get("graph"):
                from .sharing import ShareLedger
                # Schemas are initialized before the write transaction by the
                # earlier MemoryContinuity lookup in the source-receive path.
                ledger = ShareLedger.__new__(ShareLedger)
                from .graph import EventGraph
                graph = EventGraph.__new__(EventGraph)
                graph.mind, graph.engine, graph.scope = self.mind, self.engine, self.mind.scope
                ledger.mind, ledger.engine, ledger.scope, ledger.graph = self.mind, self.engine, self.mind.scope, graph
                data["content_units"] = [{"id": n["id"], "version": n["content_version"]} for n in ledger.exploration(conn, eid, data)]
                conn.execute("UPDATE mind_explorations SET data=? WHERE id=?", (dumps(data), eid))
            actions.emit(conn, "exploration-result", eid, {"exploration_id": eid, "state": state,
                # The action dispatcher adds one internal event source.
                "evidence_ids": list(dict.fromkeys([source["id"], *observation_ids, *data["evidence_ids"]]))[:49], "agent_version": agent_version})
        return dict(data, id=eid, state=state)

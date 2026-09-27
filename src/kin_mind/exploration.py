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

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.engine import DERIVED_CONFLICTS, root_id
from eventmem.core.models import Model, SourceInput

from . import liveness
from .memory import MemoryContinuity
from .state import DesireChange

EXECUTION_STATES = ("complete", "failed", "timed-out", "preempted")
# What a run whose words were taken still shows of itself among the previous explorations: its
# identity, state and time, and the wish it was for. Its report, while it stands, says the rest.
KEPT_WHEN_ERASED = ("id", "state", "created_at", "desire_id")
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
    action: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    completion: str = Field(min_length=1)


class Findings(Model):
    summary: str = Field(min_length=1)
    findings: list[str]
    sources: list[Citation]
    open_questions: list[str]
    suggested_share: str | None = None
    assistance_needed: AssistanceHint | None = None
    # Backward-compatible: claims keyed by their 1-based finding index, mapped to
    # evidence ids from the run's source ledger (read receipts or memory:// ids).
    # Absent on legacy output; the host maps `sources` strictly instead.
    evidence_map: dict[str, list[str]] | None = Field(
        default=None,
        description=(
            "可选的结论与证据映射。键是 findings 中从 1 开始的序号，如 '1'；值是非空列表，"
            "只填可引用的 state=observed 工具回执或已给来源、历史来源中的完整 evidence_id 或 locator。"
            "不填说明文字、缩短编号、版本哈希、review_* 或 action_* 编号；不需要逐项映射时使用 null。"
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
        """The latest runs, newest first, as the previous explorations are shown. A run whose words
        were taken -- by what it names, or because it names nothing of what it was shown, as a run
        from before this release (`erasure.UNNAMED`) -- shows its identity and state, and the
        summary of its report while the report stands: a report goes with what it was written
        from, so one still there may be shown. Without it, the run names no report either, so no
        brief names a deleted one as shown (CL6D-MM-04)."""
        with self.engine.db.connect() as conn:
            return [
                self._shown(conn, dict(json.loads(r["data"]), id=r["id"], state=r["state"], created_at=r["created_at"]))
                for r in conn.execute(
                    "SELECT * FROM mind_explorations WHERE scope=? ORDER BY created_at DESC LIMIT ?",
                    (self.mind.scope.key(), limit),
                ).fetchall()
            ]

    def _shown(self, conn, run):
        from .erasure import ERASED, UNNAMED_MARK

        if not run.get(UNNAMED_MARK) and ERASED not in dumps(run):
            return run
        shown = {key: run[key] for key in KEPT_WHEN_ERASED if key in run}
        sid = run.get("source_id")
        found = conn.execute("SELECT blob FROM sources WHERE id=? AND deleted=0", (sid,)).fetchone() if isinstance(sid, str) else None
        if not found:
            return shown
        shown["source_id"] = sid
        root = conn.execute("SELECT data FROM records WHERE id=? AND deleted=0", (root_id(sid),)).fetchone()
        try:
            text = json.loads(root[0]).get("content") if root else (self.engine.db.blobs / found[0]).read_text()
            summary = (json.loads(text).get("result") or {}).get("summary")
        except (OSError, ValueError, AttributeError, TypeError):
            summary = None
        if isinstance(summary, str) and summary.strip() and summary != ERASED:
            shown["result"] = {"summary": summary}
        return shown

    def reclaim_dead(self):
        """Take back a running exploration whose worker is provably gone (the host timed it out or
        restarted) at the next look, not only at the next start-up: the pid is dead or became
        another process, or the deadline it recorded has passed. A row that cannot be shown dead
        stays running (K2-09)."""
        scope = self.mind.scope.key()
        with self.engine.db.connect() as conn:
            if not liveness.checks_enabled(conn, scope):
                return []
            dead = [r["id"] for r in conn.execute("SELECT id,data FROM mind_explorations WHERE scope=? AND state='running'", (scope,))
                    if not liveness.record_alive(json.loads(r["data"]).get("liveness"))]
        if not dead:
            return []
        with self.engine.db.connect(write=True) as conn:
            current, reclaimed = self.mind._load(conn), []
            for row in conn.execute("SELECT id,data FROM mind_explorations WHERE scope=? AND state='running'", (scope,)).fetchall():
                data = json.loads(row["data"])
                if row["id"] not in dead or liveness.record_alive(data.get("liveness")):
                    continue
                conn.execute("UPDATE mind_explorations SET state='interrupted' WHERE id=? AND scope=? AND state='running'", (row["id"], scope))
                desire = current["desires"].get(data.get("desire_id"))
                if desire and desire["status"] == "in_progress":
                    desire.update(status="wanted", revision=desire["revision"] + 1, updated_at=self.mind.clock())
                reclaimed.append(row["id"])
            if reclaimed:
                current["revision"] += 1
                current["updated_at"] = self.mind.clock()
                self.mind._save(conn, current)
                self.mind._history(conn, "mind_" + digest([scope, "exploration-reclaim", current["revision"]])[:32], current,
                                   "exploration-recovery", {"interrupted": len(reclaimed)})
        return reclaimed

    def sweep_workdirs(self, directory):
        """Every settled run's directory under `directory` that still has its words loses them
        (workdirs.sweep): a run the host stopped, one settled before this release, one whose own pass
        failed. Each run asks before it starts, and the host at every start (`recover`): with no wish
        to explore, no run starts for days, and the pages the last ones fetched stayed whole all that
        time (OPS-03). Returns the run ids swept."""
        from . import workdirs
        with self.engine.db.connect() as conn:
            settled = {row["id"]: row["state"] for row in conn.execute(
                "SELECT id,state FROM mind_explorations WHERE scope=? AND state!='running'", (self.mind.scope.key(),))}
        try:
            return workdirs.sweep(directory, settled)
        except OSError:
            return []

    def _stop_when(self, canceled, desire_id, *, every=15):
        """The run stops for the host's own stop (shutdown) or for Kin's decision, never for a
        new owner message by itself: the wish it serves is no longer in progress, its plan step
        was decided again, or the owner paused exploration. Read at most every `every` seconds."""
        checked = [0.0, False]

        def withdrawn():
            with self.engine.db.connect() as conn:
                desire = self.mind._load(conn)["desires"].get(desire_id)
                if not desire or desire["status"] != "in_progress":
                    return True
                if desire.get("plan_id"):
                    from .plans import AutonomousPlans
                    try:
                        plan = AutonomousPlans(self.mind).get(conn, desire["plan_id"])
                    except Missing:
                        return True
                    step = next((s for s in plan["steps"] if s["id"] == desire.get("plan_step_id")), None)
                    if plan["status"] != "active" or not step or (step.get("decision") or {}).get("id") != desire.get("plan_decision_id"):
                        return True
            from .habits import ConversationHabits
            return bool(ConversationHabits(self.mind).read()["preferences"].get("exploration_paused"))

        def stop():
            if canceled():
                return True
            if time.monotonic() - checked[0] >= every:
                checked[0] = time.monotonic()
                try:
                    checked[1] = withdrawn()
                except Exception:  # noqa: BLE001 - an unreadable store is not a decision to stop
                    checked[1] = False
            return checked[1]
        return stop

    def run(
        self, executable, directory, agent_version, *, canceled=lambda: False,
        model=None, runner=None, brief=None, budget_seconds=1200, desire_id=None, computer=None, web=None,
    ):
        if runner is None:
            # The stock executor. Unconfigured codex pauses with a recorded reason;
            # there is no other executor to fall back to.
            from .codex_executor import run_codex
            runner = run_codex
        from . import workdirs
        from .actions import ActionEvents
        from .erasure import tombstone_mark
        with self.engine.db.connect() as conn:
            # How far the deletes went before this run read anything it hands its executor: what was
            # deleted before, the run never had the words of (CL6E-MM-02).
            mark = tombstone_mark(conn)
        # A run the host stopped, or one settled before this release, left its working directory
        # with the words of its brief, its answers and its checkpoints: they go now (CL6-MM-07).
        self.sweep_workdirs(directory)
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
                # Where its executor writes: a delete of anything the run names takes the words of
                # that directory too, once the run is settled (workdirs.erased_runs, OPS-03).
                "workdir": str((Path(directory) / eid).absolute()),
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
        # Every source the run is shown besides the wish's evidence: its report is kept only while
        # all of it still stands (CR5-MM-02).
        shown = []
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
            from .decision_context import execution_brief, sources_named
            shown.extend(sources_named(options.get("continuation")))
            reviewed_brief = execution_brief(self.mind, question=desire["content"], evidence_ids=data["evidence_ids"], shown=shown, since=mark)
            output = runner(executable, {**reviewed_brief, "topic": desire["topic"],
                "source_ids": data["evidence_ids"]}, Path(directory)/eid,
                budget_seconds=budget_seconds, canceled=self._stop_when(canceled, desire["id"]), model=model, **options)
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
        # A runner that reported no directory of its own used the one it was handed.
        data["workdir"] = data.get("workdir") or str((Path(directory) / eid).absolute())
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
                    "semantic_classification", "body_bytes_representation", "raw_body_complete",
                }}),
                occurred_at=receipt.get("read_at") or self.mind.clock(), extract=False,
                metadata={"host_event": "web-observation", "executor": "codex-cli",
                          "locator": receipt["locator"], "resource_version": receipt["version"],
                          "stored_content": "excerpt", "delivery": web_delivery(receipt)}))
            observation_ids.append(observed["id"])
        executor = data.get("executor") or "codex-cli"

        def report():
            return SourceInput(namespace="kin-exploration", key=eid,
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
                          "partial": data.get("partial", True), "sources": (data.get("result") or {}).get("sources", [])})
        if data.get("result") is None:
            source = self.engine.receive(report())
        else:
            # The report is written from the wish's evidence, which it goes with, and from what the
            # brief showed besides: checked where it is stored (CR5-MM-02).
            try:
                source = self.engine.receive(report(), derived_from=data["evidence_ids"], shown=shown)
            except Conflict as error:
                if getattr(error, "code", None) not in DERIVED_CONFLICTS:
                    raise
                # Something it was given was deleted or changed while it ran: the report is not
                # kept. The run, its observations and its cost stay facts.
                state = "failed"
                data.update(error="exploration-inputs-changed", inputs_withheld=error.code, partial=True, result=None)
                source = self.engine.receive(report())
        data["source_id"] = source["id"]
        data["observation_ids"] = observation_ids
        # The run names everything its brief showed: one deleted since loses it its words, in this
        # write and at any later delete (CR5-MM-02).
        data["evaluated_sources"] = shown
        with self.engine.db.connect(write=True) as conn:
            from .erasure import drop_deleted
            # What was deleted since the run began: before it, nothing of it reached the run (CL6E-MM-02).
            data = drop_deleted(conn, data, since=mark)
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
                # A finished run is not yet a finished step: with questions still open the step waits,
                # and the next review, which reads them, is where Kin decides whether to go on (K2-10).
                open_questions = list((data.get("result") or {}).get("open_questions") or [])[:8]
                AutonomousPlans(self.mind).settle_linked(conn, active, {"id": eid, "kind": "exploration-result",
                    "complete": action == "complete" and not open_questions, "source_id": source["id"], "state": state,
                    **({"open_questions": open_questions} if open_questions else {})})
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
        try:
            # Settled: the report, the checkpoint and the receipts are in the store, where a delete
            # reaches them. The working directory keeps its files without their words (CL6-MM-07).
            workdirs.scrub(Path(directory) / eid, state=state)
        except OSError:
            pass
        return dict(data, id=eid, state=state)

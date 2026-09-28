"""Private host entry point. Configuration contains paths, never inline API keys."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.db import digest
from eventmem.core.integrity import enforce_source_root, warn_interpreter
from eventmem.core.models import Scope, SourceInput
from eventmem.paths import atomic_write

from .actions import ActionEvents
from .appraisal import Appraisals, DailyReview, DeepSeek
from .conflicts import classify
from .context import Contexts
from .continuity import ConcernChange, ContinuityConfig
from .exploration import Explorations
from .exploration_cadence import ExplorationCadence
from .liveness import checks_enabled, record_alive
from .memory import MemoryContinuity
from .state import Mind


def load_config(path):
    config = json.loads(Path(path).read_text())
    # A dedicated existing file may supply the DeepSeek key. Never execute it.
    for line in Path(config["credentials_file"]).read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key.strip() == "EVENTMEM_API_KEY":
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return config


def dispatch(config, action, request):
    if action == "model-lease":
        # The fallback of the lease routes. Answered before any engine opens, so a busy
        # database costs the caller two seconds and never the engine's thirty.
        from .model_lanes import lease_command
        return lease_command(config["root"], request)
    registry = config.get("session_registry_file")
    if registry and Path(registry).exists():
        binding = json.loads(Path(registry).read_text())["binding"]
        config = {**config, "session_id": binding["threadId"]}
    engine = Engine(Path(config["root"]))
    from .model_lanes import context_lane, declared, sync_capacity
    # `recover` is the host's start-up call: there the configured capacity replaces what meta
    # holds. Any other action only fills a missing value.
    sync_capacity(engine, config, startup=action == "recover")
    mind = Mind(engine, Scope.model_validate(config["scope"]))
    if config.get("contact_policy_file"):
        # Kin sees the contact constraints the host applies, read live from its policy (K1-03).
        mind.register_contact_policy(config["contact_policy_file"])
    if action.startswith("plan-") or action in {"autonomous-plans", "manage-autonomous-plan", "procedure-memory"}:
        from .plans import AutonomousPlans
        from .procedures import Procedures
        plans = AutonomousPlans(mind)
        if action == "autonomous-plans":
            return plans.read(**request)
        if action == "manage-autonomous-plan":
            return plans.manage(request)
        if action == "plan-migrate":
            return plans.migrate_desires()
        if action == "plan-deferral":
            # The router's deferral pass (WS3) hands over a task Kin chose to do later (N10).
            return plans.defer_owner_task(request)
        if action == "plan-renew":
            return plans.renew(**request)
        if action == "plan-recover":
            return plans.recover(**request)
        if action == "plan-interrupt":
            # The host's static reason for stopping goes into the plan's receipt, so Kin sees why
            # a step stopped instead of one generic phrase (K2-02). Never free text.
            reason = request.get("reason")
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", reason):
                reason = "executor-stopped-before-settlement"
            return plans.settle(request["run_id"], request["owner"], request["fence"], state="interrupted",
                result={"checkpoint_retained": True, "reason": reason})
        if action == "plan-result":
            from .creation import accept_result
            return accept_result(mind, config, request)
        if action == "plan-claim":
            from .decision_context import execution_brief
            from .erasure import tombstone_mark
            with mind.engine.db.connect() as conn:
                # Before the claim reads the plan: what was deleted before it, the run never has the
                # words of, and only the deletes after it count against its result (CL6E-MM-02).
                mark = tombstone_mark(conn)
            result = plans.claim(**request)
            if result["state"] == "claimed":
                shown = []
                result["brief"] = execution_brief(mind, question=result["step"]["goal"],
                    evidence_ids=[r["record_id"] for r in result["run"]["decision"]["evidence"]],
                    plan=result["plan"], step=result["step"], shown=shown, since=mark)
                # What the brief showed is what the run's result is checked against when stored (CR5-MM-02).
                plans.note_shown(result["run"]["id"], shown, mark)
            return result
        if action == "procedure-memory":
            return Procedures(mind).read(**request)
        raise ValueError("Unknown autonomy operation")
    session_context = None
    observation_file = config.get("session_observation_file")
    if observation_file and Path(observation_file).exists():
        session_context = json.loads(Path(observation_file).read_text())
    from .codex_executor import exploration_capabilities
    jobs = Appraisals(mind, exploration_capabilities=exploration_capabilities(config),
                      session_context=session_context)
    explorer = Explorations(mind)
    cadence = ExplorationCadence(mind)
    actions = ActionEvents(mind)
    memory = MemoryContinuity(mind)
    if action == "operational-status":
        from .operational_status import operational_status
        return operational_status(mind, config)
    if action == "recover-operational":
        from .recovery import migrate_operational
        return migrate_operational(mind, workers_stopped=request.get("workers_stopped"))
    if action == "recover-history":
        from .recovery import recover_history
        return recover_history(mind, **request)
    if action == "recover-batched":
        from .recovery import recover_batched
        return recover_batched(mind, **request)
    if action == "recover-appraisals":
        from .recovery import recover_quarantined
        return recover_quarantined(mind, **request)
    if action == "appraisal-triage":
        # Operator action: every quarantined appraisal sorted into a class, with what would be done
        # to it. Nothing is written without `--apply`; ids, classes, counts and static codes only.
        from .recovery import triage_quarantined
        options = {key: request[key] for key in ("command_id", "source", "resume", "retire", "job_ids",
                                                 "per_slot", "spacing_minutes") if key in request}
        return triage_quarantined(mind, apply=bool(request.get("apply")), **options)
    if action.startswith("history-"):
        # Operator actions on the stored history itself. Each one registers with the history
        # registry instead of adding a branch here, so a package that ships a new command does
        # not have to touch this dispatch to be reachable.
        # The configuration travels with them because compaction has to prove nothing is running,
        # and where the host keeps its pid files and its status file is not something the store
        # knows. Commands that do not need it are not given it.
        from .history_admin import dispatch as history_command
        return history_command(mind, action, request, config)
    if action == "appraisal-attempts":
        # Read-only accounting: outcomes, static codes, digests and usage. No private text.
        from .attempts import read as read_attempts
        return read_attempts(engine, mind.scope.key(), job_id=request.get("job_id"),
                             limit=request.get("limit", 20))
    if action in {"session-snapshot", "session-checkpoint", "session-validate"}:
        from .session_checkpoint import SessionCheckpoint
        checkpoints = SessionCheckpoint(mind, agent_version=config["agent_version"])
        if action == "session-validate":
            return checkpoints.validate(request["checkpoint"])
        if action == "session-snapshot":
            if memory.settings()["event_lifecycle"] and isinstance(request.get("foreground"), bool):
                from .lifecycle import foreground_lease
                foreground_lease(engine, mind.scope.key(), "host:" + config["session_id"], active=request["foreground"], seconds=120)
            return checkpoints.snapshot(request.get("pending"), tasks=request.get("tasks"), intent=request.get("intent"))
        snapshot = dict(request['snapshot'])
        if not request.get('shadow') and not memory.settings().get('manifest_restore'):
            snapshot.pop('manifestVersion', None)
            snapshot.pop('linked', None)
        # The checkpoint carries the user's session across a rotation, so somebody is waiting
        # for it unless the host says this one is maintenance.
        with declared(context_lane("work", request.get("access_origin", "user_query")), "session-checkpoint"):
            return checkpoints.build(snapshot, request["binding"], budget=request.get("budget", 2000), provider=DeepSeek.from_engine(engine), allow_model=request.get("allow_model", True), adaptive_budget=True)
    if action.startswith("context-delivery-"):
        from .context_delivery import ContextDelivery
        deliveries = ContextDelivery(Contexts(mind))
        methods = {"context-delivery-begin": deliveries.begin, "context-delivery-ack": deliveries.acknowledge,
                   "context-delivery-uncertain": deliveries.uncertain, "context-delivery-pending": deliveries.pending,
                   "context-delivery-metrics": deliveries.metrics, "context-delivery-prepare": deliveries.prepare}
        if action not in methods:
            raise ValueError('Unknown context delivery operation')
        return methods[action](**request)
    if action == "session-review":
        if not config.get("adaptive_sessions") or not session_context:
            return {"state": "disabled"}
        # The observed snapshot is the stimulus; nothing is written into memory for a review.
        # The host's attempt is part of the queue key: the same attempt is one job, a new
        # attempt a new one, and the answer names the attempt it belongs to (CR-RT-08).
        if request.get("snapshotId") not in (None, session_context["id"]):
            return {"state": "stale", "snapshotId": session_context["id"], "requestId": request.get("id")}
        return jobs.enqueue_maintenance(session_context["id"], config["agent_version"], request_id=request.get("id"))
    if action == "configure-habits":
        return memory.habits.update(request)
    if action == "reply-choice":
        return memory.habits.choose_reply(request)
    if action == "reply-status":
        return memory.habits.reply_status(request["input_id"])
    if action == "configure-model-capacity":
        from .model_runtime import configure_capacity
        return configure_capacity(engine, request["limit"])
    if action == "share-cancel":
        return memory.sharing.cancel(request["draft_id"])
    if action == "reply-references":
        return memory.sharing.register(request)
    if action == "graph":
        return memory.graph.read(**request)
    if action == "graph-detail":
        return memory.graph.detail(request["identifier"])
    if action == "graph-revise":
        return memory.graph.revise(request)
    if action == "event-thread":
        return Contexts(mind).event_thread(**request)
    if action in {"lifecycle-status", "lifecycle-backfill"}:
        from .lifecycle import EventLifecycle
        lifecycle = EventLifecycle(mind, memory.graph)
        return lifecycle.status() if action == "lifecycle-status" else lifecycle.backfill(**request)
    if action == "migrate-evidence-isolation":
        # Operator action. A dry run writes nothing; the impact list goes to `--output` and only
        # its counts are printed, because the list itself can name thousands of identifiers.
        from .isolation_migration import IsolationMigration
        migration = IsolationMigration(mind)
        options = {"apply": bool(request.get("apply")), "output": request.get("output")}
        result = (migration.undo(**options) if request.get("undo")
                  else migration.run(**options, registry_file=request.get("registry")))
        return {k: v for k, v in result.items()
                if k in {"migration", "operation", "state", "applied", "rules_version", "summary", "steps", "output"}}
    if action in {"maintenance-tick", "vector-optimize"}:
        # Housekeeping for derived data only. Both write nothing without `--apply`, and both
        # report what they would have done so the decision can be taken on the numbers: the
        # tick's is the one hard delete in the programme, of compressed context that costs a
        # model call to rebuild, and the Lance command removes superseded manifests of the
        # vectors while leaving every current row where it is.
        from .maintenance import VECTOR_KEEP_DAYS, tick, vector_optimize
        if action == "vector-optimize":
            days = request.get("older_than_days", VECTOR_KEEP_DAYS)
            if type(days) is not int or days < 0:
                raise ValueError("A version age is a whole number of days")
            return vector_optimize(mind, config, apply=bool(request.get("apply")), older_than_days=days)
        return tick(mind, config, apply=bool(request.get("apply")))
    if action in {"evidence-keys-backfill", "evidence-keys-verify"}:
        # Operator actions. The backfill writes nothing without `--apply`, resumes from its cursor
        # and can be rerun; the verification is read only and compares both directions row by row.
        from .evidence_keys import BATCH, SAMPLE, backfill, verify
        if action == "evidence-keys-verify":
            return verify(mind, limit=request.get("limit", SAMPLE))
        return backfill(mind, apply=bool(request.get("apply")), batch=request.get("batch", BATCH))
    if action in {"desire-archive", "desire-unarchive"}:
        # Operator actions. Neither writes without `--apply`, and the dry run asks for no flag at
        # all: it reads what the retention rule would keep, move and let go, and what holds the
        # rest, which is what makes it safe against a copy of a live store. `days` and `keep`
        # override the configured rule for this run only. The restore is never gated by the flag,
        # because it is what runs before a rollback to a release that cannot see the archive.
        from .desire_archive import archive, restore
        if action == "desire-unarchive":
            return restore(mind, apply=bool(request.get("apply")), ids=request.get("ids"))
        return archive(mind, apply=bool(request.get("apply")), days=request.get("days"),
                       keep=request.get("keep"), limit=request.get("limit"))
    if action in {"exploration-decision-archive", "exploration-decision-unarchive"}:
        # Operator actions, as the wish archive's: neither writes without `--apply`, the dry run
        # needs no flag, and the restore is never gated because it runs before a rollback.
        from . import exploration_decision_archive as decisions
        if action == "exploration-decision-unarchive":
            return decisions.restore(mind, apply=bool(request.get("apply")), ids=request.get("ids"))
        return decisions.archive(mind, apply=bool(request.get("apply")), days=request.get("days", decisions.DAYS),
                                 keep=request.get("keep", decisions.KEEP), limit=request.get("limit"))
    if action == "slim-evidence-refs":
        # Operator action (evidence_refs). The dry run writes nothing and says how many references
        # the document and the two archives hold, how many would change and what that does to their
        # size. `--apply` keeps them to what their readers read, in one recorded revision, and marks
        # the document so every later save does the same; a second apply writes nothing. `--undo
        # --apply` puts every reference back from the sources table and clears the mark: it is what
        # runs before a rollback to a release that expects the full references. Never gated.
        from .evidence_refs import run as slim_evidence_refs
        return slim_evidence_refs(mind, apply=bool(request.get("apply")), undo=bool(request.get("undo")))
    if action in {"archive-memory", "archive-memory-backfill"}:
        # Operator actions. Without `--apply` both only read: the queue's counts, or what the
        # backfill would queue and the calls that would take. `archive-memory --apply` runs one
        # batch now (one paid DeepSeek call at most); `retry_failed` first puts back what ran out
        # of attempts. `archive-memory-backfill --apply` queues what was archived before, once.
        from . import archive_memory
        if action == "archive-memory-backfill":
            return archive_memory.backfill(mind, apply=bool(request.get("apply")), kinds_wanted=request.get("kinds"))
        if not request.get("apply"):
            return archive_memory.status(mind)
        retried = archive_memory.retry_failed(mind) if request.get("retry_failed") else 0
        return {**archive_memory.run(mind, DeepSeek.from_engine(engine), limit=request.get("limit", archive_memory.BATCH)),
                **({"retried": retried} if retried else {})}
    if action == "configure-memory":
        return memory.configure(request)
    if action == "runtime-event":
        result = memory.ingest(request)
        if result.get("source_id") and memory.settings()["semantic"] and not request.get("historical"):
            result["appraisal"] = jobs.enqueue([result["source_id"]], config["agent_version"], origin="reflection", stimulus="delivery" if request["kind"] == "delivery" else "runtime-result")
        return result
    if action == "memory-context":
        from .adaptive_recall import select_mode
        from .dialogue import clock_context
        requested_mode = request.get("mode", "auto")
        if request.get("purpose") == "read" and requested_mode == "auto":
            requested_mode = "deep"
        if memory.settings()["adaptive_recall"] and select_mode(request.get("query", ""), requested_mode, request.get("history", False)) == "deep":
            request.setdefault("allow_model", True)
        if config.get("adaptive_sessions"):
            request["native_pressure_managed"] = True
        if memory.settings().get('context_receipts') and request.get('session') and request.get('purpose') != 'read':
            request['receipt_mode'] = True
        return {**Contexts(mind).build(**request), "clock": clock_context(mind.clock())}
    if action == "state-overview":
        return Contexts(mind).affective(request.get("query", ""))
    if action == "prepare-memory":
        if not memory.settings()["context"]:
            return {"state": "disabled"}
        recent = memory.semantic_context(event_limit=1)["recent_interaction"]
        owner = next((e for e in reversed(recent) if e.get("kind") == "owner-message"), None)
        if not owner:
            return {"state": "idle"}
        result = Contexts(mind).warm(query=owner.get("text", ""))
        return {k: v for k, v in result.items() if k in {"state", "tokens", "cache_hit", "receipt"}}
    if action == "memory-compact-ack":
        return Contexts(mind).compact_ack(**request)
    if action == "memory-injection-ack":
        return Contexts(mind).injection_ack(**request)
    if action in {"share-history", "work-history"}:
        return Contexts(mind).read_history("share" if action == "share-history" else "work", **request)
    if action == "configure-continuity":
        return mind.configure_continuity(ContinuityConfig.model_validate(request))
    if action == "concern":
        return mind.manage_concern(ConcernChange.model_validate(request))
    if action == "migrate-continuity":
        return jobs.migrate_continuity(request["evidence_ids"], request["agent_version"])
    if action == "configure-actions":
        return actions.configure(request)
    if action == "observe":
        source = engine.receive(SourceInput(namespace="kin-assistant-output", key=request["id"],
            scope=mind.scope, session=config["session_id"], text=request["text"], authority="model",
            occurred_at=request["at"], extract=not memory.settings()["semantic"],
            metadata={"host_event": "assistant-result", "role": "assistant", "channel": request["channel"]}))
        memory.ingest({**request, "kind": "assistant-message", "source_id": source["id"], "session": config["session_id"]})
        return jobs.enqueue([source["id"]], config["agent_version"], origin="reflection", stimulus="assistant-result")
    if action == "ingest":
        # Only an authenticated host calls this. Internal results have another origin.
        source = engine.receive(
            SourceInput(
                namespace="kin-owner-input",
                key=request["id"],
                scope=mind.scope,
                session=request.get("session") or config["session_id"],
                text=request["text"],
                authority="explicit",
                occurred_at=request["at"],
                extract=not memory.settings()["semantic"],
                metadata={
                    "host_event": "message",
                    "role": "user",
                    "channel": request["channel"],
                },
            )
        )
        job = jobs.enqueue([source["id"]], config["agent_version"])
        memory.ingest({**request, "kind": "owner-message", "source_id": source["id"], "session": request.get("session") or config["session_id"]})
        # A resident action only stores (CR4-MM-05): whatever the request asks, `ingest` prepares no
        # context. A context that may call a model -- a deep recall, an embedding -- is prepared by
        # `memory-context`, a worker of its own under the model lanes' admission. With the context
        # layer off, the answer is the state view, as before, and nothing to prepare.
        if memory.settings()["context"]:
            return {"source_id": source["id"], "appraisal": job, "memory_enabled": True}
        return {
            "source_id": source["id"],
            "appraisal": job,
            "state": mind.read(query=request["text"]),
            "findings": explorer.recent(),
            "memory_context": {"state": "disabled", "text": "", "tokens": 0},
        }
    if action == "read":
        projection = request.get("projection")
        if projection is not None:
            # A bounded part of the state for a reader that uses only that (interaction_projection):
            # a contact draft. Without it the answer is the whole view, as it always was.
            from .interaction_projection import PROJECTION, interaction_projection
            if projection != PROJECTION or request.get("history"):
                raise ValueError("Unknown state projection")
            return {"projection": PROJECTION, "state": interaction_projection(mind.read(query=request.get("query", "")))}
        return {
            "state": mind.read(history=request.get("history", 0), query=request.get("query", "")),
            "exploration_capabilities": jobs.exploration_capabilities,
            "appraisals": jobs.status(),
            "findings": explorer.recent(),
            "exploration_cadence": cadence.status(),
            "memory": {"settings": memory.settings(), "review": memory.semantic_context(event_limit=1)["next_review"]} if memory.settings()["records"] else {"state": "disabled"},
        }
    if action in {"traits", "trait-revoke", "traits-migrate", "trait-wish-review"}:
        from .traits import Traits
        ledger = Traits(mind)
        if action == "traits":
            return ledger.read(request.get("identifier"), limit=request.get("limit", 12), history=request.get("history", False))
        if action == "trait-revoke":
            # An owner correction that must not wait for an appraisal. The cited source decides
            # whether it is recorded as the owner's own correction or as the operator's.
            return ledger.revoke(request)
        if action == "trait-wish-review":
            # What a moved trait left not ready, and then what was done about it: read first, so the
            # answer says what it found as well as what it moved. Without `apply` it writes nothing;
            # with it those wishes go to the existing `waiting` state — which is what an older
            # release has to see before a rollback, because to it they would still look ready.
            from .trait_refs import review_view, settle_wishes
            return {**review_view(mind), **settle_wishes(mind, apply=bool(request.get("apply")))}
        # Idempotent, and a dry run writes nothing.
        return ledger.migrate(apply=bool(request.get("apply")))
    if action == "configure-behavior-models":
        # The operator's own route, for the release step: the model this host answers with is the
        # one thing about a behavioral check that the store cannot read for itself.
        from .compat import BEHAVIOR_MODELS, CHAT_FIELDS
        values = {field: request.get(field) for field in CHAT_FIELDS}
        if any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values.values()):
            raise ValueError("Behavior models need a chat model id and an effort, as short strings")
        return {"state": "registered", BEHAVIOR_MODELS: engine.settings(
            BEHAVIOR_MODELS, {field: value.strip() for field, value in values.items()})}
    if action == "next-moves":
        # Read only: what each appraisal said it was doing, and what the host found behind it.
        from .next_move import recent
        return recent(mind, limit=request.get("limit", 20))
    if action == "configure-autonomy":
        return mind.configure_autonomy(request)
    if action == "computer-context":
        from .computer import ComputerReader
        computer = config.get("computer_exploration", {})
        if not computer.get("enabled"):
            return {"state": "unavailable", "reason": "computer-exploration-disabled"}
        return ComputerReader({**computer, "ledger": str(Path(config["root"]) / "computer-context.json")}).context()
    if action == "configure-behavior":
        return mind.configure_behavior(request)
    if action == "configure-contact":
        return mind.configure_contact(request)
    if action == "configure-contact-frequency":
        return mind.configure_contact_frequency(request)
    if action == "review-enrichment":
        if config.get("review_paused") or not memory.settings()["operational_lanes"]:
            return {"state": "paused"}
        if not request.get("job_id"):
            # The background lane also writes the memories of archived records (archive_memory):
            # when it has no job of its own to run, or when they have waited an hour for it.
            from . import archive_memory
            if archive_memory.due(mind, waited=0 if not jobs.runnable("enrichment") else archive_memory.STARVED_SECONDS):
                return archive_memory.run(mind, DeepSeek.from_engine(engine))
        return jobs.run_one(DeepSeek.from_engine(engine), lane="enrichment", job_id=request.get("job_id"))
    def review_provider():
        if config.get("main_session_review"):
            from .appraisal import NativeReview
            import sys
            def exchange(payload):
                print(json.dumps({"native_review": payload}, ensure_ascii=False), flush=True)
                response = json.loads(sys.stdin.readline())
                if response.get("id") != payload["id"]:
                    raise RuntimeError("native-review-response-mismatch")
                return response
            if not request.get("native_review_profile"):
                raise RuntimeError("main-session-required")
            return NativeReview.from_engine(engine, profile=request["native_review_profile"], exchange=exchange,
                                            used_tokens=request.get("native_review_used_tokens") or 0)
        else:
            return DeepSeek.from_engine(engine)

    def review_pause():
        if config.get("review_paused"):
            return {"state": "paused", "reason": "host-maintenance"}
        from .history import COMPACTING, compacting
        with engine.db.connect() as conn:
            # Every commit is refused while history compaction owns the store (WS6, K3-14): the
            # minute neither queues nor claims anything until it ends.
            return {"state": "paused", "reason": COMPACTING} if compacting(conn) else None

    def review_minute(run=None):
        """The minute's review: queue what is due, run one appraisal when `run` is given, then
        settle the wishes it decided. No extra model call is used for the clock."""
        actions.crossings()
        from .plans import AutonomousPlans
        plans = AutonomousPlans(mind)
        plans.tick(actions)
        memory.queue_idle(actions)
        actions.review_unselected()
        actions.drain(jobs)
        if memory.settings()["semantic"]:
            memory.queue_history(jobs, config["agent_version"])
            if memory.settings()["graph"]:
                from .graph_migration import GraphMigration
                GraphMigration(mind).queue_history(jobs, config["agent_version"])
        result = run() if run else None
        plans.sync_wishes()
        if run and isinstance(result, dict) and result.get("state") == "complete":
            # After a committed assessment the retention rule runs (desire_archive): a read that
            # finds nothing to move writes nothing, and a move is one revision of its own.
            from .desire_archive import retain
            retention = retain(mind)
            if retention["state"] not in {"disabled", "idle"}:
                result = {**result, "desire_retention": retention}
        actions.drain(jobs)
        # Settled exploration decisions leave the document once nothing reads them there: only with
        # `exploration_decision_archive` on, and one count query a minute while nothing can move.
        from .exploration_decision_archive import auto as archive_decisions
        archive_decisions(mind)
        if cadence.status()["state"] == "ready":
            wake = Path(config["exploration_stop_file"]).parent / "mind-exploration-request.json"
            if not wake.exists():
                # `mind-exploration-request.tmp` was one name two reviews shared, and
                # the host reads this file the moment it appears: a second review
                # renaming the first one's half-written temporary put a truncated
                # request in front of it. A name of its own per write, and the bytes
                # reach the disk before the rename rather than after it.
                atomic_write(wake, json.dumps({"kind": "internal-exploration-wakeup", "at": mind.clock()}))
        return result

    if action == "review-due":
        # T-14: the resident worker does the minute's bookkeeping and says whether an appraisal is
        # there to run; the host starts a model process only then. `tick: false` only asks.
        paused = review_pause()
        if paused:
            return paused
        if request.get("tick", True):
            review_minute()
        due = {lane: jobs.runnable(lane) for lane in ("action", "enrichment")}
        if not due["enrichment"] and memory.settings()["operational_lanes"]:
            # The memories of archived records are the background lane's work too (archive_memory).
            from .archive_memory import due as memories_due
            due["enrichment"] = memories_due(mind) > 0
        # The day's review has a gate of its own now: the host starts it only when there is something
        # to merge or evaluate, never to be told again that it is still waiting.
        return {"state": "due" if any(due.values()) else "idle", **due, "daily": DailyReview(mind).due()}
    if action == "review":
        paused = review_pause()
        if paused:
            return paused
        return review_minute(lambda: jobs.run_one(review_provider(), lane="action"))
    if action == "daily":
        provider = review_provider()
        provider.native_attempt = ("daily:" + mind.clock()[:10], config["agent_version"])
        return DailyReview(mind).run(provider, config["agent_version"])
    if action == "prepare-exploration":
        view = mind.read()
        reservation = cadence.reserve(config["agent_version"])
        if reservation["state"] != "ready":
            return reservation
        return {**reservation, "desires": view["desires"],
                "recent_results": [x["result"] for x in explorer.recent(6) if x.get("result")],
                "autonomy": view.get("autonomy", {})}
    if action == "explore":
        stop = Path(config["exploration_stop_file"])
        backend = config.get("exploration_backend") or "codex"
        if backend != "codex":
            # Exploration is codex-cli/DeepSeek only (KIN-ITER-20260919-01). There is
            # no kimi fallback: an unknown backend pauses instead of running anything.
            return {"state": "waiting", "reason": "exploration-backend-unknown",
                    "detail": str(backend)}
        from .codex_executor import exploration_capabilities, prepare_codex_exploration
        def repair_final(text, ledger, *, timeout):
            from .exploration import Findings
            provider = DeepSeek.from_engine(engine)
            provider.timeout = timeout
            try:
                fixed, receipt = provider.structured("repair_exploration_result", Findings,
                    "只修正原探索结果的 JSON 包装、结构与字段格式。只使用原结果和给定来源账本，不执行工具、不添加发现或证据、不降低引用要求。返回单个完整结果；无法补足的来源保留缺口。",
                    {"original_result": text, "source_ledger": ledger})
                return fixed.model_dump(), receipt
            except Exception as error:
                error.receipt = getattr(provider, "failure_receipt", None)
                raise
        prepared = prepare_codex_exploration(config, repair=repair_final)
        if prepared["state"] != "ready":
            return prepared
        resolved_computer = prepared.get("computer") or config.get("computer_exploration")
        capabilities = exploration_capabilities(config, computer_override=resolved_computer)
        web = None
        if capabilities["capabilities"]["search"]["available"]:
            web = {**(config.get("exploration_web") or {}), "enabled": True}
        budget = min(int(request.get("budget_seconds") or prepared["budget_seconds"]),
                     prepared["budget_seconds"])
        return explorer.run(
            prepared["executable"],
            config["exploration_directory"],
            config["agent_version"],
            canceled=stop.exists,
            model=prepared["model"],
            runner=prepared["runner"],
            brief=request.get("brief"),
            desire_id=request.get("desire_id"),
            budget_seconds=budget,
            computer=resolved_computer,
            web=web,
        )
    if action == "candidate":
        return mind.contact_candidate()
    if action == "reconsider":
        return mind.reconsider_contacts(**request)
    if action == "claim":
        return mind.claim_contact(**request)
    if action == "check":
        return mind.check_contact(**request)
    if action == "settle":
        return mind.settle_contact(**request)
    if action == "recover":
        # CR-MIND-12: an older release kept the session judgment in the versioned state.
        mind.retire_session_advice()
        # Emotion system v2: the dimensions this release adds join a state initialized before them.
        mind.extend_dimensions(agent_version=config["agent_version"])
        # Emotion system v2b: the derived layers' block, with every slow layer starting where its score
        # stands, joins a state kept before them -- after the dimensions, so each gets its anchor.
        mind.ensure_affect_layers(agent_version=config["agent_version"])
        # The owner's recorded word on contact frequency, which the host config names, becomes the
        # state's own field (2026-09-27); it reached an assessment only through one expression style.
        mind.adopt_contact_frequency(config.get("contact_frequency_evidence_ids"), agent_version=config["agent_version"])
        # A start-up used to assume the previous service was gone and interrupt every
        # running exploration. It asks now: a row is interrupted only when its worker
        # is provably gone — the pid is dead, the pid became some other process, or
        # the budget it recorded has run out. Anything it cannot establish is left
        # running, because a start-up that races a live exploration must lose.
        with engine.db.connect(write=True) as conn:
            evidence = checks_enabled(conn, mind.scope.key())
            running = conn.execute("SELECT id,data FROM mind_explorations WHERE scope=? AND state='running'", (mind.scope.key(),)).fetchall()
            current = mind._load(conn)
            interrupted, retained = [], []
            for row in running:
                data = json.loads(row["data"])
                if evidence and record_alive(data.get("liveness")):
                    retained.append(row["id"])
                    continue
                interrupted.append(row["id"])
                desire = current["desires"].get(data.get("desire_id"))
                if desire and desire["status"] == "in_progress":
                    desire.update(status="wanted", revision=desire["revision"]+1, updated_at=mind.clock())
            if interrupted:
                current["revision"] += 1
                current["updated_at"] = mind.clock()
                mind._save(conn, current)
                mind._history(conn, "mind_" + digest([mind.scope.key(), current["revision"]])[:32], current, "exploration-recovery", {"interrupted": len(interrupted)})
            for identifier in interrupted:
                conn.execute(
                    "UPDATE mind_explorations SET state='interrupted' WHERE id=? AND scope=? AND state='running'",
                    (identifier, mind.scope.key()),
                )
            attempts = conn.execute(
                "SELECT id FROM mind_contacts WHERE scope=? AND state='drafting'",
                (mind.scope.key(),),
            ).fetchall()
        for attempt in attempts:
            mind.settle_contact(
                attempt_id=attempt["id"],
                state="canceled",
                reason="Host restarted before sending",
            )
        # Every settled run's working directory that still has its words loses them now, the ones
        # just interrupted among them, rather than when the next exploration starts (OPS-03).
        directory = config.get("exploration_directory")
        swept = explorer.sweep_workdirs(directory) if directory else []
        return {"state": "recovered", "canceled_drafts": len(attempts),
                "interrupted_explorations": interrupted, "live_explorations": retained,
                "swept_workdirs": len(swept)}
    raise ValueError("Unknown host action")


MIGRATION_ACTION = "migrate-evidence-isolation"
# The operator actions that run from a terminal with nothing to pipe in, so `--apply` is how they
# are told to write. Every one of them defaults to a dry run.
APPLY_ACTIONS = (MIGRATION_ACTION, "evidence-keys-backfill", "desire-archive", "desire-unarchive",
                 "maintenance-tick", "vector-optimize", "history-compact", "history-restore", "appraisal-triage",
                 "exploration-decision-archive", "exploration-decision-unarchive",
                 "archive-memory", "archive-memory-backfill", "slim-evidence-refs")
# The operator actions `--undo` is read for.
UNDO_ACTIONS = (MIGRATION_ACTION, "slim-evidence-refs")


# The resident worker's actions (§5.7): short reads and writes on the store, no model call and
# no long executor. Exploration, creation's completion review, memory preparation and every
# request that may take a native review keep their own process, executor and lease.
RESIDENT_ACTIONS = frozenset({
    "reply-status", "ingest", "runtime-event", "observe", "read", "candidate", "reconsider", "claim",
    "check", "settle", "plan-claim", "plan-renew", "plan-interrupt", "plan-deferral", "model-lease",
    "memory-compact-ack", "memory-injection-ack", "operational-status", "session-review", "session-snapshot",
    "context-delivery-begin", "context-delivery-ack", "context-delivery-uncertain", "context-delivery-pending",
    "context-delivery-metrics", "configure-habits", "reply-choice", "review-due",
})


def _failure(error):
    found = classify(error)
    return {"error": type(error).__name__, "kind": found.kind, **({"code": found.code} if found.code else {})}


def serve(config, stdin=None, stdout=None):
    """The resident worker (§5.7): one request per line, `{id, action, args, timeoutMs}`, answered in
    order as `{id, ok, result|error}`. Requests run one at a time; the caller owns each timeout and
    restarts this process when one runs over. Anything a request prints goes to stderr, so the
    answer stream carries frames only."""
    import sys

    stdin, out = stdin or sys.stdin, stdout or sys.stdout
    previous, sys.stdout = sys.stdout, sys.stderr
    try:
        while True:
            line = stdin.readline()
            if not line:
                return None
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
                identifier, action = frame["id"], frame["action"]
                args = frame.get("args") or {}
                if not isinstance(args, dict):
                    raise TypeError("args")
            except (ValueError, KeyError, TypeError):
                reply = {"id": None, "ok": False, "error": {"error": "ValueError", "kind": "semantic", "code": "invalid-frame"}}
            else:
                if action not in RESIDENT_ACTIONS:
                    reply = {"id": identifier, "ok": False,
                             "error": {"error": "ValueError", "kind": "semantic", "code": "not-a-resident-action"}}
                else:
                    try:
                        reply = {"id": identifier, "ok": True, "result": dispatch(config, action, args)}
                    except Exception as error:  # noqa: BLE001 - one request's failure is its own answer
                        reply = {"id": identifier, "ok": False, "error": _failure(error)}
            out.write(json.dumps(reply, ensure_ascii=False) + "\n")
            out.flush()
    finally:
        sys.stdout = previous


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    # Only the operator actions read these; every other action takes its request on stdin as
    # before. A dry run is the default, so nothing is written without `--apply`.
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--registry")
    parser.add_argument("action")
    args = parser.parse_args()
    # The host's control channel is this worker's alone: read out of the environment before
    # anything here can start a process that would inherit it (CR3-MM-02).
    from . import worker_groups  # noqa: F401
    try:
        import sys

        config = load_config(args.config)
        # The host's own directory: its state holds the runtime bundle and Kin's Codex home, which
        # exploration uses unless the configuration names others (K2-15).
        config.setdefault("host_root", str(Path(args.config).resolve().parent))
        # The live contact policy sits beside the host's configuration unless it names another.
        policy = Path(args.config).resolve().with_name("proactive-policy.json")
        if "contact_policy_file" not in config and policy.exists():
            config["contact_policy_file"] = str(policy)
        # Before the store opens and before the request is even read: the code this process
        # would run has to be the code the deployment points at. A refusal is a SystemExit,
        # which the handler below cannot turn into a result -- an ordinary exception here
        # would be printed as one more failed action and the host would keep running the
        # wrong copy, which is the exact failure this check exists for.
        enforce_source_root(config.get("source_root"))
        # And whether the next worker will have anything to start with. Only said,
        # never acted on: this process is running, so a broken interpreter cannot
        # hurt it, and refusing would remove the one path still able to report it.
        warn_interpreter(config.get("python"))
        if args.action == "serve":
            return serve(config)
        # An operator runs these from a terminal, with no request to pipe in.
        raw = "" if sys.stdin.isatty() else (sys.stdin.readline() if args.action in {"review", "daily"} and config.get("main_session_review") else sys.stdin.read())
        request = json.loads(raw) if raw.strip() else {}
        if args.action in APPLY_ACTIONS:
            if args.apply and args.dry_run:
                raise ValueError("Choose either a dry run or an apply")
            request["apply"] = args.apply
        if args.action == MIGRATION_ACTION:
            request.update(undo=args.undo, output=args.output, registry=args.registry)
        elif args.action in UNDO_ACTIONS:
            request["undo"] = args.undo
        elif args.undo:
            raise ValueError("--undo belongs to a migration")
        result = dispatch(config, args.action, request)
    except Exception as error:  # noqa: BLE001 - worker boundary persists a redacted failure receipt
        # Caller sees an error category, never provider payloads or credentials.
        # Additive: the class stays the caller's contract, the taxonomy tells it
        # whether waiting can help. Static codes only, never the failing payload.
        result = _failure(error)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

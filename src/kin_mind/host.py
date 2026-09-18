"""Private host entry point. Configuration contains paths, never inline API keys."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.db import digest
from eventmem.core.models import Scope, SourceInput

from .actions import ActionEvents
from .appraisal import Appraisals, DailyReview, DeepSeek
from .conflicts import classify
from .context import Contexts
from .continuity import ConcernChange, ContinuityConfig
from .exploration import Explorations
from .exploration_cadence import ExplorationCadence
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
    if action.startswith("plan-") or action in {"autonomous-plans", "manage-autonomous-plan", "procedure-memory", "procedure-trial"}:
        from .plans import AutonomousPlans
        from .procedures import Procedures
        plans = AutonomousPlans(mind)
        if action == "autonomous-plans":
            return plans.read(**request)
        if action == "manage-autonomous-plan":
            return plans.manage(request)
        if action == "plan-migrate":
            return plans.migrate_desires()
        if action == "plan-renew":
            return plans.renew(**request)
        if action == "plan-recover":
            return plans.recover(**request)
        if action == "plan-interrupt":
            return plans.settle(request["run_id"], request["owner"], request["fence"], state="interrupted",
                result={"checkpoint_retained": True, "reason": "executor-stopped-before-settlement"})
        if action == "plan-result":
            from .creation import accept_result
            return accept_result(mind, config, request)
        if action == "plan-claim":
            from .decision_context import execution_brief
            result = plans.claim(**request)
            if result["state"] == "claimed":
                result["brief"] = execution_brief(mind, question=result["step"]["goal"],
                    evidence_ids=[r["record_id"] for r in result["run"]["decision"]["evidence"]],
                    plan=result["plan"], step=result["step"])
            return result
        if action == "procedure-memory":
            return Procedures(mind).read(**request)
        if action == "procedure-trial":
            return Procedures(mind).record_trial(**request)
        raise ValueError("Unknown autonomy operation")
    session_context = None
    observation_file = config.get("session_observation_file")
    if observation_file and Path(observation_file).exists():
        session_context = json.loads(Path(observation_file).read_text())
    jobs = Appraisals(mind, exploration_capabilities={
        "computer": bool(config.get("computer_exploration", {}).get("enabled")),
        "decisions": bool(config.get("exploration_decisions_enabled")),
        "version": config.get("agent_version"),
    }, session_context=session_context)
    explorer = Explorations(mind)
    cadence = ExplorationCadence(mind)
    actions = ActionEvents(mind)
    memory = MemoryContinuity(mind)
    if action == "operational-status":
        from .operational_status import operational_status
        return operational_status(mind)
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
    if action.startswith("history-"):
        # Operator actions on the stored history itself. Each one registers with the history
        # registry instead of adding a branch here, so a package that ships a new command does
        # not have to touch this dispatch to be reachable.
        from .history_admin import dispatch as history_command
        return history_command(mind, action, request)
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
    if action == "continuity-manifest":
        from .continuity_manifest import ContinuityManifest
        return ContinuityManifest(mind).read(**request)
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
        source = engine.receive(SourceInput(namespace="kin-session-maintenance", key=request["id"],
            session=config["session_id"], scope=mind.scope, authority="operation", kind="observation", extract=False,
            text="宿主请求检查当前原生会话。依据 session_context 判断压缩与接续；此事件不是用户消息，也不改变情绪和愿望。",
            metadata={"session_snapshot_id": session_context["id"], "maintenance_only": True}))
        return jobs.enqueue([source["id"]], config["agent_version"], origin="reflection", stimulus="session-maintenance")
    if action == "configure-habits":
        return memory.habits.update(request)
    if action == "reply-choice":
        return memory.habits.choose_reply(request)
    if action == "reply-status":
        return memory.habits.reply_status(request["input_id"])
    if action == "configure-model-capacity":
        from .model_runtime import configure_capacity
        return configure_capacity(engine, request["limit"])
    if action == "share-preflight-group":
        from .reply_review import ReplyReviews
        provider = DeepSeek.from_engine(engine) if request.get("allow_model") else None
        if provider:
            provider.timeout = 120
        with declared("foreground", action):
            return ReplyReviews(memory.sharing).preflight(request, provider)
    if action == "share-preflight":
        provider = DeepSeek.from_engine(engine) if request.get("allow_model") else None
        if provider:
            provider.timeout = 120
        with declared("foreground", action):
            return memory.sharing.preflight(request, provider)
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
    if action == "configure-memory":
        return memory.configure(request)
    if action == "runtime-event":
        result = memory.ingest(request)
        if result.get("source_id") and memory.settings()["semantic"] and not request.get("historical"):
            result["appraisal"] = jobs.enqueue([result["source_id"]], config["agent_version"], origin="reflection", stimulus="delivery" if request["kind"] == "delivery" else "runtime-result")
        return result
    if action == "memory-context":
        from .dialogue import clock_context
        from .adaptive_recall import select_mode
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
    if action == "memory-window":
        window = Contexts(mind).window(config["session_id"])
        return {"epoch":window["epoch"], "used":window["used"], "compact_requested":window["used"]>=10000 and not config.get("adaptive_sessions"), "automatic_background_exhausted":window["used"]>=12000}
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
        if request.get("defer_context") and memory.settings()["context"]:
            return {"source_id": source["id"], "appraisal": job, "memory_enabled": True}
        return {
            "source_id": source["id"],
            "appraisal": job,
            "state": mind.read(query=request["text"]),
            "findings": explorer.recent(),
            "memory_context": Contexts(mind).build(query=request["text"], session=config["session_id"], event_id=request["id"], purpose=request.get("purpose", "chat")),
        }
    if action == "read":
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
    if action == "review-enrichment":
        if config.get("review_paused") or not memory.settings()["operational_lanes"]:
            return {"state": "paused"}
        return jobs.run_one(DeepSeek.from_engine(engine), lane="enrichment", job_id=request.get("job_id"))
    if action == "review":
        if config.get("review_paused"):
            return {"state": "paused", "reason": "host-maintenance"}
        # The existing minute review queues work; the original host owns execution
        # and waits for owner tasks. No extra model call is used for the clock.
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
        result = jobs.run_one(DeepSeek.from_engine(engine), lane="action")
        plans.sync_wishes()
        actions.drain(jobs)
        if cadence.status()["state"] == "ready":
            wake = Path(config["exploration_stop_file"]).parent / "mind-exploration-request.json"
            if not wake.exists():
                temporary = wake.with_suffix(".tmp")
                temporary.write_text(json.dumps({"kind": "internal-exploration-wakeup", "at": mind.clock()}))
                temporary.replace(wake)
        return result
    if action == "daily":
        return DailyReview(mind).run(
            DeepSeek.from_engine(engine), config["agent_version"]
        )
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
        return explorer.run(
            config["kimi_executable"],
            config["exploration_directory"],
            config["agent_version"],
            canceled=stop.exists,
            model=config.get("kimi_model"),
            brief=request.get("brief"),
            desire_id=request.get("desire_id"),
            budget_seconds=request.get("budget_seconds", 1200),
            computer=config.get("computer_exploration"),
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
        # Startup must follow verified termination of the previous service/owned worker.
        with engine.db.connect(write=True) as conn:
            interrupted = conn.execute("SELECT data FROM mind_explorations WHERE scope=? AND state='running'", (mind.scope.key(),)).fetchall()
            current = mind._load(conn)
            for row in interrupted:
                data = json.loads(row["data"])
                desire = current["desires"].get(data.get("desire_id"))
                if desire and desire["status"] == "in_progress":
                    desire.update(status="wanted", revision=desire["revision"]+1, updated_at=mind.clock())
            if interrupted:
                current["revision"] += 1
                current["updated_at"] = mind.clock()
                mind._save(conn, current)
                mind._history(conn, "mind_" + digest([mind.scope.key(), current["revision"]])[:32], current, "exploration-recovery", {"interrupted": len(interrupted)})
            conn.execute(
                "UPDATE mind_explorations SET state='interrupted' WHERE scope=? AND state='running'",
                (mind.scope.key(),),
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
        return {"state": "recovered", "canceled_drafts": len(attempts)}
    raise ValueError("Unknown host action")


MIGRATION_ACTION = "migrate-evidence-isolation"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    # Only `migrate-evidence-isolation` reads these; every other action takes its request on
    # stdin as before. A dry run is the default, so nothing is written without `--apply`.
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--registry")
    parser.add_argument("action")
    args = parser.parse_args()
    try:
        import sys

        # An operator runs the migration from a terminal, with no request to pipe in.
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        request = json.loads(raw) if raw.strip() else {}
        if args.action == MIGRATION_ACTION:
            if args.apply and args.dry_run:
                raise ValueError("Choose either a dry run or an apply")
            request.update(apply=args.apply, undo=args.undo, output=args.output, registry=args.registry)
        result = dispatch(load_config(args.config), args.action, request)
    except Exception as error:  # noqa: BLE001 - worker boundary persists a redacted failure receipt
        # Caller sees an error category, never provider payloads or credentials.
        # Additive: the class stays the caller's contract, the taxonomy tells it
        # whether waiting can help. Static codes only, never the failing payload.
        found = classify(error)
        result = {"error": type(error).__name__, "kind": found.kind,
                  **({"code": found.code} if found.code else {})}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

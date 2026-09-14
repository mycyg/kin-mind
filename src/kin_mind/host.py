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
    engine = Engine(Path(config["root"]))
    mind = Mind(engine, Scope.model_validate(config["scope"]))
    jobs = Appraisals(mind, exploration_capabilities={
        "computer": bool(config.get("computer_exploration", {}).get("enabled")),
        "decisions": bool(config.get("exploration_decisions_enabled")),
        "version": config.get("agent_version"),
    })
    explorer = Explorations(mind)
    cadence = ExplorationCadence(mind)
    actions = ActionEvents(mind)
    memory = MemoryContinuity(mind)
    if action == "configure-habits":
        return memory.habits.update(request)
    if action == "reply-choice":
        return memory.habits.choose_reply(request)
    if action == "reply-status":
        return memory.habits.reply_status(request["input_id"])
    if action == "share-preflight":
        provider = DeepSeek.from_engine(engine) if request.get("allow_model") else None
        if provider:
            provider.timeout = 120
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
    if action == "configure-memory":
        return memory.configure(request)
    if action == "runtime-event":
        result = memory.ingest(request)
        if result.get("source_id") and memory.settings()["semantic"] and not request.get("historical"):
            result["appraisal"] = jobs.enqueue([result["source_id"]], config["agent_version"], origin="reflection", stimulus="delivery" if request["kind"] == "delivery" else "runtime-result")
        return result
    if action == "memory-context":
        return Contexts(mind).build(**request)
    if action == "memory-window":
        window = Contexts(mind).window(config["session_id"])
        return {"epoch":window["epoch"], "used":window["used"], "compact_requested":window["used"]>=10000}
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
                session=config["session_id"],
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
        memory.ingest({**request, "kind": "owner-message", "source_id": source["id"], "session": config["session_id"]})
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
    if action == "review":
        if config.get("review_paused"):
            return {"state": "paused", "reason": "host-maintenance"}
        # The existing minute review queues work; the original host owns execution
        # and waits for owner tasks. No extra model call is used for the clock.
        actions.crossings()
        memory.queue_idle(actions)
        actions.review_unselected()
        actions.drain(jobs)
        if memory.settings()["semantic"]:
            memory.queue_history(jobs, config["agent_version"])
            if memory.settings()["graph"]:
                from .graph_migration import GraphMigration
                GraphMigration(mind).queue_history(jobs, config["agent_version"])
        result = jobs.run_one(DeepSeek.from_engine(engine))
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("action")
    args = parser.parse_args()
    try:
        import sys

        result = dispatch(load_config(args.config), args.action, json.load(sys.stdin))
    except Exception as error:  # noqa: BLE001 - worker boundary persists a redacted failure receipt
        # Caller sees an error category, never provider payloads or credentials.
        result = {"error": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

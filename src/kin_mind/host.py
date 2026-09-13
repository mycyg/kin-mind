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
from .continuity import ConcernChange, ContinuityConfig
from .exploration import Explorations
from .exploration_cadence import ExplorationCadence
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
    jobs = Appraisals(mind)
    explorer = Explorations(mind)
    cadence = ExplorationCadence(mind)
    actions = ActionEvents(mind)
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
            occurred_at=request["at"], extract=True,
            metadata={"host_event": "assistant-result", "role": "assistant", "channel": request["channel"]}))
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
                extract=True,
                metadata={
                    "host_event": "message",
                    "role": "user",
                    "channel": request["channel"],
                },
            )
        )
        job = jobs.enqueue([source["id"]], config["agent_version"])
        return {
            "source_id": source["id"],
            "appraisal": job,
            "state": mind.read(query=request["text"]),
            "findings": explorer.recent(),
        }
    if action == "read":
        return {
            "state": mind.read(history=request.get("history", 0), query=request.get("query", "")),
            "appraisals": jobs.status(),
            "findings": explorer.recent(),
            "exploration_cadence": cadence.status(),
        }
    if action == "configure-autonomy":
        return mind.configure_autonomy(request)
    if action == "configure-behavior":
        return mind.configure_behavior(request)
    if action == "configure-contact":
        return mind.configure_contact(request)
    if action == "review":
        # The existing minute review queues work; the original host owns execution
        # and waits for owner tasks. No extra model call is used for the clock.
        actions.crossings()
        actions.review_unselected()
        actions.drain(jobs)
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

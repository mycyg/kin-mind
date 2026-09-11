"""Private host entry point. Configuration contains paths, never inline API keys."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.models import Scope, SourceInput

from .appraisal import Appraisals, DailyReview, DeepSeek
from .exploration import Explorations
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
            "state": mind.read(),
            "findings": explorer.recent(),
        }
    if action == "read":
        return {
            "state": mind.read(history=request.get("history", 0)),
            "appraisals": jobs.status(),
            "findings": explorer.recent(),
        }
    if action == "review":
        return jobs.run_one(DeepSeek.from_engine(engine))
    if action == "daily":
        return DailyReview(mind).run(
            DeepSeek.from_engine(engine), config["agent_version"]
        )
    if action == "explore":
        stop = Path(config["exploration_stop_file"])
        return explorer.run(
            config["kimi_executable"],
            config["exploration_directory"],
            config["agent_version"],
            canceled=stop.exists,
            model=config.get("kimi_model"),
        )
    if action == "candidate":
        return mind.contact_candidate()
    if action == "claim":
        return mind.claim_contact(**request)
    if action == "check":
        return mind.check_contact(**request)
    if action == "settle":
        return mind.settle_contact(**request)
    if action == "recover":
        # Startup must follow verified termination of the previous service/owned worker.
        with engine.db.connect(write=True) as conn:
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

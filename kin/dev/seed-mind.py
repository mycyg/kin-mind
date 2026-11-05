"""Dev only (dev/e2e-steward.mjs): give a synthetic mind what her reviews would have left — a sourced
question she wants to explore, and a plan step she decided to make — through Core's own classes.
Usage: python dev/seed-mind.py <dataDir>/mind-config.json"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.models import Scope
from kin_mind.plans import AutonomousPlans
from kin_mind.state import DesireChange, Mind

config = json.loads(Path(sys.argv[1]).read_text())
mind = Mind(Engine(Path(config["root"])), Scope.model_validate(config["scope"]))
version = config["agent_version"]
with mind.engine.db.connect() as conn:
    seed = conn.execute("SELECT id FROM sources WHERE namespace='kin-setup' AND deleted=0").fetchone()[0]

mind.manage_desire(DesireChange(
    command_id="e2e:explore", agent_version=version, expected_revision=mind.read()["revision"], evidence_ids=[seed],
    action="create", kind="explore", content="钟表里的齿轮是怎么一格一格走的？", topic="钟表", strength=80,
    expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), completion="弄明白齿轮怎么走", reason="合成的好奇"))

plans = AutonomousPlans(mind)
plan = plans.manage({"command_id": "e2e:plan", "action": "create", "key": "e2e-clock", "goal": "做一只小钟",
    "motivation": "想给主人做个小东西", "reason": "合成的计划", "evidence_ids": [seed],
    "steps": [{"id": "make", "actor": "create", "goal": "画一只小钟", "completion": "一个 SVG 的小钟"}]})
with mind.engine.db.connect(write=True) as conn:
    plans.decide(conn, {"plan_id": plan["id"], "step_id": "make", "expected_revision": plan["revision"], "action": "execute",
        "reason": "现在就做", "evidence_ids": [seed]}, "e2e:decide", {"provider": "deepseek", "reasoning": "high", "agent_version": version},
        mind._evidence(conn, [seed]))
print(json.dumps({"seeded": True, "plan": plan["id"]}))

"""The frozen recall replay refuses what it must not touch: it runs on a copy (T1-07, T1-10, E3-10)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eventmem.core import Engine, SourceInput
from eventmem.core.models import Scope
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from live_root import refuse_live  # noqa: E402

KIN = Scope(project="personal", persona="Kin")


def held(tmp_path, marker):
    root = tmp_path / "held"
    root.mkdir()
    (root / marker).write_text("synthetic")
    return root


def snapshot(tmp_path):
    engine = Engine(tmp_path / "snapshot")
    ids = []
    for i in range(3):
        source = engine.receive(SourceInput(namespace="replay", key=str(i), scope=KIN, kind="fact",
                                            authority="explicit", text=f"合成回放记录 {i}：花园的阳光。"))
        ids.append(engine.source(source["id"])["record_ids"][0])
    mind = Mind(engine, KIN)
    mind.initialize(agent_version="replay-v1", evidence_ids=[ids[0]])
    MemoryContinuity(mind).configure({"records": True, "context": True})
    return engine, ids


def stored_settings(root):
    with Engine(root).db.connect() as conn:
        return (conn.execute("SELECT key,data FROM settings ORDER BY key").fetchall(),
                conn.execute("SELECT scope,data FROM mind_memory_config ORDER BY scope").fetchall())


@pytest.mark.parametrize("marker", ["local-token", "service.pid"])
def test_a_root_a_service_holds_is_refused(tmp_path, marker):
    with pytest.raises(SystemExit, match="a service holds it"):
        refuse_live(held(tmp_path, marker))


def test_the_live_root_and_everything_below_it_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("KIN_MEMORY_ROOT", str(tmp_path / "memory"))
    for root in (tmp_path / "memory", tmp_path / "memory" / "exports"):
        with pytest.raises(SystemExit, match="live memory root"):
            refuse_live(root)
    assert refuse_live(tmp_path / "copy") == (tmp_path / "copy").resolve()


def test_a_replay_on_a_held_root_stops_before_opening_it(tmp_path):
    root = held(tmp_path, "local-token")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"frozen_before_retrieval": True, "cases": []}))
    run = subprocess.run([sys.executable, str(SCRIPTS / "lifecycle_replay.py"), "--root", str(root),
                          "--cases", str(cases), "--output", str(tmp_path / "out.json"),
                          "--scope", json.dumps(KIN.model_dump())], capture_output=True, text=True)
    assert run.returncode != 0 and "refusing" in run.stderr
    assert sorted(p.name for p in root.iterdir()) == ["local-token"]


def test_a_replay_keeps_its_settings_in_process(tmp_path, monkeypatch):
    from lifecycle_replay import evaluate, replay_settings

    engine, ids = snapshot(tmp_path)
    before = stored_settings(engine.db.root)
    monkeypatch.setattr(MemoryContinuity, "settings", MemoryContinuity.settings)
    replay_settings(engine, adaptive_recall=True, embedding_env="KIN_REPLAY_EMBEDDING_TOKEN")
    assert engine.settings("models")["embedding"]["api_key_env"] == "KIN_REPLAY_EMBEDDING_TOKEN"
    assert MemoryContinuity(Mind(engine, KIN)).settings()["adaptive_recall"] is True
    cases = [{"id": f"case-{i}", "category": "synthetic", "critical": i == 0, "query": "花园的阳光",
              "expected_ids": [ids[i % 3]]} for i in range(3)]
    for legacy in (True, False):
        assert evaluate(engine, KIN, cases, legacy=legacy)["cases"] == 3
    assert stored_settings(engine.db.root) == before

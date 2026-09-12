from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import dumps
from eventmem.core.models import Scope, SourceInput
from kin_mind import host
from kin_mind.exploration import Explorations
from kin_mind.exploration_cadence import ExplorationCadence
from kin_mind.state import Mind


@pytest.fixture
def world(tmp_path):
    clock = [datetime.now(timezone.utc)]
    engine = Engine(tmp_path / "memory")

    def create(persona="synthetic-cadence"):
        scope = Scope(persona=persona)
        mind = Mind(engine, scope, clock=lambda: clock[0].isoformat())
        source = engine.receive(SourceInput(
            namespace="test", key="configuration", scope=scope,
            authority="explicit", text="Synthetic exploration authorization",
            occurred_at=clock[0].isoformat(),
        ))["id"]
        mind.initialize(agent_version="test-v1", evidence_ids=[source])
        return mind, source

    mind, source = create()
    return mind, source, clock, create


def test_empty_selection_consumes_window_across_restart(world):
    mind, _, clock, _ = world
    cadence = ExplorationCadence(mind)
    before = mind.read()["revision"]
    reservation = cadence.reserve("test-v1")
    assert reservation["state"] == "ready"
    # The model declines, fails or exits: no research row is ever created.
    assert Explorations(mind).recent() == []
    clock[0] += timedelta(minutes=1)
    restored = Mind(mind.engine, mind.scope, clock=mind.clock)
    restarted = ExplorationCadence(restored)
    assert restarted.reserve("test-v1") == {
        "state": "waiting", "reason": "four-hour-exploration-cadence",
        "next_at": reservation["next_at"],
    }
    assert mind.read()["revision"] == before
    assert restarted.recent()[0]["kind"] == "internal-exploration-selection"
    clock[0] = datetime.fromisoformat(reservation["next_at"]) - timedelta(seconds=1)
    assert restarted.status()["state"] == "waiting"
    clock[0] += timedelta(seconds=1)
    assert restarted.reserve("test-v2")["state"] == "ready"
    assert [item["agent_version"] for item in restarted.recent()] == ["test-v2", "test-v1"]


def test_concurrent_wakes_have_one_reservation_and_separate_scopes(world):
    mind, _, _, create = world
    cadence = ExplorationCadence(mind)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: cadence.reserve("test-v1"), range(4)))
    assert [r["state"] for r in results].count("ready") == 1
    assert [r["state"] for r in results].count("waiting") == 3
    assert len(cadence.recent()) == 1
    other, _ = create("another-synthetic-scope")
    assert ExplorationCadence(other).reserve("test-v1")["state"] == "ready"


def test_minute_review_does_not_requeue_declined_selection(world, tmp_path, monkeypatch):
    mind, source, clock, _ = world
    mind.configure_autonomy({
        "command_id": "open-exploration", "agent_version": "test-v1",
        "expected_revision": mind.read()["revision"], "evidence_ids": [source],
        "reason": "Synthetic user authorization",
    })
    monkeypatch.setattr(host, "Engine", lambda _: mind.engine)
    monkeypatch.setattr(host, "Mind", lambda *args: mind)
    monkeypatch.setattr(host.DeepSeek, "from_engine", lambda _: None)
    monkeypatch.setattr(host.Appraisals, "run_one", lambda *args: {"state": "idle"})
    config = {
        "root": str(tmp_path), "scope": mind.scope.model_dump(),
        "agent_version": "test-v1", "exploration_stop_file": str(tmp_path / "stop"),
    }
    wake = tmp_path / "mind-exploration-request.json"
    host.dispatch(config, "review", {})
    assert wake.exists()
    wake.unlink()
    first = host.dispatch(config, "prepare-exploration", {})
    assert first["state"] == "ready"
    # This is the production regression: text:null never calls explore.
    for _ in range(3):
        clock[0] += timedelta(minutes=1)
        host.dispatch(config, "review", {})
        assert not wake.exists()
        assert host.dispatch(config, "prepare-exploration", {})["state"] == "waiting"
    clock[0] = datetime.fromisoformat(first["next_at"])
    host.dispatch(config, "review", {})
    assert wake.exists()
    assert host.dispatch(config, "prepare-exploration", {})["state"] == "ready"


def test_existing_research_window_and_running_job_remain_protected(world):
    mind, _, clock, _ = world
    Explorations(mind)
    cadence = ExplorationCadence(mind)
    started = clock[0] - timedelta(hours=1)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (
            "synthetic-job", mind.scope.key(), "complete", started.isoformat(), dumps({}),
        ))
    assert cadence.reserve("test-v1")["next_at"] == (started + timedelta(hours=4)).isoformat()
    assert cadence.recent() == []
    clock[0] += timedelta(hours=3)
    assert cadence.status()["state"] == "ready"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_explorations SET state='running' WHERE id='synthetic-job'")
    assert cadence.reserve("test-v1") == {"state": "waiting", "reason": "exploration-in-progress"}


def test_reserved_selection_can_start_its_research(world, tmp_path):
    mind, source, _, _ = world
    mind.configure_autonomy({
        "command_id": "research-permission", "agent_version": "test-v1",
        "expected_revision": mind.read()["revision"], "evidence_ids": [source],
        "reason": "Synthetic user authorization",
    })
    cadence = ExplorationCadence(mind)
    assert cadence.reserve("test-v1")["state"] == "ready"
    calls = []

    def runner(executable, topic, directory, **kwargs):
        calls.append(topic["question"])
        assert kwargs["budget_seconds"] == 1190
        return {"state": "complete", "partial": False, "result": {
            "summary": "Synthetic report", "findings": ["Fixture finding"],
            "sources": [{"title": "Fixture", "url": "https://example.com"}],
            "open_questions": [], "suggested_share": None,
        }}

    result = Explorations(mind).run(
        "fake-kimi", tmp_path / "jobs", "test-v1", runner=runner,
        brief="A selected synthetic question", budget_seconds=1190,
    )
    assert result["state"] == "complete"
    assert calls == ["A selected synthetic question"]
    assert cadence.status()["state"] == "waiting"

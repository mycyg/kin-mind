"""Emotion system v2 (2026-09-27): the dimensions the role profile adds, their groups, and the
migration that brings a state initialized before them up to the profile.

The new dimensions are appraised by the same assessment as the old ones (DeepSeek); nothing here
calls a model. A state kept by an older release gains them once, at their baselines, without
touching a single score, baseline or event it already held.
"""
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from pydantic import ValidationError

from kin_mind import appraisal as A
from kin_mind.expression import TENDENCIES, compile_expression
from kin_mind.profile import DIMENSIONS, GROUPS
from kin_mind.state import AffectiveEvent

from test_kin_mind import event, setup  # noqa: F401  (the synthetic mind fixture)

NEW = ["joy", "contentment", "sadness", "irritability", "protectiveness", "jealousy", "fear", "wonder"]


def as_older_release(mind):
    """The state an older release initialized: the same role, without the dimensions v2 adds."""
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        for key in NEW:
            state["profile"]["dimensions"].pop(key)
            state["dimensions"].pop(key)
        mind._save(conn, state)
    return state


def test_every_dimension_sits_in_exactly_one_group_and_keeps_both_expression_ends():
    members = [key for group in GROUPS.values() for key in group["members"]]
    assert sorted(members) == sorted(DIMENSIONS) and len(members) == len(set(members)) == 28
    assert all(DIMENSIONS[key]["group"] == group for group, spec in GROUPS.items() for key in spec["members"])
    assert [spec["label"] for spec in GROUPS.values()] == ["依恋与亲密", "警觉与保护", "心境与行动", "好奇与创造"]
    labels = [spec["label"] for spec in DIMENSIONS.values()]
    assert len(labels) == len(set(labels))
    # Both kept, apart: 好奇心 is the quick spark, 探索欲 the lasting drive it feeds.
    assert DIMENSIONS["wonder"]["label"] == "好奇心" and DIMENSIONS["curiosity"]["label"] == "探索欲"
    assert set(TENDENCIES) == set(DIMENSIONS) and all(len(ends) == 2 and all(ends) for ends in TENDENCIES.values())


def test_a_new_mind_reads_every_dimension_with_its_group(setup):
    mind, _source, _clock = setup
    view = mind.read()
    assert len(view["dimensions"]) == 28
    assert all(value["group"] == DIMENSIONS[key]["group"] for key, value in view["dimensions"].items())
    assert [g["id"] for g in view["dimension_groups"]] == list(GROUPS)
    assert view["dimension_groups"][1]["members"] == GROUPS["vigilance"]["members"]
    assert view["dimensions"]["wonder"]["value"] == 65 and view["dimensions"]["fear"]["value"] == 8


def test_an_older_state_gains_the_new_dimensions_once_and_keeps_everything_it_held(setup):
    mind, source, clock = setup
    mind.record(event(mind, source, "before-v2", {"mood": 30, "longing": 80}))
    older = as_older_release(mind)
    held = deepcopy(older["dimensions"])
    role_evidence = older["dimensions"]["closeness"]["evidence"]
    clock[0] += timedelta(minutes=5)

    moved = mind.extend_dimensions(agent_version="synthetic-v2")
    assert moved["state"] == "extended" and moved["added"] == NEW and moved["revision"] == older["revision"] + 1
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        kind, row = conn.execute("SELECT kind,data FROM mind_events WHERE scope=? AND revision=?",
                                 (mind.scope.key(), moved["revision"])).fetchone()
    assert kind == "profile-dimensions-added"
    assert {key: state["dimensions"][key] for key in held} == held
    assert state["profile_version"] != older["profile_version"]
    for key in NEW:
        entry, spec = state["dimensions"][key], state["profile"]["dimensions"][key]
        assert spec == DIMENSIONS[key]
        assert entry["score"] == entry["baseline"] == entry["target"] == DIMENSIONS[key]["baseline"]
        assert entry["basis"] == "role_default" and entry["evidence"] == role_evidence
        assert entry["agent_version"] == "synthetic-v2"
    view = mind.read()
    assert view["dimensions"]["longing"]["basis"] == "event_inferred" and view["dimensions"]["joy"]["value"] == 60
    assert not any(view["dimensions"][key]["needs_review"] for key in NEW)

    assert mind.extend_dimensions(agent_version="synthetic-v2") == {"state": "unchanged"}
    assert mind.read()["revision"] == moved["revision"]


def test_an_event_scored_on_an_older_state_adds_the_dimensions_instead_of_failing(setup):
    """Between a release and its start-up migration an assessment may already score a new one."""
    mind, source, _clock = setup
    as_older_release(mind)
    mind.record(event(mind, source, "joy-before-migration", {"joy": 85, "fear": 30, "mood": 70}))
    view = mind.read()
    assert view["dimensions"]["joy"]["value"] == 85 and view["dimensions"]["joy"]["basis"] == "event_inferred"
    assert view["dimensions"]["fear"]["value"] == 30
    assert view["dimensions"]["sadness"]["value"] == 15 and view["dimensions"]["sadness"]["basis"] == "role_default"
    assert len(view["dimensions"]) == 28
    assert mind.extend_dimensions(agent_version="synthetic-v2") == {"state": "unchanged"}


def test_an_unknown_dimension_is_still_refused(setup):
    mind, source, _clock = setup
    with pytest.raises(ValueError, match="Unknown affective dimension"):
        mind.record(event(mind, source, "unknown", {"boredom": 50}))


def test_an_assessment_may_score_every_dimension_at_once():
    every = {key: 50 for key in DIMENSIONS}
    assert A.Appraisal.model_validate({"reason": "全部", "values": every}).values == every
    AffectiveEvent(command_id="all", agent_version="v", expected_revision=1, evidence_ids=["x"], values=every, reason="全部")
    with pytest.raises(ValidationError):
        A.Appraisal.model_validate({"reason": "未知", "values": {"boredom": 50}})


def test_the_new_emotions_reach_the_expression_hints_with_their_guardrails():
    def entry(key, value):
        return {"value": value, "baseline": DIMENSIONS[key]["baseline"], "basis": "event_inferred", "evidence_ids": ["s1"]}
    guidance = compile_expression({"jealousy": entry("jealousy", 85), "fear": entry("fear", 75), "mood": entry("mood", 65)})["guidance"]
    texts = {item["id"]: item["text"] for item in guidance}
    assert set(texts) == {"jealousy", "fear"}
    assert "不盘问" in texts["jealousy"] and "不要求对方交代" in texts["jealousy"] and "不用害怕给对方施压" in texts["fear"]
    # A role default of the new ones stays quiet: no hint at a baseline.
    quiet = compile_expression({key: {**entry(key, DIMENSIONS[key]["baseline"]), "basis": "role_default"} for key in NEW})
    assert [item["id"] for item in quiet["guidance"]] == ["natural"] and quiet["version"] == "expression-v2"


def test_the_host_start_up_brings_an_older_state_up_to_the_profile(tmp_path):
    """`recover` is the host's start-up call: the first start of the release migrates."""
    from eventmem.core import Engine
    from eventmem.core.models import Scope, SourceInput
    from kin_mind.host import dispatch
    from kin_mind.state import Mind

    engine, scope = Engine(tmp_path / "memory"), Scope(persona="synthetic-v2")
    mind = Mind(engine, scope)
    init = engine.receive(SourceInput(namespace="test", key="configuration", scope=scope, text="configuration",
                                      authority="explicit"))["id"]
    mind.initialize(agent_version="test-v1", evidence_ids=[init])
    as_older_release(mind)
    config = {"root": str(tmp_path / "memory"), "scope": scope.model_dump(), "agent_version": "test-v2"}
    assert dispatch(config, "recover", {})["state"] == "recovered"
    with engine.db.connect() as conn:
        state = mind._load(conn)
        kinds = [row[0] for row in conn.execute("SELECT kind FROM mind_events WHERE scope=? ORDER BY revision", (scope.key(),))]
    assert sorted(state["dimensions"]) == sorted(DIMENSIONS) and kinds.count("profile-dimensions-added") == 1
    assert state["dimensions"]["wonder"]["agent_version"] == "test-v2"
    dispatch(config, "recover", {})
    with engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_events WHERE scope=? AND kind='profile-dimensions-added'",
                            (scope.key(),)).fetchone()[0] == 1


def test_migration_keeps_initialization_evidence_after_every_old_dimension_was_scored(setup):
    mind, source, _clock = setup
    older = as_older_release(mind)
    refs = deepcopy(older["dimensions"]["mood"]["evidence"])
    mind.record(event(mind, source, "all-scored", {key: 42 for key in older["dimensions"]}))
    as_older_release(mind)
    mind.extend_dimensions(agent_version="synthetic-v2")
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
    assert all(state["dimensions"][key]["evidence"] == refs for key in NEW)


def test_migration_does_not_revive_unavailable_initialization_evidence(setup):
    mind, source, _clock = setup
    older = as_older_release(mind)
    init_id = older["dimensions"]["mood"]["evidence"][0]["source_id"]
    mind.record(event(mind, source, "all-scored", {key: 42 for key in older["dimensions"]}))
    as_older_release(mind)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (init_id,))
    mind.extend_dimensions(agent_version="synthetic-v2")
    view = mind.read()
    assert all(view["dimensions"][key]["needs_review"] for key in NEW)
    with mind.engine.db.connect() as conn:
        assert all(not mind._load(conn)["dimensions"][key]["evidence"] for key in NEW)


@pytest.mark.parametrize("migrated", [False, True])
def test_reverting_pre_upgrade_evolution_keeps_new_dimensions(setup, monkeypatch, migrated):
    from test_history_layer import evolve, revert
    mind, source, clock = setup
    as_older_release(mind)
    # Produce genuine pre-upgrade history through the old event behavior.
    with monkeypatch.context() as patch:
        patch.setattr(mind, "_ensure_dimensions", lambda *args, **kwargs: [])
        mind.record(event(mind, source, "old-snapshot", {"mood": 65}))
        changed = evolve(mind, source, clock)
    if migrated:
        mind.extend_dimensions(agent_version="synthetic-v2")
        mind.record(event(mind, source, "new-joy", {"joy": 83}))
        with mind.engine.db.connect() as conn:
            held = deepcopy({key: mind._load(conn)["dimensions"][key] for key in NEW})
    revert(mind, source, clock, changed["event_id"])
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
    assert len(state["dimensions"]) == len(state["profile"]["dimensions"]) == 28
    assert state["dimensions"]["curiosity"]["baseline"] == 75
    if migrated:
        assert {key: state["dimensions"][key] for key in NEW} == held
    else:
        assert all(state["dimensions"][key]["score"] == DIMENSIONS[key]["baseline"] for key in NEW)
    assert mind.extend_dimensions(agent_version="synthetic-v2") == {"state": "unchanged"}


def test_migration_preserves_evolved_baseline_and_active_motivation(setup, monkeypatch):
    from test_history_layer import evolve
    mind, source, clock = setup
    as_older_release(mind)
    with monkeypatch.context() as patch:
        patch.setattr(mind, "_ensure_dimensions", lambda *args, **kwargs: [])
        evolve(mind, source, clock)
        request = event(mind, source, "motivated", {"curiosity": 90})
        request = AffectiveEvent.model_validate({**request.model_dump(), "motivations": {
            "curiosity": {"target": 95, "half_life_minutes": 60, "reason": "A live research goal"}}})
        mind.record(request)
    with mind.engine.db.connect() as conn:
        before = deepcopy(mind._load(conn))
    mind.extend_dimensions(agent_version="synthetic-v2")
    with mind.engine.db.connect() as conn:
        after = mind._load(conn)
    assert before["dimensions"]["curiosity"]["baseline"] == 77
    assert before["dimensions"]["curiosity"]["motivation"]
    assert all(after["dimensions"][key] == value for key, value in before["dimensions"].items())

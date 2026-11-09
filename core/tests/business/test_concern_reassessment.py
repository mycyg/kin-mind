"""Existing observations can support revised interpretation without inventing outcomes."""
import pytest

from eventmem.core.db import Conflict
from kin_mind.continuity import ConcernChange, ConcernProposal, ContinuityConfig, OwnerRequest
from kin_mind.memory import MemoryContinuity

pytest_plugins = ("test_kin_mind",)


def seed(setup):
    mind, source, _ = setup
    evidence = source("observation", "Alex has a presentation tomorrow.")
    mind.configure_continuity(ContinuityConfig(command_id="on", agent_version="test",
        expected_revision=mind.read()["revision"], evidence_ids=[evidence],
        features={"concerns": True}, reason="Synthetic setup"))
    made = mind.manage_concern(ConcernChange(command_id="create", agent_version="test",
        expected_revision=mind.read()["revision"], action="create", key="presentation",
        kind="care", content="Alex has a presentation; I plan to ask tonight.", topic="presentation",
        intensity=70, basis="explicit", confidence=0.9, evidence_ids=[evidence], reason="Care"))
    return mind, source, evidence, made["concern_id"]


def change(mind, cid, **values):
    return mind.manage_concern(ConcernChange(command_id="change-" + str(mind.read()["revision"]),
        agent_version="test", expected_revision=mind.read()["revision"], concern_id=cid,
        **{"action": "update", **values}))


def stored(mind, cid):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)["concerns"][cid]


def test_existing_source_corrects_mixed_fact_and_own_plan(setup):
    mind, _, evidence, cid = seed(setup)
    content = "Alex has a presentation; I no longer plan to ask tonight."
    result = change(mind, cid, content=content, evidence_ids=[evidence], reason="Reconsidered my plan")
    assert not result.get("replayed")
    entry = stored(mind, cid)
    assert entry["content"] == content and entry["basis"] == "explicit"
    assert entry["evidence"][0]["source_id"] == evidence
    replay = change(mind, cid, content=content, evidence_ids=[evidence], reason="Reconsidered my plan")
    assert replay["replayed"] and replay["concern_revision"] == result["concern_revision"]


def test_intensity_and_reason_retain_existing_provenance_without_new_citation(setup):
    mind, _, evidence, cid = seed(setup)
    assert not change(mind, cid, intensity=25, reason="Less worried").get("replayed")
    assert not change(mind, cid, reason="I can leave this for later").get("replayed")
    entry = stored(mind, cid)
    assert entry["intensity"] == 25 and entry["evidence"][0]["source_id"] == evidence
    assert not change(mind, cid, action="ease").get("replayed")
    assert change(mind, cid, action="ease", reason="Still easing")["replayed"]
    assert not change(mind, cid, action="archive").get("replayed")


@pytest.mark.parametrize("action", ["update", "ease", "archive"])
def test_fact_changes_cannot_use_attention_only_evidence_exemption(setup, action):
    mind, _, _, cid = seed(setup)
    with pytest.raises(Conflict, match="current source evidence"):
        change(mind, cid, action=action, content="Alex passed the presentation.", intensity=10)
    assert stored(mind, cid)["status"] == "active"


def test_old_source_cannot_resolve_or_claim_owner_acceptance(setup):
    mind, _, evidence, cid = seed(setup)
    with pytest.raises(Conflict, match="new outcome"):
        change(mind, cid, action="resolve", evidence_ids=[evidence])
    request = OwnerRequest(kind="invitation", action="Discuss presentation", reason="Care",
                           completion="Conversation", status="accepted")
    with pytest.raises(Conflict, match="new explicit owner response"):
        change(mind, cid, owner_request=request, evidence_ids=[evidence])
    with pytest.raises(Conflict, match="internal thought"):
        change(mind, cid, basis="internal_thought", evidence_ids=[evidence])
    with pytest.raises(Conflict, match="shared plan"):
        change(mind, cid, kind="shared_plan", evidence_ids=[evidence])


def test_retiming_cannot_smuggle_uncited_content(setup):
    mind, _, _, cid = seed(setup)
    MemoryContinuity(mind).configure({"timed_concerns": True})
    proposal = ConcernProposal(action="update", concern_id=cid, surface_after="2030-01-01T10:00:00Z",
                               content="Alex passed the presentation.")
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict, match="current source evidence"):
        mind._apply_concern(conn, mind._load(conn), proposal, "timing", "test")


def test_reassessment_citations_still_must_be_in_appraisal(setup):
    mind, source, evidence, cid = seed(setup)
    other = source("other", "A different observation.")
    proposal = ConcernProposal(action="update", concern_id=cid, content="Revised interpretation.",
                               evidence_ids=[evidence])
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict, match="outside this appraisal"):
        mind._apply_concern(conn, mind._load(conn), proposal, "bounds", "test",
                            allowed=mind._evidence(conn, [other]))


def test_new_outcome_still_resolves(setup):
    mind, source, _, cid = seed(setup)
    outcome = source("outcome", "Alex finished the presentation.")
    result = change(mind, cid, action="resolve", evidence_ids=[outcome])
    assert not result.get("replayed") and stored(mind, cid)["status"] == "resolved"


def test_attention_inherits_slim_provenance_without_losing_authority(setup):
    from kin_mind.evidence_refs import slim
    mind, _, evidence, cid = seed(setup)
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        slim(state)
        mind._save(conn, state)
    assert "authority" not in stored(mind, cid)["evidence"][0]
    assert not change(mind, cid, intensity=20).get("replayed")
    assert stored(mind, cid)["evidence"][0]["source_id"] == evidence


@pytest.mark.parametrize("action", ["update", "ease", "archive"])
def test_subjective_attention_can_change_after_an_old_citation_expires(setup, action):
    from datetime import timedelta
    mind, source, clock = setup
    evidence = source("passing-thought", "A garden reminded me of tea.")
    mind.configure_continuity(ContinuityConfig(command_id="subjective-on", agent_version="test",
        expected_revision=mind.read()["revision"], evidence_ids=[source("subjective-config")],
        features={"concerns": True}, reason="Synthetic setup"))
    made = mind.manage_concern(ConcernChange(command_id="subjective-create", agent_version="test",
        expected_revision=mind.read()["revision"], action="create", key="tea-thought",
        kind="care", content="I feel like thinking about tea.", topic="tea", intensity=70,
        basis="internal_thought", confidence=0.9, evidence_ids=[evidence]))
    cid = made["concern_id"]
    clock[0] += timedelta(seconds=1)
    source("passing-thought", "A revised observation about the garden.", version="2")
    with mind.engine.db.connect() as conn:
        assert not mind._fresh(conn, stored(mind, cid)["evidence"])
    result = change(mind, cid, action=action, intensity=20, reason="My attention has moved on.")
    assert not result.get("replayed")
    entry = stored(mind, cid)
    assert entry["intensity"] == 20 and entry["evidence"] == []
    assert entry["basis"] == "internal_thought"
    assert entry["status"] == {"update": "active", "ease": "easing", "archive": "archived"}[action]

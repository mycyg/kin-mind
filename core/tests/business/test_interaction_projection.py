"""The interaction projection of the state (`read` with `projection: "interaction"`, 2026-09-28).

A contact draft read the whole state view through the resident worker: every wish that ever
finished, with its evidence, the findings and the appraisal queue. On 2026-09-28 that answer was
2.79M characters, past the worker's frame guard, and every draft failed before it started. The
draft now reads the projection: what the old host's `interactionView` rendered for Kin, at its
bounds, and what the rendered words were written from -- nothing else. The ids the projection names are
the ids of what Kin was shown (`shown_ids`, CL6D-MM-01): a finished wish, a wish past the render's
bound, a dimension's evidence behind an expression are not named any more."""
import json

import pytest

from eventmem.core.db import NAMED, digest
from kin_mind.host import dispatch
from kin_mind.interaction_projection import INTERACTION_LIMITS, PROJECTION, interaction_projection
from kin_mind.state import DesireChange
from test_fork_reads import fork_receipt
from test_kin_mind import wish

pytest_plugins = ('test_kin_mind',)


def named(value):
    return set(NAMED.findall(json.dumps(value, ensure_ascii=False)))


def evidence_ids(desire):
    return {v for ref in desire["evidence"] for v in (ref["record_id"], ref["source_id"])}


@pytest.fixture
def lived(setup):
    """A state with history: two finished wishes, eighteen still wanted -- two more than the render
    shows -- and one of them waiting with a reason Kin wrote from what her draft was shown."""
    mind, source, clock = setup
    shown_source = source("shown-note", "a note the memory context handed her")
    shown_record = "mem_" + digest([shown_source, "root"])[:32]
    finished = []
    for key, action in (("done-wish", "complete"), ("dropped-wish", "abandon")):
        finished.append(wish(mind, source, key, content="Finished " + key)["desire_id"])
        mind.manage_desire(DesireChange(command_id=key + "-end", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
                                        action=action, desire_id=finished[-1], evidence_ids=[source(key + "-end")], reason="Finished"))
    for i in range(18):
        wish(mind, source, f"active-{i}", strength=50 + i, content=f"Still wanted {i}")
    attempt = mind.claim_contact(owner_epoch="owner-1")
    waiting = attempt["desire_ids"][0]
    mind.settle_contact(attempt_id=attempt["id"], state="canceled", reason="draft-decision", desire_ids=[waiting],
                        decision={"action": "wait", "condition": "owner_reply", "reason": "Wait for her"},
                        shown_ids=[shown_source, shown_record], draft_receipt=fork_receipt())
    return mind, source, finished, waiting, {shown_source, shown_record}


def test_the_projection_is_what_the_render_shows_and_what_its_words_rest_on(lived):
    mind, _source, finished, waiting, shown = lived
    view = mind.read(query="Still wanted")
    projection = interaction_projection(view)
    active = [d for d in view["desires"] if not d["expired"] and d["status"] in ("wanted", "waiting", "in_progress")]
    assert len(active) == 18 and {d["id"] for d in view["desires"]} >= set(finished)
    kept = [d["id"] for d in projection["desires"]]
    assert kept == [d["id"] for d in active[-INTERACTION_LIMITS["desires"]:]], "the newest sixteen still wanted, in order"
    assert not set(finished) & set(kept) and waiting in kept
    assert view["expression"] is None, "continuity is not configured in this store"
    # Named: every rendered wish's evidence, and what the waiting wish's copied reason rests on, which
    # the render strips; no finished wish, no wish past the bound, nothing of what else the view holds.
    ids = named(projection)
    assert all(evidence_ids(d) <= named(shown_wish) for d, shown_wish in zip(active[-16:], projection["desires"])), \
        "each rendered wish carries what its words were written from"
    assert shown <= ids, "the reason's sources are named"
    assert not any(evidence_ids(d) & ids for d in view["desires"] if d["id"] in finished)
    assert not any(evidence_ids(d) - named(active[-16:]) & ids for d in active[:-16])
    # No expression: the dimensions' reasons are rendered, so what each was written from is named beside it.
    for key, entry in view["dimensions"].items():
        assert projection["dimensions"][key]["reason_evidence_ids"] == entry["evidence_ids"]
        assert set(projection["dimensions"][key]) == {"value", "basis", "needs_review", "reason", "undertone", "reason_evidence_ids"}
    # And none of the rest of the view.
    assert not {"findings", "appraisals", "concerns", "action_events", "session_advice", "autonomy", "trait_ledger",
                "dimension_groups", "exploration", "action_policy", "history"} & set(projection)


def test_behind_an_expression_no_dimension_reason_is_shown_and_none_of_its_evidence_is_named(lived):
    mind, *_ = lived
    view = mind.read()
    expression = {"version": "v", "guidance": [{"text": f"g{i}"} for i in range(5)], "concern_ids": [], "fingerprint": "f"}
    shown = {**view, "expression": expression, "continuity": {**view["continuity"], "activation": "active"}}
    projection = interaction_projection(shown)
    assert all("reason_evidence_ids" not in d for d in projection["dimensions"].values())
    assert not named(projection["dimensions"])
    assert len(projection["expression"]["guidance"]) == INTERACTION_LIMITS["guidance"]
    # In shadow the expression is not shown, and the reasons and their sources are again.
    shadow = {**shown, "continuity": {**shown["continuity"], "activation": "shadow"}}
    projected = interaction_projection(shadow)
    assert all("reason_evidence_ids" in d for d in projected["dimensions"].values())


def test_the_read_action_answers_the_projection_bounded_and_refuses_what_it_does_not_know(lived):
    mind, *_ = lived
    config = {"root": str(mind.engine.db.root), "scope": mind.scope.model_dump()}
    full = dispatch(config, "read", {"query": "Still wanted"})
    answer = dispatch(config, "read", {"query": "Still wanted", "projection": PROJECTION})
    assert set(answer) == {"projection", "state"} and answer["projection"] == PROJECTION
    assert answer["state"]["projection"] == PROJECTION and len(answer["state"]["desires"]) == INTERACTION_LIMITS["desires"]
    assert answer["state"]["contact"] == full["state"]["contact"]
    assert set(answer["state"]["interaction_timing"]) == set(full["state"]["interaction_timing"])
    # Here eighteen wishes still wanted are most of the state; on the store of 2026-09-28 the whole
    # answer was 1,268,933 characters after 139 wishes were moved out, and this one 36,302.
    assert len(json.dumps(answer, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False))
    assert not {d["id"] for d in full["state"]["desires"] if d["status"] in ("completed", "abandoned")} & {d["id"] for d in answer["state"]["desires"]}
    for bad in ({"projection": "everything"}, {"projection": PROJECTION, "history": 3}):
        with pytest.raises(ValueError, match="Unknown state projection"):
            dispatch(config, "read", bad)
    # Without it, the whole view as it always was.
    assert {"state", "findings", "appraisals"} <= set(full)


def test_concern_time_survives_selection_memory_and_real_host_projection(setup):
    from datetime import datetime, timedelta, timezone
    from kin_mind.context import affect_projection
    from kin_mind.continuity import ConcernProposal
    from test_companion_continuity import concerns_on, create, propose
    mind, source, clock = setup
    clock[0] = datetime(2030, 10, 14, 17, 2, 0, 123456, tzinfo=timezone.utc)
    concerns_on(mind, source)
    cid, _ = create(mind, source, clock, 'synthetic-activity')
    clock[0] += timedelta(minutes=5)
    evidence = source('later-source', 'synthetic observation')
    clock[0] += timedelta(minutes=2)
    propose(mind, 'new-assessment', ConcernProposal(action='update', concern_id=cid, reason='synthetic assessment',
        intensity=65, evidence_ids=[evidence]), evidence)
    clock[0] += timedelta(hours=3)
    view = mind.read()
    concern = next(c for c in view['selected_concerns'] if c['id'] == cid)
    assert concern['created_at'] == '2030-10-14T17:03:00.123456+00:00'
    assert concern['created_local_time'] == '2030-10-15T01:03:00.123456+08:00'
    assert concern['updated_at'] == concern['assessed_at'] == '2030-10-14T17:10:00.123456+00:00'
    assert concern['updated_local_time'] == '2030-10-15T01:10:00.123456+08:00'
    assert concern['latest_evidence_time'] == {'source_id':evidence, 'occurred_at':'2030-10-14T17:08:00.123456+00:00',
        'local_time':'2030-10-15T01:08:00.123456+08:00', 'timezone':'Asia/Singapore'}
    memory = affect_projection(view)['concerns'][0]
    for shown in (memory, interaction_projection(view)['selected_concerns'][0]):
        for field in ('created_at','updated_at','assessed_at','created_local_time','updated_local_time','timezone','latest_evidence_time','temporal_note'):
            assert shown[field] == concern[field]
        assert '不证明用户此刻仍在进行' in shown['temporal_note']
    # Presentation only: no guessed end state or display fields are persisted into the concern.
    with mind.engine.db.connect() as conn:
        stored = mind._load(conn)['concerns'][cid]
        assert stored['status'] == 'active' and 'temporal_note' not in stored and 'latest_evidence_time' not in stored

"""The interaction projection of the state (`read` with `projection: "interaction"`, 2026-09-28).

A contact draft read the whole state view through the resident worker: every wish that ever
finished, with its evidence, the findings and the appraisal queue. On 2026-09-28 that answer was
2.79M characters, past the worker's frame guard, and every draft failed before it started. The
draft now reads the projection: what `interactionView` (adapters/owner-host.mjs) renders for Kin,
at its bounds, and what the rendered words were written from -- nothing else. `interactionView`
over the projection is `interactionView` over the full view, and the ids the projection names are
the ids of what Kin was shown (`shown_ids`, CL6D-MM-01): a finished wish, a wish past the render's
bound, a dimension's evidence behind an expression are not named any more."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from eventmem.core.db import NAMED, digest
from kin_mind.host import dispatch
from kin_mind.interaction_projection import INTERACTION_LIMITS, PROJECTION, interaction_projection
from kin_mind.state import DesireChange
from test_fork_reads import fork_receipt
from test_kin_mind import wish

pytest_plugins = ('test_kin_mind',)

ADAPTER = Path(__file__).resolve().parents[2] / "adapters" / "owner-host.mjs"


def named(value):
    return set(NAMED.findall(json.dumps(value, ensure_ascii=False)))


def evidence_ids(desire):
    return {v for ref in desire["evidence"] for v in (ref["record_id"], ref["source_id"])}


def rendered(*views):
    """`interactionView` of each view, as the host renders it for a draft."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs node to run adapters/owner-host.mjs")
    script = ("import {interactionView} from " + json.dumps(ADAPTER.as_uri()) + ";let t='';process.stdin.on('data',c=>t+=c);"
              "process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(t).map(v=>interactionView(v)))));")
    answer = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(list(views)),
                            capture_output=True, text=True, timeout=60)
    assert answer.returncode == 0, answer.stderr[-2000:]
    return json.loads(answer.stdout)


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
    # The render over it is the render over the whole view, key for key -- with an expression shown
    # and, below, without one.
    assert view["expression"] is None, "continuity is not configured in this store"
    whole, part = rendered(view, projection)
    assert whole == part
    assert [d["id"] for d in part["desires"]] == kept
    # Named: every rendered wish's evidence, and what the waiting wish's copied reason rests on, which
    # the render strips; no finished wish, no wish past the bound, nothing of what else the view holds.
    ids = named(projection)
    assert all(evidence_ids(d) <= ids for d in active[-16:])
    assert shown <= ids and not (shown & named(part)), "the reason's sources are named, not rendered"
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
    whole, part = rendered(shown, projection)
    assert whole == part and all("reason" not in d for d in part["dimensions"].values())
    # In shadow the expression is not shown, and the reasons and their sources are again.
    shadow = {**shown, "continuity": {**shown["continuity"], "activation": "shadow"}}
    projected = interaction_projection(shadow)
    assert all("reason_evidence_ids" in d for d in projected["dimensions"].values())
    whole, part = rendered(shadow, projected)
    assert whole == part and part["expression"] is None


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


def test_the_bounds_are_the_render_s_own():
    """kin_mind.interaction_projection mirrors INTERACTION_LIMITS of adapters/owner-host.mjs."""
    node = shutil.which("node")
    if not node:
        pytest.skip("needs node to read adapters/owner-host.mjs")
    script = "import {INTERACTION_LIMITS} from " + json.dumps(ADAPTER.as_uri()) + ";console.log(JSON.stringify(INTERACTION_LIMITS));"
    answer = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert answer.returncode == 0, answer.stderr[-2000:]
    limits = json.loads(answer.stdout)
    assert {key: INTERACTION_LIMITS[key] for key in limits} == limits

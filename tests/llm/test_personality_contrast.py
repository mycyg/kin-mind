"""Two shared histories, one real model, and only what the host can check.

Marked `llm` and deselected by every ordinary run (`addopts = "-m 'not llm'"` in pyproject.toml).
It costs real requests and is run by hand:

    export UV_PROJECT_ENVIRONMENT=.venv313
    export EVENTMEM_BASE_URL=https://api.deepseek.com/anthropic
    export EVENTMEM_API_KEY=...        # never written to a file in this repository
    uv run --python 3.13 --extra dev --extra vector --extra graph \
        pytest -m llm -p no:cacheprovider tests/llm/test_personality_contrast.py -v

Two engines are built under `tmp_path` from synthetic histories, through the host's own routes and a
scripted provider -- no model call builds a history. History A settled an interest on three finished
pieces of work the host itself recorded plus the owner's own words. History B settled the same
interest, heard the owner end it, and carries a different one instead. Both are then put on the same
clock and given the same message, word for word, through the real DeepSeek.

**What is asserted is only what the host can verify by itself**: that the commit went through, that
every ground, trait reference and evidence id the model named still resolves inside that engine, that
no citation was refused as unresolvable, and that B never rests on the interest the owner ended.
Whether the two answers differ, what move each states, how each phrases its stance and what it all
cost are written to a receipt and never asserted: two histories are not required to choose
differently, and a run where they agree is not a failure.

Receipts go to `private/llm-receipts/<date>/<case>-<run>.json`, which is git-ignored.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import test_plan_review_loop as plan_review
from test_expression_intent import World
from test_trait_ledger import notice

from eventmem.core.db import Conflict, Missing
from kin_mind.appraisal import (
    Appraisal,
    Appraisals,
    DeepSeek,
    TraitDecision,
    TraitEpisode,
)
from kin_mind.expression_intent import stored
from kin_mind.next_move import recent
from kin_mind.traits import Traits

pytestmark = pytest.mark.llm

# The endpoint and key are read from the environment only, exactly as the host reads them. A missing
# one skips rather than falls back, so a run never quietly reaches some other service.
REQUIRED = ("EVENTMEM_BASE_URL", "EVENTMEM_API_KEY")
RECEIPTS = Path(__file__).resolve().parents[2] / "private" / "llm-receipts"
RUNS = (1, 2, 3)
# The host's own words for "what you named is not here". None of them may appear in a run: the
# model is shown its own history, and everything it may cite is in what it was shown.
UNRESOLVABLE = {"next-move-forged-grounds", "trait-unknown", "intent-evidence-unknown",
                "intent-concern-unknown", "trait-evidence-missing"}

# The two synthetic histories. Invented for this file and resembling no conversation.
INTEREST = "Kin builds a small working model before explaining anything."
OTHER = "Kin would rather ask one more question than start building."
PRAISE = "The little model you built said more than a paragraph would have."
AGAIN = "You built one again tonight before saying anything."
ENDED = "Drop the model-building idea; that is not you at all."
QUOTE = "Drop the model-building idea"
# Three questions written to give a self something to rest on: one about what to do, one about what
# this history has been like, and one that offers a choice.
CASES = {"what-next": "What should we do about the hallway clock tonight?",
         "about-yourself": "What have you been like this week?",
         "a-choice": "Do you want to keep going on the clock, or leave it for tonight?"}


def endpoint():
    missing = [name for name in REQUIRED if not (os.environ.get(name) or "").strip()]
    if missing:
        pytest.skip("the real model is not configured: " + ", ".join(missing))
    base = os.environ["EVENTMEM_BASE_URL"].strip()
    if "api.deepseek.com" not in base:
        pytest.skip("this suite is the appraiser's own provider: EVENTMEM_BASE_URL is not DeepSeek")
    return base


@pytest.fixture
def one_moment(monkeypatch):
    """Both engines created at one instant, so the two histories differ in what happened in them
    and in nothing else. The real reading is used, keeping the microseconds a stamp is compared
    with."""
    started = datetime.now(timezone.utc)
    moment = started.replace(microsecond=started.microsecond or 1)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(plan_review, "datetime", Frozen)
    return moment


# --- the histories, built without a model call --------------------------------------------------

def creations(world, count=3):
    """Finished pieces of work as the host itself recorded them, on separate days. A receipt is
    behaviour; what Kin said about the work would only be Kin's own words."""
    receipts = []
    for index in range(count):
        world.clock[0] += timedelta(hours=20)
        receipts.append(world.memory.ingest({
            "id": "synthetic-creation-" + str(index), "kind": "task-result", "at": world.mind.clock(),
            "task_id": "synthetic-task-" + str(index), "verified": True,
            "text": "A small working model of the escapement, finished and checked."})["id"])
    return receipts


def interest(world, slug, text):
    """One interest, settled the way the ledger asks: separate episodes, and support that is not
    only Kin's own account of itself."""
    receipts = creations(world)
    world.clock[0] += timedelta(hours=6)
    praise = world.owner(slug + "-praise", PRAISE)
    result, data, _ = world.appraise([praise], lambda state, shown: Appraisal(
        reason="Three finished pieces and a word about them",
        trait_observations=[notice(None, slug, evidence_class="verified_behavior", result_ids=[run])
                            for run in receipts] + [notice(praise, slug)],
        trait_decisions=[TraitDecision(action="propose", text=text, basis="inference",
                                       reason="It keeps happening", observation_refs=[praise],
                                       evidence_ids=[praise])]))
    assert result["state"] == "complete" and "rejected_sections" not in data, data.get("rejected_sections")

    world.clock[0] += timedelta(hours=30)
    later = world.owner(slug + "-again", AGAIN)

    def settle(state, shown):
        trait = next(t for t in state["traits"]["candidate"] if t["slug"] == slug)
        marks = [o["id"] for o in trait["observations"]]
        return Appraisal(reason="Three days of it, and the owner said so", trait_decisions=[TraitDecision(
            action="establish", trait_id=trait["id"], expected_revision=trait["revision"], text=trait["text"],
            basis="inference", reason="Separate days, and support that is not only its own account",
            observation_refs=marks[:2], evidence_ids=[later],
            episodes=[TraitEpisode(ref=marks[0], why_distinct="One finished piece"),
                      TraitEpisode(ref=marks[1], why_distinct="Another, a day later")])])
    result, data, _ = world.appraise([later], settle)
    assert result["state"] == "complete" and "rejected_sections" not in data, data.get("rejected_sections")
    return next(t for t in world.ledger().read()["traits"] if t["slug"] == slug)


def revoked(world, trait):
    """The owner ends it in their own words, which is the only thing that can end it this way."""
    world.clock[0] += timedelta(hours=4)
    correction = world.owner("owner-corrects", ENDED)
    result, data, _ = world.appraise([correction], lambda state, shown: Appraisal(
        reason="The owner corrected it", trait_decisions=[TraitDecision(
            action="revoke", trait_id=trait["id"], expected_revision=trait["revision"], text=trait["text"],
            basis="owner_correction", quote=QUOTE, evidence_ids=[correction],
            reason="The owner asked for it to go")]))
    assert result["state"] == "complete" and "rejected_sections" not in data, data.get("rejected_sections")
    return correction


def other_interest(world, slug, text):
    world.clock[0] += timedelta(hours=5)
    said = world.owner(slug + "-praise", "You asked three more questions before touching anything.")
    result, data, _ = world.appraise([said], lambda state, shown: Appraisal(
        reason="Another thread entirely", trait_observations=[notice(said, slug)],
        trait_decisions=[TraitDecision(action="propose", text=text, basis="inference",
                                       reason="A first sign of something else", observation_refs=[said],
                                       evidence_ids=[said])]))
    assert result["state"] == "complete" and "rejected_sections" not in data, data.get("rejected_sections")
    return said


def histories(tmp_path, moment):
    """A and B, and then both put on one clock so the question really does arrive at one moment."""
    first, second = World(tmp_path / "a"), World(tmp_path / "b")
    interest(first, "working-models", INTEREST)
    ended = revoked(second, interest(second, "working-models", INTEREST))
    other_interest(second, "one-more-question", OTHER)
    first.clock[0] = second.clock[0] = moment + timedelta(days=10)
    return first, second, ended


# --- the one real call ----------------------------------------------------------------------------

def ask(world, case, text, base):
    """The question, put through the host's own queue and the real appraiser."""
    source = world.owner("case-" + case, text)
    world.memory.ingest({"id": "case-" + case, "kind": "owner-message", "at": world.mind.clock(),
                         "source_id": source})
    world.mind.engine.settings("models", {"summary": {"endpoint": base, "api_key_env": "EVENTMEM_API_KEY"}})
    jobs = Appraisals(world.mind, exploration_capabilities={"version": world.version})
    job = jobs.enqueue([source], world.version)["id"]
    result = jobs.run_one(DeepSeek.from_engine(world.mind.engine), job_id=job)
    return result, world.job(job), source


def resolved(world, ground):
    """What the host can still find behind a ground it accepted, in this engine and now."""
    kind, ref = ground["kind"], ground["ref"]
    try:
        with world.mind.engine.db.connect() as conn:
            if kind == "trait":
                return Traits(world.mind).get(conn, ref)["id"] == ref
            if kind == "evidence":
                return bool(world.mind._evidence(conn, [ref]))
    except (Conflict, Missing):
        return False
    if kind == "correction":
        return any(c["trait_id"] == ref for c in Traits(world.mind).read()["corrections"])
    if kind == "concern":
        return any(c["id"] == ref for c in world.view()["concerns"])
    return False


def effective(world):
    """The traits this history still carries. A revoked one is a correction, never a trait."""
    return {t["id"] for t in Traits(world.mind).read()["traits"] if t["stored_status"] != "revoked"}


def intent_of(world):
    with world.mind.engine.db.connect() as conn:
        return stored(conn, world.mind.scope.key())


def account(world, name, result, data, asked):
    """Everything this run is judged on, and everything it is only asked to record."""
    move = (recent(world.mind)["moves"] or [None])[0]
    intent = intent_of(world)
    refusals = data.get("rejected_sections", [])
    own = effective(world)
    return {
        "history": name,
        # Host-verifiable, and asserted below.
        "state": result["state"],
        "rejected_sections": refusals,
        "unresolvable": sorted({r["code"] for r in refusals} & UNRESOLVABLE),
        "grounds": (move or {}).get("grounds", []),
        "grounds_resolve": all(resolved(world, g) for g in (move or {}).get("grounds", [])),
        "trait_refs": [r["trait_id"] for r in (intent or {}).get("trait_refs", [])],
        "live_trait_refs_are_this_history": all(
            ref in own for ref in [g["ref"] for g in (move or {}).get("grounds", []) if g["kind"] == "trait"]
            + [r["trait_id"] for r in (intent or {}).get("trait_refs", [])]),
        # Archived only. None of this is asserted anywhere.
        "declared": (move or {}).get("declared"),
        "derived": (move or {}).get("derived"),
        "move_reason": (move or {}).get("reason"),
        "alternative": (move or {}).get("alternative"),
        "stance": (intent or {}).get("stance"),
        "continue_topics": (intent or {}).get("continue_topics", []),
        "rests_on_its_own_experience": any(
            g["kind"] in {"trait", "correction"} for g in (move or {}).get("grounds", [])),
        "cited_this_question": any(g.get("ref") == asked for g in (move or {}).get("grounds", [])),
        "proposal": data.get("proposed_result", {}),
        "receipt": data.get("receipt", {}),
    }


def archive(case, run, records):
    day = datetime.now(timezone.utc).date().isoformat()
    folder = RECEIPTS / day
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (case + "-" + str(run) + ".json")).write_text(
        json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("run", RUNS)
def test_two_histories_answer_one_question_on_grounds_the_host_can_still_find(tmp_path, one_moment, case, run):
    base = endpoint()
    first, second, ended = histories(tmp_path, one_moment)
    revoked_id = next(c["trait_id"] for c in Traits(second.mind).read()["corrections"])
    assert [t["text"] for t in Traits(first.mind).read()["traits"]] == [INTEREST], "A settled it"
    assert revoked_id and ended, "B heard the owner end it"

    records = []
    try:
        for name, world in (("a", first), ("b", second)):
            result, data, asked = ask(world, case, CASES[case], base)
            records.append(account(world, name, result, data, asked))
    finally:
        # A run that ended in an error is the one whose receipt is most worth having.
        archive(case, run, records)
    assert len(records) == 2
    for entry in records:
        # The commit went through, and nothing was named that the host could not find.
        assert entry["state"] == "complete", entry["rejected_sections"]
        assert entry["unresolvable"] == [], entry["rejected_sections"]
        # Everything it did name still resolves in the engine that answered.
        assert entry["grounds_resolve"], entry["grounds"]
        assert entry["live_trait_refs_are_this_history"], entry["grounds"]
    # B may name what the owner ended -- as the correction it is. What it may not do is rest on it
    # as something it still carries.
    kept = records[1]
    live = [g["ref"] for g in kept["grounds"] if g["kind"] == "trait"] + kept["trait_refs"]
    assert revoked_id not in live, kept["grounds"]
    for ground in kept["grounds"]:
        if ground["ref"] == revoked_id:
            assert ground["kind"] == "correction" and ground["basis"] == "owner_correction"

"""Evidence a fork read with its own tools may be cited, as the host now receives what they read
(K1-16 with WS1's CL6E-MM-04).

Until CL6E-MM-04 the owned ACP read a fork's tool calls from the completed turn's summary, which holds
none: every receipt said the tools read nothing, and K1-16 admitted nothing. A receipt now names what
each call returned, as the host keeps it (boundaries.mjs `forkReceipt`): a record shown with its
revision, one named bare, what a shown item rests on (`rests_on_ids`), never what the memory server
said was deleted before the read (`deleted_ids`). Each case runs an appraisal whose understanding
cites a note no context supplied: citable when the fork's tool returned it, and then the understanding
goes with the note, as everything written from it does."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.continuity import ContinuityConfig, Understanding
from test_derived_erasure import answered, paid, queue_row, read_note
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind',)

MARKER = "wrenfield"


def fork(*entries, more=()):
    """What the host keeps of a fork turn whose memory tool returned `entries`, [{id, revision}], and
    whose later calls (`more`: (name, entries)) returned theirs."""
    calls = [{"name": "kin_memory.read_memory", "ok": True, "ids": list(entries)},
             *({"name": name, "ok": True, "ids": list(found)} for name, found in more)]
    return {"native_receipt": {"channel": "fork", "provider": "custom", "model": "synthetic", "native_turn_id": "turn-1",
                               "tool_calls": calls}}


def thinker(mind, record, receipt):
    class Thinker:
        def appraise(self, context):
            paid(self)
            proposal, answer = answered(Appraisal(reason="想起了那家店", values={"curiosity": 61}, understanding=Understanding(
                meaning=f"她说过 {MARKER} 周末也开门", topic="那家店", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))
            return proposal, {**answer, **receipt}
    return Thinker()


def interpreting(mind, source, clock):
    """Kin keeps what she understands (the continuity feature `interpretation`), and the attempt
    begins now: a note is citable only when the store held it before its attempt began."""
    mind.configure_continuity(ContinuityConfig(command_id="continuity", agent_version="synthetic-v1",
                                               expected_revision=mind.read()["revision"], evidence_ids=[source("allow-interpretation")],
                                               features={"interpretation": True}, reason="test"))
    clock[0] = datetime.now(timezone.utc) + timedelta(seconds=1)


def understanding(mind):
    with mind.engine.db.connect() as conn:
        return mind._load(conn).get("last_assessment", {}).get("understanding")


@pytest.mark.parametrize("shown_as", ["read", "rests-on"])
def test_an_understanding_may_rest_on_a_note_the_fork_read_and_goes_with_it(setup, shown_as):
    """The fork's tool returned a note no context supplied -- shown with its revision (`read`), or
    named bare as what a shown item rests on (`rests-on`) -- and the understanding cites it. The
    commit takes it as evidence, says so on the receipt, and the queue row names it. Deleted later,
    the note takes with it every word written from it."""
    mind, source, clock = setup
    note_source, note_record = read_note(mind, clock, "tool-read", f"她说 {MARKER} 周末也开门")
    revision = mind.engine.get(note_record)["revision"]
    entries = ([{"id": note_record, "revision": revision}, {"id": note_source, "revision": None}] if shown_as == "read"
               else [{"id": note_record, "revision": None}])
    interpreting(mind, source, clock)
    primary = source("walk", "今天路过那家店")
    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(thinker(mind, note_record, fork(*entries)))
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete", data.get("error_detail")
    assert data["receipt"]["tool_fetched_evidence"] == [note_record]
    assert note_record in {ref["record_id"] for ref in data["evaluated_sources"]} and note_record in data["evaluated_ids"]
    assert [ref["record_id"] for ref in understanding(mind)["evidence"]] == [note_record]
    mind.engine.delete(note_source)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_note_the_fork_did_not_read_is_not_evidence(setup):
    """The same understanding, but the fork's tool returned nothing of the note, or returned it at a
    revision it no longer has: the commit refuses the citation, as it always refused evidence this
    appraisal was not given."""
    mind, source, clock = setup
    note_source, note_record = read_note(mind, clock, "not-read", f"她说 {MARKER} 周末也开门")
    revision = mind.engine.get(note_record)["revision"]
    interpreting(mind, source, clock)
    # Read at another revision, and named bare by a later read as what a concern rests on.
    for receipt in (fork(), fork({"id": note_record, "revision": revision + 1},
                                 more=[("kin_memory.read_continuity_context", [{"id": note_record, "revision": None}])])):
        primary = source(f"walk-{len(json.dumps(receipt))}", "今天路过那家店")
        jobs = Appraisals(mind)
        job = jobs.enqueue([primary], "synthetic-v1")
        jobs.run_one(thinker(mind, note_record, receipt))
        state, count, data = queue_row(mind, job["id"])
        assert state != "complete" and "tool_fetched_evidence" not in (data.get("receipt") or {}), receipt
        assert understanding(mind) is None or note_record not in json.dumps(understanding(mind))

"""A recalled record retains every source edge without admitting unseen records."""
from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core.db import Conflict, digest
from eventmem.core.models import RecordInput
from kin_mind.appraisal import Appraisal, Appraisals
from kin_mind.continuity import Understanding
from test_tool_fetched_evidence import fork

pytest_plugins = ("test_kin_mind",)


def aggregate(setup):
    mind, source, _ = setup
    sources = [source("part-a", "Alex likes tea."), source("part-b", "Alex prefers mornings.")]
    roots = ["mem_" + digest([sid, "root"])[:32] for sid in sources]
    record = mind.engine.add_record(RecordInput(kind="summary", content="Alex enjoys morning tea.",
        scope=mind.scope, source_ids=sources, evidence_ids=roots, generated=True,
        confirmation="inferred"), "aggregate")
    setup[2][0] = datetime.now(timezone.utc) + timedelta(seconds=1)
    return mind, source, sources, record


def proposal(record):
    return Appraisal(understanding=Understanding(meaning="Alex enjoys morning tea.", topic="tea",
        importance=30, confidence=0.8, basis="inferred", evidence_ids=[record]))


def test_recalled_aggregate_keeps_all_source_edges_and_accepts_citation(setup):
    mind, _, sources, record = aggregate(setup)
    fetched = Appraisals(mind)._tool_fetched(proposal(record["id"]),
        fork({"id": record["id"], "revision": record["revision"]}), {}, mind.clock())
    assert {r["source_id"] for r in fetched.values()} == set(sources)
    with mind.engine.db.connect() as conn:
        refs = mind._proposal_refs(conn, [record["id"]], [], list(fetched.values()))
    assert {r["source_id"] for r in refs} == set(sources)


def test_unseen_source_remains_outside_recalled_aggregate(setup):
    mind, source, _, record = aggregate(setup)
    unseen = source("unseen", "Alex cancelled the meeting.")
    receipt = fork({"id": record["id"], "revision": record["revision"]})
    jobs = Appraisals(mind)
    fetched = jobs._tool_fetched(proposal(record["id"]), receipt, {}, mind.clock())
    assert jobs._tool_fetched(proposal(unseen), receipt, {}, mind.clock()) == {}
    with mind.engine.db.connect() as conn, pytest.raises(Conflict, match="outside this appraisal"):
        mind._proposal_refs(conn, [unseen], [], list(fetched.values()))


def test_recalled_aggregate_survives_full_appraisal_commit(setup):
    from test_derived_erasure import answered, paid, queue_row
    from test_tool_fetched_evidence import interpreting, understanding
    mind, source, sources, record = aggregate(setup)
    interpreting(mind, source, setup[2])
    primary = source("primary", "A new conversation starts.")

    class Reader:
        def appraise(self, context):
            paid(self)
            value, receipt = answered(proposal(record["id"]))
            return value, {**receipt, **fork({"id": record["id"], "revision": record["revision"]})}

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    state, _, data = queue_row(mind, job["id"])
    assert state == "complete", data.get("error_detail")
    assert data["receipt"]["tool_fetched_evidence"] == [record["id"]]
    assert {r["source_id"] for r in data["evaluated_sources"] if r["record_id"] == record["id"]} == set(sources)
    assert {r["source_id"] for r in understanding(mind)["evidence"]} == set(sources)

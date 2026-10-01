"""Deferred event routes come back (`deferred_routes`, kin_mind.event_deferrals).

An append whose binding the host cannot verify is committed as `defer`. That used to be the end
of it: the receipt went into `mind_event_routes`, read only by the route's own replay, and the
experience stayed in pieces. With the switch on, the material is shown again to later memory
assessments -- at most four at a time, each at most twice -- until a route places it; erased, it
goes. Off, nothing is kept or shown, and the prompt and the memory context are what they were."""
import json
from datetime import timedelta

import pytest

from kin_mind.appraisal import DEFERRED_ROUTES_PROMPT, Appraisal, Appraisals, DeepSeek
from kin_mind.event_deferrals import RECONSIDER_LIMIT, RETRY_MINUTES, SHOWN
from kin_mind.lifecycle import EventRoute
from kin_mind.memory import DEFAULTS, MemoryAssessment, MemoryContinuity
from test_derived_erasure import answered, paid
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind',)

MARKER = "xyzzyquux"


def lifecycle(mind, **values):
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "graph": True, "event_lifecycle": True, **values})
    return memory


def root(mind, source_id):
    return mind.engine.source(source_id)["record_ids"][0]


def routed(memory, routes, sources, appraisal_id, shown=None):
    """One memory section committed as an appraisal commits it: its routes, and the deferrals it was shown."""
    with memory.engine.db.connect(write=True) as conn:
        refs = memory.mind._evidence(conn, sources)
        return memory.apply_assessment(conn, MemoryAssessment(event_routes=routes), refs, appraisal_id, 0, 30,
                                       {"model": "synthetic"}, schedule=False, deferrals_shown=shown)


def trip(memory, source):
    """An event to append to: a morning at the sea."""
    first = source("trip", "我们周六去海边看日出")
    assert routed(memory, [EventRoute(key="trip", action="create", title="海边看日出", evidence_ids=[root(memory.mind, first)],
                                      reason="一段新的经历")], [first], "appraisal-trip") == []
    event_id = memory.graph.identifier("event", ["route", "appraisal-trip", "trip"])
    with memory.engine.db.connect() as conn:
        return event_id, memory.graph.get(conn, event_id)["revision"]


def unverified_append(memory, source, event, key, text):
    """An append on a semantic likeness alone: the host defers it."""
    later = source(key, text)
    routed(memory, [EventRoute(key=key, action="append", event_id=event[0], expected_revision=event[1],
                               evidence_ids=[root(memory.mind, later)], binding="semantic_candidate", reason="可能是同一次")],
           [later], "appraisal-" + key)
    return later


def deferrals(mind):
    with mind.engine.db.connect() as conn:
        return [dict(row) | {"data": json.loads(row["data"])} for row in conn.execute("SELECT * FROM mind_event_deferrals ORDER BY created_at,id")]


def metric(mind, name):
    with mind.engine.db.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT data FROM metrics WHERE name=?", (name,))]


def test_an_unverified_append_is_kept_and_shown_again_with_its_event(setup):
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True)
    event = trip(memory, source)
    later = unverified_append(memory, source, event, "wind", "日出的时候风好大")
    with mind.engine.db.connect() as conn:
        [route] = [data for data in (json.loads(row[0]) for row in conn.execute("SELECT data FROM mind_event_routes"))
                   if data["requested_action"] == "append"]
    assert route["state"] == "defer"
    [kept] = deferrals(mind)
    assert (kept["event_id"], kept["reason"], kept["attempts"]) == (event[0], "binding-unverified", 0)
    assert kept["data"]["members"] == {root(mind, later): [later]} and kept["data"]["requested_action"] == "append"
    assert "风" not in json.dumps(kept, ensure_ascii=False), "ids, codes and times only"
    context = memory.semantic_context(query="日出")
    [shown] = context["pending_deferrals"]
    assert shown["id"] == kept["id"] and shown["event_id"] == event[0] and shown["reason"] == "binding-unverified"
    assert shown["member_ids"] == [root(mind, later)] and shown["members"][0]["id"] == root(mind, later)
    assert "风好大" in shown["members"][0]["text"] and "metadata" not in json.dumps(shown)
    [candidate] = [n for n in context["graph_candidates"] if n["id"] == event[0]]
    assert candidate["identity_evidence"], "the event is shown as a candidate, with what it holds"


def test_a_later_route_that_places_the_material_settles_it(setup):
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True)
    event = trip(memory, source)
    later = unverified_append(memory, source, event, "wind", "日出的时候风好大")
    routed(memory, [EventRoute(key="wind-own", action="create", title="海边的风", evidence_ids=[root(mind, later)],
                               reason="另一件事")], [later], "appraisal-later")
    assert deferrals(mind) == []
    assert metric(mind, "event_deferrals_settled") == [{}]
    assert "pending_deferrals" in memory.semantic_context() and memory.semantic_context()["pending_deferrals"] == []


def test_a_link_settles_it_too_and_a_defer_again_keeps_its_count(setup):
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True)
    event = trip(memory, source)
    later = unverified_append(memory, source, event, "wind", "日出的时候风好大")
    [kept] = deferrals(mind)
    routed(memory, [], [later], "appraisal-look", shown=[kept["id"]])
    routed(memory, [EventRoute(key="again", action="defer", event_id=event[0], expected_revision=event[1],
                               evidence_ids=[root(mind, later)], reason="还是不确定")], [later], "appraisal-again")
    [again] = deferrals(mind)
    assert (again["id"], again["attempts"], again["reason"]) == (kept["id"], 1, "assessment-deferred")
    routed(memory, [EventRoute(key="link", action="link", event_id=event[0], expected_revision=event[1],
                               evidence_ids=[root(mind, later)], reason="有关联")], [later], "appraisal-link")
    assert deferrals(mind) == []


def test_it_is_looked_at_again_at_most_twice(setup):
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True)
    event = trip(memory, source)
    later = unverified_append(memory, source, event, "wind", "日出的时候风好大")
    [kept] = deferrals(mind)
    assert RECONSIDER_LIMIT == 2
    routed(memory, [], [later], "appraisal-look-1", shown=[kept["id"]])
    [looked] = deferrals(mind)
    assert looked["attempts"] == 1
    assert memory.semantic_context()["pending_deferrals"] == [], "not again before its next time"
    clock[0] += timedelta(minutes=RETRY_MINUTES)
    [shown] = memory.semantic_context()["pending_deferrals"]
    assert shown["reconsidered"] == 1
    routed(memory, [], [later], "appraisal-look-2", shown=[kept["id"]])
    assert deferrals(mind) == []
    assert metric(mind, "event_deferrals_dropped") == [{"reconsidered": 1, "expired": 0}]


def test_at_most_four_are_shown_oldest_first(setup):
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True)
    event = trip(memory, source)
    sources = []
    for index in range(SHOWN + 1):
        clock[0] += timedelta(seconds=1)
        sources.append(unverified_append(memory, source, event, f"more-{index}", f"第{index}件小事"))
    shown = memory.semantic_context()["pending_deferrals"]
    assert SHOWN == 4 and [entry["member_ids"] for entry in shown] == [[root(mind, s)] for s in sources[:SHOWN]]


def test_an_erased_member_takes_its_deferral_with_it(setup):
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True)
    event = trip(memory, source)
    later = unverified_append(memory, source, event, "wind", f"日出的时候 {MARKER} 风好大")
    assert len(deferrals(mind)) == 1
    mind.engine.delete(later)
    settle(mind.engine)
    assert deferrals(mind) == []
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_off_by_default_nothing_is_kept_or_shown(setup):
    mind, source, clock = setup
    assert DEFAULTS["deferred_routes"] is False
    memory = lifecycle(mind)
    event = trip(memory, source)
    unverified_append(memory, source, event, "wind", "日出的时候风好大")
    assert deferrals(mind) == []
    assert "pending_deferrals" not in memory.semantic_context()


@pytest.mark.parametrize("stimulus", [None, "memory-enrichment"])
def test_the_prompt_says_what_they_are_only_when_there_are_some(stimulus):
    provider = DeepSeek("https://api.deepseek.com", "deepseek-chat")
    entry = {"id": "defer_x", "event_id": None, "member_ids": ["mem_x"], "members": [], "reason": "binding-unverified", "reconsidered": 0}
    plain = provider._system({"stimulus": stimulus, "memory_context": {}}, None)
    empty = provider._system({"stimulus": stimulus, "memory_context": {"pending_deferrals": []}}, None)
    shown = provider._system({"stimulus": stimulus, "memory_context": {"pending_deferrals": [entry]}}, None)
    assert DEFERRED_ROUTES_PROMPT not in plain and plain == empty
    assert shown == plain + DEFERRED_ROUTES_PROMPT


def test_an_enrichment_shown_a_deferral_can_place_it(setup):
    """Through the queue: the enrichment's model is shown the deferral and its event, places the
    member as a new event citing the shown record, and the commit settles it. One it leaves alone
    is counted as looked at."""
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True, operational_lanes=True)
    event = trip(memory, source)
    later = unverified_append(memory, source, event, "wind", "日出的时候风好大")
    [kept] = deferrals(mind)
    seen = []

    class Placing:
        def appraise(self, context):
            [entry] = context["memory_context"]["pending_deferrals"]
            assert event[0] in {n["id"] for n in context["memory_context"]["graph_candidates"]}
            seen.append(entry["id"])
            paid(self)
            return answered(Appraisal(reason="想清楚了", memory=MemoryAssessment(event_routes=[EventRoute(
                key="placed", action="create", title="海边的风", member_ids=entry["member_ids"],
                evidence_ids=[entry["members"][0]["id"]], reason="这是另一段经历")])))

    jobs = Appraisals(mind)
    jobs.enqueue([source("today", "今天天气不错")], "synthetic-v1", origin="reflection", stimulus="memory-backfill")
    result = jobs.run_one(Placing(), lane="enrichment")
    assert result["state"] == "complete" and not result.get("rejected_sections"), result
    assert seen == [kept["id"]] and deferrals(mind) == []
    assert metric(mind, "event_deferrals_settled") == [{}]


def test_a_frozen_context_keeps_no_words_of_an_erased_deferral(setup):
    """The enrichment lane freezes the memory context it built, deferrals and all, while its
    evidence waits to be compressed. The deferred message is deleted: the queue row keeps none of
    its words, and the retry builds its context again, without it."""
    mind, source, clock = setup
    memory = lifecycle(mind, deferred_routes=True, operational_lanes=True)
    event = trip(memory, source)
    later = unverified_append(memory, source, event, "wind", f"日出的时候 {MARKER} 风好大")
    shown = []

    class Compressing:
        def appraise(self, context):
            shown.append(json.dumps(context["memory_context"]["pending_deferrals"], ensure_ascii=False))
            raise RuntimeError("deepseek-evidence-compression-pending:budget")

    class Again:
        def appraise(self, context):
            shown.append(json.dumps(context["memory_context"].get("pending_deferrals"), ensure_ascii=False))
            paid(self)
            return answered(Appraisal(reason="翻了翻"))

    jobs = Appraisals(mind)
    job = jobs.enqueue([source("today", "今天天气不错")], "synthetic-v1", origin="reflection", stimulus="memory-backfill")
    assert jobs.run_one(Compressing(), lane="enrichment")["state"] == "pending"
    assert MARKER in shown[0]
    with mind.engine.db.connect() as conn:
        frozen = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0])["frozen_memory_context"]
    assert MARKER in json.dumps(frozen["pending_deferrals"], ensure_ascii=False)
    mind.engine.delete(later)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    for _ in range(3):
        if jobs.run_one(Again(), lane="enrichment")["state"] == "complete":
            break
    assert MARKER not in "".join(shown[1:]) and json.loads(shown[-1]) == []

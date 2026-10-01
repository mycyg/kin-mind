"""Relevance admission for the automatic memory context, and 小光's "不主动提起" marks.

Unset, a build is what it was. Shadow decides and records and injects what it always did. On, only a
recall item scored at the threshold goes in, best first, at most the quota, and what the window has
seen goes without a weaker item taking its place; no score is no admission (Serein's reverse tests:
a failure never becomes admission, no padding, no refill). Explicit reads are never gated. What is kept
is ids and numbers, and an erase takes every observation and mark naming what it erased."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from eventmem.core import Engine, local_embedding, providers
from eventmem.core.db import Conflict, dumps
from eventmem.core.models import RecordInput, Scope, SourceInput

from kin_mind import recall_admission
from kin_mind.adaptive_recall import RecallRanking
from kin_mind.context import CONTEXT_USAGE_HINT, Contexts
from kin_mind.memory import MemoryContinuity
from kin_mind.recall_admission import ADMISSION_MORE_NOTE, Admission, QuietMarks, decide
from kin_mind.state import Mind

SCOPE = Scope(persona="synthetic-admission")
VOCAB = ("harbour", "walk", "sunday", "cat", "tax", "boat", "lantern", "kite")
QUERY = "harbour walk"
TEXTS = {
    "planned": "We planned the harbour walk for sunday.",      # 0.82 against QUERY
    "lantern": "A harbour walk with a lantern at dusk.",        # 0.82
    "cat": "The harbour cat sleeps on the boat.",              # 0.41
}


def vector(text):
    text = text.lower()
    return [1.0 if word in text else 0.0 for word in VOCAB] + [0.1]


class Embeddings:
    """The local embedding service: a vector per text from the words it holds, as the service
    answers (no usage). `fail` makes every call an HTTP 503."""

    def __init__(self):
        self.calls, self.texts, self.fail = 0, [], False

    def __call__(self, *args, **kwargs):
        service = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, json=None, **kwargs):
                service.calls += 1
                if service.fail:
                    return httpx.Response(503, json={"detail": "unavailable"})
                service.texts.extend(json["input"])
                return httpx.Response(200, json={"data": [{"index": i, "embedding": vector(t)}
                                                          for i, t in enumerate(json["input"])]})
        return Client()


@pytest.fixture
def system(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 20, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, SCOPE, clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)

    def source(key, text, owner=False):
        clock[0] += timedelta(minutes=1)
        return engine.receive(SourceInput(namespace="kin-owner-input" if owner else "synthetic", key=key, text=text,
                                          scope=SCOPE, authority="explicit", occurred_at=mind.clock(), extract=False,
                                          **({"metadata": {"role": "user", "host_event": "message"}} if owner else {})))["id"]

    mind.initialize(agent_version="fixture-v1", evidence_ids=[source("initial", "fixture setup")])
    memory.configure({"records": True, "context": True})
    engine.settings("models", {"embedding": {"endpoint": "http://127.0.0.1:8321/v1", "model": local_embedding.MODEL,
                                             "local_embedding": True, "dimensions": len(VOCAB) + 1}})
    (engine.db.root / "embedding-token").write_text("synthetic-token")

    def never(*args, **kwargs):
        raise AssertionError("no wake-up for a service that answers")

    monkeypatch.setattr(local_embedding, "ensure_started", never)
    service = Embeddings()
    monkeypatch.setattr(providers.httpx, "Client", service)
    sources = {key: source(key, text) for key, text in TEXTS.items()}
    records = {key: engine.source(sid)["record_ids"][0] for key, sid in sources.items()}
    return mind, memory, source, service, sources, records


def build(mind, **options):
    return Contexts(mind).build(QUERY, purpose=options.pop("purpose", "chat"), **options)


def recall_ids(packed, records):
    shown = {entry["id"] for entry in packed["index"]}
    return {key for key, rid in records.items() if rid in shown}


def observations(mind):
    with mind.engine.db.connect() as conn:
        try:
            return [json.loads(r[0]) for r in conn.execute("SELECT data FROM mind_recall_observations ORDER BY at")]
        except Exception:  # noqa: BLE001 - no table yet: nothing was ever observed
            return []


def admission(memory, mode, **more):
    memory.configure({"recall_admission": mode, "recall_admission_threshold": 0.6, **more})


# --- off and shadow change nothing -----------------------------------------------------------------

def test_unset_an_automatic_build_is_exactly_what_it_was(system):
    mind, memory, source, service, sources, records = system
    assert memory.settings()["recall_admission"] == "off" and not memory.settings()["recall_quiet_marks"]
    before = build(mind)
    automatic = build(mind, automatic=True)
    assert automatic["rendered_text"] == before["rendered_text"] and automatic["index"] == before["index"]
    assert "recall_admission" not in automatic and service.calls == 0 and observations(mind) == []
    assert recall_ids(automatic, records) == {"planned", "lantern", "cat"}


def test_shadow_never_changes_what_is_injected_and_keeps_only_ids_and_numbers(system):
    mind, memory, source, service, sources, records = system
    off = build(mind, automatic=True)
    admission(memory, "shadow")
    shadow = build(mind, automatic=True)
    assert shadow["rendered_text"] == off["rendered_text"] and shadow["index"] == off["index"]
    assert service.calls >= 1 and shadow["recall_admission"]["state"] == "scored"
    [row] = observations(mind)
    assert row["setting"] == "shadow" and row["purpose"] == "chat" and row["mode"] == "light"
    assert set(row["admitted"]) == {records["planned"], records["lantern"]}
    assert {d["id"]: d["reason"] for d in row["dropped"]} == {records["cat"]: "below_threshold"}
    assert set(row["injected"]) == set(records.values())
    scores = {c["id"]: c["score"] for c in row["candidates"]}
    assert scores[records["planned"]] > .8 > .5 > scores[records["cat"]]
    stored = dumps(row)
    assert not any(word in stored for word in ("harbour", "walk", "sunday", "lantern", "boat"))


# --- on: admission, no padding, no refill ------------------------------------------------------------

def test_on_injects_only_what_scored_at_the_threshold(system):
    mind, memory, source, service, sources, records = system
    admission(memory, "on")
    packed = build(mind, automatic=True)
    assert recall_ids(packed, records) == {"planned", "lantern"}
    assert TEXTS["cat"] not in packed["rendered_text"] and TEXTS["planned"] in packed["rendered_text"]
    assert ADMISSION_MORE_NOTE not in packed["rendered_text"]
    # The same question asked as an explicit read, or by a tool, is not gated.
    assert recall_ids(build(mind, purpose="read", automatic=True), records) == {"planned", "lantern", "cat"}
    assert recall_ids(build(mind), records) == {"planned", "lantern", "cat"}


def test_one_qualified_item_is_never_padded_to_the_quota(system):
    mind, memory, source, service, sources, records = system
    admission(memory, "on", recall_admission_threshold=0.9)
    service.texts.clear()
    nothing = build(mind, automatic=True)
    assert recall_ids(nothing, records) == set() and ADMISSION_MORE_NOTE not in nothing["rendered_text"]
    admission(memory, "on", recall_admission_threshold=0.6, recall_admission_quota=4)
    exact = source("exact", "harbour walk")  # the query's own words: the one item at 0.9
    memory.configure({"recall_admission_threshold": 0.9})
    packed = build(mind, automatic=True)
    shown = {entry["id"] for entry in packed["index"]}
    assert mind.engine.source(exact)["record_ids"][0] in shown and recall_ids(packed, records) == set()
    assert len(observations(mind)[-1]["admitted"]) == 1


def test_the_quota_takes_the_best_and_a_seen_winner_is_not_replaced(system):
    mind, memory, source, service, sources, records = system
    admission(memory, "on", recall_admission_quota=1)
    first = build(mind, automatic=True, session="thread-1", event_id="turn-1")
    won = recall_ids(first, records)
    assert len(won) == 1 and won <= {"planned", "lantern"}
    row = observations(mind)[-1]
    assert [d["reason"] for d in row["dropped"] if d["id"] in {records["planned"], records["lantern"]}] == ["quota"]
    # The winner is in the window now. Asked again, it goes as seen, and the runner-up does not
    # take its place.
    second = build(mind, automatic=True, session="thread-1", event_id="turn-2")
    assert recall_ids(second, records) == set()
    reasons = {d["id"]: d["reason"] for d in observations(mind)[-1]["dropped"]}
    assert reasons[records[won.pop()]] == "seen" and "quota" in reasons.values()


def test_a_failure_never_becomes_admission(system):
    mind, memory, source, service, sources, records = system
    admission(memory, "on")
    service.fail = True
    with mind.engine.db.connect(write=True) as conn:
        rule = mind.engine._insert(conn, RecordInput(scope=SCOPE, kind="preference", title="rule", content="Always ask before booking a boat.",
                                                     source_ids=[sources["cat"]], evidence_ids=[records["cat"]], confirmation="explicit",
                                                     attributes={"constraint": True}))["id"]
    packed = build(mind, automatic=True)
    assert recall_ids(packed, records) == set()
    assert rule in {entry["id"] for entry in packed["index"]}  # a constraint is never gated
    assert packed["rendered_text"].endswith(ADMISSION_MORE_NOTE)
    row = observations(mind)[-1]
    assert row["state"] == "unavailable" and row["reason"] and row["admitted"] == [] and row["exempt"] == [rule]
    assert {d["reason"] for d in row["dropped"]} == {"no_score"}


def test_a_remote_embedding_role_is_never_asked_on_the_reply_path(system):
    mind, memory, source, service, sources, records = system
    mind.engine.settings("models", {"embedding": {"endpoint": "https://embeddings.example/v1", "model": "remote",
                                                  "dimensions": 9}})
    admission(memory, "on")
    packed = build(mind, automatic=True)
    assert service.calls == 0 and recall_ids(packed, records) == set()
    assert observations(mind)[-1]["reason"] == "embedding-not-local"


def test_a_look_asks_for_no_embedding_and_keeps_nothing(system):
    mind, memory, source, service, sources, records = system
    admission(memory, "on")
    packed = build(mind, automatic=True, record=False)
    assert service.calls == 0 and recall_ids(packed, records) == set()
    assert packed["recall_admission"]["state"] == "unavailable" and observations(mind) == []


def test_a_deadline_or_a_busy_service_admits_nothing(system, monkeypatch):
    mind, memory, source, service, sources, records = system
    admission(memory, "on")

    def busy(call, seconds):
        raise TimeoutError("recall-provider-capacity")

    monkeypatch.setattr("kin_mind.adaptive_recall.bounded", busy)
    assert recall_ids(build(mind, automatic=True), records) == set()
    assert observations(mind)[-1]["reason"] == "capacity"


def test_candidates_past_the_pool_are_never_scored(system, monkeypatch):
    mind, memory, source, service, sources, records = system
    monkeypatch.setitem(recall_admission.POOL, "lexical", 2)
    admission(memory, "on")
    service.texts.clear()
    build(mind, automatic=True)
    row = observations(mind)[-1]
    assert len(service.texts) == 1 + 2 and sum(d["reason"] == "pool" for d in row["dropped"]) == 1


def test_an_unchanged_items_vector_is_reused_and_the_query_is_never_kept(system):
    mind, memory, source, service, sources, records = system
    admission(memory, "on")
    build(mind, automatic=True)
    service.texts.clear()
    build(mind, automatic=True)
    assert service.texts == [QUERY]  # the items' vectors were kept, the query's was not
    assert observations(mind)[-1]["timings"]["embedded"] == 1
    with mind.engine.db.connect() as conn:
        kept = {row[0] for row in conn.execute("SELECT item_id FROM mind_recall_vectors")}
    assert kept == set(records.values())


# --- deep: the ranking's selection is the admission ---------------------------------------------------

class Ranker:
    def __init__(self, ids=("c1",), fail=False):
        self.ids, self.fail, self.timeout, self.absolute_deadline, self.calls = list(ids), fail, None, None, 0

    def structured(self, name, model, prompt, payload, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic ranking failure")
        return RecallRanking(ids=self.ids), {"usage": {"input_tokens": 1, "output_tokens": 1}}


def deep(mind, ranker):
    return Contexts(mind).build(QUERY, purpose="chat", mode="deep", allow_model=True, provider=ranker, automatic=True)


@pytest.fixture
def deep_system(system, monkeypatch):
    def no_embedding(*args, **kwargs):
        raise RuntimeError("no vector channel in this test")

    monkeypatch.setattr("eventmem.core.providers.Providers.embed", no_embedding)
    system[1].configure({"adaptive_recall": True})
    return system


def test_the_deep_ranking_admits_its_selection_and_not_the_ranked_tail(deep_system):
    mind, memory, source, service, sources, records = deep_system
    off = deep(mind, Ranker())
    assert recall_ids(off, records) == {"planned", "lantern", "cat"}  # the ranked tail, as before
    admission(memory, "on")
    packed = deep(mind, Ranker())
    assert len(recall_ids(packed, records)) == 1
    row = observations(mind)[-1]
    assert row["state"] == "ranked" and row["mode"] == "deep" and len(row["admitted"]) == 1
    assert {d["reason"] for d in row["dropped"]} == {"not_selected"}


def test_a_ranking_that_selects_nothing_admits_nothing(deep_system):
    mind, memory, source, service, sources, records = deep_system
    admission(memory, "on")
    packed = deep(mind, Ranker(ids=()))
    assert recall_ids(packed, records) == set() and ADMISSION_MORE_NOTE not in packed["rendered_text"]


def test_a_failed_deep_ranking_never_becomes_admission(deep_system):
    mind, memory, source, service, sources, records = deep_system
    off = deep(mind, Ranker(fail=True))
    assert recall_ids(off, records) == {"planned", "lantern", "cat"}  # explicit reads keep this
    admission(memory, "on")
    packed = deep(mind, Ranker(fail=True))
    assert recall_ids(packed, records) == set() and packed["rendered_text"].endswith(ADMISSION_MORE_NOTE)
    row = observations(mind)[-1]
    assert row["state"] == "unavailable" and row["reason"].startswith("rerank:")


# --- the decision itself -------------------------------------------------------------------------------

def item(identifier, revision=1):
    return {"id": identifier, "revision": revision, "text": identifier}


def test_the_decision_never_pads_never_refills_and_ranks_by_score():
    candidates = [(item("a"), "lexical"), (item("b"), "lexical"), (item("c"), "graph"), (item("d"), "work"),
                  (item("k"), "constraint")]
    scores = {"a": .7, "b": .9, "c": .65, "d": .2}
    admitted, dropped = decide(candidates, scores=scores, threshold=.6, quota=2, seen={"b": 1}, state="scored")
    assert [i["id"] for i in admitted] == ["a"]  # b won and was seen; c may not take its place
    assert dict(dropped) == {"b": "seen", "c": "quota", "d": "below_threshold"}
    admitted, dropped = decide(candidates, scores={}, threshold=.6, quota=2, seen={}, state="unavailable:deadline")
    assert admitted == [] and set(dict(dropped).values()) == {"no_score"}
    admitted, dropped = decide(candidates, scores={}, threshold=.6, quota=2, seen={}, state="no_query")
    assert admitted == [] and set(dict(dropped).values()) == {"no_query"}
    deep = [(item("x"), "deep"), (item("y"), "deep"), (item("z"), "deep")]
    admitted, dropped = decide(deep, scores={}, threshold=.6, quota=4, seen={}, state="ranked", selected=["z", "x"])
    assert [i["id"] for i in admitted] == ["z", "x"] and dict(dropped) == {"y": "not_selected"}


def test_a_note_its_digest_covers_goes_once(system):
    mind, memory, source, service, sources, records = system
    from kin_mind.lifecycle_schema import SCHEMA
    from kin_mind.recall_admission import covering_digests
    scope = SCOPE.key()
    with mind.engine.db.connect(write=True) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO mind_event_dependencies VALUES(?,?,?,1)", (scope, "graph_event", records["planned"]))
        conn.execute("INSERT INTO mind_event_digests VALUES(?,?,'ready',1,1,'h','t',0,?)",
                     (scope, "graph_event", dumps({"source_versions": {records["planned"]: 1}})))
    note = {"source_ids": [sources["planned"]], "evidence_ids": [records["planned"]]}
    with mind.engine.db.connect() as conn:
        assert covering_digests(conn, scope, note) == ["graph_event"]
        assert covering_digests(conn, scope, {**note, "source_ids": [sources["planned"], sources["cat"]]}) == []
    admission_ = Admission(Contexts(mind))
    notes = {"mem_note": note}
    assert admission_.covered([item("graph_event"), item("mem_note")], notes, {}) == {"mem_note"}
    assert admission_.covered([item("mem_note"), item("graph_event")], notes, {}) == {"graph_event"}
    assert admission_.covered([item("mem_note")], notes, {"graph_event": "r"}) == {"mem_note"}
    assert admission_.covered([item("mem_note")], notes, {}) == set()


# --- the envelope's usage sentence ------------------------------------------------------------------

def test_the_usage_sentence_is_added_only_when_switched_on(system):
    mind, memory, source, service, sources, records = system
    assert CONTEXT_USAGE_HINT not in build(mind, automatic=True)["rendered_text"]
    memory.configure({"context_usage_hint": True})
    assert CONTEXT_USAGE_HINT in build(mind, automatic=True)["rendered_text"]


# --- quiet marks -----------------------------------------------------------------------------------------

def test_a_quiet_item_is_left_out_of_automatic_context_and_still_read_explicitly(system):
    mind, memory, source, service, sources, records = system
    marks = QuietMarks(mind)
    with pytest.raises(Conflict):
        marks.change({"command_id": "q0", "item_id": records["cat"], "quiet": True}, actor="console")
    memory.configure({"recall_quiet_marks": True})
    assert marks.change({"command_id": "q1", "item_id": records["cat"], "quiet": True}, actor="console")["state"] == "quiet"
    assert "cat" not in recall_ids(build(mind, automatic=True), records)
    assert "cat" in recall_ids(build(mind, purpose="read", automatic=True), records)
    assert "cat" in recall_ids(build(mind), records)
    # Admission on: a quiet candidate is dropped as such, never scored.
    admission(memory, "shadow")
    build(mind, automatic=True)
    assert {d["id"]: d["reason"] for d in observations(mind)[-1]["dropped"]}[records["cat"]] == "quiet"
    assert marks.change({"command_id": "q2", "item_id": records["cat"], "quiet": False}, actor="console")["state"] == "cleared"
    memory.configure({"recall_admission": "off"})
    assert "cat" in recall_ids(build(mind, automatic=True), records)


def test_the_main_session_sets_a_mark_only_on_her_own_current_word(system):
    mind, memory, source, service, sources, records = system
    memory.configure({"recall_quiet_marks": True})
    marks = QuietMarks(mind)
    request = {"command_id": "q1", "item_id": records["cat"], "quiet": True, "reason": "小光说以后别主动提"}
    with pytest.raises(ValueError):
        marks.change(request, actor="owner")
    with pytest.raises(Conflict):
        marks.change({**request, "evidence_ids": [sources["planned"]]}, actor="owner")  # not 小光 speaking
    said = source("said", "猫的事以后别主动提了", owner=True)
    assert marks.change({**request, "evidence_ids": [said]}, actor="owner")["state"] == "quiet"
    assert marks.change({**request, "evidence_ids": [said]}, actor="owner")["state"] == "quiet"  # a replay
    with pytest.raises(Conflict):
        marks.change({**request, "evidence_ids": [said], "quiet": False}, actor="owner")  # same id, other change
    [mark] = marks.list()
    assert mark["item_id"] == records["cat"] and mark["actor"] == "owner"
    tools = {tool.name: tool for tool in asyncio.run(__import__("eventmem.core.mcp", fromlist=["create_mcp"]).create_mcp(mind.engine).list_tools())}
    assert tools["set_memory_quiet"].description == recall_admission.QUIET_TOOL_DESCRIPTION


# --- erasure, retention, calibration ---------------------------------------------------------------------

def test_an_erase_takes_every_observation_and_mark_naming_what_it_erased(system):
    mind, memory, source, service, sources, records = system
    memory.configure({"recall_quiet_marks": True})
    admission(memory, "shadow")
    build(mind, automatic=True)
    QuietMarks(mind).change({"command_id": "q1", "item_id": records["cat"], "quiet": True}, actor="console")
    QuietMarks(mind).change({"command_id": "q2", "item_id": records["planned"], "quiet": True}, actor="console")
    assert len(observations(mind)) == 1
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM mind_recall_vectors WHERE item_id=?", (records["cat"],)).fetchone()
    mind.engine.delete(sources["cat"])
    assert observations(mind) == []
    assert [mark["item_id"] for mark in QuietMarks(mind).list()] == [records["planned"]]
    with mind.engine.db.connect() as conn:
        for table in ("mind_recall_observations", "mind_recall_quiet"):
            assert not conn.execute(f"SELECT 1 FROM {table} WHERE instr(data,?)>0", (records["cat"],)).fetchone()
        assert not conn.execute("SELECT 1 FROM mind_recall_vectors WHERE item_id=?", (records["cat"],)).fetchone()
        assert conn.execute("SELECT 1 FROM mind_recall_vectors WHERE item_id=?", (records["planned"],)).fetchone()


def test_observations_are_bounded(system, monkeypatch):
    mind, memory, source, service, sources, records = system
    monkeypatch.setattr(recall_admission, "OBSERVATIONS_KEPT", 3)
    admission(memory, "shadow")
    for _ in range(5):
        build(mind, automatic=True)
    assert len(observations(mind)) == 3


def test_calibration_replays_thresholds_as_numbers_only(system):
    mind, memory, source, service, sources, records = system
    admission(memory, "shadow")
    for _ in range(2):
        build(mind, automatic=True)
    report = recall_admission.calibrate(mind, thresholds=[.3, .6, .9])
    by = {row["threshold"]: row for row in report["thresholds"]}
    assert report["scored_builds"] == 2 and by[.3]["mean_passing"] == 3 and by[.6]["mean_passing"] == 2
    assert by[.9]["builds_with_none"] == 2
    text = dumps(report)
    assert not any(marker in text for marker in ("mem_", "src_", "harbour"))
    with pytest.raises(ValueError):
        recall_admission.calibrate(mind, thresholds=[2])


def test_the_settings_are_checked(system):
    mind, memory, source, service, sources, records = system
    for bad in ({"recall_admission": "maybe"}, {"recall_admission_threshold": 1.5}, {"recall_admission_quota": 0},
                {"recall_admission_timeout_ms": 10}, {"recall_quiet_marks": "yes"}, {"context_usage_hint": 1}):
        with pytest.raises(ValueError):
            memory.configure(bad)


def test_association_is_one_neighbour_one_hop_and_passes_the_gate_like_any_item(system):
    from kin_mind.graph import GraphAssessment, GraphNode
    mind, memory, source, service, sources, records = system
    memory.configure({"graph": True, "graph_recall": True})
    graph = memory.graph
    old = "2026-09-01T00:00:00+00:00"  # past the 72 hours a graph read also takes as recent
    titles = {"walk": "harbour walk plan", "group": "harbourside walking group", "boat": "boat repairs", "far": "kite contest"}
    with mind.engine.db.connect(write=True) as conn:
        refs = graph.proof(conn, [sources["planned"]])
        graph.apply(conn, GraphAssessment(nodes=[GraphNode(key=k, kind="entity", title=t, entity_type="project", occurred_at=old,
                                                           evidence_ids=[sources["planned"]]) for k, t in titles.items()]),
                    refs, "fixture", {})
        node = {k: graph.get(conn, graph.identifier("entity", ["fixture", k]))["id"] for k in titles}
        graph.link(conn, node["walk"], "about", node["group"], refs, reason="fixture")
        graph.link(conn, node["walk"], "about", node["boat"], refs, reason="fixture")
        graph.link(conn, node["group"], "about", node["far"], refs, reason="fixture")  # two hops away
    shown = {entry["id"] for entry in build(mind, automatic=True)["index"]}
    assert {node["walk"], node["group"], node["boat"], node["far"]} <= shown  # as before: two hops, every neighbour
    admission(memory, "on")
    packed = build(mind, automatic=True)
    row = observations(mind)[-1]
    routes = {c["id"]: c["route"] for c in row["candidates"]}
    neighbours = [i for i, route in routes.items() if route == "neighbor"]
    assert routes[node["walk"]] == "graph" and len(neighbours) == 1 and neighbours[0] in {node["group"], node["boat"]}
    assert node["far"] not in routes
    scores = {c["id"]: c["score"] for c in row["candidates"]}
    shown = {entry["id"] for entry in packed["index"]}
    assert (neighbours[0] in shown) == (scores[neighbours[0]] >= .6)
    assert node["walk"] in shown and node["far"] not in shown

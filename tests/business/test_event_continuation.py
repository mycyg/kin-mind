"""A genuine continuation grows its event (`event_continuation`, off by default).

Routes are proposed by the enrichment lane. Its graph candidates were rebuilt without the identity
evidence a current assessment is shown, and its prompt carried no route rules, so a model that saw
a continuation could cite no member of the event it continued: it named the event's own id, which
the host never accepts, and every append was deferred. With the switch on, the enrichment is shown
each candidate event with its latest members (id, revision, excerpt) and told how a continuation
cites them; the host's check is the one it always was. Off, nothing changes."""
import json
from datetime import timedelta

import pytest

from kin_mind.adaptive_recall import evidence_excerpt
from kin_mind.appraisal import EVENT_CONTINUATION_PROMPT, Appraisal, Appraisals, DeepSeek, appraisal_context
from kin_mind.lifecycle import EventIdentityJudgement, EventLifecycle, EventRoute
from kin_mind.memory import (CONTINUATION_EVENTS, CONTINUATION_EXCERPT_TOKENS, CONTINUATION_RECORDS, DEFAULTS,
                             MemoryAssessment, MemoryContinuity)
from kin_mind.model_view import leaks
from test_deferred_routes import deferrals, root, routed
from test_derived_erasure import answered, paid
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind',)

MARKER = "xyzzyquux"
SAME = dict(participants_match=True, object_match=True, time_compatible=True, continuation_supported=True)


def configured(mind, **values):
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "graph": True, "event_lifecycle": True,
                      "operational_lanes": True, **values})
    return memory


def said(memory, clock, key, text, minutes_ago=0, kind="owner-message"):
    at = (clock[0] - timedelta(minutes=minutes_ago)).isoformat()
    return memory.ingest({"id": key, "kind": kind, "at": at, "text": text})["source_id"]


class Action:
    """The action lane's model: it feels and decides; memory is left to the enrichment lane."""
    def __init__(self):
        self.continuation = []

    def appraise(self, context):
        self.continuation.append(self.event_continuation)
        paid(self)
        return answered(Appraisal(reason="收到了"))


class Organizer:
    """The enrichment lane's model, scripted: `decide(context, self)` gives the routes."""
    def __init__(self, decide):
        self.decide, self.contexts, self.continuation = decide, [], []

    def appraise(self, context):
        self.contexts.append(context)
        self.continuation.append(self.event_continuation)
        paid(self)
        return answered(Appraisal(reason="整理了", memory=MemoryAssessment(event_routes=self.decide(context))))


def organised(memory, source_id, decide):
    """One owner message, as the host takes it in: the action lane's turn, then its enrichment."""
    jobs = Appraisals(memory.mind)
    action = Action()
    jobs.enqueue([source_id], "synthetic-v1")
    assert jobs.run_one(action, lane="action")["state"] == "complete"
    assert action.continuation == [False], "the action lane organises no memory"
    organizer = Organizer(decide)
    result = jobs.run_one(organizer, lane="enrichment")
    assert result["state"] == "complete" and not result.get("rejected_sections"), result
    return organizer


def created(key, title, record_id):
    return lambda context: [EventRoute(key=key, action="create", title=title, evidence_ids=[record_id], reason="一段新的经历")]


def event_id(memory, appraisal_id, key):
    return memory.graph.identifier("event", ["route", appraisal_id, key])


def appraisal_of(memory, record_id):
    """The appraisal (mind event) whose route made a member of `record_id`."""
    with memory.engine.db.connect() as conn:
        for row in conn.execute("SELECT data FROM mind_event_routes"):
            data = json.loads(row[0])
            if record_id in data["member_ids"] and data["state"] == "create":
                return data["appraisal_id"], data["event_id"]


def candidate(context, identifier):
    [node] = [n for n in context["memory_context"]["graph_candidates"] if n["id"] == identifier]
    return node


def members(memory, identifier):
    with memory.engine.db.connect() as conn:
        return set(EventLifecycle(memory.mind, memory.graph).snapshot(conn, identifier)["records"])


def routes(memory, action):
    with memory.engine.db.connect() as conn:
        return [d for d in (json.loads(row[0]) for row in conn.execute("SELECT data FROM mind_event_routes"))
                if d["requested_action"] == action]


def continuing(key, target, record_id, quote, prior=None, **identity):
    """The scripted model continuing `target`: cites the first record of its identity evidence."""
    def decide(context):
        node = candidate(context, target)
        cited = prior(node) if prior else [node["identity_evidence"][0]["id"]]
        return [EventRoute(key=key, action="append", event_id=target, expected_revision=node["revision"],
                           evidence_ids=[record_id], binding="sourced_continuation", quote=quote, reason="同一件事的后续",
                           identity=EventIdentityJudgement(decision=identity.pop("decision", "same_event"),
                                                           **{**SAME, **identity}, prior_record_ids=cited))]
    return decide


def test_a_later_message_that_continues_an_experience_grows_its_event(setup):
    """The business case: 小光 tells Kin about the sunrise; later the same morning goes on. The
    first message makes an event; the second, organised by its own enrichment, is appended to it,
    citing what the enrichment was shown of the event."""
    mind, source, clock = setup
    memory = configured(mind, event_continuation=True)
    first = said(memory, clock, "owner-1", "周六我和小明去海边看日出，五点就到了沙滩", minutes_ago=90)
    organised(memory, first, created("sunrise", "和小明在海边看日出", root(mind, first)))
    _, event = appraisal_of(memory, root(mind, first))
    assert members(memory, event) == {root(mind, first)}

    later = said(memory, clock, "owner-2", "对了，那天看完日出小明还在沙滩上捡到一只小螃蟹")
    organizer = organised(memory, later, continuing("crab", event, root(mind, later), "小明还在沙滩上捡到一只小螃蟹"))
    [context], [continuation] = organizer.contexts, organizer.continuation
    assert continuation is True, "the enrichment is told how a continuation cites an event"
    [shown] = candidate(context, event)["identity_evidence"]
    assert shown["id"] == root(mind, first) and "五点就到了沙滩" in shown["text"]
    # What the model is shown of it: ids to cite and the words, never a source's metadata.
    projected = appraisal_context(context)
    assert candidate(projected, event)["identity_evidence"] == [shown] and leaks(projected) == []

    [append] = routes(memory, "append")
    assert append["state"] == "append" and append["event_id"] == event
    assert append["identity_evidence_versions"] == {root(mind, first): shown["revision"]}
    assert members(memory, event) == {root(mind, first), root(mind, later)}, "one event, grown"


def two_sunrises(memory, source, clock):
    """Two different mornings at the sea with nearly the same title: one with 小明 in July, one
    with colleagues this week. Each its own event, both among the enrichment's candidates."""
    july = source("july", "七月和小明去海边看日出，风很大")
    september = source("september", "这周和同事去海边看日出，大家都没睡醒")
    assert routed(memory, [EventRoute(key="july", action="create", title="海边看日出", evidence_ids=[root(memory.mind, july)],
                                      reason="一段经历")], [july], "appraisal-july") == []
    assert routed(memory, [EventRoute(key="september", action="create", title="海边看日出", evidence_ids=[root(memory.mind, september)],
                                      reason="另一段经历")], [september], "appraisal-september") == []
    return ((event_id(memory, "appraisal-july", "july"), root(memory.mind, july)),
            (event_id(memory, "appraisal-september", "september"), root(memory.mind, september)))


def test_should_merge_and_should_split_with_two_events_of_the_same_title(setup):
    """Serein's pair. Both events are shown with their own evidence, so a model can tell them apart.
    The continuation of this week's morning is appended to it. A different morning, or a route that
    takes the other event's evidence for this one's, grows neither: the July event stays as it was."""
    mind, source, clock = setup
    memory = configured(mind, event_continuation=True)
    (july, july_record), (week, week_record) = two_sunrises(memory, source, clock)

    later = said(memory, clock, "owner-3", "和同事看日出那天回来的路上还下了一场雨")
    organizer = organised(memory, later, lambda context: [
        *continuing("rain", week, root(mind, later), "回来的路上还下了一场雨")(context),
        # A wrong merge by title: this week's material into July's event, July judged different.
        *continuing("by-title", july, root(mind, later), "回来的路上还下了一场雨",
                    decision="different_event", participants_match=False)(context),
    ])
    [context] = organizer.contexts
    assert [r["id"] for r in candidate(context, july)["identity_evidence"]] == [july_record]
    assert [r["id"] for r in candidate(context, week)["identity_evidence"]] == [week_record]
    by_target = {r["event_id"]: r["state"] for r in routes(memory, "append")}
    assert by_target == {week: "append", july: "defer"}
    assert members(memory, week) == {week_record, root(mind, later)}, "should merge"
    assert members(memory, july) == {july_record}, "should split"

    # The other confusion: July's event named, this week's evidence cited for it, all else in order.
    another = said(memory, clock, "owner-4", "那天和同事在海边还拍了一张合照")
    organised(memory, another, continuing("swapped", july, root(mind, another), "还拍了一张合照",
                                          prior=lambda node: [week_record]))
    assert [r["state"] for r in routes(memory, "append") if r["event_id"] == july] == ["defer", "defer"]
    assert members(memory, july) == {july_record}, "evidence of a look-alike event is not this event's"


FAULTS = ["own-id", "no-prior", "unshown", "stale", "paraphrase", "no-quote", "kin-said", "stray-member", "related",
          "one-false", "semantic"]


@pytest.mark.parametrize("fault", [None, *FAULTS])
def test_the_host_check_is_unchanged(setup, fault):
    """Every part of the safety design still holds with the switch on: a verbatim quote of the
    owner's words in the new material its members carry, `same_event` with all four judgments, and
    prior records that belong to the event at the revision the assessment was evaluated against.
    The route without a fault is appended; each fault alone defers it."""
    mind, source, clock = setup
    memory = configured(mind, event_continuation=True)
    (event, prior), _ = two_sunrises(memory, source, clock)
    kind = "assistant-message" if fault == "kin-said" else "owner-message"
    later = said(memory, clock, "later", "七月那次回来以后小明还感冒了两天", kind=kind)
    stray = said(memory, clock, "stray", "今天的晚饭吃了面") if fault == "stray-member" else None
    evidence = [root(mind, later), *([root(mind, stray)] if stray else [])]
    with memory.engine.db.connect() as conn:
        revision = memory.graph.get(conn, event)["revision"]
    route = EventRoute(key="continue", action="append", event_id=event, expected_revision=revision, evidence_ids=evidence,
                       binding="semantic_candidate" if fault == "semantic" else "sourced_continuation",
                       quote={"paraphrase": "小明感冒了两天", "no-quote": ""}.get(fault, "小明还感冒了两天"), reason="后续",
                       identity=EventIdentityJudgement(
                           decision="related_event" if fault == "related" else "same_event",
                           **{**SAME, **({"time_compatible": False} if fault == "one-false" else {})},
                           prior_record_ids={"own-id": [event], "no-prior": []}.get(fault, [prior])))
    with memory.engine.db.connect(write=True) as conn:
        shown = memory.mind._evidence(conn, [*evidence, *([] if fault == "unshown" else [prior])])
        if fault == "stale":
            shown = [{**ref, "revision": ref["revision"] + 1} if ref["record_id"] == prior else ref for ref in shown]
        [result] = EventLifecycle(memory.mind, memory.graph).apply_routes(conn, [route], shown, "appraisal-" + str(fault))
    assert result["state"] == ("append" if fault is None else "defer"), fault
    assert members(memory, event) == ({prior, root(mind, later)} if fault is None else {prior})


def test_off_by_default_the_enrichment_and_both_prompts_are_what_they_were(setup):
    """Unset: the enrichment's candidates carry no identity evidence, no prompt says anything new,
    and the continuation the switch lets through is deferred, as in production before it."""
    mind, source, clock = setup
    assert DEFAULTS["event_continuation"] is False
    memory = configured(mind)
    assert memory.settings()["event_continuation"] is False
    first = said(memory, clock, "owner-1", "周六我和小明去海边看日出，五点就到了沙滩", minutes_ago=90)
    organised(memory, first, created("sunrise", "和小明在海边看日出", root(mind, first)))
    _, event = appraisal_of(memory, root(mind, first))
    later = said(memory, clock, "owner-2", "对了，那天看完日出小明还在沙滩上捡到一只小螃蟹")
    # What a model in production did: it cited the event itself, having been shown no member.
    organizer = organised(memory, later, continuing("crab", event, root(mind, later), "小明还在沙滩上捡到一只小螃蟹",
                                                     prior=lambda node: [node["id"]]))
    [context] = organizer.contexts
    assert organizer.continuation == [False]
    assert all("identity_evidence" not in node for node in context["memory_context"]["graph_candidates"])
    [append] = routes(memory, "append")
    assert append["state"] == "defer" and members(memory, event) == {root(mind, first)}


def test_without_event_lifecycle_the_switch_alone_does_nothing(setup):
    """No routes are applied without `event_lifecycle`: nothing is shown or said for them."""
    mind, source, clock = setup
    memory = configured(mind, event_continuation=True)
    (event, prior), _ = two_sunrises(memory, source, clock)
    memory.configure({"event_lifecycle": False})
    later = said(memory, clock, "owner-2", "七月那次回来以后小明还感冒了两天")
    organizer = organised(memory, later, lambda context: [])
    assert organizer.continuation == [False]
    assert all("identity_evidence" not in node for node in organizer.contexts[0]["memory_context"]["graph_candidates"])


@pytest.mark.parametrize("stimulus", [None, "memory-enrichment"])
def test_the_prompt_says_how_only_with_the_switch(stimulus):
    provider = DeepSeek("https://api.deepseek.com", "deepseek-chat")
    context = {"stimulus": stimulus, "memory_context": {}}
    plain = provider._system(context, None)
    provider.event_continuation = False
    assert provider._system(context, None) == plain and EVENT_CONTINUATION_PROMPT not in plain
    provider.event_continuation = True
    assert provider._system(context, None) == plain + EVENT_CONTINUATION_PROMPT
    profile = provider.request_profile(context)["system"]
    provider.event_continuation = False
    assert provider.request_profile(context)["system"] != profile, "a changed prompt is a changed policy, never a reuse"


def test_a_current_assessment_shows_identity_evidence_as_it_always_did(setup):
    """The helper both lanes use gives a current assessment exactly what its inline code gave."""
    mind, source, clock = setup
    memory = configured(mind)
    long = source("long", "七月和小明去海边看日出。" + "路上说了很多话。" * 400)
    routed(memory, [EventRoute(key="long", action="create", title="海边看日出", evidence_ids=[root(mind, long)],
                               reason="一段经历")], [long], "appraisal-long")
    event, prior = event_id(memory, "appraisal-long", "long"), root(mind, long)
    context = memory.semantic_context(query="海边看日出")
    node = candidate({"memory_context": context}, event)
    assert node["identity_evidence"][0]["excerpt_only"] is True
    with memory.engine.db.connect() as conn:
        record = memory.engine._get(conn, prior)
    from eventmem.core.read_policy import ReadPolicy
    policy = ReadPolicy.load(memory.engine, memory.scope, "experience_recall")
    assert node["identity_evidence"] == [{"id": prior, "revision": record["revision"], "basis": policy.basis(record),
                                          "occurred_at": record["valid_from"],
                                          "text": evidence_excerpt(record["content"], "海边看日出")[0],
                                          "excerpt_only": evidence_excerpt(record["content"], "海边看日出")[1]}]


def test_what_an_enrichment_is_shown_is_bounded(setup):
    """The first CONTINUATION_EVENTS events in the candidates' order, and every event a deferral
    names; each its latest CONTINUATION_RECORDS members, short excerpts; none for one under review."""
    mind, source, clock = setup
    memory = configured(mind, event_continuation=True)
    assert (CONTINUATION_EVENTS, CONTINUATION_RECORDS, CONTINUATION_EXCERPT_TOKENS) == (8, 2, 250)
    events, made = [], []
    for index in range(CONTINUATION_EVENTS + 2):
        clock[0] += timedelta(seconds=1)
        made.append(root(mind, source(f"day-{index}", f"第{index}天的早晨" + "很长的一句话。" * 400)))
        routed(memory, [EventRoute(key=f"e{index}", action="create", title=f"早晨{index}", evidence_ids=[made[-1]],
                                   reason="一段经历")], [made[-1]], f"appraisal-{index}")
        events.append(event_id(memory, f"appraisal-{index}", f"e{index}"))
    for later in ("又一句", "再一句"):
        clock[0] += timedelta(seconds=1)
        more = root(mind, source(later, f"第0天的早晨{later}"))
        with memory.engine.db.connect(write=True) as conn:
            revision = memory.graph.get(conn, events[0])["revision"]
            [appended] = EventLifecycle(memory.mind, memory.graph).apply_routes(conn, [EventRoute(
                key="more", action="append", event_id=events[0], expected_revision=revision, evidence_ids=[more],
                binding="sourced_continuation", quote=later, reason="后续",
                identity=EventIdentityJudgement(decision="same_event", **SAME, prior_record_ids=[made[0]]))],
                memory.mind._evidence(conn, [more, made[0]]), "appraisal-" + later)
        assert appended["state"] == "append"
        made.append(more)
    with memory.engine.db.connect() as conn:
        graph = [memory.graph.get(conn, identifier) for identifier in events]
    graph[1]["needs_review"] = True
    with memory.engine.db.connect() as conn:
        memory.show_continuation(conn, graph, "早晨", named={events[-1]})
    shown = [node["id"] for node in graph if "identity_evidence" in node]
    assert shown == [events[0], *events[2:CONTINUATION_EVENTS + 1], events[-1]]
    assert [r["id"] for r in graph[0]["identity_evidence"]] == [made[-1], made[-2]], "the latest two of three"
    with memory.engine.db.connect() as conn:
        content = memory.engine._get(conn, made[2])["content"]
    [excerpt] = graph[2]["identity_evidence"]
    assert (excerpt["text"], excerpt["excerpt_only"]) == evidence_excerpt(content, "早晨", budget=CONTINUATION_EXCERPT_TOKENS)
    assert excerpt["excerpt_only"] is True and excerpt["text"] != evidence_excerpt(content, "早晨")[0]


def test_a_deferred_continuation_shown_again_can_now_be_placed(setup):
    """A continuation deferred days ago comes back (deferred_routes). Its event is found by the
    enrichment's own query, among the candidates rebuilt for it; with the switch on it carries its
    identity evidence there too, so the append the deferral asks for can pass."""
    mind, source, clock = setup
    memory = configured(mind, deferred_routes=True, event_continuation=True)
    clock[0] -= timedelta(days=4)
    first = said(memory, clock, "owner-1", "周六我和小明去海边看日出，五点就到了沙滩")
    organised(memory, first, created("sunrise", "海边看日出", root(mind, first)))
    _, event = appraisal_of(memory, root(mind, first))
    later = said(memory, clock, "owner-2", "看完日出小明还在沙滩上捡到一只小螃蟹")
    organised(memory, later, continuing("crab", event, root(mind, later), "小明还在沙滩上捡到一只小螃蟹",
                                        prior=lambda node: [node["id"]]))
    [kept] = deferrals(mind)
    clock[0] += timedelta(days=4)

    def place(context):
        [entry] = context["memory_context"]["pending_deferrals"]
        node = candidate(context, event)
        return [EventRoute(key="placed", action="append", event_id=event, expected_revision=node["revision"],
                           member_ids=entry["member_ids"], evidence_ids=[entry["members"][0]["id"]],
                           binding="sourced_continuation", quote="捡到一只小螃蟹", reason="同一个早晨",
                           identity=EventIdentityJudgement(decision="same_event", **SAME,
                                                           prior_record_ids=[node["identity_evidence"][0]["id"]]))]
    today = said(memory, clock, "owner-3", "海边看日出的照片我洗出来了")
    organizer = organised(memory, today, place)
    assert candidate(organizer.contexts[0], event)["identity_evidence"][0]["id"] == root(mind, first)
    assert deferrals(mind) == []
    assert members(memory, event) == {root(mind, first), root(mind, later)}


def test_an_erased_member_leaves_no_words_in_a_frozen_enrichment(setup):
    """The enrichment lane freezes the context it built, identity evidence and all, while its
    evidence waits to be compressed. The member is deleted: no word of it stays anywhere."""
    mind, source, clock = setup
    memory = configured(mind, event_continuation=True)
    first = said(memory, clock, "owner-1", f"周六我和小明去海边看日出 {MARKER}", minutes_ago=90)
    organised(memory, first, created("sunrise", "海边看日出", root(mind, first)))
    later = said(memory, clock, "owner-2", "那天看完日出还捡到一只小螃蟹")
    jobs = Appraisals(mind)
    jobs.enqueue([later], "synthetic-v1")
    assert jobs.run_one(Action(), lane="action")["state"] == "complete"
    seen = []

    class Compressing:
        def appraise(self, context):
            seen.append(json.dumps(context["memory_context"]["graph_candidates"], ensure_ascii=False))
            raise RuntimeError("deepseek-evidence-compression-pending:budget")

    assert jobs.run_one(Compressing(), lane="enrichment")["state"] == "pending"
    assert MARKER in seen[0], "the enrichment was shown the event's member"
    mind.engine.delete(first)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()

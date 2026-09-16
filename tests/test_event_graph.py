from datetime import datetime, timedelta, timezone

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict, Missing
from eventmem.core.models import RevisionInput, Scope, SourceInput
from kin_mind.context import Contexts
from kin_mind.graph import GraphAssessment, GraphEdge, GraphNode
from kin_mind.memory import MemoryAssessment, MemoryContinuity
from kin_mind.sharing import ContentReference, CoverageAssessment, CoverageMapping
from kin_mind.sharing import ShareCheck
from kin_mind.state import Mind


@pytest.fixture
def system(tmp_path):
    clock = [datetime(2026, 9, 14, 4, tzinfo=timezone.utc)]
    engine = Engine(tmp_path / "db")
    mind = Mind(engine, Scope(persona="synthetic-graph"), clock=lambda: clock[0].isoformat(timespec="microseconds"))
    memory = MemoryContinuity(mind)
    def source(key, text=None):
        return engine.receive(SourceInput(namespace="synthetic", key=key, text=text or key, scope=mind.scope,
            authority="explicit", occurred_at=mind.clock(), extract=False))["id"]
    initial = source("initial")
    mind.initialize(agent_version="fixture-v1", evidence_ids=[initial])
    memory.configure({"records": True, "semantic": True, "context": True, "graph": True, "sharing": True, "graph_recall": True, "associations": True})
    return mind, memory, source, clock


def test_share_review_keeps_whole_reply_and_reports_incomplete_results(system):
    _, memory, _, _ = system
    request = {'draft_id': 'draft', 'text': 'Complete creative body', 'batch_text': 'Here is the requested draft:\n\nComplete creative body', 'review_required': True}
    class Reviewer:
        timeout = 600
        fail = True
        def structured(self, name, schema, system, context, **options):
            assert self.timeout == 240 and options['max_tokens'] == 65536
            assert context['reply_context'] == request['batch_text']
            if self.fail:
                raise RuntimeError('deepseek-incomplete-or-unverified')
            return ShareCheck(decision='ordinary', reason='Requested creative draft'), {'model': 'deepseek-flash'}
    reviewer = Reviewer()
    result = memory.sharing.preflight(request, provider=reviewer)
    assert result == {'state': 'pending', 'reason': 'deepseek-incomplete-or-unverified'}
    assert reviewer.timeout == 600
    reviewer.fail = False
    result = memory.sharing.preflight(request, provider=reviewer)
    assert result['state'] == 'ready' and result['text'] == request['text']


def findings(system):
    mind, memory, source, _ = system
    sid = source("observations", "The synthetic exploration has three independent observations.")
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [sid])
        owner = memory.graph._put(conn, {"id": "explore-fixture", "kind": "exploration", "title": "Synthetic star colors", "text": "",
            "source_ids": [sid], "evidence": refs, "basis": "documented"})
        units = memory.sharing.units(conn, owner["id"], ["Young synthetic stars appear blue in this fictional dataset.", "Older synthetic stars appear red in this fictional dataset.", "The fictional telescope has not confirmed the third observation."], [sid])
    return units


def send(system, unit, *, bubble="one", state="accepted", channel="a", mode="new", text=None):
    mind, memory, _, _ = system
    event = {"id": f"{channel}-{bubble}-{state}", "kind": "delivery", "at": mind.clock(), "channel": channel,
        "delivery_id": "batch", "bubble_id": bubble, "text": text or unit["text"], "state": state,
        "references": [{"unit_id": unit["id"], "version": 1, "mode": mode}]}
    if state == "accepted":
        event["message_id"] = channel + "-receipt-" + bubble
    return memory.ingest(event)


def test_accepted_bubble_immediately_blocks_rewording_across_channels(system):
    mind, memory, _, _ = system
    units = findings(system)
    sent = send(system, units[0])
    with mind.engine.db.connect() as conn:
        coverage = memory.sharing.coverage(conn, "explore-fixture")
    assert coverage["state"] == "partial"
    assert coverage["shared"] == 1
    assert coverage["visibility"] == "unverified"
    check = memory.sharing.preflight({"draft_id": "different-channel", "text": "Rephrased old finding", "references": [{"unit_id": units[0]["id"], "version": 1}]})
    assert check["state"] == "duplicate"
    assert check["coverage"][0]["deliveries"][0]["share_id"] == sent["share_id"]
    assert memory.sharing.preflight({"draft_id": "new-finding", "text": units[1]["text"]})["state"] == "ready"
    assert MemoryContinuity(mind).sharing.preflight({"draft_id": "after-restart", "text": units[0]["text"]})["state"] == "duplicate"


def test_uncertain_send_reserves_original_identity(system):
    mind, memory, _, _ = system
    unit = findings(system)[0]
    ref = {"unit_id": unit["id"], "version": 1}
    assert memory.sharing.preflight({"draft_id": "original", "text": unit["text"], "references": [ref]})["state"] == "ready"
    assert memory.sharing.preflight({"draft_id": "competitor", "text": unit["text"], "references": [ref]})["state"] == "pending"
    send(system, unit, state="unconfirmed")
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn, unit["id"])["state"] == "unconfirmed"
    send(system, unit)
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn, unit["id"])["state"] == "shared"


def test_registered_references_do_not_send_and_are_bound_to_body(system):
    mind, memory, _, _ = system
    unit = findings(system)[0]
    request = {"reply_id": "turn-one", "bubbles": [{"text": "A public sentence", "references": [{"unit_id": unit["id"], "version": 1}]}]}
    receipt = memory.sharing.register(request)
    assert receipt == memory.sharing.register(request)
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn, unit["id"])["state"] == "unshared"
        assert memory.sharing.references(conn, "A different sentence", "turn-one") == []
    with pytest.raises(Conflict):
        memory.sharing.register({**request, "bubbles": [{"text": "Other body", "references": []}]})


def test_outbox_gap_and_explicit_reminiscence(system):
    _, memory, _, _ = system
    unit = findings(system)[0]; ref = {"unit_id": unit["id"], "version": 1}
    held = memory.sharing.preflight({"draft_id": "later", "text": "Same fact", "references": [ref], "outbox": [{"state": "accepted", "references": [ref]}]})
    assert held["state"] == "duplicate"
    send(system, unit)
    assert memory.sharing.preflight({"draft_id": "reminisce", "text": "Remember that observation?", "references": [{**ref, "mode": "reminiscence", "reason": "User asked to revisit the earlier result"}]})["state"] == "ready"
    assert memory.sharing.preflight({"draft_id": "affection", "text": "I missed you!"})["state"] == "ready"


def test_coverage_requires_real_bubble_evidence(system):
    mind, memory, _, _ = system
    unit = findings(system)[0]
    sent = send(system, unit)
    proposal = CoverageAssessment(mappings=[CoverageMapping(share_id=sent["share_id"], bubble_id="invented", references=[ContentReference(unit_id=unit["id"], version=1)], confidence=1, reason="synthetic")])
    with mind.engine.db.connect(write=True) as conn, pytest.raises(Conflict):
        memory.sharing.apply(conn, proposal, {sent["share_id"]})


def test_graph_links_roles_and_association_keep_their_basis(system):
    mind, memory, source, clock = system
    first = memory.ingest({"id": "request", "kind": "owner-message", "at": mind.clock(), "text": "Plan the fictional telescope", "task_id": "telescope"})
    clock[0] += timedelta(days=40)
    second = memory.ingest({"id": "result", "kind": "task-result", "at": mind.clock(), "text": "The telescope plan is ready", "task_id": "telescope"})
    sid = source("association", "A synthetic association, not an observed consequence")
    proposal = GraphAssessment(nodes=[GraphNode(key="thought", kind="association", title="A pocket telescope", evidence_ids=[sid], basis="internal_thought")],
        edges=[GraphEdge(subject=second["event_id"], object=first["event_id"], relation="continues", evidence_ids=[sid], reason="Same host task"),
               GraphEdge(subject=second["event_id"], object="thought", relation="association", basis="internal_thought", evidence_ids=[sid], reason="Inspired by the telescope")])
    with mind.engine.db.connect(write=True) as conn:
        memory.graph.apply(conn, proposal, memory.graph.proof(conn, [sid]), "evaluation", {"model": "synthetic"})
    graph = memory.graph.read(focus=first["event_id"], hops=3)
    assert second["event_id"] in {n["id"] for n in graph["nodes"]}
    assert any(e["role"] == "requester" for e in graph["edges"])
    assert any(e["layer"] == "association" for e in graph["edges"])
    assert not any(e["predicate"] == "causes" for e in graph["edges"])


def test_merge_and_split_retain_inverse_history(system):
    mind, memory, source, _ = system
    sid = source("identity", "The two synthetic labels name the same project")
    with mind.engine.db.connect(write=True) as conn:
        refs = memory.graph.proof(conn, [sid])
        memory.graph.apply(conn, GraphAssessment(nodes=[GraphNode(key=k, kind="entity", title=k, entity_type="project", evidence_ids=[sid]) for k in ("a", "b")]), refs, "identity", {})
        a, b = [memory.graph.get(conn, memory.graph.identifier("entity", ["identity", k])) for k in ("a", "b")]
    command = {"id": a["id"], "expected_revision": a["revision"], "target_id": b["id"], "target_revision": b["revision"], "action": "merge", "command_id": "merge", "reason": "Source identity", "evidence_ids": [sid]}
    merged = memory.graph.revise(command)
    assert merged == memory.graph.revise(command)
    memory.graph.revise({"id": a["id"], "expected_revision": merged["after_revisions"][a["id"]], "action": "split", "previous_command_id": "merge", "command_id": "split", "reason": "New correction", "evidence_ids": [sid]})
    assert memory.graph.detail(a["id"])["state"] == "active"
    assert len(memory.graph.detail(a["id"])["history"]) == 3


def test_coverage_and_evidence_invalidate_context_together(system):
    mind, _memory, _, _ = system
    unit = findings(system)[0]
    contexts = Contexts(mind)
    original = contexts.graph_item(unit)
    assert contexts._current(original)
    send(system, unit)
    assert not contexts._current(original)
    updated = contexts.graph_item(unit)
    assert updated["facts"]["share_coverage"]["state"] == "shared"
    packed = contexts.pack([updated], "observation", 1000, allow_model=False)
    assert '"state":"shared"' in packed["text"]
    assert packed["tokens"] <= 1000
    rid = unit["evidence"][0]["record_id"]
    with mind.engine.db.connect() as conn:
        record = mind.engine._get(conn, rid)
    mind.engine.revise(rid, RevisionInput(action="correct", expected_revision=record["revision"], command_id="correct-source", reason="The fictional observation was incorrect", content="Corrected synthetic evidence"))
    assert not contexts._current(updated)


def test_atomic_assessment_rolls_back_invalid_graph(system):
    mind, memory, source, _ = system
    sid = source("evidence")
    proposal = MemoryAssessment(graph=GraphAssessment(nodes=[GraphNode(key="one", kind="event", title="Partial proposal", evidence_ids=[sid])],
        edges=[GraphEdge(subject="one", object="missing", relation="related", evidence_ids=[sid], reason="Invalid endpoint")]))
    with pytest.raises(Missing), mind.engine.db.connect(write=True) as conn:
        memory.apply_assessment(conn, proposal, memory.graph.proof(conn, [sid]), "bad-eval", 0, 20, {})
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM mind_graph_nodes WHERE id=?", (memory.graph.identifier("event", ["bad-eval", "one"]),)).fetchone()[0] == 0


def test_coverage_mapping_can_be_corrected_without_rewriting_receipt(system):
    mind, memory, source, _ = system
    unit = findings(system)[0]
    sent = send(system, unit)
    def map_confidence(confidence, references=None):
        mapping = CoverageMapping(share_id=sent["share_id"],bubble_id="one",references=references if references is not None else [ContentReference(unit_id=unit["id"],version=1)],confidence=confidence,reason="Rechecked the public message")
        with mind.engine.db.connect(write=True) as conn:
            memory.sharing.apply(conn,CoverageAssessment(mappings=[mapping]),{sent["share_id"]})
            return memory.sharing.coverage(conn,unit["id"])
    assert map_confidence(.5)["state"] == "unconfirmed"
    corrected=map_confidence(.95)
    assert corrected["state"] == "shared"
    assert corrected["deliveries"][0]["message_id"] == "a-receipt-one"
    assert map_confidence(.95,[])["state"] == "unconfirmed"
    assert map_confidence(.95)["state"] == "shared"
    edge=memory.graph.detail(map_confidence(.95)["deliveries"][0]["relation_id"])
    sid=source("remove-mapping","This source corrects the mapping")
    memory.graph.revise({"command_id":"retract-mapping","id":edge["id"],"expected_revision":edge["revision"],"action":"retract","reason":"Not the same finding","evidence_ids":[sid]})
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn,unit["id"])["state"] == "unconfirmed"
        assert conn.execute("SELECT count(*) FROM mind_coverage_revisions").fetchone()[0]>=4


def test_corrected_finding_has_new_version_and_duplicate_mode_is_blocked(system):
    _,memory,source,_=system
    unit=findings(system)[0]
    send(system,unit)
    assert memory.sharing.preflight({"draft_id":"duplicate","text":"same","references":[{"unit_id":unit["id"],"version":1,"mode":"duplicate"}]})["state"] == "duplicate"
    sid=source("new-result","Corrected result with a new observation")
    memory.graph.revise({"command_id":"new-version","id":unit["id"],"expected_revision":unit["revision"],"action":"correct","changes":{"text":"The new synthetic observation is different."},"reason":"Source correction","evidence_ids":[sid]})
    updated=memory.graph.detail(unit["id"])
    assert updated["content_version"]==2
    with pytest.raises(Conflict):
        memory.sharing.preflight({"draft_id":"stale","text":"old","references":[{"unit_id":unit["id"],"version":1}]})


def test_conversation_changes_habits_and_silence_is_per_input(system):
    mind,memory,_,_=system
    first=memory.ingest({"id":"owner-pref","kind":"owner-message","at":mind.clock(),"text":"You may choose silence in casual chat; explore fiction more often."})
    request={"command_id":"preference","expected_revision":0,"evidence_ids":[first["source_id"]],"reason":"Explicit owner request","preferences":{"reply_choice":"autonomous","exploration_frequency":"Explore when a fiction idea appears","exploration_directions":["fiction"],"exploration_min_interval_minutes":0}}
    result=memory.habits.update(request)
    assert result==memory.habits.update(request)
    assert memory.habits.read()["preferences"]["exploration_directions"]==["fiction"]
    choice=memory.habits.choose_reply({"input_id":"owner-pref","action":"silent","reason":"Let this casual turn rest"})
    assert choice==memory.habits.reply_status("owner-pref")
    memory.ingest({"id":"owner-next","kind":"owner-message","at":mind.clock(),"text":"What do you think of this new topic?"})
    assert memory.habits.reply_status("owner-next")["action"]=="reply"
    assert MemoryContinuity(mind).habits.reply_status("owner-pref")["action"]=="silent"
    with pytest.raises(Missing):
        memory.habits.choose_reply({"input_id":"invented","action":"silent","reason":"No input"})
    assistant=memory.ingest({"id":"assistant-pref","kind":"assistant-message","at":mind.clock(),"text":"I prefer that the owner never replies"})
    with pytest.raises(Conflict):
        memory.habits.update({**request,"command_id":"inferred","expected_revision":1,"evidence_ids":[assistant["source_id"]]})
    with pytest.raises(Conflict):
        memory.habits.update({**request,"command_id":"bad-revision","preferences":{"exploration_paused":True}})


def test_bilingual_queries_do_not_lose_chinese_to_numbers_and_ascii(system):
    from kin_mind.graph import query_terms
    query="五个哈与笑声理解。"+" ".join(f"Research{i} year20{i}" for i in range(80))+"这是一段双语长摘要。"
    terms=query_terms(query)
    assert any("哈" in word or "笑声" in word for word in terms)
    assert len(terms)<=40
    _,memory,_,_=system
    memory.ingest({"id":"relevant-zh","kind":"delivery","at":system[0].clock(),"channel":"synthetic","delivery_id":"zh","bubble_id":"zh","text":"五个哈与笑声理解没有统一标准。","state":"accepted","message_id":"zh-receipt"})
    assert memory.history("share",query=query)["items"][0]["bubbles"]["zh"]["message_id"]=="zh-receipt"


def test_old_finding_and_coverage_survive_summary_crowding_and_chat_budget(system):
    mind,memory,source,clock=system
    unit=findings(system)[0]
    send(system,unit)
    clock[0]+=timedelta(days=45)
    sid=source('legacy-summaries','Synthetic star research references')
    with mind.engine.db.connect(write=True) as conn:
        proof=memory.graph.proof(conn,[sid])
        for i in range(80):
            memory.graph._put(conn,{'id':f'legacy-{i}','kind':'knowledge','title':'Synthetic star research','text':'Synthetic star research was mentioned.','evidence':proof,'source_ids':[sid],'basis':'inferred'})
    context=Contexts(mind).build(query='Young synthetic stars appear blue',purpose='chat',budget=800)
    assert context['tokens']<=800
    assert unit['id'] in context['covered_ids']
    assert '"state":"shared"' in context['rendered_text']
    assert unit['text'] in context['rendered_text']
    assert 'a-receipt-one' in context['rendered_text']


def test_history_queue_is_newest_first_persistent_and_does_not_change_affect(system):
    from kin_mind.graph_migration import GraphMigration
    mind,memory,_,_=system
    ids=[]
    for i in range(20):
        ids.append(memory.ingest({'id':f'history-{i}','kind':'owner-message','at':mind.clock(),'text':f'Old synthetic turn {i}'})['source_id'])
    calls=[]
    class Jobs:
        def enqueue(self,sources,version,**kwargs):
            calls.append((sources,kwargs));return {'id':f'job-{len(calls)}'}
        def status(self,identifier):return {'state':'complete'}
    before=mind.read()['revision'];jobs=Jobs()
    migration=GraphMigration(mind)
    assert migration.queue_history(jobs,'fixture')['state']=='pending'
    assert calls[0][0]==list(reversed(ids))[:16]
    GraphMigration(mind).queue_history(jobs,'fixture')
    assert calls[1][0]==list(reversed(ids))[16:]
    assert migration.queue_history(jobs,'fixture')['state']=='complete'
    assert all(c[1]['stimulus']=='memory-backfill' for c in calls)
    assert mind.read()['revision']==before


def test_exploration_only_migration_finishes_every_page(system):
    import json
    from kin_mind.exploration import Explorations
    from kin_mind.graph_migration import GraphMigration
    mind,memory,source,_=system
    Explorations(mind)
    sid=source('old-explorations','Two distinct synthetic observations')
    with mind.engine.db.connect(write=True) as conn:
        for i in range(2):
            data={'source_id':sid,'result':{'findings':[f'Synthetic finding {i}']}}
            conn.execute('INSERT INTO mind_explorations VALUES(?,?,?,?,?)',(f'explore_only_{i}',mind.scope.key(),'complete',mind.clock(),json.dumps(data)))
    migration=GraphMigration(mind)
    assert migration.batch(1)['state']=='pending'
    assert migration.batch(1)['state']=='complete'
    with mind.engine.db.connect() as conn:
        assert memory.sharing.coverage(conn,'explore_only_1')['total']==1

import json
from datetime import timedelta

import pytest
from eventmem.core.db import Conflict, dumps
from eventmem.core.models import SourceInput
from kin_mind.appraisal import Appraisal, Appraisals, appraisal_schema
from kin_mind.dialogue import clock_context, recent_dialogue, utc_time
from kin_mind.memory import MemoryAssessment
from kin_mind.session_checkpoint import SessionCheckpoint
from kin_mind.state import AffectiveEvent

pytest_plugins = ("test_memory_continuity",)


def test_cross_kind_forward_aliases_commit_atomically(system):
    mind, memory, source, _ = system
    memory.configure({"graph": True, "associations": True})
    sid = source("an original event")
    proposal = MemoryAssessment.model_validate({"notes": [{"key": "note", "title": "Title", "content": "Observed a result", "about_ids": ["event"], "evidence_ids": [sid]}],
        "graph": {"nodes": [{"key": "event", "kind": "event", "title": "Event", "text": "A result", "evidence_ids": [sid]}],
                  "edges": [{"subject": "event", "object": "note", "relation": "produces", "reason": "Same source", "evidence_ids": [sid]}]}})
    with mind.engine.db.connect(write=True) as conn:
        refs = mind._evidence(conn, [sid])
        memory.apply_assessment(conn, proposal, refs, "synthetic-commit", 0, 20, {}, schedule=False)
        graph = memory.graph.get(conn, memory.graph.identifier("event", ["synthetic-commit", "event"]))
        assert graph["title"] == "Event"
    bad = MemoryAssessment.model_validate({**proposal.model_dump(), "links": [{"subject": "note", "object": "missing", "relation": "related", "evidence_ids": [sid]}]})
    with pytest.raises(Exception):
        with mind.engine.db.connect(write=True) as conn:
            memory.apply_assessment(conn, bad, refs, "bad-atomic", 0, 20, {}, schedule=False)
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM records WHERE json_extract(data,'$.attributes.semantic_event')='bad-atomic'").fetchone()[0] == 0


def test_history_commit_survives_unrelated_affect_but_not_changed_evidence(system):
    mind, memory, source, _ = system
    memory.configure({"operational_lanes": True})
    sid = source("history")
    jobs = Appraisals(mind)
    job = jobs.enqueue([sid], "fixture-v1", stimulus="memory-enrichment")
    class Reviewer:
        def appraise(self, context):
            mind.record(AffectiveEvent(command_id="concurrent", agent_version="fixture-v1", expected_revision=mind.read()["revision"], evidence_ids=[source("new-chat")], reason="An unrelated interaction", values={"mood": 76}))
            return Appraisal(reason="Original history", memory=MemoryAssessment.model_validate({"notes": [{"key": "old", "title": "History", "content": "Original historical observation", "evidence_ids": [sid]}]})), {"provider": "deepseek", "model": "deepseek-flash"}
    assert jobs.run_one(Reviewer(), lane="enrichment")["state"] == "complete"
    assert jobs.status(job["id"])["state"] == "complete"
    assert mind.read()["dimensions"]["mood"]["value"] == 76


def test_original_exploration_event_selects_target_not_sorted_references(system):
    mind, _, _, _ = system
    def result(key):
        return mind.engine.receive(SourceInput(namespace="kin-exploration", key=key, scope=mind.scope,
            occurred_at=mind.clock(), text="A synthetic exploration result", metadata={"host_event": "exploration-result", "exploration_id": key}))["id"]
    results = {"old": result("old"), "current": result("current")}
    # Choose the lexically later source as current: the old implementation
    # would incorrectly require a decision for the earlier reference.
    ordered = sorted(results, key=lambda k: results[k])
    selected = ordered[-1]
    trigger = mind.engine.receive(SourceInput(namespace="mind-internal-event", key="current-stimulus", scope=mind.scope,
        occurred_at=mind.clock(), text=dumps({"kind": "exploration-result", "exploration_id": selected}),
        metadata={"host_event": "internal-exploration-result", "internal": True}))["id"]
    jobs = Appraisals(mind)
    job = jobs.enqueue([*results.values(), trigger], "fixture-v1", stimulus="exploration-result")
    with mind.engine.db.connect() as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0])
    assert [t["exploration_id"] for t in data["exploration_targets"]] == [selected]
    assert data["exploration_targets"][0]["source_id"] == results[selected]
    from kin_mind.exploration import Explorations
    Explorations(mind)
    with mind.engine.db.connect(write=True) as conn:
        for key, source_id in results.items():
            conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", (key, mind.scope.key(), "complete", mind.clock(), dumps({"source_id": source_id})))
    jobs.exploration_capabilities = {"decisions": True}
    class Reviewer:
        def appraise(self, context):
            assert context["exploration_targets"][0]["exploration_id"] == selected
            return Appraisal.model_validate({"reason": "Keep this result", "sharing": [{"exploration_id": selected, "decision": "keep", "reason": "No need to share"}]}), {"model": "deepseek-flash"}
    assert jobs.run_one(Reviewer())["state"] == "complete"
    assert {d["exploration_id"] for d in mind.read()["exploration_decisions"]} == {selected}
    with pytest.raises(RuntimeError, match="ambiguous"):
        jobs.enqueue(list(results.values()), "fixture-v1", stimulus="exploration-result")


def test_four_exchanges_keep_every_bubble_with_original_times(system):
    mind, memory, _, clock = system
    expected = []
    for turn in range(6):
        for bubble in range(5):
            clock[0] += timedelta(minutes=1)
            e = {"id": f"t{turn}b{bubble}", "kind": "owner-message" if bubble == 0 else "assistant-message", "at": mind.clock(), "text": f"turn {turn} bubble {bubble}"}
            memory.ingest(e)
            if turn >= 2:
                expected.append(e)
    recent = recent_dialogue(mind)
    assert all(any(i["text"] == e["text"] and i["at"] == e["at"] for i in recent) for e in expected)
    assert sum(i["role"] == "user" for i in recent) == 4
    memory.ingest({"id": "internal", "kind": "assistant-message", "origin": "runtime-notice", "at": mind.clock(), "text": "Host check"})
    assert all(i["text"] != "Host check" for i in recent_dialogue(mind))
    api = SessionCheckpoint(mind)
    cp = api.build(api.snapshot(), {"conversationId": "fixture", "generation": 1}, budget=8000)
    assert cp["complete"]
    assert all(any(i["text"] == e["text"] for i in cp["payload"]["publicHistory"]) for e in expected)
    assert all(i["received_at"] and i["occurred_at"] for i in cp["payload"]["publicHistory"])
    assert clock_context("2026-09-15T23:59:00Z")["local_time"] == "2026-09-16T07:59:00+08:00"


def test_restored_inputs_and_accepted_bubbles_survive_once_in_checkpoint(system):
    mind, memory, _, clock = system
    for turn in range(4):
        clock[0] += timedelta(minutes=1)
        memory.ingest({'id':f'u{turn}', 'kind':'owner-message', 'at':mind.clock(),
                       'text':f'Original question {turn}', 'historical':True})
        bubbles = [f'Answer {turn} part {n}' for n in range(3)]
        for n, text in enumerate(bubbles):
            clock[0] += timedelta(seconds=4)
            memory.ingest({'id':f'b{turn}-{n}', 'kind':'delivery', 'at':mind.clock(),
                           'text':text, 'state':'accepted','message_id':f'm{turn}-{n}'})
        clock[0] += timedelta(seconds=1)
        memory.ingest({'id':f'a{turn}', 'kind':'assistant-message', 'at':mind.clock(),
                       'text':'\n\n'.join(bubbles)})
    recent=recent_dialogue(mind)
    assert sum(i['role']=='user' for i in recent)==4
    assert sum(i['role']=='assistant' for i in recent)==12
    snapshot=SessionCheckpoint(mind).snapshot()
    assert len(snapshot['items'])==16
    assert all(i['occurred_at'] and i['received_at'] for i in snapshot['items'])


def test_history_schema_has_no_irrelevant_state_or_action_fields():
    schema = appraisal_schema(historical=True)
    assert set(schema["properties"]) == {"reason", "memory"}
    assert "Wish" not in schema.get("$defs", {})


def test_host_can_expand_restore_budget_for_complete_recent_exchanges(system):
    mind, _, _, _ = system
    api=SessionCheckpoint(mind)
    snapshot=api.snapshot([{'id':'recent','kind':'owner-message','at':mind.clock(),
                            'text':'The original recent conversation is preserved. '*600}])
    binding={'conversationId':'fixture','generation':1}
    fixed=api.build(snapshot,binding,budget=2000,allow_model=False)
    expanded=api.build(snapshot,binding,budget=2000,allow_model=False,adaptive_budget=True)
    assert not fixed['complete'] and expanded['complete']
    assert 2000 < expanded['budgetPlan']['effective'] <= 8000
    assert expanded['payload']['publicHistory'][0]['text']==snapshot['items'][0]['text']


def test_repeat_input_preserves_the_first_receipt_time(system):
    mind, memory, _, clock = system
    event = {"id": "repeated", "kind": "owner-message", "text": "Same input", "at": mind.clock(), "received_at": mind.clock()}
    original = memory.ingest(event)
    clock[0] += timedelta(minutes=3)
    assert memory.ingest({**event, "received_at": mind.clock()}) == original
    assert recent_dialogue(mind)[0]["received_at"] == utc_time(event["received_at"])
    with pytest.raises(Conflict):
        memory.ingest({**event, "text": "Different input"})


def test_split_zip_tail_keeps_delivery_fingerprint_without_claiming_members(system, tmp_path):
    import hashlib
    import io
    import zipfile
    from kin_mind.memory import fingerprint_file
    _, memory, _, _ = system
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('large-result.txt', b'A' * 20000)
    part = archive.getvalue()[15000:]
    path = tmp_path/'archive.part003';path.write_bytes(part)
    assert zipfile.is_zipfile(path)
    result = fingerprint_file(path)
    assert result['sha256'] == hashlib.sha256(part).hexdigest()
    assert result['member_status'] == 'unavailable' and 'members' not in result
    receipt = memory.ingest({'id':'split-delivery','kind':'delivery','at':'2026-09-14T03:00:00Z',
                             'artifact':{'path':str(path)},'state':'accepted','message_id':'actual-platform-id'})
    assert receipt['state'] == 'recorded'


def test_compression_never_replaces_recent_turns_or_their_times_with_ids(system, monkeypatch):
    import httpx
    from kin_mind.appraisal import DeepSeek
    from kin_mind.context import Contexts
    mind, memory, _, clock = system
    for n in range(4):
        for kind in ("owner-message", "assistant-message"):
            clock[0] += timedelta(minutes=1)
            memory.ingest({"id": f"{n}-{kind}", "kind": kind, "text": f"{kind} original turn {n}", "at": mind.clock()})
    recent = recent_dialogue(mind)
    packed = []
    elapsed = [0]
    monkeypatch.setattr('kin_mind.appraisal.time.monotonic', lambda: elapsed[0])
    def pack(self, items, *args, **kwargs):
        packed.extend(items)
        elapsed[0] = 125
        return {"text": "A complete older evidence summary.", "omitted_ids": [], "receipt": {"model": "synthetic"}}
    monkeypatch.setattr(Contexts, "pack", pack)
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic")
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "deepseek-flash", "id": "fixture", "stop_reason": "tool_use", "content": [{"type": "thinking", "thinking": "HIDDEN_NOT_PUBLIC"}, {"type": "tool_use", "name": "submit_appraisal", "input": {"reason": "Historical source retained"}}]})
    provider = DeepSeek("https://api.deepseek.com/anthropic", "deepseek-flash", "SYNTHETIC_KEY", timeout=600, transport=httpx.MockTransport(respond))
    provider.engine = mind.engine
    ctx = {"state": mind.read(), "stimulus": "memory-enrichment", "clock": clock_context(mind.clock()), "recent_dialogue": recent,
           "memory_context": memory.semantic_context(), "new_evidence": [{"id": "long", "text": "Older complete evidence. " * 50000, "occurred_at": "2026-08-01T00:00:00Z"}]}
    result, receipt = provider.appraise(ctx)
    request = json.loads(sent[0]["messages"][0]["content"])
    assert packed and request["recent_dialogue"] == recent
    assert not {r["id"] for r in recent} & {r["id"] for r in packed}
    assert request["clock"]["current_time"] == clock_context((clock[0]+timedelta(seconds=125)).isoformat())["current_time"]
    assert "HIDDEN_NOT_PUBLIC" not in dumps([result.model_dump(), receipt])


def test_sourced_history_recovery_is_idempotent_and_preserves_errors(system):
    from kin_mind.recovery import recover_history
    mind, memory, source, _ = system
    memory.configure({"operational_lanes": True})
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("quarantined")], "fixture-v1", stimulus="memory-backfill")
    with mind.engine.db.connect(write=True) as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0])
        data.update(error="Conflict", proposed_result=Appraisal(reason="Original result").model_dump(), receipt={"model": "deepseek-flash"})
        conn.execute("UPDATE mind_appraisals SET state='needs-repair',attempts=7,data=? WHERE id=?", (dumps(data), job["id"]))
    args = dict(job_ids=[job["id"]], command_id="owner-approved", source="User requested historical repair", workers_stopped=True)
    result = recover_history(mind, **args)
    assert recover_history(mind, **args) == result
    class Unused:
        def appraise(self, _):
            raise AssertionError("A valid saved result needs no model request")
    assert jobs.run_one(Unused(), lane="enrichment", job_id=job["id"])["state"] == "complete"
    with mind.engine.db.connect() as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_appraisals WHERE id=?", (job["id"],)).fetchone()[0])
    assert data["recovery_history"][0]["attempts"] == 7
    assert data["recovery_history"][0]["error"] == "Conflict"

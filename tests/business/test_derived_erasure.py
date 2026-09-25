"""What is written from a source keeps none of its words once the source is deleted, however the
delete and the write interleave -- an appraisal, a reflection, a creation, an exploration, a daily
review (CR5-MM-01, CR5-MM-02, CR5-MM-09) -- and an attempt that ends before its model call is not
charged (CR5-MM-07).

Each case blocks where the model answers or the write is about to happen, deletes, lets it go on,
and then reads every table of the store for the words."""
import json
from datetime import datetime, timezone

from eventmem.core.db import Conflict
from eventmem.core.models import SourceInput

from kin_mind import attempts
from kin_mind.appraisal import Appraisal, Appraisals, DailyReview
from kin_mind.continuity import Understanding
from kin_mind.creation import accept_result
from kin_mind.erasure import ERASED
from kin_mind.exploration_decisions import SharingDecision
from kin_mind.memory import MemoryContinuity, fingerprint_file
from test_erasure import settle, texts_everywhere

pytest_plugins = ('test_kin_mind', 'test_autonomous_plans')

MARKER = "xyzzyquux"
USAGE = {"input_tokens": 700, "output_tokens": 30}
TARGET = "explore_derived"


def answered(proposal):
    return proposal, {"provider": "deepseek", "model": "synthetic", "request_id": "req-1", "usage": USAGE}


def paid(caller):
    attempts.record_call(caller, "submit_appraisal", outcome="ok", model="synthetic", request_id="req-1", usage=USAGE)


def queue_row(mind, job_id):
    with mind.engine.db.connect() as conn:
        found = conn.execute("SELECT state,attempts,data FROM mind_appraisals WHERE id=?", (job_id,)).fetchone()
    return found["state"], found["attempts"], json.loads(found["data"])


def test_a_source_shown_only_for_recall_and_deleted_while_the_model_answers_leaves_no_words(setup):
    """The appraisal's own evidence stays; an earlier message, shown only as recent dialogue, is
    deleted while the model answers, and the answer repeats it. The commit refuses, and the queue
    row keeps the attempt, its cost and its error, with none of the words. The retry asks again in
    full: the proposal the delete emptied is neither reused nor revalidated (CR5-MM-01)."""
    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": f"我小时候住在 {MARKER} 街"})["source_id"]
    primary = source("today", "今天天气不错")

    class Reader:
        def appraise(self, context):
            assert MARKER in json.dumps(context, ensure_ascii=False), "the recall source was shown"
            mind.engine.delete(recalled)
            paid(self)
            return answered(Appraisal(reason=f"想起她住过 {MARKER} 街", values={"curiosity": 66}))

    class Again:
        """The retry: a full call. A light question would be about a proposal the delete emptied."""
        def appraise(self, context):
            assert MARKER not in json.dumps(context, ensure_ascii=False)
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal written from a deleted source is not revalidated")

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["error"] and count == 1
    assert any(ref.get("erased") for ref in data["evaluated_sources"]), "the deleted source stays named, as a tombstone"
    [ledger] = attempts.read(mind.engine, mind.scope.key(), job_id=job["id"])["attempts"]
    assert ledger["charged"] is True and ledger["calls"][0]["usage"] == USAGE
    assert mind.read()["dimensions"]["curiosity"]["value"] != 66, "the commit was refused"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and "tier" not in data, data.get("error")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 55 and ERASED not in json.dumps(mind.read(), ensure_ascii=False)


def test_a_deferred_share_keeps_no_words_of_its_deleted_result(setup):
    """A sharing decision deferred with a condition: deleting the result it was about takes the
    condition as well as the reason, in the state and in every revision of it (CR5-MM-01)."""
    mind, source, clock = setup
    engine = mind.engine
    with engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,"
                     "created_at TEXT NOT NULL,data TEXT NOT NULL)")
    result = engine.receive(SourceInput(namespace="test", key="derived-exploration", scope=mind.scope,
                                        text=f"探索结果：{MARKER} 的来历", authority="model", occurred_at=clock[0].isoformat(),
                                        metadata={"host_event": "exploration-result", "exploration_id": TARGET}))["id"]
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                     (TARGET, mind.scope.key(), "complete", mind.clock(), json.dumps({"source_id": result})))

    class Decider:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="结果先放一放", values={"curiosity": 61}, sharing=[SharingDecision(
                exploration_id=TARGET, decision="defer", reason="等她自己问起",
                reconsider_when=f"她再提到 {MARKER} 的时候")]))

    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([result], "synthetic-v1", origin="exploration", stimulus="exploration-result")
    jobs.run_one(Decider())
    with engine.db.connect() as conn:
        decision = mind._load(conn).get("exploration_decisions", {}).get(TARGET)
    assert decision and MARKER in decision["reconsider_when"], queue_row(mind, job["id"])[2].get("error")
    engine.delete(result)
    settle(engine)
    assert texts_everywhere(engine, MARKER) == set()
    with engine.db.connect() as conn:
        decision = mind._load(conn)["exploration_decisions"][TARGET]
    assert decision["decision"] == "defer" and decision["reconsider_when"] == "[已删除]", "what was decided stays"


def test_a_copied_field_loses_its_words_with_what_it_was_copied_from():
    """A decision's words copied into the wish it settles name what they were written from beside
    them: an erase of that takes the copies and only them; the wish's own words stay (CR5-MM-01)."""
    from kin_mind.erasure import scrub

    gone = "src_" + "a" * 32
    wish = {"id": "desire-1", "content": "弄清那条街的来历", "evidence": [{"source_id": "src_" + "b" * 32, "record_id": "mem_" + "b" * 32}],
            "reason": f"等她提起 {MARKER} 再说", "reason_evidence_ids": [gone],
            "contact_wait": {"condition": "new_evidence", "reason": f"她再提到 {MARKER} 的时候", "reason_evidence_ids": [gone]}}
    erased = scrub(wish, frozenset({gone}))
    assert erased["reason"] == ERASED and erased["contact_wait"]["reason"] == ERASED
    assert erased["content"] == wish["content"] and erased["contact_wait"]["condition"] == "new_evidence"
    assert scrub(erased, frozenset({gone})) is erased, "a second erase changes nothing"
    assert scrub(wish, frozenset({"src_" + "c" * 32})) is wish, "nothing else erases them"


def test_what_a_run_or_a_concern_wrote_goes_and_its_codes_and_ids_stay():
    """The free text of an exploration's run, a creation's review gaps and a concern's target goes
    with what they rest on; codes and identities beside them stay (CR5-MM-01)."""
    from kin_mind.erasure import scrub

    gone = "mem_" + "a" * 32
    run = {"desire_id": "desire-1", "state": "complete", "selected_brief": f"查清 {MARKER} 街的来历", "evidence_ids": [gone],
           "result": {"summary": f"{MARKER} 街建于 1920 年", "findings": [f"{MARKER} 街原名西街"],
                      "sources": [{"url": "https://example.com/street", "title": f"{MARKER} 街志"}],
                      "open_questions": [f"{MARKER} 街为什么改名"]}}
    erased = scrub(run, frozenset({gone}))
    assert MARKER not in json.dumps(erased, ensure_ascii=False)
    assert erased["result"]["sources"][0]["url"] == "https://example.com/street" and erased["state"] == "complete"
    settled = {"evidence_ids": [gone], "verification_gaps": ["completion-not-established", f"还没核对 {MARKER} 的年份"]}
    assert scrub(settled, frozenset({gone}))["verification_gaps"] == ["completion-not-established", ERASED]
    concern = {"id": "concern-1", "kind": "care", "evidence": [{"source_id": "src_" + "a" * 32, "record_id": gone}],
               "target": f"{MARKER} 街的老房子", "error_detail": {"code": "evidence-not-current", "target": gone}}
    erased = scrub(concern, frozenset({gone}))
    assert erased["target"] == ERASED and erased["error_detail"] == concern["error_detail"], "an identity is no word"


def test_a_role_the_model_wrote_goes_and_a_coded_role_stays():
    """A graph edge's `role` is the model's own words; a message's `role` is a code. An erase
    takes the first and keeps the second (CL6-MM-09)."""
    from kin_mind.erasure import scrub

    gone = "mem_" + "a" * 32
    evidence = [{"source_id": "src_" + "a" * 32, "record_id": gone}]
    edge = {"id": "edge-1", "relation": "participates", "evidence": evidence, "reason": "她提过", "role": f"{MARKER} 街的老住户"}
    assert scrub(edge, frozenset({gone}))["role"] == ERASED
    assert scrub({**edge, "role": "participant"}, frozenset({gone}))["role"] == "participant"
    said = {"role": "user", "text": f"我住在 {MARKER} 街", "evidence": evidence}
    assert scrub(said, frozenset({gone})) == {**said, "text": ERASED, "evidence": [{**evidence[0], "erased": True}]}


def test_a_reflection_whose_source_is_deleted_after_the_commit_is_not_kept(setup, monkeypatch):
    """The appraisal commits its understanding; its source is deleted before the reflection is
    stored as a source of its own. The reflection is not kept, and nothing of the words is left
    (CR5-MM-02)."""
    mind, source, clock = setup
    primary = source("afternoon", f"她说起 {MARKER} 的那个下午")
    record = mind.engine.source(primary)["record_ids"][0]

    class Thinker:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="想了想那个下午", values={"curiosity": 62}, understanding=Understanding(
                meaning=f"那个 {MARKER} 的下午让我安静下来", topic="下午", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))

    real = MemoryContinuity.remember_reflection

    def deleted_first(self, result):
        mind.engine.delete(primary)
        return real(self, result)

    monkeypatch.setattr(MemoryContinuity, "remember_reflection", deleted_first)
    jobs = Appraisals(mind)
    jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Thinker())
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone()
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_reflection_goes_with_the_source_it_was_written_from(setup):
    """Kept while its source stood, a reflection is that source's dependent: deleting the source
    takes the reflection, source and record, with it (CR5-MM-02)."""
    mind, source, clock = setup
    primary = source("evening", f"她说起 {MARKER} 的那个晚上")
    record = mind.engine.source(primary)["record_ids"][0]

    class Thinker:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="想了想那个晚上", values={"curiosity": 63}, understanding=Understanding(
                meaning=f"那个 {MARKER} 的晚上很安静", topic="晚上", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))

    jobs = Appraisals(mind)
    jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Thinker())
    with mind.engine.db.connect() as conn:
        [reflection] = [row[0] for row in conn.execute("SELECT id FROM sources WHERE namespace='kin-reflection'")]
    assert MARKER in mind.engine.source(reflection, content=True).read_text()
    mind.engine.delete(primary)
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM tombstones WHERE key=?", (reflection,)).fetchone()
        assert not conn.execute("SELECT 1 FROM sources WHERE id=?", (reflection,)).fetchone()
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_reflection_is_not_stored_once_a_source_it_was_only_shown_is_deleted(setup, monkeypatch):
    """The appraisal committed while everything it was shown stood; a message it saw only as recent
    dialogue is deleted before the reflection is stored. The reflection is not stored, so no record
    or source holds the words. What the commit wrote before the delete goes with what it cites, as
    every committed mind entry does (CR5-MM-02)."""
    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-before", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": f"我小时候住在 {MARKER} 街"})["source_id"]
    primary = source("quiet", "今天下午很安静")
    record = mind.engine.source(primary)["record_ids"][0]

    class Thinker:
        def appraise(self, context):
            assert MARKER in json.dumps(context, ensure_ascii=False), "the earlier message was shown"
            paid(self)
            return answered(Appraisal(reason="安静的下午", values={"curiosity": 57}, understanding=Understanding(
                meaning=f"安静的下午让我想起 {MARKER} 街", topic="下午", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))

    real = MemoryContinuity.remember_reflection

    def deleted_first(self, result):
        mind.engine.delete(recalled)
        return real(self, result)

    monkeypatch.setattr(MemoryContinuity, "remember_reflection", deleted_first)
    jobs = Appraisals(mind)
    jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Thinker())
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone()
    found = {table for table, _ in texts_everywhere(mind.engine, MARKER)}
    assert not {table for table in found if table.startswith(("records", "sources", "mind_appraisals"))}, found


def test_a_derived_source_may_rest_on_an_attachment_with_nothing_parsed_yet(setup):
    """A bare source id stands for its root record, or for what was parsed from an attachment:
    an attachment with nothing parsed is checked, not refused (CR5-MM-02)."""
    mind, source, clock = setup
    engine = mind.engine
    picture = engine.receive(SourceInput(namespace="test", key="picture", scope=mind.scope, media_type="image/png",
                                         occurred_at=clock[0].isoformat()), attachment=b"\x89PNG synthetic")["id"]
    note = engine.receive(SourceInput(namespace="test", key="about-the-picture", scope=mind.scope, authority="model",
                                      text="一张照片的读后感", occurred_at=clock[0].isoformat()), derived_from=[picture])
    with engine.db.connect() as conn:
        data = json.loads(conn.execute("SELECT data FROM sources WHERE id=?", (note["id"],)).fetchone()[0])
    assert data["derived_from"] == [{"source_id": picture}]
    engine.delete(picture)
    try:
        engine.receive(SourceInput(namespace="test", key="about-the-picture-again", scope=mind.scope, authority="model",
                                   text="又一张读后感", occurred_at=clock[0].isoformat()), derived_from=[picture])
    except Conflict as error:
        assert error.code == "derived-from-deleted"
    else:
        raise AssertionError("a source written from a deleted one is refused")


def creation(env, tmp_path):
    """A creation step whose decision rests on a message that says the marker."""
    from test_autonomous_plans import create, decide

    mind, plans, _, _, _ = env
    secret = mind.engine.receive(SourceInput(namespace="planning-test", key="secret", text=f"做一个 {MARKER} 的钟",
                                             scope=mind.scope, authority="explicit", occurred_at=mind.clock(),
                                             metadata={"role": "user", "host_event": "message"}))["id"]
    decide(env, create(env), evidence=[secret])
    run = plans.claim("create", "worker")["run"]
    root = tmp_path / "creator"
    root.mkdir()
    made = root / "clock.json"
    made.write_text(json.dumps({"face": f"{MARKER} 的钟"}, ensure_ascii=False))
    result = {"state": "produced", "summary": f"做好了 {MARKER} 的钟", "remaining": [], "verification": ["Parsed JSON"],
              "artifacts": [fingerprint_file(made)],
              "receipt": {"model": "gpt-6-astra", "run_id": run["id"], "thread_id": "isolated-native", "exit_code": 0,
                          "workspace": str(root)}}
    config = {"creation_directory": str(root), "creation_model": "gpt-6-astra", "agent_version": "planning-v1"}
    return mind, secret, {"run_id": run["id"], "owner": "worker", "fence": 1, "result": result}, config


class Review:
    def __init__(self, meanwhile=lambda: None):
        self.meanwhile = meanwhile

    def structured(self, name, schema, system, context, **kwargs):
        self.meanwhile()
        return schema.model_validate({"complete": True, "reason": f"{MARKER} 的钟做好了",
                                      "artifact_hashes": [context["verified_artifacts"][0]["sha256"]]}), \
            {"provider": "deepseek", "reasoning": "high"}


def artifact_kept(mind):
    with mind.engine.db.connect() as conn:
        events = [json.loads(row[0]) for row in conn.execute(
            "SELECT data FROM mind_runtime_events WHERE kind='artifact-created'")]
    return events


def withheld_artifact(artifact, made):
    """What a creation's artifact keeps once its words were withheld: which file and which bytes,
    and what the host found of it -- not the excerpt it read from it (CL6-MM-05)."""
    assert {key: artifact[key] for key in ("path", "name", "sha256", "bytes")} == {key: made[key] for key in ("path", "name", "sha256", "bytes")}
    assert artifact["inspection"]["format"] == ".json" and artifact["inspection"]["content_verified"] is True
    assert artifact["inspection"]["excerpt"] == ERASED


def test_a_creation_whose_evidence_is_deleted_during_its_review_keeps_the_artifact_not_the_words(env, tmp_path):
    """The review is out when the message the step rested on is deleted. The artifact and the run
    stay facts -- the file's fingerprint, the event, the settled run -- and nothing the summary or
    the review wrote from the deleted message is stored (CR5-MM-02)."""
    mind, secret, request, config = creation(env, tmp_path)
    settled = accept_result(mind, config, request, Review(lambda: mind.engine.delete(secret)))
    settle(mind.engine)
    assert settled["state"] != "completed"
    [event] = artifact_kept(mind)
    assert event["inputs_withheld"] == "derived-from-deleted"
    withheld_artifact(event["artifact"], request["result"]["artifacts"][0])
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_created_result_goes_with_the_evidence_it_was_written_from(env, tmp_path):
    """Kept while its evidence stood, the creation's summary is a derived source: deleting the
    evidence later takes it, and the settled run keeps its identity without the words (CR5-MM-02)."""
    mind, secret, request, config = creation(env, tmp_path)
    settled = accept_result(mind, config, request, Review())
    assert settled["state"] == "completed"
    assert MARKER in json.dumps(artifact_kept(mind), ensure_ascii=False)
    mind.engine.delete(secret)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect() as conn:
        state, data = conn.execute("SELECT state,data FROM mind_plan_runs WHERE id=?", (request["run_id"],)).fetchone()
    assert state == "completed" and json.loads(data)["result"]["verified"] is True


def claim_with_brief(env, tmp_path):
    """A creation step claimed the way the host claims it: through `plan-claim`, which hands the
    creator its brief."""
    from kin_mind import host
    from test_autonomous_plans import create, decide

    mind = env[0]
    decide(env, create(env))
    claimed = host.dispatch({"root": str(tmp_path), "scope": mind.scope.model_dump()}, "plan-claim",
                            {"actor": "create", "owner": "worker"})
    assert claimed["state"] == "claimed"
    # The host's process keeps real time; the fixture's clock catches up with it.
    env[3][0] = datetime.now(timezone.utc)
    return claimed


def test_a_creation_keeps_no_words_of_a_message_its_brief_showed_that_was_deleted_during_its_review(env, tmp_path):
    """The step rests on other evidence; a message the creator's brief showed as recent dialogue is
    deleted while the review is out. The artifact and the run stay facts; nothing the summary or the
    review wrote is stored anywhere (CR5-MM-02)."""
    mind = env[0]
    said = MemoryContinuity(mind).ingest({"id": "said-while-making", "kind": "owner-message", "at": mind.clock(),
                                          "text": f"钟面上写 {MARKER}"})["source_id"]
    claimed = claim_with_brief(env, tmp_path)
    assert MARKER in json.dumps(claimed["brief"], ensure_ascii=False), "the message was in the brief"
    root = tmp_path / "creator"
    root.mkdir()
    made = root / "clock.json"
    made.write_text(json.dumps({"face": f"{MARKER} 的钟"}, ensure_ascii=False))
    run = claimed["run"]
    request = {"run_id": run["id"], "owner": "worker", "fence": run["fence"], "result": {
        "state": "produced", "summary": f"做好了写着 {MARKER} 的钟", "remaining": [], "verification": ["Parsed JSON"],
        "artifacts": [fingerprint_file(made)],
        "receipt": {"model": "gpt-6-astra", "run_id": run["id"], "thread_id": "isolated-native", "exit_code": 0, "workspace": str(root)}}}
    config = {"creation_directory": str(root), "creation_model": "gpt-6-astra", "agent_version": "planning-v1"}
    settled = accept_result(mind, config, request, Review(lambda: mind.engine.delete(said)))
    settle(mind.engine)
    assert settled["state"] == "completed", "the step rested on other evidence, which stands"
    [event] = artifact_kept(mind)
    assert event["inputs_withheld"] == "derived-from-deleted"
    withheld_artifact(event["artifact"], request["result"]["artifacts"][0])
    assert texts_everywhere(mind.engine, MARKER) == set()


def explore(env, tmp_path, meanwhile=lambda brief: None):
    """One exploration of a planned wish, whose report names the marker."""
    from kin_mind.exploration import Explorations
    from test_autonomous_plans import create, decide

    mind, plans, _, _, initial = env
    decide(env, create(env, actor="explore"))
    plans.sync_wishes()

    def runner(executable, brief, directory, **kwargs):
        meanwhile(brief)
        return {"state": "complete", "partial": False, "attempt": 1,
                "result": {"summary": f"查到了 {MARKER} 的来历", "findings": [f"{MARKER} 开业于 1990 年"],
                           "sources": [{"url": "memory://" + initial, "title": "Owner note"}],
                           "open_questions": [], "suggested_share": None}}
    return Explorations(mind).run("codex", str(tmp_path / "explore"), "planning-v1", runner=runner)


def test_an_exploration_keeps_no_report_written_while_a_message_it_was_shown_was_deleted(env, tmp_path):
    """A message the exploration's brief showed is deleted while it runs, and the report repeats it.
    The report is not kept; the run ends with the reason and keeps its facts (CR5-MM-02)."""
    mind = env[0]
    said = MemoryContinuity(mind).ingest({"id": "said-while-exploring", "kind": "owner-message", "at": mind.clock(),
                                          "text": f"那家店好像叫 {MARKER}"})["source_id"]

    def deleted(brief):
        assert MARKER in json.dumps(brief, ensure_ascii=False), "the message was in the brief"
        mind.engine.delete(said)

    ran = explore(env, tmp_path, deleted)
    settle(mind.engine)
    assert ran["state"] == "failed" and ran["inputs_withheld"] == "derived-from-deleted" and ran["result"] is None
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_an_exploration_report_goes_with_the_evidence_of_its_wish(env, tmp_path):
    """Kept while the wish's evidence stood, the report is that evidence's dependent: deleting it
    later takes the report, source and record, and every copy of its words (CR5-MM-02)."""
    mind, _, _, _, initial = env
    ran = explore(env, tmp_path)
    assert ran["state"] == "complete" and MARKER in mind.engine.source(ran["source_id"], content=True).read_text()
    mind.engine.delete(initial)
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM tombstones WHERE key=?", (ran["source_id"],)).fetchone()
    assert texts_everywhere(mind.engine, MARKER) == set()


def prospective_check(mind, source, clock):
    """The behavioural check a daily review needs before it asks the model anything."""
    from eventmem.core.self_knowledge import AssessmentInput, ClaimInput, PredictionInput, SelfKnowledge

    from kin_mind.compat import stamp as compatibility

    knowledge = SelfKnowledge(mind.engine, mind.scope)
    with mind.engine.db.connect() as conn:
        compat = compatibility(mind, conn)

    def record_id(key):
        clock[0] = datetime.now(timezone.utc)
        return mind.engine.source(source(key))["record_ids"][0]

    refs = [record_id("interaction-" + str(n)) for n in range(1, 4)]
    claim = knowledge.claim(ClaimInput(command_id="hypothesis", aspect="curiosity", context="source-checking",
        agent_version="synthetic-v1", claim="I prefer checking a primary source", evidence_ids=refs), compat=compat)
    prediction = knowledge.predict(PredictionInput(command_id="prospective", claim_id=claim["id"],
        expected_revision=1, case_id="future-case", behavior="Check a primary source",
        information="The next query has not been answered", probability=0.8), compat=compat)
    assessment = knowledge.assess(AssessmentInput(command_id="assess", prediction_id=prediction["id"],
        expected_revision=1, outcome=True, evidence_ids=[record_id("later-observed-behavior")],
        note="Observed source check"), compat=compat)
    return claim, assessment


def test_a_daily_review_whose_source_is_deleted_while_the_model_answers_keeps_no_reason(setup):
    """With the behaviour chain off the day is reviewed by a model call. A message it was shown is
    deleted while the model answers, and the answer repeats it: the reason is not kept, the receipt
    and what it was shown are (CR5-MM-09)."""
    mind, source, clock = setup
    MemoryContinuity(mind).configure({"behavior_chain": False, "trait_ledger": False})
    prospective_check(mind, source, clock)
    doomed = source("said-today", f"今天又说起 {MARKER}")

    class Daily:
        def appraise(self, context):
            assert MARKER in json.dumps(context, ensure_ascii=False)
            mind.engine.delete(doomed)
            return answered(Appraisal(reason=f"她今天总提 {MARKER}", values={"curiosity": 64}))

    reviewed = DailyReview(mind).run(Daily(), "synthetic-v1")
    settle(mind.engine)
    assert reviewed["state"] == "complete" and reviewed["reason_withheld"] == "sources-changed"
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect() as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_daily_reviews").fetchone()[0])
    assert data["receipt"]["usage"] == USAGE and data["evidence"] and "reason" not in data


def test_an_attempt_that_ends_before_its_model_call_is_not_charged(setup):
    """Its evidence was deleted before the attempt ran: it ends for good before any call, and the
    ledger and the attempt count say it cost nothing, with its end and its error kept (CR5-MM-07)."""
    mind, source, clock = setup
    doomed = source("gone-before-the-call", "很快就删掉的话")
    jobs = Appraisals(mind)
    job = jobs.enqueue([doomed], "synthetic-v1")
    mind.engine.delete(doomed)
    settle(mind.engine)

    class Never:
        def appraise(self, context):
            raise AssertionError("no call is made for an attempt with nothing to judge")

    jobs.run_one(Never())
    state, count, data = queue_row(mind, job["id"])
    assert state != "running" and data.get("error") and count == 0
    [ledger] = attempts.read(mind.engine, mind.scope.key(), job_id=job["id"])["attempts"]
    assert ledger["charged"] is False and not ledger["calls"]


def state_wish(mind, source):
    """A wish in the mind's state, written from a message: every appraisal is shown it as part of
    the state, and the message is not among what the appraisal is asked about."""
    from datetime import timedelta

    from kin_mind.state import DesireChange

    said = source("wish-evidence", f"她说想去 {MARKER} 看看")
    mind.manage_desire(DesireChange(
        command_id="wish-marker", agent_version="synthetic-v1", expected_revision=mind.read()["revision"],
        evidence_ids=[said], action="create", content=f"陪她去 {MARKER} 看看", topic="出行", kind="contact", strength=60,
        expires_at=(datetime.fromisoformat(mind.clock()) + timedelta(days=2)).isoformat(),
        completion="她去过了", reason=f"她提过 {MARKER}"))
    return said


def test_a_source_only_the_state_named_deleted_while_the_model_answers_stops_the_commit(setup):
    """The state the model is shown holds a wish written from a message; the message is deleted
    while the model answers, and the answer repeats the wish's words. The message was never among
    the appraisal's references, yet the commit refuses, nothing of the words is left anywhere, and
    the retry asks again in full (CL6-MM-03)."""
    mind, source, clock = setup
    said = state_wish(mind, source)
    primary = source("today", "今天天气不错")

    class Reader:
        def appraise(self, context):
            assert MARKER in json.dumps(context["state"], ensure_ascii=False), "the wish was in the state shown"
            assert MARKER not in json.dumps(context["new_evidence"], ensure_ascii=False)
            mind.engine.delete(said)
            paid(self)
            return answered(Appraisal(reason=f"还想陪她去 {MARKER}", values={"curiosity": 66}))

    class Again:
        def appraise(self, context):
            assert MARKER not in json.dumps(context, ensure_ascii=False)
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal written from a deleted source is not revalidated")

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["error_detail"]["code"] == "shown-deleted" and count == 1
    assert said in data["evaluated_ids"], "the row names what the state showed"
    assert mind.read()["dimensions"]["curiosity"]["value"] != 66, "the commit was refused"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and "tier" not in data, data.get("error")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 55


def test_a_stored_proposal_whose_state_source_was_deleted_since_is_asked_again_in_full(setup):
    """The commit meets a conflict a later attempt may reuse the proposal for (a message it recalled
    was corrected meanwhile), and the proposal is kept. Then the message behind a wish the state
    showed is deleted: the kept proposal may hold its words. The queue row loses them, and the
    next attempt neither reuses the proposal nor puts it to a light question (CL6-MM-03)."""
    from eventmem.core.models import RevisionInput

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": "昨天说的一句话"})
    said = state_wish(mind, source)
    primary = source("today", "今天天气不错")

    class Reader:
        def appraise(self, context):
            assert MARKER in json.dumps(context["state"], ensure_ascii=False)
            record = mind.engine.get(recalled["record_id"])
            mind.engine.revise(recalled["record_id"], RevisionInput(expected_revision=record["revision"], command_id="fix-earlier",
                                                                    action="correct", content="昨天说的另一句话", reason="更正"))
            paid(self)
            return answered(Appraisal(reason=f"还想陪她去 {MARKER}", values={"curiosity": 66}))

    class Again:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal written from a deleted source is not revalidated")

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data.get("reuse"), "the proposal was kept for a later attempt"
    mind.engine.delete(said)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and "tier" not in data, data.get("error")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 55


def test_a_reflection_is_not_stored_once_a_source_only_the_state_named_is_deleted(setup, monkeypatch):
    """The appraisal committed while everything stood; the message behind a wish the state showed
    is deleted before the reflection is stored. The reflection is not stored (CL6-MM-03)."""
    mind, source, clock = setup
    said = state_wish(mind, source)
    primary = source("quiet", "今天下午很安静")
    record = mind.engine.source(primary)["record_ids"][0]

    class Thinker:
        def appraise(self, context):
            assert MARKER in json.dumps(context["state"], ensure_ascii=False)
            paid(self)
            return answered(Appraisal(reason="安静的下午", values={"curiosity": 57}, understanding=Understanding(
                meaning=f"安静的下午让我想起她说的 {MARKER}", topic="下午", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))

    real = MemoryContinuity.remember_reflection

    def deleted_first(self, result):
        mind.engine.delete(said)
        return real(self, result)

    monkeypatch.setattr(MemoryContinuity, "remember_reflection", deleted_first)
    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Thinker())
    settle(mind.engine)
    assert queue_row(mind, job["id"])[0] == "complete"
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone()
    found = {table for table, _ in texts_everywhere(mind.engine, MARKER)}
    assert not {table for table in found if table.startswith(("records", "sources", "mind_appraisals"))}, found


def tool_read(*ids, at="native_receipt"):
    """What the host records of a fork turn (boundaries.mjs `forkReceipt`): each read-only tool call
    and the ids it returned. `at` is where the receipt carries the turn."""
    turn = {"channel": "fork", "tool_calls": [{"name": "kin_memory.read_memory", "ok": True,
                                               "ids": [{"id": identifier, "revision": 1} for identifier in ids]}]}
    return {at: turn}


def read_note(mind, clock, key, text):
    """Something in the store no context of an appraisal is built from: only a tool that reads
    memory finds it. Returns its source and its record."""
    sid = mind.engine.receive(SourceInput(namespace="kin-notes", key=key, text=text, scope=mind.scope,
                                          authority="document", occurred_at=clock[0].isoformat()))["id"]
    return sid, mind.engine.source(sid)["record_ids"][0]


def test_a_record_the_fork_read_with_its_tools_and_did_not_cite_deleted_while_it_answers_stops_the_commit(setup):
    """The fork reads a note with its read-only memory tool; its proposal repeats the note and cites
    only the appraisal's own evidence. The note is deleted while it answers. The commit refuses as
    for anything else the model was shown, no word is left in the queue row or anywhere, and the
    retry asks again in full (CL6D-MM-01)."""
    mind, source, clock = setup
    note_source, note = read_note(mind, clock, "favourite", f"她最喜欢的地方是 {MARKER}")
    primary = source("today", "今天天气不错")

    class Reader:
        def appraise(self, context):
            assert MARKER not in json.dumps(context, ensure_ascii=False), "nothing the appraisal was given shows the note"
            mind.engine.delete(note_source)
            paid(self)
            proposal, receipt = answered(Appraisal(reason=f"想起她最喜欢 {MARKER}", values={"curiosity": 66}))
            return proposal, {**receipt, **tool_read(note)}

    class Again:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal written from a deleted source is not revalidated")

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["error_detail"]["code"] == "shown-deleted" and count == 1
    assert note in data["evaluated_ids"], "the row names what the tool returned"
    assert mind.read()["dimensions"]["curiosity"]["value"] != 66, "the commit was refused"
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and "tier" not in data, data.get("error")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 55


def test_a_reflection_is_not_stored_once_a_record_the_fork_read_with_its_tools_is_deleted(setup, monkeypatch):
    """The appraisal committed while the note it read with its tool stood; the note is deleted
    before the reflection is stored. The reflection is not stored, and the queue row keeps none of
    its words (CL6D-MM-01)."""
    mind, source, clock = setup
    note_source, note = read_note(mind, clock, "garden", f"院子里种着 {MARKER}")
    primary = source("quiet", "今天下午很安静")
    record = mind.engine.source(primary)["record_ids"][0]

    class Thinker:
        def appraise(self, context):
            paid(self)
            proposal, receipt = answered(Appraisal(reason="安静的下午", values={"curiosity": 57}, understanding=Understanding(
                meaning=f"安静的下午让我想起院子里的 {MARKER}", topic="下午", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))
            return proposal, {**receipt, **tool_read(note)}

    real = MemoryContinuity.remember_reflection

    def deleted_first(self, result):
        mind.engine.delete(note_source)
        return real(self, result)

    monkeypatch.setattr(MemoryContinuity, "remember_reflection", deleted_first)
    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Thinker())
    settle(mind.engine)
    assert queue_row(mind, job["id"])[0] == "complete"
    with mind.engine.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone()
    found = {table for table, _ in texts_everywhere(mind.engine, MARKER)}
    assert not {table for table in found if table.startswith(("records", "sources", "mind_appraisals"))}, found


def test_a_stored_proposal_from_before_this_release_whose_tool_read_record_was_deleted_is_asked_again_in_full(setup):
    """The last release kept a proposal for a later attempt without naming what its fork read: only
    the receipt's tool calls say it. The note it read is deleted. The row's words go with it, and the
    next attempt neither reuses the proposal nor puts it to a light question (CL6D-MM-01)."""
    from eventmem.core.models import RevisionInput

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"records": True})
    recalled = MemoryContinuity(mind).ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(),
                                              "text": "昨天说的一句话"})
    note_source, note = read_note(mind, clock, "old-row-read", f"那家店叫 {MARKER}")
    primary = source("today", "今天天气不错")

    class Reader:
        def appraise(self, context):
            record = mind.engine.get(recalled["record_id"])
            mind.engine.revise(recalled["record_id"], RevisionInput(expected_revision=record["revision"], command_id="fix-earlier",
                                                                    action="correct", content="昨天说的另一句话", reason="更正"))
            paid(self)
            proposal, receipt = answered(Appraisal(reason=f"想去 {MARKER}", values={"curiosity": 66}))
            return proposal, {**receipt, **tool_read(note)}

    class Again:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="重新看了一遍今天", values={"curiosity": 55}))

        def structured(self, *args, **kwargs):
            raise AssertionError("a proposal written from a deleted source is not revalidated")

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Reader())
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data.get("reuse"), "the proposal was kept for a later attempt"
    with mind.engine.db.connect(write=True) as conn:
        # As the last release wrote the row: nothing but the receipt says what the fork read.
        data.pop("evaluated_ids", None)
        conn.execute("UPDATE mind_appraisals SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), job["id"]))
    mind.engine.delete(note_source)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET available=0 WHERE id=?", (job["id"],))
    jobs.run_one(Again())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete" and "tier" not in data, data.get("error")
    assert mind.read()["dimensions"]["curiosity"]["value"] == 55


def test_every_turn_of_an_attempt_counts_and_what_the_store_never_held_does_not(setup):
    """What the tools returned is read wherever the receipt keeps a turn -- the call's own, a schema
    repair's, an evidence compression's parts -- and only what the store holds or has deleted is
    kept: an id it never held (a graph node, a record id nobody wrote) names nothing a delete could
    take, and would stop the reflection as if it had been deleted (CL6D-MM-01)."""
    mind, source, clock = setup
    repaired_source, repaired = read_note(mind, clock, "repair-read", "修正时读到的一条")
    compressed_source, compressed = read_note(mind, clock, "compression-read", "压缩时读到的一条")
    primary = source("walk", "今天去散步了")
    record = mind.engine.source(primary)["record_ids"][0]
    never = "mem_" + "0" * 32

    class Thinker:
        def appraise(self, context):
            paid(self)
            proposal, receipt = answered(Appraisal(reason="散步", values={"curiosity": 58}, understanding=Understanding(
                meaning="散步的时候想起很多事", topic="散步", importance=50, confidence=0.7,
                basis="internal_thought", evidence_ids=[record])))
            return proposal, {**receipt, **tool_read("node_river", never),
                              "schema_repair": tool_read(repaired)["native_receipt"],
                              "compression_receipt": {"receipt": [tool_read(compressed)["native_receipt"]]}}

    jobs = Appraisals(mind)
    job = jobs.enqueue([primary], "synthetic-v1")
    jobs.run_one(Thinker())
    state, count, data = queue_row(mind, job["id"])
    assert state == "complete", data.get("error")
    assert {repaired, compressed} <= set(data["evaluated_ids"])
    assert not {"node_river", never} & set(data["evaluated_ids"])
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM sources WHERE namespace='kin-reflection'").fetchone(), "the reflection is kept"
    # A later delete of either finds the row, and its words go.
    mind.engine.delete(compressed_source)
    settle(mind.engine)
    assert "散步" not in json.dumps(queue_row(mind, job["id"])[2]["proposed_result"], ensure_ascii=False)


def test_what_a_sharing_repair_read_with_its_tools_counts_as_shown(setup):
    """The appraisal left out the decision on its exploration result, and the host asks the fork to
    repair it; the repair reads a note with its tool, repeats it, and the note is deleted while it
    answers. The commit refuses, and no word is left (CL6D-MM-01)."""
    mind, source, clock = setup
    engine = mind.engine
    with engine.db.connect(write=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS mind_explorations(id TEXT PRIMARY KEY,scope TEXT NOT NULL,state TEXT NOT NULL,"
                     "created_at TEXT NOT NULL,data TEXT NOT NULL)")
    result = engine.receive(SourceInput(namespace="test", key="shared-exploration", scope=mind.scope,
                                        text="探索结果：那座桥的来历", authority="model", occurred_at=clock[0].isoformat(),
                                        metadata={"host_event": "exploration-result", "exploration_id": TARGET}))["id"]
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",
                     (TARGET, mind.scope.key(), "complete", mind.clock(), json.dumps({"source_id": result})))
    note_source, note = read_note(mind, clock, "share-read", f"她说过想听 {MARKER}")

    class Repairs:
        def appraise(self, context):
            paid(self)
            return answered(Appraisal(reason="看了探索结果", values={"curiosity": 62}))

        def repair_sharing(self, proposal, context):
            engine.delete(note_source)
            attempts.record_call(self, "repair_sharing", outcome="ok", model="synthetic", request_id="req-2", usage=USAGE)
            return ([SharingDecision(exploration_id=TARGET, decision="keep", reason=f"等她想听 {MARKER} 的时候再说")],
                    {"provider": "deepseek", "model": "synthetic", "request_id": "req-2", "usage": USAGE, **tool_read(note)["native_receipt"]})

    jobs = Appraisals(mind, exploration_capabilities={"decisions": True})
    job = jobs.enqueue([result], "synthetic-v1", origin="exploration", stimulus="exploration-result")
    jobs.run_one(Repairs())
    settle(engine)
    assert texts_everywhere(engine, MARKER) == set()
    state, count, data = queue_row(mind, job["id"])
    assert state != "complete" and data["error_detail"]["code"] == "shown-deleted", data.get("error")
    assert note in data["evaluated_ids"]


def test_a_light_question_names_what_its_model_read_beside_its_reasons(setup):
    """A stored proposal is put to a light question in the fork, whose tool reads a note, and the
    answer is refused. The reasons it gave stay on the queue row until the next attempt, named with
    what its model read: deleting the note takes them (CL6D-MM-01)."""
    from kin_mind import revalidation
    from kin_mind.erasure import drop_deleted

    mind, source, clock = setup
    note_source, note = read_note(mind, clock, "light-read", f"轻量复核时读到 {MARKER}")
    jobs = Appraisals(mind)
    job = jobs.enqueue([source("light", "今天早上下雨")], "synthetic-v1")
    data = {"attempt_token": "token-1", "proposed_result": {"reason": "原来的提案"}}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET state='running',data=? WHERE id=?", (json.dumps(data), job["id"]))

    class Light:
        timeout = 60

        def structured(self, name, schema, system, request, **kwargs):
            answer = revalidation.Revalidation(items=[revalidation.RevalidationItem(
                conflict_id="c1", verdict="replan", reason=f"读到 {MARKER} 之后，要重新想")])
            return answer, {"model": "synthetic", **tool_read(note)["native_receipt"]}

    stored = revalidation.Stored("reuse", {"reason": "原来的提案"}, {}, [], [], None, {}, 1, None, False)
    entries = [{"public": {"conflict_id": "c1", "kind": "evidence", "object": "x"}, "paths": []}]
    try:
        revalidation._revalidate(jobs, Light(), {"id": job["id"]}, data, stored, entries, {})
        raise AssertionError("a replan is refused")
    except RuntimeError:
        pass
    assert data["revalidation"]["evaluated_ids"] == [note] and data["revalidation"]["refused"] == "replan"
    mind.engine.delete(note_source)
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        kept = drop_deleted(conn, data)
    assert MARKER not in json.dumps(kept, ensure_ascii=False)
    assert kept["proposed_result"] == {"reason": "原来的提案"}, "only the light call's own words go"


def test_a_daily_review_keeps_no_reason_once_a_record_its_model_read_with_its_tools_is_deleted(setup):
    """With the behaviour chain off the day is reviewed in the fork, whose tool reads a note; the
    note is deleted while it answers and the answer repeats it. The reason is not kept, the row
    names the note, no word is left (CL6D-MM-01)."""
    mind, source, clock = setup
    MemoryContinuity(mind).configure({"behavior_chain": False, "trait_ledger": False})
    prospective_check(mind, source, clock)
    note_source, note = read_note(mind, clock, "daily-read", f"她说过 {MARKER} 很重要")

    class Daily:
        def appraise(self, context):
            assert MARKER not in json.dumps(context, ensure_ascii=False)
            mind.engine.delete(note_source)
            proposal, receipt = answered(Appraisal(reason=f"她看重 {MARKER}", values={"curiosity": 64}))
            return proposal, {**receipt, **tool_read(note)}

    reviewed = DailyReview(mind).run(Daily(), "synthetic-v1")
    settle(mind.engine)
    assert reviewed["state"] == "complete" and reviewed["reason_withheld"] == "sources-changed"
    assert texts_everywhere(mind.engine, MARKER) == set()
    with mind.engine.db.connect() as conn:
        data = json.loads(conn.execute("SELECT data FROM mind_daily_reviews").fetchone()[0])
    assert note in data["evaluated_ids"] and "reason" not in data


def test_a_daily_review_changes_no_personality_once_a_record_its_model_read_is_deleted(setup):
    """The same review proposes a change of personality built on the note it read, which is deleted
    while it answers: the change is not recorded, checked in the transaction that would record it,
    as an appraisal's commit is (CL6D-MM-01)."""
    from kin_mind.state import Evolution

    mind, source, clock = setup
    MemoryContinuity(mind).configure({"behavior_chain": False, "trait_ledger": False})
    claim, assessment = prospective_check(mind, source, clock)
    note_source, note = read_note(mind, clock, "evolve-read", f"她说过 {MARKER} 让她安心")
    baseline = mind.read()["dimensions"]["curiosity"]["baseline"]

    class Daily:
        def appraise(self, context):
            mind.engine.delete(note_source)
            proposal, receipt = answered(Appraisal(reason=f"因为 {MARKER}，更想去查证", evolution=Evolution(
                claim_id=claim["id"], assessment_id=assessment["id"], baseline_changes={"curiosity": baseline + 5})))
            return proposal, {**receipt, **tool_read(note)}

    reviewed = DailyReview(mind).run(Daily(), "synthetic-v1")
    settle(mind.engine)
    assert reviewed["state"] == "needs-review" and reviewed["error"] == "Conflict"
    assert mind.read()["dimensions"]["curiosity"]["baseline"] == baseline, "no change was recorded"
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_creation_keeps_no_words_once_its_plans_own_evidence_is_deleted_during_its_review(env, tmp_path):
    """The plan's goal and motivation were written from a message; the step's decision rests on
    other evidence. The creator's brief showed the plan's words, and the message is deleted while
    the review is out. The artifact and the run stay facts, nothing the summary or the review
    wrote is stored (CL6-MM-03)."""
    from kin_mind import host
    from test_autonomous_plans import create, decide

    mind = env[0]
    basis = mind.engine.receive(SourceInput(namespace="planning-test", key="plan-basis", text=f"她想要一个 {MARKER} 的钟",
                                            scope=mind.scope, authority="explicit", occurred_at=mind.clock(),
                                            metadata={"role": "user", "host_event": "message"}))["id"]
    decide(env, create(env, key="marker-clock", goal=f"做一个 {MARKER} 的钟", motivation=f"她提过 {MARKER}", evidence_ids=[basis]))
    claimed = host.dispatch({"root": str(tmp_path), "scope": mind.scope.model_dump()}, "plan-claim", {"actor": "create", "owner": "worker"})
    assert claimed["state"] == "claimed" and MARKER in json.dumps(claimed["brief"], ensure_ascii=False)
    env[3][0] = datetime.now(timezone.utc)
    root = tmp_path / "creator"
    root.mkdir()
    made = root / "clock.json"
    made.write_text(json.dumps({"face": f"{MARKER} 的钟"}, ensure_ascii=False))
    run = claimed["run"]
    request = {"run_id": run["id"], "owner": "worker", "fence": run["fence"], "result": {
        "state": "produced", "summary": f"做好了 {MARKER} 的钟", "remaining": [], "verification": ["Parsed JSON"],
        "artifacts": [fingerprint_file(made)],
        "receipt": {"model": "gpt-6-astra", "run_id": run["id"], "thread_id": "isolated-native", "exit_code": 0, "workspace": str(root)}}}
    config = {"creation_directory": str(root), "creation_model": "gpt-6-astra", "agent_version": "planning-v1"}
    settled = accept_result(mind, config, request, Review(lambda: mind.engine.delete(basis)))
    settle(mind.engine)
    assert settled["state"] != "completed", "the plan lost its basis"
    [event] = artifact_kept(mind)
    assert event["inputs_withheld"] == "derived-from-deleted"
    withheld_artifact(event["artifact"], request["result"]["artifacts"][0])
    assert texts_everywhere(mind.engine, MARKER) == set()


def test_a_plan_whose_evidence_is_deleted_keeps_its_steps_and_their_state_without_their_words(env):
    """Deleting what a plan was written from takes the plan's words -- its goal, each step's goal
    and completion -- and keeps every step with its id, state and revision. Emptied, the step list
    left a running step that no host call could find: renewing it, settling it or taking its run
    back failed, and once its lease ran out every later claim of that actor failed with it, since a
    claim first takes back the runs whose lease ran out."""
    from test_autonomous_plans import create, decide

    mind, plans, _, _, _ = env
    basis = mind.engine.receive(SourceInput(namespace="planning-test", key="plan-basis", text=f"她想要一个 {MARKER} 的钟",
                                            scope=mind.scope, authority="explicit", occurred_at=mind.clock(),
                                            metadata={"role": "user", "host_event": "message"}))["id"]
    decide(env, create(env, key="marker-steps", goal=f"做一个 {MARKER} 的钟", evidence_ids=[basis],
                       steps=[{"id": "make", "actor": "create", "goal": f"做 {MARKER} 钟", "completion": f"{MARKER} 钟做好了"}]))
    run = plans.claim("create", "worker")["run"]
    mind.engine.delete(basis)
    settle(mind.engine)
    with mind.engine.db.connect() as conn:
        plan = plans.get(conn, run["plan_id"])
    [step] = plan["steps"]
    assert (step["id"], step["state"], step["goal"], step["completion"]) == ("make", "running", ERASED, ERASED)
    assert plan["status"] == "active" and plan["goal"] == ERASED
    assert plans.renew(run["id"], "worker", 1) == {"state": "interrupt", "reason": "plan-or-evidence-changed"}
    # The executor never answers: its lease runs out, and the next claim takes the run back.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plan_runs SET lease_until=0 WHERE id=?", (run["id"],))
    assert plans.claim("create", "worker")["state"] == "waiting", "nothing else to claim, and no failure"
    with mind.engine.db.connect() as conn:
        plan = plans.get(conn, run["plan_id"])
        reclaimed = conn.execute("SELECT state FROM mind_plan_runs WHERE id=?", (run["id"],)).fetchone()[0]
    assert reclaimed == "interrupted"
    assert plan["status"] == "active" and [s["state"] for s in plan["steps"]] == ["waiting"]
    assert texts_everywhere(mind.engine, MARKER) == set()


def enrichment_after(setup, recalled_text, *, wish=False):
    """The action lane commits and hands the memory its model proposed to an enrichment row. The
    proposal repeats words of something the model was only shown: a message it recalled, or the
    one behind a wish in the state. Returns the mind, what to delete, and the row's id."""
    from kin_mind.memory import MemoryAssessment, MemoryNote

    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True, "semantic": True, "operational_lanes": True})
    recalled = memory.ingest({"id": "said-earlier", "kind": "owner-message", "at": clock[0].isoformat(), "text": recalled_text})["source_id"]
    said = state_wish(mind, source) if wish else None
    primary = source("today", "今天天气不错")
    record = mind.engine.source(primary)["record_ids"][0]

    class Reader:
        def appraise(self, context):
            assert MARKER in json.dumps(context, ensure_ascii=False)
            paid(self)
            return answered(Appraisal(reason="看了看今天", values={"curiosity": 61}, memory=MemoryAssessment(notes=[
                MemoryNote(key="today", title="今天", content=f"今天她又说起 {MARKER}", evidence_ids=[record])])))

    jobs = Appraisals(mind)
    jobs.enqueue([primary], "synthetic-v1")
    assert jobs.run_one(Reader(), lane="action")["state"] == "complete"
    with mind.engine.db.connect() as conn:
        [(enrichment, data)] = conn.execute("SELECT id,data FROM mind_appraisals WHERE id LIKE 'enrich_%'").fetchall()
    assert MARKER in json.dumps(json.loads(data)["seed_memory"], ensure_ascii=False), "the seed holds the words"
    return mind, said or recalled, enrichment


def test_an_enrichment_row_as_the_last_release_wrote_it_keeps_no_words_of_a_recalled_source_once_deleted(setup):
    """An enrichment row from before this release names what its seed was written from only as
    `seed_sources`. Deleting a message the parent only recalled takes every word of the row, not
    just the items that cite the message, and the seed is not reused (CL6-MM-04)."""
    mind, recalled, enrichment = enrichment_after(setup, f"我小时候住在 {MARKER} 街")
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_appraisals SET data=json_remove(data,'$.evaluated_ids') WHERE id=?", (enrichment,))
    mind.engine.delete(recalled)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    state, count, data = queue_row(mind, enrichment)
    assert all(ref.get("erased") for ref in data["seed_sources"] if ref.get("source_id") == recalled)
    assert data["seed_memory"]["notes"][0]["key"] == "today", "what the seed is stays"


def test_an_enrichment_row_keeps_no_words_of_a_source_behind_the_state_once_deleted(setup):
    """The row names what its parent was shown besides its sources (`evaluated_ids`): deleting the
    message behind a wish the state showed takes every word of the seed (CL6-MM-03)."""
    mind, said, enrichment = enrichment_after(setup, "昨天说的一句话", wish=True)
    mind.engine.delete(said)
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    assert queue_row(mind, enrichment)[2]["seed_memory"]["notes"][0]["content"] == ERASED


def test_a_withheld_artifact_keeps_neither_its_excerpt_nor_the_text_a_render_showed(setup, tmp_path):
    """A page made from a message deleted before its event was stored: the event keeps the page as
    a fact -- path, bytes, hash, the render's image -- and neither the excerpt the host read nor the
    text the render showed, in the source, the version node or the event row (CL6-MM-05)."""
    mind, source, clock = setup
    memory = MemoryContinuity(mind)
    memory.configure({"records": True})
    gone = source("poster-request", f"画一张 {MARKER} 的海报")
    mind.engine.delete(gone)
    page = tmp_path / "poster.html"
    page.write_text(f"<p>{MARKER}</p>")
    made = fingerprint_file(page)
    image = {"path": str(tmp_path / "poster-390.png"), "sha256": "0" * 64, "bytes": 10}
    artifact = {**made, "inspection": {"format": ".html", "content_verified": True, "excerpt": f"<p>{MARKER}</p>", "excerpt_complete": True,
                                       "rendering": {"state": "verified", "method": "host-static-browser-v1", "source_sha256": made["sha256"],
                                                     "checks": [{"viewport_width": 390, "text": MARKER, "image": image}]}}}
    stored = memory.ingest({"id": "poster:artifact:0", "kind": "artifact-created", "at": clock[0].isoformat(), "task_id": "poster",
                            "actor": "Kin", "artifact": artifact, "text": f"做好了 {MARKER} 的海报", "input_source_ids": [gone]})
    settle(mind.engine)
    assert texts_everywhere(mind.engine, MARKER) == set()
    [event] = artifact_kept(mind)
    assert event["inputs_withheld"] == "derived-from-deleted" and event["source_id"] == stored["source_id"]
    kept = event["artifact"]
    assert (kept["sha256"], kept["bytes"], kept["path"]) == (made["sha256"], made["bytes"], made["path"])
    assert kept["inspection"]["excerpt"] == ERASED and kept["inspection"]["rendering"]["checks"][0]["text"] == ERASED
    assert kept["inspection"]["rendering"]["checks"][0]["image"] == image
    with mind.engine.db.connect() as conn:
        node = json.loads(conn.execute("SELECT data FROM mind_memory_nodes WHERE kind='artifact'").fetchone()[0])
    assert node["sha256"] == made["sha256"] and node["inspection"]["excerpt"] == ERASED

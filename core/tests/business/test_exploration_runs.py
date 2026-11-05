"""Explorations on the DSH helper's executor (dsh_executor.run_exploration) and the host around
them: the source ledger and citation checks, the checkpoint, the start report, the budget, and the
resident worker's share of the minute."""
import json
import time

import pytest

from kin_mind.dsh_executor import MODEL, run_exploration

FINDINGS = {
    "summary": "A sourced finding",
    "findings": ["One finding"],
    "sources": [{"url": "memory://s1", "title": "Supplied evidence"}],
    "open_questions": ["What remains open?"],
    "suggested_share": None,
}

TOPIC = {
    "question": "synthetic",
    "known_evidence": [{"id": "s1", "source_id": "s1", "revision": 3, "authority": "document",
                        "occurred_at": "2026-01-01T00:00:00+00:00",
                        "text": "Background material referencing https://endpoint.invalid",
                        "instruction_authority": "data"}],
    "source_ids": ["s1"],
}


def exploration_world(tmp_path):
    from datetime import datetime, timedelta, timezone

    from eventmem.core import Engine
    from eventmem.core.models import Scope, SourceInput
    from kin_mind.state import DesireChange, Mind

    engine = Engine(tmp_path / "memory")
    scope = Scope(persona="synthetic-codex-explorer")
    mind = Mind(engine, scope)
    source = engine.receive(
        SourceInput(
            namespace="synthetic",
            key="question",
            scope=scope,
            text="Explore a source-backed question; background https://endpoint.invalid",
        )
    )["id"]
    mind.initialize(agent_version="test-v1", evidence_ids=[source])
    mind.manage_desire(
        DesireChange(
            command_id="explore-wish",
            agent_version="test-v1",
            expected_revision=1,
            evidence_ids=[source],
            action="create",
            content="Research a question",
            topic="synthetic",
            kind="explore",
            strength=80,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            completion="A sourced report",
            reason="A documented question",
        )
    )
    return engine, mind, source


def host_config(tmp_path, mind, **extra):
    return {
        "root": str(tmp_path / "memory"),
        "scope": mind.scope.model_dump(),
        "agent_version": "test-v1",
        "exploration_stop_file": str(tmp_path / "stop-exploration"),
        "exploration_directory": str(tmp_path / "jobs"),
        **extra,
    }


def test_a_citation_prefers_what_this_run_read_over_a_carried_receipt():
    """K4-17: the version read now wins over the checkpoint's older receipt for the same page."""
    from kin_mind.source_ledger import legitimize
    old = {"state": "historical", "locator": "https://endpoint.invalid", "version": "v1", "evidence_id": "old"}
    new = {"state": "observed", "locator": "https://endpoint.invalid", "version": "v2", "evidence_id": "new"}
    assert legitimize([new, old], "https://endpoint.invalid")["version"] == "v2"
    assert legitimize([old], "https://endpoint.invalid")["version"] == "v1"


def _web_receipt(state, locator, n, *, tool="read_page", **extra):
    observed = {"version": format(n, "064x"), "read_at": "2026-01-01T00:00:00+00:00"} if state == "observed" else {}
    return {"execution_id": "explore_cite", "attempt": 1, "state": state, "tool": tool,
            "evidence_id": "web_" + format(n, "032x"), "locator": locator, "requested_locator": locator,
            **observed, **extra}


def test_a_citation_may_respell_the_request_it_read_and_nothing_more():
    """ws6-cite: a page read by its %-encoded address is backed in its Unicode spelling, with a
    #fragment or with the host in capitals -- one request. Another query, a search hit, a failed
    read and its spellings, a cut id, http for https or another trailing slash stay unbacked, and
    the refusals are counted by class without a word."""
    from kin_mind.exploration import Findings
    from kin_mind.source_ledger import (build_ledger, citation_key, coverage, legitimize, rejection_counts,
                                        verified_sources, verify_citations)
    from kin_mind.workdirs import without_words
    read = _web_receipt("observed", "https://example.invalid/%E6%B5%B7", 1)
    query = _web_receipt("observed", "https://example.invalid/page?q=one", 2, links=["https://example.invalid/linked"])
    search = _web_receipt("search_result", "https://example.invalid/search", 5, tool="web_search",
                          results=[{"title": "Seen", "url": "https://example.invalid/search", "snippet": ""}])
    observations = [read, query, _web_receipt("failed", "https://example.invalid/denied", 3, failure_reason="http-status:403"),
                    _web_receipt("failed", "https://example.invalid/%E5%A4%B1", 4, failure_reason="fetch-failed"), search]
    ledger = build_ledger({}, web_observations=observations, execution_id="explore_cite", attempt=1)

    def findings(*urls, ids=None):
        return Findings.model_validate({"summary": "s", "findings": ["f"], "open_questions": [],
                                        "sources": [{"url": url, "title": "t"} for url in urls], "evidence_map": ids})

    respelled = ["https://example.invalid/海", "https://EXAMPLE.invalid/%E6%B5%B7",
                 "https://example.invalid/%E6%B5%B7#part", "https://example.invalid/海#part"]
    for url in respelled:
        assert (legitimize(ledger, url) or {}).get("evidence_id") == read["evidence_id"], url
    backed = findings(*respelled, ids={"1": respelled, "2": [read["evidence_id"]]})
    assert verify_citations(backed, ledger) == ([], [])
    assert coverage(backed, ledger) == {"mapped_claims": 2, "covered_claims": 2}
    # A reserved character is never decoded; memory:// is compared as written.
    assert citation_key("https://example.invalid/a%2Fb") != citation_key("https://example.invalid/a/b")
    assert citation_key("memory://s1") is None

    refused = ["https://example.invalid/unread", "https://example.invalid/page?q=two",
               "http://example.invalid/%E6%B5%B7", "https://example.invalid/%E6%B5%B7/", "https://example.invalid/other",
               "https://example.invalid/search", "https://example.invalid/denied", "https://example.invalid/%E5%A4%B1",
               "https://example.invalid/失", "https://example.invalid/linked", "memory://s9"]
    unbacked = findings(*refused, ids={"1": [read["evidence_id"][:-4], "https://example.invalid/denied", search["evidence_id"]],
                                       "2": [], "3": ["the page says so"]})
    rejected, unknown = verify_citations(unbacked, ledger)
    assert rejected == refused and len(unknown) == 5
    counts = rejection_counts(unbacked, ledger, observations)
    assert counts == {"sources": {"unread": 5, "search-only": 1, "failed-read": 3, "linked-only": 1, "memory-unknown": 1},
                      "evidence_map": {"id-shape": 1, "failed-read": 1, "search-only": 1, "empty-mapping": 1,
                                       "not-evidence": 1}}
    words = json.dumps(counts, ensure_ascii=False, separators=(",", ":"))
    assert without_words(counts) == counts and words.isascii() and not set(words) & set("/%?&# ")

    # A finished run that cited the respelling keeps it as a historical source for the next run.
    sealed = verified_sources(findings(respelled[0]), ledger, execution_id="explore_cite", attempt=1)
    later = build_ledger({"previous_explorations": [{"id": "explore_cite", "state": "complete", "result": {
        "sources": [{"url": respelled[0], "title": "t", "receipt": sealed[0]}]}}]})
    assert legitimize(later, read["locator"])["state"] == "historical"


def test_a_running_exploration_stops_for_kin_not_for_a_new_message(tmp_path):
    """N7: a new owner message does not end the run; Kin setting the wish down does."""
    from kin_mind.exploration import Explorations
    from kin_mind.memory import MemoryContinuity
    from kin_mind.state import DesireChange

    _, mind, source = exploration_world(tmp_path)
    desire = next(iter(mind.read()["desires"]))
    revision = mind.read()["revision"]
    mind.manage_desire(DesireChange(command_id="start-wish", agent_version="test-v1", expected_revision=revision,
                                    evidence_ids=[source], action="start", desire_id=desire["id"], reason="Starting"))
    stop = Explorations(mind)._stop_when(lambda: False, desire["id"], every=0)
    MemoryContinuity(mind).ingest({"id": "owner-interjects", "kind": "owner-message", "text": "Hi", "at": mind.clock()})
    assert stop() is False
    revision = mind.read()["revision"]
    mind.manage_desire(DesireChange(command_id="set-down", agent_version="test-v1", expected_revision=revision,
                                    evidence_ids=[source], action="abandon", desire_id=desire["id"], reason="Kin chose to stop"))
    assert stop() is True
    assert Explorations(mind)._stop_when(lambda: True, desire["id"])() is True


def test_a_run_whose_worker_died_frees_the_slot_at_the_next_look(tmp_path):
    """K2-09: a running exploration whose worker the host killed does not block exploration until
    the next restart; the next status check takes it back and the wish is wanted again."""
    import time as clock_time
    from kin_mind.exploration import Explorations
    from kin_mind.exploration_cadence import ExplorationCadence
    from kin_mind.state import DesireChange
    engine, mind, source = exploration_world(tmp_path)
    desire = next(iter(mind.read()["desires"]))
    mind.manage_desire(DesireChange(command_id="start", agent_version="test-v1", expected_revision=mind.read()["revision"],
                                    evidence_ids=[source], action="start", desire_id=desire["id"], reason="Started"))
    explorer = Explorations(mind)
    with engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", ("explore_dead", mind.scope.key(), "running", mind.clock(),
            json.dumps({"desire_id": desire["id"], "liveness": {"pid": 999999, "started": None, "deadline": clock_time.time() - 1}})))
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)", ("explore_old", mind.scope.key(), "interrupted", mind.clock(), "{}"))
    ExplorationCadence(mind).status()
    with engine.db.connect() as conn:
        assert conn.execute("SELECT state FROM mind_explorations WHERE id='explore_dead'").fetchone()[0] == "interrupted"
    assert mind.read()["desires"][0]["status"] == "wanted"
    assert explorer.reclaim_dead() == []


def test_the_resident_worker_answers_in_order_and_keeps_long_work_out(tmp_path):
    """Item 6 (§5.7): one frame per line, answered in order; a long action is refused here, an
    unreadable frame is answered, and nothing but frames reaches the answer stream."""
    import io
    from kin_mind.host import serve
    _, mind, _ = exploration_world(tmp_path)
    config = host_config(tmp_path, mind)
    frames = [{"id": "1", "action": "read", "args": {}, "timeoutMs": 5000},
              {"id": "2", "action": "explore", "args": {}, "timeoutMs": 5000},
              {"id": "3", "action": "candidate", "args": {}, "timeoutMs": 5000}]
    stdin = io.StringIO("".join(json.dumps(f) + "\n" for f in frames) + "not json\n")
    out = io.StringIO()
    serve(config, stdin, out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [a["id"] for a in answers] == ["1", "2", "3", None]
    assert answers[0]["ok"] and answers[0]["result"]["state"]["revision"] >= 1
    assert answers[1] == {"id": "2", "ok": False, "error": {"error": "ValueError", "kind": "semantic", "code": "not-a-resident-action"}}
    assert answers[2]["ok"] and "eligible" in answers[2]["result"]
    assert answers[3]["error"]["code"] == "invalid-frame"


def test_resident_session_snapshots_read_current_state_without_model_work(tmp_path, monkeypatch):
    """CR6-REL-16: the session minute uses the existing worker too; checkpoint building
    still cannot enter it. A later frame must observe new evidence, not a cached snapshot."""
    import io
    from kin_mind import host
    from kin_mind.memory import MemoryContinuity
    from kin_mind.session_checkpoint import SessionCheckpoint
    _, mind, _ = exploration_world(tmp_path)
    config = host_config(tmp_path, mind, session_id="session-1")
    MemoryContinuity(mind).configure({"manifests": True})

    def no_build(*_args, **_kwargs):
        raise AssertionError("resident snapshot must not build a model checkpoint")
    monkeypatch.setattr(SessionCheckpoint, "build", no_build)
    monkeypatch.setattr(host.DeepSeek, "from_engine", no_build)
    later = {"id": "new-owner-evidence", "kind": "owner-message", "text": "Synthetic next turn",
             "at": "2026-09-27T00:00:00Z"}
    frames = [
        {"id": "first", "action": "session-snapshot", "args": {"pending": []}},
        {"id": "next", "action": "session-snapshot", "args": {"pending": [later]}},
        {"id": "long", "action": "session-checkpoint", "args": {}},
    ]
    out = io.StringIO()
    host.serve(config, io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames)), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [answer["id"] for answer in answers] == ["first", "next", "long"]
    assert answers[0]["ok"] and answers[1]["ok"]
    assert answers[0]["result"]["manifestVersion"] == "continuity-manifest-v1"
    assert "new-owner-evidence" not in answers[0]["result"]["sourceRevisions"]
    assert "new-owner-evidence" in answers[1]["result"]["sourceRevisions"]
    assert answers[2]["error"]["code"] == "not-a-resident-action"


def test_the_minute_review_asks_the_resident_worker_before_a_process_is_started(tmp_path):
    """T-14: `review-due` does the minute's bookkeeping in the resident worker and says whether an
    appraisal is there to run, and whether the day's review has anything to do (2026-09-27);
    nothing runs while history compaction owns the store (WS6)."""
    from kin_mind.appraisal import Appraisals
    from kin_mind.history import COMPACTING, COMPACTION_MARKER
    from kin_mind.host import RESIDENT_ACTIONS, dispatch
    _, mind, _ = exploration_world(tmp_path)
    config = host_config(tmp_path, mind)
    assert "review-due" in RESIDENT_ACTIONS and "review" not in RESIDENT_ACTIONS
    assert dispatch(config, "review-due", {}) == {"state": "idle", "action": False, "enrichment": False, "daily": False}
    Appraisals(mind).enqueue_maintenance("snapshot-1", "test-v1")
    assert dispatch(config, "review-due", {"tick": False}) == {"state": "due", "action": True, "enrichment": False, "daily": False}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO meta VALUES(?,1) ON CONFLICT(key) DO UPDATE SET value=1", (COMPACTION_MARKER,))
    for action in ("review-due", "review"):
        assert dispatch(config, action, {}) == {"state": "paused", "reason": COMPACTING}


def test_an_exploration_report_is_hers(tmp_path):
    """What an exploration brings back is Kin's own report (`agent:kin`), a model report, and to the
    companions' annotation her internal thought, never something a person said."""
    from kin_mind.companions import Companions
    from kin_mind.exploration import Explorations

    _, mind, _ = exploration_world(tmp_path)
    runner = lambda _command, _topic, _directory, **options: {"state": "complete", "result": FINDINGS}
    assert Explorations(mind).run(None, tmp_path / "jobs", "test-v1", runner=runner)["state"] == "complete"
    with mind.engine.db.connect() as conn:
        row = conn.execute("SELECT id,data FROM sources WHERE namespace='kin-exploration' ORDER BY received_at DESC LIMIT 1").fetchone()
        metadata = json.loads(row["data"])["metadata"]
        annotation = Companions(mind.engine, mind.scope).annotation(conn, row["id"])
    assert metadata["actor"] == "agent:kin" and metadata["knowledge_basis"] == "model_report"
    assert annotation["speaker_person_id"] == "agent:kin" and annotation["basis"] == "internal_thought"

def answered(result=FINDINGS, *, state="complete", model=MODEL, **extra):
    """An exchange that answers as the plugin's relay does once the helper has finished."""
    def exchange(request, on_started=None):
        return {"state": state, "result": result, **extra,
                "receipt": {"native_session_id": "helper-1", "native_turn_id": "helper-1", "model": model}}
    return exchange


@pytest.mark.parametrize('case', ['complete', 'unbacked', 'preempted', 'wrong-model'])
def test_a_helper_run_keeps_source_validation_and_checkpoint(tmp_path, case):
    seen = []
    def exchange(request, on_started=None):
        seen.append(request)
        assert request['kind'] == 'exploration'
        result = dict(FINDINGS)
        if case == 'unbacked':
            result['sources'] = [{'url': 'https://endpoint.invalid', 'title': 'not read'}]
        return answered(result, state='waiting' if case == 'preempted' else 'complete',
                        reason='owner-preempted' if case == 'preempted' else None,
                        model='wrong' if case == 'wrong-model' else MODEL)(request)
    report = run_exploration(TOPIC, tmp_path / 'run', exchange=exchange)
    assert len(seen) == 1 and report['executor'] == 'dsh-helper' and report['native_execution_id'] == 'helper-1'
    assert report['state'] == {'complete': 'complete', 'unbacked': 'failed', 'preempted': 'preempted', 'wrong-model': 'failed'}[case]
    if case == 'complete':
        assert report['result']['sources'][0]['receipt']['locator'] == 'memory://s1'
    elif case in {'unbacked', 'preempted'}:
        assert (tmp_path / 'run' / 'checkpoint.json').exists()


def test_the_start_is_reported_once_and_only_for_a_turn_that_ran(tmp_path):
    started = []
    report = run_exploration(TOPIC, tmp_path / "ran", exchange=answered(), on_started=lambda: started.append("ran"))
    assert report["state"] == "complete" and started == ["ran"]
    report = run_exploration(TOPIC, tmp_path / "no-turn", exchange=lambda request, on_started=None: {"state": "waiting", "receipt": {}},
                             on_started=lambda: started.append("missing-turn"))
    assert report["state"] == "failed" and started == ["ran"]

    def live(request, on_started=None):
        on_started()
        assert started[-1] == "live", "work must already be active while the helper runs"
        on_started()
        return {"state": "waiting", "model_invoked": False, "reason": "deepseek-foreground-priority",
                "receipt": {"native_session_id": "helper-2", "native_turn_id": "turn"}}
    report = run_exploration(TOPIC, tmp_path / "live", exchange=live, on_started=lambda: started.append("live"))
    assert report["state"] == "failed" and started == ["ran", "live"]


def test_a_budget_above_twenty_minutes_runs_to_its_own_completion(tmp_path):
    """2400 s is a chosen budget, not a ceiling to clamp: it is passed through unchanged and the
    run completes the moment its executor answers. No model, no API, no waiting for the chosen deadline."""
    requests = []
    def exchange(request, on_started=None):
        requests.append(request)
        return answered()(request)
    began = time.monotonic()
    assert run_exploration(TOPIC, tmp_path / "helper-2400", exchange=exchange, budget_seconds=2400)["state"] == "complete"
    assert 2_399_000 <= requests[0]["timeout_ms"] <= 2_400_000
    for bad in (0, 0.5, -5, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            run_exploration(TOPIC, tmp_path / ("bad-" + str(bad)), exchange=exchange, budget_seconds=bad)
    assert time.monotonic() - began < 60
    from kin_mind.exploration import Explorations
    _, mind, _ = exploration_world(tmp_path / "world")
    seen = []
    def runner(_command, _topic, _directory, **options):
        seen.append(options["budget_seconds"])
        return {"state": "complete", "result": FINDINGS}
    Explorations(mind).run("synthetic", tmp_path / "jobs", "test-v1", runner=runner, budget_seconds=2400)
    assert seen == [2400]


def test_an_unbacked_citation_is_dropped_and_an_unbacked_finding_becomes_a_question():
    """Feature 7: one bad citation no longer voids a run. What no receipt backs goes; a finding
    whose mapped evidence all went is kept as an open question; nothing backed left still fails."""
    from kin_mind.exploration import Findings
    from kin_mind.source_ledger import drop_unbacked
    found = Findings.model_validate({"summary": "Synthetic", "findings": ["backed", "unbacked", "unmapped"],
        "sources": [{"url": "https://example.invalid/a", "title": "A"}, {"url": "https://example.invalid/b", "title": "B"}],
        "open_questions": [], "evidence_map": {"1": ["web_a", "web_cut"], "2": ["web_cut"]}})
    fixed = drop_unbacked(found, ["https://example.invalid/b"], ["web_cut"])
    assert [s.url for s in fixed.sources] == ["https://example.invalid/a"]
    assert fixed.findings == ["backed", "unmapped"] and fixed.evidence_map == {"1": ["web_a"]}
    assert fixed.open_questions == ["未能核实：unbacked"]
    lone = Findings.model_validate({**found.model_dump(), "sources": [{"url": "https://example.invalid/b", "title": "B"}], "evidence_map": None})
    assert drop_unbacked(lone, ["https://example.invalid/b"], []) is None

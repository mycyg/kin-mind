"""Session continuity storage: where a maintenance judgment travels, what a checkpoint
build leaves behind, and how much one automatic injection may add to a native window."""
from kin_mind.context import INJECTION_CEILING, Contexts
from kin_mind.memory import MemoryContinuity
from kin_mind.session_advice import latest, submit
from kin_mind.session_checkpoint import SessionCheckpoint

pytest_plugins = ('test_kin_mind',)


def record(event, snapshot='observation-1', action='keep'):
    return {'decision': {'action': action, 'reason': 'Synthetic judgment', 'evidenceIds': []},
            'snapshotId': snapshot, 'generation': 1, 'receipt': {'model': 'synthetic'}, 'eventId': event}


def tables(mind):
    with mind.engine.db.connect() as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_a_judgment_reaches_the_host_without_a_mind_revision(setup):
    # DB1-03: saving one session judgment used to rewrite the whole versioned mind state
    # and append a history event. It now rides a single replaced row to the registry.
    mind, _, _ = setup
    checkpoints = SessionCheckpoint(mind, agent_version='synthetic-v1')
    assert 'sessionAdvice' not in checkpoints.snapshot()
    before = mind.read()['revision']
    with mind.engine.db.connect(write=True) as conn:
        submit(conn, mind.scope.key(), record('event-1'), mind.clock())
    assert checkpoints.snapshot()['sessionAdvice'] == record('event-1')
    with mind.engine.db.connect(write=True) as conn:
        submit(conn, mind.scope.key(), record('event-2', action='compact'), mind.clock())
    assert checkpoints.snapshot()['sessionAdvice']['eventId'] == 'event-2'
    assert mind.read()['revision'] == before
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mind_session_advice').fetchone()[0] == 1


def test_a_store_from_an_older_release_still_answers_from_its_state(setup):
    mind, _, _ = setup
    legacy = record('legacy-event', snapshot='legacy-observation')
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state['session_advice'] = legacy
        mind._save(conn, state)
    checkpoints = SessionCheckpoint(mind, agent_version='synthetic-v1')
    assert 'mind_session_advice' not in tables(mind)
    assert checkpoints.snapshot()['sessionAdvice'] == legacy
    with mind.engine.db.connect(write=True) as conn:
        submit(conn, mind.scope.key(), record('event-3'), mind.clock())
        assert latest(conn, mind.scope.key(), mind._load(conn))['eventId'] == 'event-3'
    assert checkpoints.snapshot()['sessionAdvice']['eventId'] == 'event-3'


def test_checkpoints_are_not_copied_into_the_mind_store(setup):
    # K3-12: every build used to insert the full checkpoint, dialogue included, into a
    # table nothing read. The registry holds the checkpoint; the store keeps no copy.
    mind, _, _ = setup
    MemoryContinuity(mind).configure({'manifests': True})
    checkpoints = SessionCheckpoint(mind, agent_version='synthetic-v1')
    snapshot = checkpoints.snapshot()
    assert snapshot['manifestVersion'] == 'continuity-manifest-v1'
    snapshot['items'] = [{'id': 'owner-1', 'revision': 'r1', 'role': 'user', 'text': '今天去看海了。', 'at': '2026-09-24T01:00:00Z',
                          'basis': 'owner-statement', 'delivery': 'not-confirmed-by-this-record'}]
    built = checkpoints.build(snapshot, {'conversationId': 'c', 'generation': 1}, adaptive_budget=True, allow_model=False)
    assert built['complete']
    assert 'mind_continuity_manifests' not in tables(mind)


def test_automatic_background_has_a_fixed_ceiling_and_ignores_free_room(setup):
    # K3-10: in native-window mode nothing bounded an automatic injection except the
    # room left, and the room left was part of the runtime item's revision, so the
    # same runtime facts were injected again on every turn.
    mind, _, _ = setup
    MemoryContinuity(mind).configure({'context': True, 'native_window_context': True})
    contexts = Contexts(mind)
    first = contexts.build('', purpose='chat', session='main', runtime={'model': 'synthetic', 'contextAvailableTokens': 90000})
    assert any(i['id'] == 'host-runtime' for i in first['index'])
    again = contexts.build('', purpose='chat', session='main', runtime={'model': 'synthetic', 'contextAvailableTokens': 60000})
    assert not any(i['id'] == 'host-runtime' for i in again['index'])
    changed = contexts.build('', purpose='chat', session='main', runtime={'model': 'another', 'contextAvailableTokens': 60000})
    assert any(i['id'] == 'host-runtime' for i in changed['index'])
    large = contexts.build('', purpose='chat', session='other', runtime={'model': 'synthetic', 'contextAvailableTokens': 200000,
                                                                          'note': '很长的运行说明。' * 6000})
    assert large['budget'] == INJECTION_CEILING and large['tokens'] <= INJECTION_CEILING


def test_each_review_attempt_is_its_own_job_and_its_answer_names_it(setup):
    # CR-RT-08: the queue key was the snapshot alone, so a second attempt about the same
    # snapshot came back as the first, finished job and nothing new was ever judged.
    from kin_mind import session_advice
    from kin_mind.appraisal import Appraisal, Appraisals
    from kin_mind.session_advice import SessionAdvice
    from test_kin_mind import FakeReviewer

    class Reviewer(FakeReviewer):
        def appraise(self, context):
            self.calls += 1
            return self.proposal, {"provider": "deepseek", "model": "synthetic"}

    mind, _, _ = setup
    context = {"id": "snapshot-8", "binding": {"generation": 2}, "evidence": [], "recent": []}
    jobs = Appraisals(mind, session_context=context)
    first = jobs.enqueue_maintenance("snapshot-8", "synthetic-v1", request_id="review:snapshot-8:1")
    assert (first["created"], first["state"], first["requestId"], first["snapshotId"]) == (True, "pending", "review:snapshot-8:1", "snapshot-8")
    again = jobs.enqueue_maintenance("snapshot-8", "synthetic-v1", request_id="review:snapshot-8:1")
    assert again["id"] == first["id"] and again["created"] is False, "the same attempt is the same job"
    reviewer = Reviewer(Appraisal(reason="Keep the session", session_advice=SessionAdvice(action="keep", reason="Fine")))
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert jobs.enqueue_maintenance("snapshot-8", "synthetic-v1", request_id="review:snapshot-8:1")["state"] == "complete"
    second = jobs.enqueue_maintenance("snapshot-8", "synthetic-v1", request_id="review:snapshot-8:2")
    assert second["id"] != first["id"] and second["created"] and second["state"] == "pending", "a new attempt is a new job"
    assert jobs.run_one(reviewer)["state"] == "complete"
    assert reviewer.calls == 2
    with mind.engine.db.connect() as conn:
        carried = session_advice.latest(conn, mind.scope.key())
    assert (carried["snapshotId"], carried["requestId"]) == ("snapshot-8", "review:snapshot-8:2")
    # Without an attempt (an older host) the snapshot alone is the key, as it was.
    assert jobs.enqueue_maintenance("snapshot-9", "synthetic-v1")["id"] == jobs.enqueue_maintenance("snapshot-9", "synthetic-v1")["id"]

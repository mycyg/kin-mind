"""Shared timer identities and stale operational judgments, without real sends."""
import json
from datetime import timedelta

import pytest
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import Appraisal, Appraisals, Wish
from kin_mind.companions import person_a, person_b
from kin_mind.continuity import ConcernProposal
from kin_mind.memory import MemoryContinuity
from test_companion_continuity import concerns_on, create, propose
from test_kin_mind import FakeReviewer, wish
from test_recipient_wishes import view

pytest_plugins = ('test_kin_mind',)


@pytest.mark.parametrize('first', [person_a, person_b])
def test_shared_timed_concern_has_one_event_and_one_appraisal(setup, first):
    mind, source, clock = setup
    concerns_on(mind, source)
    MemoryContinuity(mind).configure({'timed_concerns': True})
    cid, _ = create(mind, source, clock, 'shared-date', surface_after=mind.clock())
    actions, jobs = ActionEvents(mind), Appraisals(mind)
    second = person_b if first == person_a else person_a
    for person in [first, second, first, second]:
        with view(mind, person):
            actions.crossings()
            actions.drain(jobs)
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT data FROM mind_action_events WHERE kind='idle-review'").fetchall()
        events = [json.loads(r[0]) for r in rows if json.loads(r[0]).get('concern_id') == cid]
        assert len(events) == 1
        assert events[0]['recipient_person_id'] == first
        assert conn.execute("SELECT COUNT(*) FROM mind_appraisals").fetchone()[0] == 1
    with view(mind, first):
        result = jobs.run_one(FakeReviewer(Appraisal(reason='One event, one decision', wishes=[
            Wish(kind='contact', content='Ask about the shared date', topic='shared-date', strength=70,
                 ttl_hours=24, completion='accepted')])) )
        assert result['state'] == 'complete'
        assert len(mind.read()['desires']) == 1
    with view(mind, second):
        assert jobs.run_one(FakeReviewer())['state'] == 'idle'


@pytest.mark.parametrize('semantic', [False, True])
@pytest.mark.parametrize('change', ['new-wish', 'changed-concern'])
def test_stale_appraisal_retries_instead_of_committing_another_contact(setup, semantic, change):
    mind, source, clock = setup
    concerns_on(mind, source)
    cid, evidence = create(mind, source, clock, 'pending-event')
    MemoryContinuity(mind).configure({'semantic': semantic, 'semantic_actions': semantic, 'manifests': semantic})
    jobs = Appraisals(mind)
    job = jobs.enqueue([source('event-to-review')], 'synthetic-v1', stimulus='idle-review')

    class DelayedReviewer(FakeReviewer):
        def appraise(self, context):
            if change == 'new-wish':
                wish(mind, source, 'already-created-while-thinking')
            else:
                propose(mind, 'event-was-retimed', ConcernProposal(action='update', concern_id=cid,
                    reason='The time changed while evaluating', surface_after=(clock[0] + timedelta(days=1)).isoformat()), evidence)
            return super().appraise(context)

    result = jobs.run_one(DelayedReviewer(Appraisal(reason='Old snapshot', wishes=[
        Wish(kind='contact', content='Outdated check-in', topic='pending-event', strength=70,
             ttl_hours=24, completion='accepted')])) )
    assert result['state'] != 'complete'
    assert 'Outdated check-in' not in [d['content'] for d in mind.read()['desires']]
    assert result['error_detail']['code'] == 'appraisal-inputs-changed'
    # A fresh model judgment can finish the same durable job without the stale send.
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('UPDATE mind_appraisals SET available=0 WHERE id=?', (job['id'],))
    reviewer = FakeReviewer(Appraisal(reason='Reconsidered the current state'))
    fresh = jobs.run_one(reviewer, job_id=job['id'])
    assert reviewer.calls == 1
    assert fresh['state'] == 'complete'
    assert 'Outdated check-in' not in [d['content'] for d in mind.read()['desires']]


@pytest.mark.parametrize('semantic', [False, True])
def test_other_recipient_wish_forces_fresh_judgment_not_cached_replay(setup, semantic):
    mind, source, _ = setup
    MemoryContinuity(mind).configure({'semantic': semantic, 'semantic_actions': semantic, 'manifests': True})
    jobs = Appraisals(mind)
    job = jobs.enqueue([source('cross-recipient-basis')], 'synthetic-v1', stimulus='idle-review')

    class DelayedReviewer(FakeReviewer):
        def appraise(self, context):
            with view(mind, person_b):
                wish(mind, source, 'other-recipient-during-evaluation')
            return super().appraise(context)

    result = jobs.run_one(DelayedReviewer(Appraisal(reason='Old recipient snapshot', wishes=[
        Wish(kind='contact', content='Stale recipient check-in', topic='shared-date', strength=70,
             ttl_hours=24, completion='accepted')])))
    assert result['state'] != 'complete'
    assert result['error_detail']['code'] == 'appraisal-inputs-changed'
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('UPDATE mind_appraisals SET available=0 WHERE id=?', (job['id'],))
    fresh = FakeReviewer(Appraisal(reason='Reconsidered current shared state'))
    assert jobs.run_one(fresh, job_id=job['id'])['state'] == 'complete'
    assert fresh.calls == 1, 'A recipient-filtered manifest must not revive a refused operational judgment'
    assert not mind.read()['desires']
    with view(mind, person_b):
        assert [d['content'] for d in mind.read()['desires']] == ['Share a sourced finding']


def test_context_assembly_revision_change_retries_before_model_call(setup, monkeypatch):
    mind, source, _ = setup
    MemoryContinuity(mind).configure({'semantic': True, 'semantic_actions': True, 'manifests': True})
    jobs = Appraisals(mind)
    job = jobs.enqueue([source('assembly-basis')], 'synthetic-v1', stimulus='idle-review')
    original = jobs.memory.semantic_context
    writes = []

    def changed_context(*args, **kwargs):
        writes.append(wish(mind, source, 'created-during-context-assembly'))
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs.memory, 'semantic_context', changed_context)
    reviewer = FakeReviewer(Appraisal(reason='Should not be called on a mixed snapshot'))
    result = jobs.run_one(reviewer)
    assert len(writes) == 1
    assert reviewer.calls == 0
    assert result['state'] == 'pending'
    assert result['error_detail']['code'] == 'mind-revision-changed'
    assert [d['content'] for d in mind.read()['desires']] == ['Share a sourced finding']
    monkeypatch.setattr(jobs.memory, 'semantic_context', original)
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('UPDATE mind_appraisals SET available=0 WHERE id=?', (job['id'],))
    assert jobs.run_one(reviewer, job_id=job['id'])['state'] == 'complete'
    assert reviewer.calls == 1
    assert [d['content'] for d in mind.read()['desires']] == ['Share a sourced finding']

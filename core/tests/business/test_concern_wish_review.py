"""A changed concern asks for a bounded re-review, never silently readies a wish."""
import json
from datetime import timedelta

import pytest
from eventmem.core.db import Conflict
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import NextMove
from kin_mind.continuity import ConcernProposal
from kin_mind.next_move import _binding
from kin_mind.state import DesireChange
from test_kin_mind import wish
from test_companion_continuity import concerns_on, create, propose

pytest_plugins = ('test_kin_mind',)


@pytest.mark.parametrize('kind', ['contact', 'explore'])
def test_concern_revision_review_is_deduplicated_and_resume_refreshes_links(setup, kind):
    mind, source, clock = setup
    concerns_on(mind, source)
    cid, evidence = create(mind, source, clock, 'care')
    did = wish(mind, source, kind, kind=kind, concern_ids=[cid])['desire_id']
    evidence = source('new-care-evidence')
    propose(mind, 'changed', ConcernProposal(action='update', concern_id=cid, reason='new timing', intensity=65,
                                           evidence_ids=[evidence]), evidence)
    actions = ActionEvents(mind)
    for _ in range(2):
        with mind.engine.db.connect(write=True) as conn:
            state = mind._load(conn)
            assert not mind._concern_links_fresh(conn, state, state['desires'][did])
            actions._due_reviews(conn, state)
    with mind.engine.db.connect() as conn:
        rows = conn.execute("SELECT data FROM mind_action_events WHERE kind='wish-review'").fetchall()
        found = [json.loads(row[0]) for row in rows if json.loads(row[0]).get('desire_id') == did]
        assert len(found) == 1 and found[0]['kind'] == kind
        if kind == 'contact':
            with pytest.raises(Conflict) as rejected:
                _binding(conn, mind, state, NextMove(move='reply', wish_ref=did), {}, 'new-event', mind.clock())
            assert rejected.value.code == 'next-move-concern-needs-review'
    mind.manage_desire(DesireChange(command_id='confirmed', agent_version='synthetic-v1',
        expected_revision=mind.read()['revision'], evidence_ids=[evidence], action='resume', desire_id=did, reason='still wanted'))
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        assert mind._concern_links_fresh(conn, state, state['desires'][did])
        if kind == 'contact':
            assert mind._desire_ready(conn, state['desires'][did], mind.clock(), state=state)


def test_waiting_choice_does_not_get_overridden_by_a_concern_change(setup):
    mind, source, clock = setup
    concerns_on(mind, source)
    cid, evidence = create(mind, source, clock, 'care')
    did = wish(mind, source, 'wait', concern_ids=[cid])['desire_id']
    mind.manage_desire(DesireChange(command_id='wait-choice', agent_version='synthetic-v1',
        expected_revision=mind.read()['revision'], evidence_ids=[evidence], action='wait', desire_id=did,
        reason='later', wait_condition='review'))
    evidence = source('new-care-evidence')
    propose(mind, 'changed', ConcernProposal(action='update', concern_id=cid, reason='new timing', intensity=65,
                                           evidence_ids=[evidence]), evidence)
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        ActionEvents(mind)._due_reviews(conn, state)
        assert state['desires'][did]['status'] == 'waiting'
        rows = conn.execute("SELECT data FROM mind_action_events WHERE kind='wish-review'").fetchall()
        assert not any(json.loads(row[0]).get('desire_id') == did for row in rows)


def test_timed_contact_resumption_queues_review_without_refreshing_stale_links(setup):
    mind, source, clock = setup
    concerns_on(mind, source)
    cid, evidence = create(mind, source, clock, 'care')
    did = wish(mind, source, 'later', concern_ids=[cid])['desire_id']
    mind.manage_desire(DesireChange(command_id='later-choice', agent_version='synthetic-v1',
        expected_revision=mind.read()['revision'], evidence_ids=[evidence], action='wait', desire_id=did,
        reason='later', wait_condition='time', retry_after_seconds=300))
    changed = source('new-window')
    propose(mind, 'changed', ConcernProposal(action='update', concern_id=cid, reason='new timing', intensity=65,
                                           evidence_ids=[changed]), changed)
    clock[0] += timedelta(minutes=6)
    assert mind.reconsider_contacts(owner_epoch='same')['resumed'] == [did]
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        assert not mind._desire_ready(conn, state['desires'][did], mind.clock(), state=state)
        ActionEvents(mind)._due_reviews(conn, state)
        rows = conn.execute("SELECT data FROM mind_action_events WHERE kind='wish-review'").fetchall()
        assert any(json.loads(row[0]).get('desire_id') == did for row in rows)
    # A source deletion is still a real block; the new review lane cannot manufacture support.
    mind.engine.delete(changed)
    with pytest.raises(Conflict):
        mind.manage_desire(DesireChange(command_id='bad-resume', agent_version='synthetic-v1',
            expected_revision=mind.read()['revision'], evidence_ids=[evidence], action='resume', desire_id=did, reason='still wanted'))

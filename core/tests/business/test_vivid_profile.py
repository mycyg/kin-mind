"""Synthetic regression coverage for role-default updates and same-meaning continuity."""
import json
import sqlite3
from copy import deepcopy
from datetime import timedelta

import pytest
from eventmem.core.models import SourceInput
from kin_mind import compat
from kin_mind.profile import DIMENSIONS
from kin_mind.state import project, motivation_expiry
from kin_mind.trait_refs import same_meaning_since
from test_kin_mind import setup  # noqa: F401


def raw(mind):
    with mind.engine.db.connect() as conn:
        return mind._load(conn)


@pytest.mark.parametrize('damage', [None, 'gap', 'end', 'revision', 'revise', 'revoked', 'fading', 'text'])
def test_same_meaning_requires_an_unbroken_effective_history(damage):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE mind_trait_history(id TEXT, revision INTEGER, data TEXT)')
    current = dict(id='trait', revision=3, status='established', text='same preference')
    for revision in range(1, 4):
        if (damage == 'gap' and revision == 2) or (damage == 'end' and revision == 3):
            continue
        snap = dict(current, revision=revision, status='candidate' if revision == 1 else 'established', action='reinforce')
        if revision == 2:
            if damage == 'revision': snap['revision'] = 9
            if damage == 'revise': snap['action'] = 'revise'
            if damage in {'revoked', 'fading'}: snap['status'] = damage
            if damage == 'text': snap['text'] = 'different preference'
        conn.execute('INSERT INTO mind_trait_history VALUES(?,?,?)', ('trait', revision, json.dumps(snap)))
    assert same_meaning_since(conn, current, 1) is (damage is None)
    assert not same_meaning_since(conn, current, True)
    assert not same_meaning_since(conn, current, 4)
    conn.close()


def test_plain_prose_and_runtime_changes_keep_contract_but_missing_contract_does_not():
    current = {'key': 'new', 'parts': {'contract': 'behavior-2', 'persona': 'new', 'models': 'new'}}
    assert compat.holds({'key': 'old', 'parts': {'contract': 'behavior-2', 'persona': 'old'}}, current)
    assert not compat.holds({'key': 'old', 'parts': {'persona': 'old'}}, current)
    assert not compat.holds({'key': 'old', 'parts': {'contract': 'behavior-1'}}, current)


def test_sync_defaults_projects_once_preserves_customization_and_can_move_back(setup, monkeypatch):
    mind, _, clock = setup
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        for key in ['mood', 'longing']:
            state['profile']['dimensions'][key].update(baseline=30, half_life_hours=2)
            state['dimensions'][key].update(baseline=30, half_life_hours=2, target=30, score=90, basis='event_inferred')
        state['profile_reviews']['longing'] = {'custom': True}
        state['profile']['dimensions']['longing']['expression'] = 'custom expression'
        custom = deepcopy(state['dimensions']['longing'])
        custom_profile = deepcopy(state['profile']['dimensions']['longing'])
        mind._save(conn, state)
    clock[0] += timedelta(hours=2)
    previous = raw(mind)
    expected = project(previous['dimensions']['mood'], mind.clock())
    assert mind.sync_role_profile(agent_version='v2')['state'] == 'synced'
    after = raw(mind)
    assert after['dimensions']['mood']['score'] == pytest.approx(expected)
    assert after['dimensions']['mood']['half_life_hours'] == DIMENSIONS['mood']['half_life_hours']
    assert after['dimensions']['longing'] == custom
    assert after['profile']['dimensions']['longing'] == custom_profile
    assert mind.sync_role_profile(agent_version='v2') == {'state': 'unchanged'}
    assert raw(mind) == after
    # An older release's defaults can be restored with a continuous event curve.
    monkeypatch.setitem(DIMENSIONS, 'mood', dict(DIMENSIONS['mood'], baseline=30, half_life_hours=2))
    clock[0] += timedelta(hours=1)
    expected = project(after['dimensions']['mood'], mind.clock())
    assert mind.sync_role_profile(agent_version='v1')['state'] == 'synced'
    assert raw(mind)['dimensions']['mood']['score'] == pytest.approx(expected)


@pytest.mark.parametrize('elapsed', [1, 5])
def test_sync_legacy_motivation_keeps_original_expiry_and_cleans_expired(setup, elapsed):
    mind, _, clock = setup
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        entry = state['dimensions']['mood']
        entry.update(baseline=30, score=80, target=90, half_life_hours=1, basis='event_inferred',
                     motivation={'base_half_life_hours': 2})
        state['profile']['dimensions']['mood'].update(baseline=30, half_life_hours=2)
        expiry = motivation_expiry(entry)
        mind._save(conn, state)
    clock[0] += timedelta(hours=elapsed)
    expected = project(raw(mind)['dimensions']['mood'], mind.clock())
    mind.sync_role_profile(agent_version='v2')
    entry = raw(mind)['dimensions']['mood']
    assert entry['score'] == pytest.approx(expected)
    if elapsed < 4:
        assert motivation_expiry(entry) == expiry
        assert entry['half_life_hours'] == 1 and entry['target'] == 90
        assert entry['motivation']['base_half_life_hours'] == DIMENSIONS['mood']['half_life_hours']
    else:
        assert 'motivation' not in entry
        assert entry['half_life_hours'] == DIMENSIONS['mood']['half_life_hours']
    assert mind.sync_role_profile(agent_version='v2') == {'state': 'unchanged'}


def test_entry_only_pace_drift_is_repaired_and_role_defaults_adopt_new_baseline(setup):
    mind, _, _ = setup
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        state['dimensions']['mood']['half_life_hours'] = 2
        state['dimensions']['mood']['score'] = 65
        mind._save(conn, state)
    assert mind.sync_role_profile(agent_version='v2')['state'] == 'synced'
    entry = raw(mind)['dimensions']['mood']
    assert entry['score'] == entry['target'] == DIMENSIONS['mood']['baseline']
    assert entry['half_life_hours'] == DIMENSIONS['mood']['half_life_hours']


def test_longing_after_expired_motivation_and_rest_policy(setup, monkeypatch):
    mind, _, clock = setup
    mind.engine.receive(SourceInput(namespace='kin-owner-input', key='hello', scope=mind.scope,
        text='hello', authority='explicit', occurred_at=mind.clock(), extract=False,
        metadata={'role': 'user', 'host_event': 'message'}))
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        entry = state['dimensions']['longing']
        entry.update(score=55, baseline=55, target=55, half_life_hours=0.25,
                     motivation={'base_half_life_hours': 18}, basis='event_inferred')
        mind._save(conn, state)
    clock[0] += timedelta(hours=12)
    monkeypatch.setattr(mind, "_contact_view", lambda *args: {})
    with mind.engine.db.connect() as conn:
        state = mind._load(conn)
        base = project(state['dimensions']['longing'], mind.clock())
        assert mind._longing_value(conn, state, mind.clock()) > base
        awake_at = clock[0]
        monkeypatch.setattr(mind, '_contact_view', lambda *args: {'quiet_start': 0, 'quiet_end': 23, 'timezone': 'UTC'})
        clock[0] = clock[0].replace(hour=12)
        assert mind._longing_value(conn, state, mind.clock()) == project(state['dimensions']['longing'], mind.clock())
        monkeypatch.setattr(mind, '_contact_view', lambda *args: {})
        clock[0] = awake_at
        monkeypatch.setattr(mind, '_continuity_flags', lambda *args: {'rhythm': True})
        monkeypatch.setattr(mind, '_subjective_available', lambda *args: True)
        state['rhythm'] = {'base_phase': 'roused', 'phase': 'resting'}
        state.setdefault('continuity', {})['activation'] = 'active'
        assert mind._longing_value(conn, state, mind.clock()) == project(state['dimensions']['longing'], mind.clock())
        # The chosen phase governs both conflicting legacy alias combinations.
        state['rhythm'] = {'base_phase': 'resting', 'phase': 'roused'}
        assert mind._longing_value(conn, state, mind.clock()) > project(state['dimensions']['longing'], mind.clock())


@pytest.mark.parametrize('action,new_text', [('revise', 'same'), ('revise', 'changed'), ('fade', 'same')])
def test_trait_dependents_and_intent_survive_establishment_then_follow_real_changes(setup, action, new_text):
    from kin_mind.appraisal import TraitDecision
    from kin_mind.traits import Traits
    from kin_mind import trait_refs
    from kin_mind.expression_intent import _traits_current
    mind, _, _ = setup
    ledger = Traits(mind)
    def decide(command, **values):
        with mind.engine.db.connect(write=True) as conn:
            return ledger.decide(conn, [TraitDecision(basis='self_preference', **values)], command, {}, None, command)[0]
    first = decide('first', action='propose', category='aesthetics', slug='colors', key='colors', text='same')
    refs = {first['id']: first['revision']}
    with mind.engine.db.connect(write=True) as conn:
        for kind in ('desire', 'intent', 'decision'):
            trait_refs.record(conn, mind.scope.key(), kind, kind+'-1', refs, at=mind.clock())
    second = decide('second', action='establish', trait_id=first['id'], expected_revision=1, text='same')
    with mind.engine.db.connect() as conn:
        assert trait_refs.links_fresh(conn, mind, {'trait_revisions': refs})
        assert _traits_current(conn, mind, [{'trait_id': first['id'], 'revision': 1}])
        assert all(r[0] == 'valid' for r in conn.execute('SELECT state FROM mind_trait_dependents'))
    decide('third', action=action, trait_id=second['id'], expected_revision=2, text=new_text)
    with mind.engine.db.connect() as conn:
        assert not trait_refs.links_fresh(conn, mind, {'trait_revisions': refs})
        assert not _traits_current(conn, mind, [{'trait_id': first['id'], 'revision': 1}])
        assert all(r[0] == 'needs_review' for r in conn.execute('SELECT state FROM mind_trait_dependents'))

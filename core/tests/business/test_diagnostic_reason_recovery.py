import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from eventmem.core.db import Conflict
from test_kin_mind import setup, event

spec = importlib.util.spec_from_file_location('diagnostic_reason_recovery', Path(__file__).parents[2] / 'scripts' / 'clear_diagnostic_reason.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def raw(mind):
    with mind.engine.db.connect() as conn:
        return json.loads(conn.execute('SELECT data FROM mind_state WHERE scope=?', (mind.scope.key(),)).fetchone()[0])


def test_exact_event_and_reason_only():
    state = {'revision': 7, 'last_assessment': {'event_id': 'bad', 'reason': 'schema diagnostic', 'at': 'same'},
             'dimensions': {'a': {'event_id': 'bad', 'reason': 'schema diagnostic', 'score': 12},
                            'b': {'event_id': 'new', 'reason': 'schema diagnostic', 'score': 13},
                            'c': {'event_id': 'bad', 'reason': 'actual feeling', 'score': 14}}}
    before = deepcopy(state)
    plan = repair.plan_state(state, 'bad', 'schema diagnostic', {})
    assert state == before
    assert plan['paths'] == [['last_assessment', 'reason'], ['dimensions', 'a', 'reason']]
    with pytest.raises(ValueError):
        repair.plan_state(state, 'bad', '', {})


def test_history_numbers_and_receipt_preserved_and_replay_idempotent(setup):
    mind, source, _ = setup
    request = event(mind, source, 'synthetic-diagnostic', {'care': 67, 'joy': 44}).model_copy(update={'reason': 'schema diagnostic'})
    receipt = mind.record(request)
    before = raw(mind)
    plan = repair.plan_state(before, receipt['event_id'], 'schema diagnostic', mind.scope.model_dump())
    with mind.engine.db.connect() as conn:
        history_before = [tuple(r) for r in conn.execute('SELECT * FROM mind_events ORDER BY revision')]
    result = repair.apply_plan(mind, plan, 'clear-diagnostic', 'synthetic operator approval')
    after = raw(mind)
    expected = deepcopy(before)
    for path in plan['paths']:
        item = expected
        for key in path[:-1]: item = item[key]
        item[path[-1]] = ''
    for key in ('revision', 'updated_at', 'agent_version'): expected[key] = after[key]
    assert after == expected
    assert after['revision'] == before['revision'] + 1
    assert mind.record(request) == receipt
    assert repair.apply_plan(mind, plan, 'clear-diagnostic', 'synthetic operator approval') == result
    assert raw(mind) == after
    with mind.engine.db.connect() as conn:
        history_after = [tuple(r) for r in conn.execute('SELECT * FROM mind_events ORDER BY revision')]
    assert history_after[:-1] == history_before
    assert history_after[-1][3] == 'diagnostic-reason-recovery'


def test_stale_revision_and_tampered_paths_fail_without_changes(setup):
    mind, source, _ = setup
    receipt = mind.record(event(mind, source, 'diagnostic', {'care': 67}).model_copy(update={'reason': 'schema diagnostic'}))
    plan = repair.plan_state(raw(mind), receipt['event_id'], 'schema diagnostic', mind.scope.model_dump())
    tampered = deepcopy(plan);tampered['paths'].append(['dimensions', 'care', 'score'])
    before = raw(mind)
    with pytest.raises(Conflict): repair.apply_plan(mind, tampered, 'tampered', 'test')
    assert raw(mind) == before
    mind.record(event(mind, source, 'newer', {'joy': 50}))
    before = raw(mind)
    with pytest.raises(Conflict): repair.apply_plan(mind, plan, 'stale', 'test')
    assert raw(mind) == before

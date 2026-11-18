"""The host's pre-draft limits reach the mind and linked plans without forging a decision."""
import json
from datetime import timedelta

from kin_mind import recipient
from kin_mind.decision_context import compact_plan
from kin_mind.state import Mind
from test_autonomous_plans import env, create, decide


def test_daily_limit_and_gate_are_visible_without_settling_or_redeciding(env, tmp_path):
    mind, plans, source, clock, _ = env
    policy = tmp_path / 'contact-policy.json'
    policy.write_text(json.dumps({'enabled': True, 'maxPerDay': 3}))
    mind.register_contact_policy(policy)
    assert mind.read()['contact']['max_per_day'] == 3
    plan = decide(env, create(env, actor='contact'))
    plans.sync_wishes()
    before = plans.read(plan['id'])['plans'][0]
    gate = mind.observe_contact_gate({'reason': 'enough-today', 'max_per_day': 3, 'sent_today': 3})
    assert mind.read()['contact']['host_gate'] == gate
    after = plans.read(plan['id'])['plans'][0]
    assert after['steps'][0]['execution']['host_gate'] == gate
    assert compact_plan(after)['steps'][0]['execution']['host_gate']['decided_by'] == 'host'
    assert after['revision'] == before['revision']
    assert after['steps'][0]['decision'] == before['steps'][0]['decision']
    assert after['steps'][0]['receipts'] == []
    clock[0] += timedelta(minutes=1)
    assert mind.observe_contact_gate({'reason': 'enough-today', 'max_per_day': 3, 'sent_today': 3}) == gate
    # Restart keeps the receipt, and the next day's host observation clears it without changing intent.
    restarted = Mind(mind.engine, mind.scope, clock=mind.clock)
    assert restarted.read()['contact']['host_gate'] == gate
    cleared = restarted.observe_contact_gate({'reason': None, 'max_per_day': 3, 'sent_today': 0})
    assert cleared['state'] == 'available' and cleared['reason'] is None
    assert plans.read(plan['id'])['plans'][0]['steps'][0]['execution']['host_gate'] == cleared
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM mind_contacts').fetchone()[0] == 0


def test_host_gate_receipts_do_not_cross_recipients(env):
    mind, *_ = env
    from kin_mind.companion_view import CompanionView, companion_scope
    from kin_mind.host import dispatch
    config = {'root': str(mind.engine.db.root), 'scope': mind.scope.model_dump(), 'agent_version': 'planning-v1', 'session_id': 'synthetic-session'}
    dispatch(config, 'contact-gate', {'recipient_person_id': 'person:person_a', 'reason': 'enough-today', 'max_per_day': 2, 'sent_today': 2})
    dispatch(config, 'contact-gate', {'recipient_person_id': 'person:person_b', 'reason': None, 'max_per_day': 2, 'sent_today': 0})
    with companion_scope(CompanionView(mind.engine, mind.scope, 'person:person_b', 'synthetic:other')):
        with mind.engine.db.connect() as conn:
            assert mind.contact_gate(conn)['state'] == 'available'
        assert mind.read()['contact']['host_gate']['state'] == 'available'
    assert mind.read()['contact']['host_gate']['reason'] == 'enough-today'

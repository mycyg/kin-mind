"""Repair diagnostics stay in receipts, not in Kin's judgment or feelings."""
from copy import deepcopy

from kin_mind.appraisal import Appraisal, Appraisals, preserve_valid_fields
from kin_mind.continuity import ContinuityConfig, ConcernProposal
from test_kin_mind import FakeReviewer, event
from test_main_session_review import native_provider
from test_concern_reassessment import seed, stored

pytest_plugins = ('test_kin_mind',)


def interpretation(mind, source):
    mind.configure_continuity(ContinuityConfig(command_id='interpretation', agent_version='synthetic-v1',
        expected_revision=mind.read()['revision'], evidence_ids=[source('interpretation')],
        features={'interpretation': True}, reason='Synthetic configuration'))


def test_repair_cannot_rewrite_valid_judgment_or_inject_actions(setup):
    mind, source, _ = setup
    interpretation(mind, source)
    asked = []
    original_reason = 'The garden is quiet; I want to think about the flowers.'
    def exchange(request):
        asked.append(request['name'])
        result = {'reason': original_reason, 'values': {'curiosity': 74}, 'understanding': []}
        if request['name'] == 'repair_appraisal':
            result = {'reason': 'Fixed the Understanding object structure.', 'values': {'curiosity': 1},
                      'understanding': None, 'wishes': [{'content': 'Unexpected action', 'topic': 'test',
                          'kind': 'explore', 'strength': 90, 'ttl_hours': 2, 'completion': 'Done'}]}
        return {'state': 'complete', 'result': result,
                'receipt': {'native_turn_id': 'turn-' + str(len(asked)), 'model': 'gpt-6-astra',
                            'usage': {'input_tokens': 500, 'output_tokens': 100}}}
    jobs = Appraisals(mind)
    jobs.enqueue([source('flowers')], 'synthetic-v1')
    answer = jobs.run_one(native_provider(mind, exchange))
    assert answer['state'] == 'complete', answer
    assert asked == ['submit_appraisal', 'repair_appraisal']
    read = mind.read()
    assert read['appraisal_summary']['reason'] == original_reason
    assert read['dimensions']['curiosity']['reason'] == original_reason
    assert read['dimensions']['curiosity']['value'] == 74
    assert not read['desires']


def test_field_repair_preserves_absent_fields_and_nested_originals():
    raw = {'values': {'curiosity': 74}, 'understanding': []}
    repaired = {'reason': 'Repair notes', 'values': {'curiosity': 1}, 'understanding': None, 'wishes': ['injected']}
    before = deepcopy(raw)
    result = preserve_valid_fields(raw, repaired, [{'loc': ['understanding'], 'type': 'model_type'}])
    assert result == {'values': {'curiosity': 74}, 'understanding': None, 'reason': ''}
    assert raw == before
    assert preserve_valid_fields({'reason': 'A real thought'}, {'reason': 'Repair notes'}, [{'loc': []}])['reason'] == 'A real thought'


def test_technical_followup_retains_last_public_thought_without_reusing_diagnostic(setup):
    mind, source, _ = setup
    interpretation(mind, source)
    first = event(mind, source, 'first', {'curiosity': 70}).model_copy(update={'reason': 'I like the garden.'})
    mind.record(first)
    before = deepcopy(mind.read()['appraisal_summary'])
    technical = event(mind, source, 'technical', {'curiosity': 71}).model_copy(update={'reason': 'Host validation rejected a field.'})
    with mind.engine.db.connect(write=True) as conn:
        state = mind._load(conn)
        mind._apply_event(conn, state, technical, 'technical-event', publish_reason=False)
        mind._save(conn, state)
    after = mind.read()
    assert after['appraisal_summary'] == before
    assert after['dimensions']['curiosity']['value'] == 71
    assert after['dimensions']['curiosity']['reason'] == ''


def test_unchanged_concern_is_recorded_without_rejection_or_followup(setup):
    mind, source, evidence, cid = seed(setup)
    prior = stored(mind, cid)
    jobs = Appraisals(mind)
    jobs.enqueue([evidence], 'synthetic-v1')
    proposal = Appraisal(reason='The same concern still stands.', concerns=[ConcernProposal(
        action='update', concern_id=cid, content=prior['content'], evidence_ids=[evidence])])
    answer = jobs.run_one(FakeReviewer(proposal))
    assert answer['state'] == 'complete', answer
    assert not answer['result'].get('rejected_sections')
    assert not answer['result'].get('follow_up_id')
    assert stored(mind, cid)['revision'] == prior['revision']


def test_nested_repair_preserves_valid_sibling_values():
    original = {'reason': 'A thought', 'values': {'mood': 70, 'curiosity': 'high'}}
    repaired = {'reason': 'Structure fixed', 'values': {'mood': 1, 'curiosity': 75}}
    assert preserve_valid_fields(original, repaired, [{'loc': ['values', 'curiosity']}]) == {
        'reason': 'A thought', 'values': {'mood': 70, 'curiosity': 75}}
    assert preserve_valid_fields({'values': {'mood': 70, 'secure': 5}}, {'values': {'mood': 70}},
                                 [{'loc': ['values', 'secure', '[key]']}])['values'] == {'mood': 70}


def test_two_appraisal_rounds_keep_host_diagnostics_out_of_public_state(setup):
    from kin_mind.appraisal import Wish
    from test_initiative import concern_under_review
    mind, source, _ = setup
    interpretation(mind, source)
    concern = concern_under_review(mind, source)
    jobs = Appraisals(mind)
    jobs.enqueue([source('garden')], 'synthetic-v1')
    public = 'I would like to learn why these flowers open at dusk.'
    first = jobs.run_one(FakeReviewer(Appraisal(reason=public, values={'curiosity': 76}, wishes=[Wish(
        content='Learn about evening flowers', topic='garden', kind='explore', strength=60,
        ttl_hours=2, completion='Find an explanation', concern_ids=[concern])])))
    assert first['state'] == 'complete', first
    followup = first['result']['follow_up_id']
    before = deepcopy(mind.read()['appraisal_summary'])
    assert before['reason'] == public
    technical = 'The host rejected the concern reference; retrying the isolated field.'
    second = jobs.run_one(FakeReviewer(Appraisal(reason=technical, values={'curiosity': 77}, wishes=[Wish(
        content='Learn about evening flowers', topic='garden', kind='explore', strength=60,
        ttl_hours=2, completion='Find an explanation')])), job_id=followup)
    assert second['state'] == 'complete', second
    assert 'follow_up_id' not in second['result']
    current = mind.read()
    assert current['appraisal_summary'] == before
    assert current['dimensions']['curiosity']['value'] == 77
    assert current['dimensions']['curiosity']['reason'] == ''
    assert len(current['desires']) == 1
    with mind.engine.db.connect() as conn:
        import json
        receipt = json.loads(conn.execute('SELECT data FROM mind_appraisals WHERE id=?', (followup,)).fetchone()[0])
    assert receipt['proposed_result']['reason'] == technical



def test_repair_removes_only_malformed_array_entries_and_preserves_valid_siblings():
    valid = {'content': 'A real wish', 'strength': 60}
    raw = {'reason': 'A thought', 'wishes': ['bad', valid, 3]}
    repaired = {'reason': 'Fixed structure', 'wishes': [{'content': 'Changed wish', 'strength': 99}]}
    result = preserve_valid_fields(raw, repaired, [{'loc': ['wishes', 0]}, {'loc': ['wishes', 2]}])
    assert result == {'reason': 'A thought', 'wishes': [valid]}
    assert raw['wishes'] == ['bad', valid, 3]
    result = preserve_valid_fields({'wishes': [valid, 'bad']}, {'wishes': [valid]}, [{'loc': ['wishes', 1]}])
    assert result['wishes'] == [valid]

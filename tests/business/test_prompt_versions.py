"""Prompt versions keep their provenance, can be listed and restored, and an edit is never overwritten
by a newer code default: it is reported stale until it is merged, kept or set to follow the default."""
import json

import pytest

from eventmem.core.prompts import PromptStore, project_prompt, MAX_PREVIOUS_TEXTS, carry_renamed


def test_each_version_records_its_source_and_can_be_restored(tmp_path):
    project_prompt('system', '合成默认。', tmp_path)
    store = PromptStore(tmp_path / 'prompts')
    first = store.write('system', '第一次改写。', source_input_id='input-1', external_read=True)
    second = store.write('system', '第二次改写。', source_input_id='input-2', external_read=False)
    assert (first['version'], first['source_input_id'], first['external_read']) == (2, 'input-1', True)
    assert (second['version'], second['source_input_id'], second['external_read']) == (3, 'input-2', False)
    listed = store.history('system')
    assert [v['version'] for v in listed['versions']] == [2, 1] and listed['current']['version'] == 3
    assert all('text' not in v for v in [listed['current'], *listed['versions']])
    assert store.history('system', 2)['text'] == '第一次改写。'
    restored = store.restore('system', 2, source_input_id='input-3')
    assert (restored['state'], restored['version'], restored['restored_from'], restored['text']) == ('saved', 4, 2, '第一次改写。')
    assert store.history('system', 3)['text'] == '第二次改写。'
    following = store.restore('system', 'default', source_input_id='input-4')
    assert (following['version'], following['edited'], following['restored_from']) == (5, False, 'default')
    assert project_prompt('system', '新的合成默认。', tmp_path) == '新的合成默认。'
    assert store.read('system')['version'] == 5
    assert store.write('system', '之后再改。')['version'] == 6
    assert store.history('system', 4)['text'] == '第一次改写。' and store.history('system', 5)['text'] == '新的合成默认。'


def test_a_newer_default_marks_an_edit_stale_without_replacing_it(tmp_path):
    project_prompt('reply-interface', '合成默认一。', tmp_path)
    store = PromptStore(tmp_path / 'prompts')
    store.write('reply-interface', 'Kin 的版本。')
    assert not store.read('reply-interface').get('stale')
    assert project_prompt('reply-interface', '合成默认二。', tmp_path) == 'Kin 的版本。'
    stale = store.read('reply-interface')
    assert stale['stale'] is True and stale['default_text'] == '合成默认二。'
    assert [p['name'] for p in store.list() if p.get('stale')] == ['reply-interface']
    kept = store.write('reply-interface', 'Kin 的版本。')
    assert (kept['state'], kept['version']) == ('kept', 2)
    assert not store.read('reply-interface').get('stale')
    assert store.write('reply-interface', 'Kin 的版本。')['state'] == 'unchanged'


def test_assembled_texts_freeze_on_edit_and_are_never_stale(tmp_path):
    project_prompt('assessment/submit_appraisal', '合成评估，第一种上下文。', tmp_path)
    store = PromptStore(tmp_path / 'prompts')
    store.write('assessment/submit_appraisal', '冻结的评估说明。')
    assert project_prompt('assessment/submit_appraisal', '合成评估，第二种上下文。', tmp_path) == '冻结的评估说明。'
    assert not store.read('assessment/submit_appraisal').get('stale')
    assert '固定' in store.effective('assessment/submit_appraisal')
    assert '原生直连' in store.effective('tools/read_memory') and '重新加载' not in store.effective('tools/read_memory')


def test_dot_segments_are_refused_and_defaults_keep_a_bounded_history(tmp_path):
    store = PromptStore(tmp_path / 'prompts')
    for name in ('.defaults/system', 'tools/.runtime', '.history/system', 'a//b'):
        with pytest.raises(ValueError):
            store.document(name)
    store.write('tools/mcp.foo', '说明')
    for index in range(MAX_PREVIOUS_TEXTS + 4):
        project_prompt('contact-draft', f'合成默认 {index}', tmp_path)
    registered = json.loads((tmp_path / 'prompts/.defaults/contact-draft.json').read_text())
    assert len(registered['previous_texts']) == MAX_PREVIOUS_TEXTS
    assert registered['previous_texts'][-1] == f'合成默认 {MAX_PREVIOUS_TEXTS + 2}'
    assert 'previous_texts' not in store.read('contact-draft')


def test_an_edit_under_the_old_tool_search_name_carries_over_once_and_the_old_stays(tmp_path):
    store = PromptStore(tmp_path / 'prompts')
    # The shape the code once registered in place: version 1, no `edited` -- not an edit, nothing to carry.
    store.document('tools/undefined').parent.mkdir(parents=True)
    store.document('tools/undefined').write_text(json.dumps({'name': 'tools/undefined', 'version': 1, 'text': '合成旧默认。'}))
    assert carry_renamed(store) == []
    store.write('tools/undefined', '合成的改写。', source_input_id='input-1')
    project_prompt('tools/tool_search', '合成新默认。', tmp_path)
    assert carry_renamed(store) == ['tools/tool_search']
    carried = store.read('tools/tool_search')
    assert (carried['text'], carried['edited'], carried['migrated_from'], carried['source_input_id']) == ('合成的改写。', True, 'tools/undefined', None)
    assert store.read('tools/undefined')['text'] == '合成的改写。', 'the old document stays'
    # Once: a later choice to follow the default again is not undone at the next start-up.
    store.restore('tools/tool_search', 'default')
    assert carry_renamed(store) == [] and store.read('tools/tool_search')['text'] == '合成新默认。'
    assert [v['version'] for v in store.history('tools/tool_search')['versions']] == [2, 1]


def test_an_edit_already_made_under_the_new_name_is_never_replaced(tmp_path):
    store = PromptStore(tmp_path / 'prompts')
    store.write('tools/undefined', '合成的旧改写。')
    store.write('tools/tool_search', '合成的新改写。')
    assert carry_renamed(store) == [] and store.read('tools/tool_search')['text'] == '合成的新改写。'


def test_the_start_up_recovery_carries_it(tmp_path):
    from eventmem.core import Engine
    from eventmem.core.models import Scope, SourceInput
    from kin_mind.host import dispatch
    from kin_mind.state import Mind
    engine, scope = Engine(tmp_path / 'memory'), Scope(persona='synthetic-prompts')
    mind = Mind(engine, scope)
    init = engine.receive(SourceInput(namespace='test', key='configuration', scope=scope, text='configuration', authority='explicit'))['id']
    mind.initialize(agent_version='test-v1', evidence_ids=[init])
    PromptStore(engine.db.root / 'prompts').write('tools/undefined', '合成的改写。')
    config = {'root': str(tmp_path / 'memory'), 'scope': scope.model_dump(), 'agent_version': 'test-v1'}
    assert dispatch(config, 'recover', {})['prompts_carried'] == ['tools/tool_search']
    assert dispatch(config, 'recover', {})['prompts_carried'] == []

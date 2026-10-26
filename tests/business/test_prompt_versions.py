"""Prompt versions keep their provenance, can be listed and restored, and an edit is never overwritten
by a newer code default: it is reported stale until it is merged, kept or set to follow the default."""
import json

import pytest

from eventmem.core.prompts import PromptStore, project_prompt, MAX_PREVIOUS_TEXTS


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

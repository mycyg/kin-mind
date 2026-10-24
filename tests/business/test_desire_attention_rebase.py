"""Concurrent host attention must not invalidate a wish; substantive edits must."""
import pytest
from eventmem.core.db import Conflict
from test_kin_mind import wish, event
from test_flexible_rhythm import flexible, received


def test_wish_rebases_over_attention_and_release_only(flexible):
    mind, memory, source, clock = flexible
    received(memory, clock, "synthetic-x")
    received(memory, clock, "synthetic-y")
    revision = mind.read()['revision']
    mind.attend('synthetic-x')
    mind.attend('synthetic-y')
    mind.finish_attention('synthetic-y')
    result = wish(mind, source, 'wish-after-attention', expected_revision=revision)
    assert result['revision'] == revision + 4
    assert mind.read()['desires'][0]['status'] == 'wanted'
    # Idempotency remains based on the original command, including its stale revision.
    assert wish(mind, source, 'wish-after-attention', expected_revision=revision) == result


def test_substantive_change_still_conflicts(flexible):
    mind, memory, source, clock = flexible
    received(memory, clock, "synthetic-x")
    received(memory, clock, "synthetic-y")
    revision = mind.read()['revision']
    mind.attend('synthetic-x')
    mind.record(event(mind, source, 'mood-change', {'mood': 25}))
    mind.attend('synthetic-y')
    with pytest.raises(Conflict):
        wish(mind, source, 'stale-wish', expected_revision=revision)


def test_missing_history_does_not_rebase(flexible):
    mind, memory, source, clock = flexible
    received(memory, clock, "synthetic-x")
    received(memory, clock, "synthetic-y")
    revision = mind.read()['revision']
    mind.attend('synthetic-x')
    with mind.engine.db.connect(write=True) as conn:
        conn.execute('DELETE FROM mind_events WHERE scope=? AND revision=?', (mind.scope.key(), revision + 1))
    with pytest.raises(Conflict):
        wish(mind, source, 'missing-history-wish', expected_revision=revision)


def test_original_method_conflicts_then_new_method_rebases_same_event_order(flexible):
    import ast
    import subprocess
    from pathlib import Path
    from kin_mind.state import DesireChange
    mind, memory, source, clock = flexible
    received(memory, clock, 'synthetic-x')
    received(memory, clock, 'synthetic-y')
    revision = mind.read()['revision']
    # Pin the actual pre-fix source; HEAD already contains the fix after release.
    original = subprocess.check_output(['/usr/bin/git', 'show', '12da5dccf85648685a81107a078ecae222afb1f5:src/kin_mind/state.py'], cwd=Path(__file__).resolve().parents[2], text=True, env={'PATH': '/usr/bin:/bin'})
    tree = ast.parse(original)
    method = next(n for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == 'Mind' for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'manage_desire')
    namespace = {'DesireChange': DesireChange}
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<original-manage-desire>', 'exec'), namespace)
    # An input from the other conversation updates attention after the read.
    mind.attend('synthetic-x')
    request = DesireChange(command_id='replay-create', agent_version='synthetic-v1', expected_revision=revision,
        evidence_ids=[source('wish-basis')], action='create', content='Synthetic wish', topic='test', kind='contact', strength=50,
        expires_at=(clock[0] + __import__('datetime').timedelta(days=1)).isoformat(), completion='accepted', reason='synthetic')
    with pytest.raises(Conflict):
        namespace['manage_desire'](mind, request)
    assert mind.manage_desire(request)['revision'] == revision + 2

"""Mutable persona survives all restore entry points and is read from its effective prompt."""
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from eventmem.core import Engine
from eventmem.core.api import create_app
from eventmem.core.cli import main as cli
from eventmem.core.models import Scope
from eventmem.core.persona import load_persona, validate_trait_changes
from eventmem.core.prompts import PromptStore
from eventmem.core.transfer import backup, restore

SCOPE = Scope(project='personal', persona='SyntheticKin', collection='default', world='real')

def seed(root):
    policy = dict(schema=1, version='persona-v3', scope=SCOPE.model_dump(), core='合成角色。', voice='随意说话。', maintenance='可以自主改写。', requires_owner_confirmation=True, core_sha256='stale')
    (root / 'persona-policy.json').write_text(json.dumps(policy, ensure_ascii=False))
    return policy

def prepared(tmp_path):
    engine = Engine(tmp_path / 'source')
    seed(engine.db.root)
    store = PromptStore(engine.db.root / 'prompts')
    store.write('persona', '我喜欢和小光、一一闲聊。')
    store.write('persona', '想说什么就说，也可以没什么新鲜事。')
    store.write('system', '用完整自然的话和人相处。')
    archive = tmp_path / 'persona.tar.gz'
    backup(engine, archive)
    return archive, store

def assert_current(root, store):
    engine = Engine(root)
    loaded = load_persona(engine, SCOPE)
    assert loaded['instructions'] == store.read('persona')['text']
    assert loaded['prompt_version'] == 2
    assert loaded['requires_owner_confirmation'] is False
    assert PromptStore(root / 'prompts').read('system') == store.read('system')
    assert json.loads((root / 'prompts/.history/persona/1.json').read_text())['text'] == '我喜欢和小光、一一闲聊。'
    validate_trait_changes(loaded, {'core_temperament': '亲近', 'interests': ['猫']})

def test_scope_and_seed_fallback_without_an_approval_gate(tmp_path):
    seed(tmp_path)
    engine = SimpleNamespace(db=SimpleNamespace(root=tmp_path))
    assert load_persona(engine, Scope(persona='SomeoneElse')) is None
    loaded = load_persona(engine, SCOPE)
    assert loaded['instructions'] == '合成角色。\n随意说话。\n可以自主改写。'
    assert loaded['requires_owner_confirmation'] is False

@pytest.mark.parametrize('part', ['core', 'voice', 'maintenance'])
def test_invalid_seed_shape_is_an_error_not_a_silent_fallback(tmp_path, part):
    policy = seed(tmp_path)
    policy[part] = None
    (tmp_path / 'persona-policy.json').write_text(json.dumps(policy))
    with pytest.raises(ValueError, match='must be text'):
        load_persona(SimpleNamespace(db=SimpleNamespace(root=tmp_path)), SCOPE)

def test_restore_preserves_effective_prompt_and_history_without_approval(tmp_path):
    archive, store = prepared(tmp_path)
    target = tmp_path / 'restored'
    assert restore(archive, target)['status'] == 'restored'
    assert_current(target, store)

def test_cli_restore_preserves_effective_prompt(tmp_path):
    archive, store = prepared(tmp_path)
    target = tmp_path / 'restored'
    with redirect_stdout(io.StringIO()) as printed:
        assert cli(['restore', str(archive), '--root', str(target)]) == 0
    assert json.loads(printed.getvalue())['persona_version'] == 'persona-v3'
    assert_current(target, store)

def test_http_restore_preserves_effective_prompt(tmp_path):
    archive, store = prepared(tmp_path)
    service = Engine(tmp_path / 'service')
    client = TestClient(create_app(engine=service, token='synthetic-token', workers=False, mcp_enabled=False))
    result = client.post('/v1/maintenance/restore', headers={'Authorization':'Bearer synthetic-token'}, files={'file':('backup.tar.gz', archive.read_bytes(), 'application/gzip')})
    assert result.status_code == 200, result.text
    restored = list((service.db.root / 'restores').iterdir())
    assert len(restored) == 1
    assert_current(restored[0], store)

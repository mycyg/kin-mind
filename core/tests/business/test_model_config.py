import json
from types import SimpleNamespace
import httpx
import pytest
from pydantic import BaseModel
from kin_mind.model_config import resolve, post, wire_request
from kin_mind.appraisal import DeepSeek, NativeReview
from eventmem.core import Engine
from eventmem.core.models import ModelRole
from eventmem.core.providers import Providers

@pytest.mark.parametrize('role', ['creation', 'exploration'])
def test_host_startup_accepts_supplier_on_native_role(tmp_path, monkeypatch, role):
    from kin_mind.host import load_config
    monkeypatch.setenv('KIN_MODEL_CONFIG_FILE', '')
    credentials = tmp_path / 'test.env'
    credentials.write_text('')
    host = tmp_path / 'mind-config.json'
    host.write_text(json.dumps({'credentials_file': str(credentials), role + '_model': 'old'}))
    state = tmp_path / 'state'
    state.mkdir()
    profile = {'mode': 'independent', 'providerId': 'second', 'model': 'synthetic-model',
               'reasoningEffort': 'high', 'authentication': 'api'}
    (state / 'model-config.json').write_text(json.dumps({'version': 1, 'roleProfiles': {role: profile}}))
    config = load_config(host)
    assert config[role + '_model'] == 'synthetic-model'
    assert config[role + '_reasoning'] == 'high'
    assert json.loads((state / 'model-config.json').read_text())['roleProfiles'][role] == profile

@pytest.fixture
def configured(tmp_path):
    engine = Engine(tmp_path / 'memory')
    engine.settings('models', {'summary': ModelRole(endpoint='https://api.deepseek.com/v1', model='deepseek-flash', protocol='anthropic', api_key_env='EVENTMEM_API_KEY').model_dump()})
    file = tmp_path / 'state/model-config.json';file.parent.mkdir()
    (file.parent / 'credentials').mkdir();(file.parent / 'credentials/test.env').write_text('RESPONSES_RELAY_API_KEY=synthetic')
    engine.model_config_file = str(file)
    config = {'version': 1, 'roleProfiles': {}, 'responsesRelay': {'id': 'test', 'baseUrl': 'https://relay.invalid/v1', 'models': ['opus'], 'credentialFile': 'test.env'}}
    def save():file.write_text(json.dumps(config))
    save()
    return engine, config, save, file

@pytest.mark.parametrize('protocol', ['responses', 'chat-completions', 'anthropic-messages'])
def test_direct_factory_changes_real_outbound_model_effort_protocol_and_key(configured, protocol, monkeypatch):
    engine, config, save, _ = configured
    config['roleProfiles']['memoryCompression'] = {'mode': 'independent', 'model': 'opus', 'reasoningEffort': 'medium'}
    config['responsesRelay']['protocol'] = protocol;save()
    provider = DeepSeek.from_engine(engine, role='memoryCompression')
    calls = []
    def handle(request):
        calls.append(request);body = json.loads(request.content)
        assert body['model'] == 'opus'
        assert body.get('reasoning', {}).get('effort', body.get('reasoning_effort', body.get('output_config', {}).get('effort'))) == 'medium'
        if protocol == 'responses':out={'model':'opus','output':[{'type':'function_call','call_id':'t','name':'submit','arguments':'{"ok":true}'}]}
        elif protocol == 'chat-completions':out={'model':'opus','choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'t','function':{'name':'submit','arguments':'{"ok":true}'}}]}}]}
        else:out={'model':'opus','stop_reason':'tool_use','content':[{'type':'tool_use','id':'t','name':'submit','input':{'ok':True}}]}
        return httpx.Response(200,json=out)
    provider.transport = httpx.MockTransport(handle)
    class Result(BaseModel):ok:bool
    monkeypatch.setattr('eventmem.core.db.recording', lambda: True)
    value, receipt = provider.structured('submit', Result, 'Synthetic test', {})
    assert value.ok and receipt['model'] == 'opus' and receipt['reasoning'] == 'medium'
    assert calls[0].url.path == {'responses':'/v1/responses','chat-completions':'/v1/chat/completions','anthropic-messages':'/v1/messages'}[protocol]
    assert (calls[0].headers.get('authorization') or calls[0].headers.get('x-api-key')) in ('Bearer synthetic','synthetic')


@pytest.mark.parametrize('follow', [False, True])
def test_same_model_on_two_providers_and_kimi_use_their_own_direct_credentials(configured, follow):
    engine, config, save, file = configured
    config['providers'] = {'second': {'id': 'second', 'baseUrl': 'https://second.invalid/v1', 'models': ['opus'], 'credentialFile': 'second.env'}}
    (file.parent/'credentials/second.env').write_text('RESPONSES_RELAY_API_KEY=synthetic-second')
    (file.parent/'credentials/kimi.env').write_text('KIMI_API_KEY=synthetic-kimi')
    profile = {'model': 'opus', 'providerId': 'second', 'reasoningEffort': 'high'}
    config['roleProfiles']['memoryQuery'] = {'mode': 'follow-main'} if follow else {'mode':'independent', **profile}
    (file.parent/'model-effective.json').write_text(json.dumps({'main': profile}));save()
    body={'messages':[{'role':'user','content':'Synthetic'}], 'max_tokens':100}
    calls=[]
    def handle(request):
        calls.append((str(request.url), request.headers['authorization'], json.loads(request.content)['model']))
        return httpx.Response(200,json={'model':json.loads(request.content)['model'],'output':[]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert post(resolve(engine,'memoryQuery'),client,body).status_code==200
        profile={'model':'k3','providerId':'kimi','reasoningEffort':'high'}
        if follow:(file.parent/'model-effective.json').write_text(json.dumps({'main':profile}))
        else:config['roleProfiles']['memoryQuery']={'mode':'independent',**profile};save()
        assert post(resolve(engine,'memoryQuery'),client,body).status_code==200
    assert calls==[('https://second.invalid/v1/responses','Bearer synthetic-second','opus'),('https://api.kimi.com/coding/v1/responses','Bearer synthetic-kimi','k3')]


def test_follow_main_resolves_each_read_and_subscription_combinations_fail(configured):
    engine, config, save, file = configured
    config['roleProfiles']['memoryRecall'] = {'mode':'follow-main'};save()
    active=file.parent/'model-effective.json'
    active.write_text(json.dumps({'main':{'model':'opus','reasoningEffort':'high'}}))
    assert resolve(engine,'memoryRecall')['model']=='opus'
    active.write_text(json.dumps({'main':{'model':'deepseek-flash','reasoningEffort':'high'}}))
    assert resolve(engine,'memoryRecall')['provider']=='deepseek'
    active.write_text(json.dumps({'main':{'model':'gpt-subscription','reasoningEffort':'high'}}))
    with pytest.raises(ValueError,match='authentication combination'):resolve(engine,'memoryRecall')
    assert resolve(engine,'memoryCompression') is None
    assert DeepSeek.from_engine(engine).model=='deepseek-flash'


def test_native_detached_explicit_profile_is_not_overwritten_by_legacy_ds(configured):
    engine,_,_,_=configured
    profile={'model':'opus','reasoningEffort':'medium','modelContextWindow':100000,'outputReserve':1000,'toolReserve':1000,'detachedContext':True,'modelProfile':{'model':'opus'}}
    provider=NativeReview.from_engine(engine,profile=profile,exchange=lambda _:None)
    assert provider.model=='opus' and provider.input_budget==98000


def test_general_memory_provider_actual_json_path_uses_same_config(configured, monkeypatch):
    engine,config,save,_=configured
    config['roleProfiles']['memorySummary']={'mode':'independent','model':'opus','reasoningEffort':'high'};save()
    original=httpx.Client
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.url.path=='/v1/responses'
        assert json.loads(request.content)['model']=='opus'
        return httpx.Response(200,json={'model':'opus','output':[{'type':'message','content':[{'type':'output_text','text':'{"content":"synthetic"}'}]}]})
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(**{**kw,'transport':httpx.MockTransport(handle)}))
    monkeypatch.setattr('eventmem.core.db.recording',lambda:True)
    assert Providers(engine).json('summary','Synthetic',{})=={'content':'synthetic'}
    assert len(calls)==1
    assert 'embedding' not in config['roleProfiles']


def test_tool_result_correlations_survive_protocol_conversion():
    messages=[{'role':'assistant','content':[{'type':'tool_use','id':'call1','name':'read','input':{'n':1}}]},{'role':'user','content':[{'type':'tool_result','tool_use_id':'call1','content':'result'}]}]
    for protocol in ('responses','chat-completions'):
        _,body=wire_request({'model':'opus','effort':'high','protocol':protocol},{'messages':messages})
        result=(body.get('input') or body['messages'])[-1]
        assert result.get('call_id',result.get('tool_call_id'))=='call1'


def test_direct_calls_never_extract_subscription_credentials(configured):
    engine, config, save, _ = configured
    config['roleProfiles']['memorySummary'] = {'mode':'independent','model':'opus','reasoningEffort':'high','authentication':'codex-subscription'};save()
    with pytest.raises(ValueError,match='API authentication'):resolve(engine,'memorySummary')


@pytest.mark.parametrize('mode', ['independent', 'follow-main'])
def test_deepseek_vision_config_reaches_image_request(configured, monkeypatch, mode):
    import base64
    engine, config, save, file = configured
    profile = {'model': 'deepseek-flash', 'reasoningEffort': 'high'}
    config['roleProfiles']['memoryVision'] = {'mode': mode, **(profile if mode == 'independent' else {})}
    (file.parent / 'model-effective.json').write_text(json.dumps({'main': profile}))
    save()
    monkeypatch.setenv('EVENTMEM_API_KEY', 'synthetic')
    monkeypatch.setattr('eventmem.core.db.recording', lambda: True)
    raw = b'synthetic-image-bytes'
    calls = []
    original = httpx.Client
    def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        assert str(request.url) == 'https://api.deepseek.com/v1/messages'
        assert request.headers['x-api-key'] == 'synthetic'
        assert body['model'] == 'deepseek-flash'
        assert body['output_config']['effort'] == 'high'
        image = body['messages'][-1]['content'][-1]
        assert image == {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': base64.b64encode(raw).decode()}}
        return httpx.Response(200, json={'model': 'deepseek-flash', 'content': [{'type': 'text', 'text': '{"color":"red"}'}]})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(**{**kw, 'transport': httpx.MockTransport(handle)}))
    assert Providers(engine).json('vision', 'Read the synthetic image.', {}, image=('image/png', raw)) == {'color': 'red'}
    assert len(calls) == 1


def configured_provider(configured, protocol, model):
    engine, config, save, _ = configured
    config['responsesRelay'].update(protocol=protocol, models=[model])
    config['roleProfiles']['memoryStructured'] = {'mode': 'independent', 'model': model, 'reasoningEffort': 'high'}
    save()
    return DeepSeek.from_engine(engine)


def tool_response(protocol, model, calls, *, text=None):
    common = {'id': 'synthetic-response', 'model': model, 'usage': {'input_tokens': 10, 'output_tokens': 5}}
    if protocol == 'responses':
        return {**common, 'output': [{'type': 'function_call', 'call_id': c['id'], 'name': c['name'], 'arguments': json.dumps(c['input'])} for c in calls]}
    if protocol == 'chat-completions':
        return {**common, 'choices': [{'finish_reason': 'tool_calls' if calls else 'stop', 'message': {
            'content': text, 'tool_calls': [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': json.dumps(c['input'])}} for c in calls]}}]}
    return {**common, 'stop_reason': 'tool_use' if calls else 'end_turn',
            'content': ([{'type': 'text', 'text': text}] if text else []) + calls}


@pytest.mark.parametrize('protocol,model,reported', [
    ('responses', 'gpt-5', 'gpt-5'), ('anthropic-messages', 'deepseek-flash', 'deepseek-flash'),
    ('chat-completions', 'k3', 'k3'), ('chat-completions', 'claude-sonnet-4-5', 'claude-sonnet-4-5'),
    ('responses', 'gpt-5', 'gpt-5-2025-08-07'),
    ('chat-completions', 'claude-sonnet-4-5', 'claude-sonnet-4-5-20250929'),
    ('anthropic-messages', 'claude-sonnet-4-5', 'claude-sonnet-4-5-20250929'),
])
def test_appraisal_multiple_reads_keep_one_turn_and_each_result(configured, monkeypatch, protocol, model, reported):
    """Use the real recall loop and reader; only the remote model is scripted."""
    provider = configured_provider(configured, protocol, model)
    provider.memory_recall = True
    monkeypatch.setattr('eventmem.core.db.recording', lambda: True)
    calls = [{'type': 'tool_use', 'id': 'read-' + str(i), 'name': 'read_memory',
              'input': {'record_id': 'mem_' + str(i) * 32}} for i in (1, 2)]
    sent, executed = [], []
    original_read = provider._read
    def read(use, scope):
        executed.append(use['id'])
        return original_read(use, scope)
    monkeypatch.setattr(provider, '_read', read)
    def handle(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            return httpx.Response(200, json=tool_response(protocol, reported, calls, text='Read both records.'))
        if protocol == 'chat-completions':
            history = body['messages'][2:]
            assert [m['role'] for m in history] == ['assistant', 'tool', 'tool']
            assert history[0]['content'] == 'Read both records.'
            assert [c['id'] for c in history[0]['tool_calls']] == ['read-1', 'read-2']
            assert [m['tool_call_id'] for m in history[1:]] == ['read-1', 'read-2']
            assert all(json.loads(m['content']) == {'error': 'not-found'} for m in history[1:])
        elif protocol == 'responses':
            history = body['input'][2:]
            assert [m['type'] for m in history] == ['function_call', 'function_call', 'function_call_output', 'function_call_output']
            assert [m['call_id'] for m in history] == ['read-1', 'read-2', 'read-1', 'read-2']
        else:
            assistant, results = body['messages'][1:]
            assert assistant['content'][1:] == calls
            assert [c['tool_use_id'] for c in results['content']] == ['read-1', 'read-2']
            assert all(c['is_error'] for c in results['content'])
        return httpx.Response(200, json=tool_response(protocol, reported, [
            {'type': 'tool_use', 'id': 'submit', 'name': 'submit_appraisal', 'input': {'reason': 'Synthetic result', 'values': {}}}]))
    provider.transport = httpx.MockTransport(handle)
    proposal, receipt = provider.appraise({'state': {'scope': {'persona': 'synthetic'}}})
    assert proposal.values == {} and executed == ['read-1', 'read-2'] and len(sent) == 2
    assert [c['ok'] for c in receipt['tool_calls']] == [False, False]
    assert receipt['recall_rounds'][0]['model'] == reported
    assert receipt['recall_rounds'][0]['model_route']['requested_model'] == model


@pytest.mark.parametrize('protocol,requested,reported', [
    ('responses', 'gpt-5', 'gpt-5-2025-08-07'),
    ('chat-completions', 'gpt-5', 'gpt-5-2025-08-07'),
    ('anthropic-messages', 'claude-sonnet-4-5', 'claude-sonnet-4-5-20250929'),
])
@pytest.mark.parametrize('entry', ['structured', 'appraise'])
def test_dated_model_reply_keeps_requested_and_reported_identity(configured, monkeypatch, protocol, requested, reported, entry):
    provider = configured_provider(configured, protocol, requested)
    provider.attempt_calls = []
    monkeypatch.setattr('eventmem.core.db.recording', lambda: True)
    tool = 'submit' if entry == 'structured' else 'submit_appraisal'
    def handle(request):
        assert json.loads(request.content)['model'] == requested
        return httpx.Response(200, json=tool_response(protocol, reported, [
            {'type': 'tool_use', 'id': 'submit', 'name': tool, 'input': {'reason': 'Synthetic result', 'values': {}}}]))
    provider.transport = httpx.MockTransport(handle)
    from kin_mind.appraisal import Appraisal
    _, receipt = provider.structured(tool, Appraisal, 'Synthetic', {}) if entry == 'structured' else provider.appraise({})
    assert receipt['model'] == reported
    assert receipt['model_route'] == {'provider': 'test', 'protocol': protocol, 'requested_model': requested,
                                     'reported_model': reported, 'match': 'dated-snapshot'}
    assert provider.attempt_calls[-1]['model'] == reported
    assert provider.attempt_calls[-1]['detail']['model_route'] == receipt['model_route']


@pytest.mark.parametrize('requested,reported', [
    ('gpt-5', 'gpt-5-mini-2025-08-07'), ('gpt-5', 'gpt-5-preview'),
    ('gpt-5', 'gpt-5-2025-08-07-extra'), ('gpt-5', None),
    ('gpt-5-2025-08-07', 'gpt-5-2025-09-01'), ('k3', 'deepseek-flash'),
])
@pytest.mark.parametrize('entry', ['structured', 'appraise'])
def test_unknown_model_reply_is_rejected_and_recorded(configured, monkeypatch, requested, reported, entry):
    provider = configured_provider(configured, 'responses', requested)
    provider.attempt_calls = []
    monkeypatch.setattr('eventmem.core.db.recording', lambda: True)
    tool = 'submit' if entry == 'structured' else 'submit_appraisal'
    provider.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=tool_response('responses', reported, [
        {'type': 'tool_use', 'id': 'submit', 'name': tool, 'input': {'reason': 'Synthetic result', 'values': {}}}])))
    from kin_mind.appraisal import Appraisal
    with pytest.raises(RuntimeError, match='unverified'):
        provider.structured(tool, Appraisal, 'Synthetic', {}) if entry == 'structured' else provider.appraise({})
    call = provider.attempt_calls[-1]
    assert call['outcome'] == 'model-unverified' and call['model'] == reported
    assert call['detail']['model_route']['requested_model'] == requested
    assert call['detail']['model_route']['match'] == 'unverified'


def test_native_model_identity_stays_exact(configured):
    provider = configured_provider(configured, 'responses', 'gpt-5')
    provider.native_review = True
    assert provider._model_verified({'model': 'gpt-5'})
    assert not provider._model_verified({'model': 'gpt-5-2025-08-07'})


@pytest.mark.parametrize('protocol', ['responses', 'chat-completions', 'anthropic-messages'])
def test_empty_reply_does_not_invent_a_structured_submission(configured, monkeypatch, protocol):
    provider = configured_provider(configured, protocol, 'synthetic-model')
    provider.attempt_calls = []
    monkeypatch.setattr('eventmem.core.db.recording', lambda: True)
    provider.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=tool_response(protocol, 'synthetic-model', [])))
    class Result(BaseModel):ok: bool
    with pytest.raises(RuntimeError, match='missing-structured-result'):
        provider.structured('submit', Result, 'Synthetic', {})
    assert [c['outcome'] for c in provider.attempt_calls] == ['missing-tool-call']


def test_provider_registry_override_applies_to_legacy_default_and_follow(configured):
    engine, config, save, file = configured
    edited = {**config['responsesRelay'], 'models': ['new'], 'baseUrl': 'https://edited.invalid/v1', 'credentialFile': 'edited.env'}
    config['providers'] = {'test': edited}
    config['roleProfiles']['memorySummary'] = {'mode': 'independent', 'model': 'new', 'reasoningEffort': 'high'}
    save()
    result = resolve(engine, 'memorySummary')
    assert result['endpoint'] == 'https://edited.invalid/v1'
    assert result['credential'].name == 'edited.env'
    config['roleProfiles']['memorySummary'] = {'mode': 'follow-main'}
    (file.parent / 'model-effective.json').write_text(json.dumps({'main': {'providerId': 'test', 'model': 'new', 'reasoningEffort': 'high'}}))
    save()
    assert resolve(engine, 'memorySummary') == result


def test_follow_main_ignores_single_reply_fallback_effective_model(configured):
    engine, config, save, file = configured
    config['roleProfiles']['memoryRecall'] = {'mode': 'follow-main'}
    config['replyFallback'] = {'enabled': True, 'routes': [{'model': 'opus', 'providerId': 'test', 'reasoningEffort': 'high'}]}
    (file.parent / 'model-effective.json').write_text(json.dumps({'main': {'model': 'gpt-6.1-terra', 'providerId': 'official'}}))
    save()
    assert resolve(engine, 'memoryRecall')['model'] == 'opus'
    assert resolve(engine, 'memoryRecall')['provider'] == 'test'

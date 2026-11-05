"""One user-owned model file for host and direct Python calls; no credential copies."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse, unquote
import httpx

DIRECT_ROLES = {'memoryCompression', 'memoryRecall', 'memoryMaintenance', 'memoryStructured',
                'memoryExtraction', 'memoryConflict', 'memorySummary', 'memoryPrediction',
                'memoryQuery', 'memoryRerank', 'memoryVision', 'memoryAnswer', 'memoryJudge'}
PROVIDER_ROLES = {name: 'memory' + name.title() for name in
                  ('extraction', 'conflict', 'summary', 'prediction', 'query', 'rerank', 'vision', 'answer', 'judge')}


def config_path(engine):
    explicit = getattr(engine, 'model_config_file', None) or os.environ.get('KIN_MODEL_CONFIG_FILE')
    if explicit:
        return Path(explicit)
    pointer = Path(engine.db.root) / 'model-config.path'
    return Path(pointer.read_text().strip()) if pointer.exists() else None


def resolve(engine, role):
    file = config_path(engine)
    if file is None or not file.exists():
        return None
    config = json.loads(file.read_text())
    if config.get('version') != 1:
        raise ValueError('Unsupported model configuration version')
    profile = config.get('roleProfiles', {}).get(role)
    if not profile:
        return None
    if profile.get('mode') == 'follow-main':
        fallback = config.get('replyFallback') or {}
        if fallback.get('enabled'):
            profile = fallback['routes'][0]
        else:
            active = file.parent / 'model-effective.json'
            if not active.exists():
                raise ValueError('Current main model is unavailable; start the host first')
            profile = json.loads(active.read_text())['main']
    if profile.get('authentication') == 'codex-subscription' or profile.get('providerId') == 'official':
        raise ValueError('Direct Python calls need API authentication, not Codex subscription')
    model, effort = profile.get('model'), profile.get('reasoningEffort', 'high')
    if not model:
        raise ValueError('An independent direct model needs an explicit model')
    provider = profile.get('providerId')
    if model.startswith('kin-relay:'):
        _, provider, model = model.split(':', 2)
        provider, model = unquote(provider), unquote(model)
    default = config.get('responsesRelay') or {}
    relays = {**({default['id']: default} if default else {}), **config.get('providers', {})}
    if provider and provider not in {'official', 'deepseek', 'kimi'} and provider not in relays:
        raise ValueError('Unknown configured provider: ' + provider)
    relay = relays.get(provider, {}) if provider else relays.get(default.get('id'), default)
    if model in relay.get('models', []):
        credential = relay.get('credentialFile', 'responses-relay.env')
        if not re.fullmatch(r'[a-zA-Z0-9._-]+\.env', credential):
            raise ValueError('Invalid credential reference')
        endpoint = relay['baseUrl'].rstrip('/')
        url = urlparse(endpoint)
        # Plain HTTP only to this machine: the DSH plugin's own relay listens on a loopback port.
        loopback = url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1', 'localhost')
        if url.scheme != 'https' and not loopback or url.username or url.password or url.query or url.fragment:
            raise ValueError('Invalid direct model endpoint')
        protocol = relay.get('protocol', 'responses')
        if protocol not in ('responses', 'chat-completions', 'anthropic-messages'):
            raise ValueError('Unsupported direct model protocol')
        return {'model': model, 'effort': effort, 'provider': relay['id'], 'protocol': protocol,
                'endpoint': endpoint, 'credential': file.parent / 'credentials' / credential,
                'key_name': 'RESPONSES_RELAY_API_KEY'}
    if provider == 'kimi' or not provider and model in {'k3','k3.1-pro','k3-256k','kimi-for-coding','kimi-for-coding-highspeed','k3.1-pro-highspeed'}:
        return {'model': model, 'effort': effort, 'provider': 'kimi', 'protocol': 'responses',
                'endpoint': 'https://api.kimi.com/coding/v1', 'credential': file.parent / 'credentials' / 'kimi.env', 'key_name': 'KIMI_API_KEY'}
    if provider == 'deepseek' or not provider and model == 'deepseek-flash':
        if effort != 'high':
            raise ValueError('KIN DeepSeek adapter supports high only')
        # Keep the established official credential source; never send it to a different host.
        cfg = engine.settings('models')['summary']
        endpoint = cfg['endpoint'].rstrip('/')
        if urlparse(endpoint).hostname != 'api.deepseek.com' or not endpoint.startswith('https://'):
            raise ValueError('DeepSeek requires the official HTTPS endpoint')
        return {'model': model, 'effort': effort, 'provider': 'deepseek', 'protocol': 'anthropic-messages',
                'endpoint': endpoint if endpoint.endswith('/v1') else endpoint + '/v1',
                'key_env': cfg.get('api_key_env', 'EVENTMEM_API_KEY')}
    raise ValueError('Direct Python calls do not support this model/authentication combination: ' + model)


def credential(profile):
    if profile.get('key_env'):
        key = os.environ.get(profile['key_env'])
    else:
        text = profile['credential'].read_text()
        match = re.search(r'^' + re.escape(profile['key_name']) + r'\s*=\s*(.+)$', text, re.M)
        key = match[1].strip().strip('\"\'') if match else None
    if not key:
        raise RuntimeError('Configured model credential is unavailable')
    return key


def wire_request(profile, body):
    """Convert the existing Anthropic-shaped text/tool contract; preserve tool result IDs."""
    protocol, model, effort = profile['protocol'], profile['model'], profile['effort']
    body = {**body, 'model': model}
    if protocol == 'anthropic-messages':
        body.pop('temperature', None)
        body.pop('thinking', None)
        body['output_config'] = {**body.get('output_config', {})}
        if effort != 'none':
            body['thinking'] = {'type': 'enabled' if model.startswith('deepseek') else 'adaptive'}
            body['output_config']['effort'] = effort
        return 'messages', body
    messages = []
    if body.get('system'):
        messages.append({'role': 'system', 'content': body['system']})
    for message in body['messages']:
        blocks = message['content']
        if isinstance(blocks, str):
            messages.append({'role': message['role'], 'content': blocks})
            continue
        assistant = None
        if (protocol == 'chat-completions' and message['role'] == 'assistant'
                and any(block.get('type') == 'tool_use' for block in blocks)):
            # One model turn may ask for several tools; their results answer this one message.
            assistant = {'role': 'assistant', 'content': None}
            messages.append(assistant)
        for block in blocks:
            kind = block['type']
            if kind in ('thinking', 'redacted_thinking'):
                continue
            if kind == 'text':
                if assistant is not None:
                    assistant['content'] = (assistant['content'] or '') + block['text']
                else:
                    messages.append({'role': message['role'], 'content': block['text']})
            elif kind == 'image':
                source = block['source']
                url = source.get('url') or 'data:' + source['media_type'] + ';base64,' + source['data']
                messages.append({'role': message['role'], 'content': [{'type': 'input_image' if protocol == 'responses' else 'image_url', **({'image_url': url} if protocol == 'responses' else {'image_url': {'url': url}})}]})
            elif kind == 'tool_use':
                if protocol == 'responses':
                    messages.append({'type': 'function_call', 'call_id': block['id'], 'name': block['name'], 'arguments': json.dumps(block['input'])})
                else:
                    assistant.setdefault('tool_calls', []).append({'id': block['id'], 'type': 'function', 'function': {'name': block['name'], 'arguments': json.dumps(block['input'])}})
            elif kind == 'tool_result':
                value = block['content'] if isinstance(block['content'], str) else json.dumps(block['content'])
                messages.append({'type': 'function_call_output', 'call_id': block['tool_use_id'], 'output': value} if protocol == 'responses' else {'role': 'tool', 'tool_call_id': block['tool_use_id'], 'content': value})
            else:
                raise ValueError('Unsupported configured model content: ' + kind)
    tools = [{'type': 'function', 'name': t['name'], 'description': t.get('description', ''), 'parameters': t['input_schema']} for t in body.get('tools', [])]
    if protocol == 'responses':
        result = {'model': model, 'input': messages, 'stream': False, 'store': False,
                  'max_output_tokens': body.get('max_tokens', 8192), 'reasoning': {'effort': effort}}
        if tools:
            result.update(tools=tools, tool_choice='auto')
        return 'responses', result
    result = {'model': model, 'messages': messages, 'stream': False,
              'max_completion_tokens': body.get('max_tokens', 8192), 'reasoning_effort': effort}
    if tools:
        result.update(tools=[{'type': 'function', 'function': {k: v for k, v in t.items() if k != 'type'}} for t in tools], tool_choice='auto')
    return 'chat/completions', result


def wire_response(value, protocol):
    if value.get('error') or value.get('status') == 'failed':
        raise RuntimeError('Configured model returned a failed response')
    if protocol == 'anthropic-messages':
        return value
    content, stop = [], 'end_turn'
    if protocol == 'responses':
        stop = 'max_tokens' if value.get('status') == 'incomplete' else 'end_turn'
        for item in value.get('output', []):
            if item['type'] == 'function_call':
                content.append({'type': 'tool_use', 'id': item['call_id'], 'name': item['name'], 'input': json.loads(item['arguments'])})
            elif item['type'] == 'message':
                content += [{'type': 'text', 'text': b['text']} for b in item['content'] if b['type'] == 'output_text']
    else:
        choice = value['choices'][0]
        if choice['finish_reason'] in ('content_filter', 'error'):
            raise RuntimeError('Configured model refused the response')
        stop = 'max_tokens' if choice['finish_reason'] == 'length' else 'end_turn'
        message = choice['message']
        if message.get('content'):
            content.append({'type': 'text', 'text': message['content']})
        content += [{'type': 'tool_use', 'id': t['id'], 'name': t['function']['name'], 'input': json.loads(t['function']['arguments'])} for t in message.get('tool_calls', [])]
    usage = value.get('usage') or {}
    return {'id': value.get('id'), 'model': value.get('model'), 'content': content, 'stop_reason': stop,
            'usage': {'input_tokens': usage.get('input_tokens', usage.get('prompt_tokens', 0)), 'output_tokens': usage.get('output_tokens', usage.get('completion_tokens', 0))} if usage else None}


def model_match(requested, reported):
    """Name compatibility, not proof of the upstream model's identity."""
    if requested == reported:
        return 'exact'
    if isinstance(reported, str) and not re.search(r'-(?:\d{4}-\d{2}-\d{2}|\d{8})$', requested):
        if re.fullmatch(re.escape(requested) + r'-(?:\d{4}-\d{2}-\d{2}|\d{8})', reported):
            return 'dated-snapshot'
    return 'unverified'


def post(profile, client, body):
    route, payload = wire_request(profile, body)
    key = credential(profile)
    headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'} if profile['protocol'] == 'anthropic-messages' else {'Authorization': 'Bearer ' + key}
    response = client.post(profile['endpoint'] + '/' + route, headers=headers, json=payload)
    if response.status_code != 200:
        return response
    value = wire_response(response.json(), profile['protocol'])
    value['model_route'] = {'provider': profile['provider'], 'protocol': profile['protocol'],
                            'requested_model': profile['model'], 'reported_model': value.get('model'),
                            'match': model_match(profile['model'], value.get('model'))}
    return httpx.Response(200, json=value)


def describe(memory_root, file):
    """Read-only settings view; never return credential values or private file contents."""
    import sqlite3
    from types import SimpleNamespace
    with sqlite3.connect('file:' + str(Path(memory_root) / 'memory.sqlite3') + '?mode=ro', uri=True) as conn:
        row = conn.execute("SELECT data FROM settings WHERE key='models'").fetchone()
    settings = json.loads(row[0]) if row else {}
    engine = SimpleNamespace(db=SimpleNamespace(root=memory_root), settings=lambda _: settings, model_config_file=file)
    values = {}
    for role in DIRECT_ROLES:
        try:
            wire = resolve(engine, role)
            if wire:
                values[role] = {'model': wire['model'], 'reasoningEffort': wire['effort'], 'provider': wire['provider'], 'protocol': wire['protocol']}
            else:
                name = next((name for name, target in PROVIDER_ROLES.items() if role == target), None)
                cfg = settings.get(name or 'summary') or {}
                model = cfg.get('model') if name else 'deepseek-flash'
                values[role] = {'model': model, 'reasoningEffort': 'high' if model and model.startswith('deepseek') else None, 'protocol': cfg.get('protocol', 'anthropic'), 'mode': 'legacy'}
        except (ValueError, KeyError, OSError) as error:
            values[role] = {'error': str(error)}
    return values


if __name__ == '__main__':
    import sys
    print(json.dumps(describe(sys.argv[1], sys.argv[2])))

"""Paid web API contract and capture delivery, with no real credentials/network."""
import json
import httpx
import pytest
from kin_mind.web_service import DeepSeekWebReader, SEARCH_ENDPOINT, FETCH_ENDPOINT
from kin_mind.source_ledger import valid_web_receipt

@pytest.fixture
def setup(tmp_path, monkeypatch):
    key = tmp_path / 'credential.env'
    key.write_text('EVENTMEM_API_KEY=synthetic-secret\n')
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/search'):
            return httpx.Response(200, json={'results': [{'title': 'Example', 'url': 'https://example.com', 'summary': 'A lead', 'snippet': 'snippet'}]})
        return httpx.Response(200, json={'title': 'Example', 'content': '# Example\nPublic page body for capture.'})
    monkeypatch.setattr('kin_mind.web_service.public_host_refusal', lambda *a: False)
    cfg = {'ledger': str(tmp_path / 'ledger.json'), 'execution_id': 'test', 'attempt': 1, 'credentials_file': str(key)}
    return cfg, calls, handler


def test_paid_contract_and_actual_page_delivery(setup):
    cfg, calls, handler = setup
    reader = DeepSeekWebReader(cfg, api_client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw))
    found = reader.search('example domain')
    assert found['state'] == 'search_result' and found['results'][0]['summary'] == 'A lead'
    assert json.loads(calls[0].content) == {'query': 'example domain', 'count': 8, 'need_content': False}
    assert str(calls[0].url) == SEARCH_ENDPOINT
    read = reader.read_page('https://example.com', limit=10)
    assert str(calls[1].url) == FETCH_ENDPOINT
    assert json.loads(calls[1].content) == {'url': 'https://example.com'}
    assert read['text'] == '# Example\n' and read['title'] == 'Example'
    assert valid_web_receipt(read, execution_id='test', attempt=1)
    later = reader.read_page('https://example.com', evidence_id=read['evidence_id'], expected_version=read['version'], offset=10)
    assert later['text'] == 'Public page body for capture.' and len(calls) == 2
    assert 'synthetic-secret' not in json.dumps([found, read, later])
    assert read['transport']['final_url_verified'] is False


@pytest.mark.parametrize('status', [401,402,403,404,408,429,500,504])
def test_service_failure_is_truthful_and_bounded(setup, status):
    cfg, calls, _ = setup
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': 'synthetic-secret private provider error'}})
    reader = DeepSeekWebReader(cfg, api_client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw))
    result = reader.search('example')
    assert result['state'] == 'failed' and result['failure_reason'] == f'web-service-http:{status}'
    assert len(calls) == 1 and 'synthetic-secret' not in json.dumps(result)


def test_private_url_and_bad_query_never_reach_paid_service(setup, monkeypatch):
    cfg, calls, handler = setup
    monkeypatch.setattr('kin_mind.web_service.public_host_refusal', lambda *a: 'address-not-public')
    reader = DeepSeekWebReader(cfg, api_client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw))
    for url in ['file:///etc/passwd', 'http://user:pass@example.com', 'http://127.0.0.1']:
        assert reader.read_page(url)['state'] == 'failed'
    assert reader.search('x' * 129)['state'] == 'failed'
    assert calls == []


def test_empty_search_is_not_a_page_read(setup):
    cfg, _, _ = setup
    reader = DeepSeekWebReader(cfg, api_client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200,json={'results':[]})), **kw))
    result = reader.search('example')
    assert result['state'] == 'search_result' and result['results_empty']


def test_paid_calls_have_turn_day_and_site_budgets(setup, tmp_path):
    import sqlite3
    cfg, calls, handler = setup
    turn = ['turn-1']
    factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    paid = {'database': str(tmp_path / 'memory.sqlite3'), 'scope': 'synthetic-scope', 'purpose': 'chat'}
    reader = DeepSeekWebReader({**cfg, 'paid_calls': paid, 'day_limit': 10}, api_client_factory=factory, turn=lambda: turn[0])
    pages = [reader.read_page(f'https://site0.example/{i}') for i in range(5)]
    assert [p['failure_reason'] for p in pages] == [None] * 4 + ['host-courtesy-limit'] and len(calls) == 4
    found = [reader.search(f'query {i}') for i in range(5)]
    assert [f['failure_reason'] for f in found] == [None] * 4 + ['web-budget-exhausted'] and len(calls) == 8
    turn[0] = 'turn-2'
    assert reader.read_page('https://site0.example/again')['state'] == 'observed'
    assert reader.search('one more')['state'] == 'search_result'
    assert reader.search('over the day')['failure_reason'] == 'web-budget-exhausted' and len(calls) == 10
    rows = sqlite3.connect(paid['database']).execute(
        'SELECT service,purpose,calls,failures FROM mind_paid_calls ORDER BY service').fetchall()
    assert rows == [('deepseek-fetch', 'chat', 5, 0), ('deepseek-search', 'chat', 5, 0)]
    other = DeepSeekWebReader({**cfg, 'paid_calls': {**paid, 'purpose': 'exploration'}, 'turn_limit': None, 'day_limit': 10},
                              api_client_factory=factory)
    assert all(other.search(f'explore {i}')['state'] == 'search_result' for i in range(9))


def test_a_timed_out_paid_call_is_unconfirmed_and_not_sent_again_that_turn(setup, tmp_path):
    from kin_mind.web_read import register_web_tools
    cfg, calls, _ = setup
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('synthetic timeout', request=request)
    paid = {'database': str(tmp_path / 'memory.sqlite3'), 'scope': 'synthetic-scope', 'purpose': 'chat'}
    turn = ['turn-1']
    reader = DeepSeekWebReader({**cfg, 'paid_calls': paid}, turn=lambda: turn[0],
                               api_client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw))
    tools = {}
    class Server:
        def tool(self):
            def register(fn):
                tools[fn.__name__] = fn
                return fn
            return register
    register_web_tools(Server(), reader)
    first = tools['web_search']('same question')
    assert first['state'] == 'unconfirmed' and first['reason'] == 'web-service-timeout' and '计费' in first['note']
    again = tools['web_search']('same question')
    assert again['state'] == 'unconfirmed' and again['reason'] == 'web-request-unconfirmed' and len(calls) == 1
    turn[0] = 'turn-2'
    assert tools['web_search']('same question')['reason'] == 'web-service-timeout' and len(calls) == 2
    import sqlite3
    assert sqlite3.connect(paid['database']).execute('SELECT calls,failures FROM mind_paid_calls').fetchall() == [(2, 2)]


def test_web_results_are_marked_untrusted(setup):
    from kin_mind.web_read import register_web_tools
    cfg, calls, handler = setup
    reader = DeepSeekWebReader(cfg, api_client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw))
    tools = {}
    class Server:
        def tool(self):
            return lambda fn: tools.setdefault(fn.__name__, fn)
    register_web_tools(Server(), reader)
    assert tools['web_search']('example')['content_is_untrusted'] is True
    assert tools['read_page']('https://example.com')['content_is_untrusted'] is True

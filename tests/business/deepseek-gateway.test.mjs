import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import {deepseekRequest,gatewayContracts,NEUTRAL_OWNER_NAME,startDeepSeekGateway} from '../../adapters/deepseek-gateway.mjs';

const source = fs.readFileSync(new URL('../../adapters/deepseek-gateway.mjs', import.meta.url), 'utf8');
const user = text => ({type: 'message', role: 'user', content: [{type: 'input_text', text}]});

test('the public gateway names no particular owner; the host supplies the name', () => {
  assert.doesNotMatch(source, /小光/);
  const neutral = gatewayContracts();
  assert.match(neutral.reply, new RegExp(`只输出给${NEUTRAL_OWNER_NAME}的回复`));
  const named = gatewayContracts('阿澄');
  for (const [profile, text] of Object.entries(named))
    if (profile !== 'continuity-check') assert.match(text, /阿澄/, profile);
  const body = {model: 'deepseek-flash', instructions: 'base', input: [user('在吗')]};
  assert.match(deepseekRequest(body, 'high', null, named).instructions, /^base\n\n只输出给阿澄的回复/);
  assert.match(deepseekRequest(body, 'high', 'exploration', named).instructions, /不是与阿澄聊天/);
  assert.match(deepseekRequest(body).instructions, /只输出给主人的回复/);
});

test('a name that could break the contract text is refused at start', async () => {
  for (const bad of ['', 'two words', '`x`', '${x}', 'x'.repeat(21)])
    assert.throws(() => gatewayContracts(bad), /invalid-owner-name/, bad);
  await assert.rejects(startDeepSeekGateway({key: 'k', ownerName: 'two words', fetchImpl: async () => { throw Error('no call'); }}),
    /invalid-owner-name/);
});

test('the session effort is forwarded; the host default fills in, and fixed background profiles keep theirs', () => {
  const body = effort => ({model: 'deepseek-flash', input: [user('在吗')], ...(effort === undefined ? {} : {reasoning: {effort}})});
  assert.equal(deepseekRequest(body('max'), 'high').reasoning.effort, 'max', 'an explicit manual choice reaches the provider');
  assert.equal(deepseekRequest(body('low'), 'high', 'assessment').reasoning.effort, 'low');
  assert.equal(deepseekRequest(body(undefined), 'high').reasoning.effort, 'high');
  assert.equal(deepseekRequest(body('ultra'), 'high').reasoning.effort, 'high', 'an unknown effort is not forwarded');
  assert.equal(deepseekRequest(body('max'), 'high', 'exploration').reasoning.effort, 'high');
  assert.equal(deepseekRequest(body('none'), 'high', 'computer-action-review').reasoning.effort, 'high');
});

test('a request body is read as bytes: multi-byte text split across chunks arrives intact, and evidence names both efforts', async t => {
  const upstream = [], events = [];
  const gateway = await startDeepSeekGateway({key: 'test-key', ownerName: '阿澄', onRequestEvidence: event => events.push(event),
    fetchImpl: async (url, init) => { upstream.push(JSON.parse(init.body));
      return {ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => ({id: 'resp-1', status: 'completed', output: [], usage: {}})}; }});
  t.after(() => gateway.close());
  const text = '你好'.repeat(3000) + '，今天也想聊聊';
  const payload = Buffer.from(JSON.stringify({model: 'deepseek-flash', reasoning: {effort: 'max'}, input: [user(text)]}));
  const cut = payload.indexOf(Buffer.from('好')) + 1; // inside the three bytes of one character
  const status = await new Promise((resolve, reject) => {
    const request = http.request(gateway.baseUrl + '/responses', {method: 'POST', headers: {authorization: 'Bearer ' + gateway.token, 'content-type': 'application/json'}},
      response => { response.resume(); response.on('end', () => resolve(response.statusCode)); });
    request.on('error', reject);
    request.write(payload.subarray(0, cut));
    setTimeout(() => request.end(payload.subarray(cut)), 20);
  });
  assert.equal(status, 200);
  assert.equal(upstream.length, 1);
  assert.equal(upstream[0].input.at(-1).content[0].text, text);
  assert.equal(upstream[0].reasoning.effort, 'max');
  assert.match(upstream[0].instructions, /只输出给阿澄的回复/);
  const attempted = events.find(event => event.stage === 'forward-attempted');
  assert.equal(attempted.reasoningEffort, 'max');
  assert.equal(attempted.requestedReasoningEffort, 'max');
});

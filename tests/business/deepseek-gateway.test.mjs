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

test('the retired computer-action reviewer has no gateway, profile or contract left (N6, N7)', async () => {
  const gateway = await import('../../adapters/deepseek-gateway.mjs');
  assert.equal(gateway.startComputerActionReviewGateway, undefined);
  assert.equal(gateway.computerActionReviewSchema, undefined);
  assert.equal(gateway.GATEWAY_PROFILES['computer-action-review'], undefined);
  assert.throws(() => gateway.gatewayProfile('computer-action-review'), /unknown-gateway-profile/);
  assert.equal('computer-action-review' in gatewayContracts(), false);
  assert.doesNotMatch(source, /computer-action-review|computer_action_review/);
});

test('every Kin request to DeepSeek runs at high: sessions, assessments, drafts and background profiles (CR-MIND-11)', () => {
  const body = effort => ({model: 'deepseek-flash', input: [user('在吗')], ...(effort === undefined ? {} : {reasoning: {effort}})});
  for (const profile of [null, 'chat', 'assessment', 'contact-draft', 'exploration'])
    for (const effort of ['max', 'low', 'none', undefined, 'ultra'])
      assert.equal(deepseekRequest(body(effort), 'high', profile).reasoning.effort, 'high', `${profile} ${effort}`);
  assert.equal(deepseekRequest(body('low'), 'max').reasoning.effort, 'high', 'an instance option does not move it either');
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
  assert.equal(upstream[0].reasoning.effort, 'high');
  assert.match(upstream[0].instructions, /只输出给阿澄的回复/);
  const attempted = events.find(event => event.stage === 'forward-attempted');
  assert.equal(attempted.reasoningEffort, 'high');
  assert.equal(attempted.requestedReasoningEffort, 'max');
});

test('K3 main replies and bound task deliveries use Kimi high, while assessments and independent workers cannot', async t => {
  const calls=[], evidence=[], usage=[];
  let purpose={lane:'foreground',purpose:'native-chat-turn'};
  const gateway=await startDeepSeekGateway({key:'ds-key',kimiKey:'kimi-key',purposeFor:()=>purpose,
    onRequestEvidence:e=>evidence.push(e),onUsage:r=>usage.push(r),fetchImpl:async(url,init)=>{
      calls.push({url,headers:init.headers,body:JSON.parse(init.body)});
      return new Response(JSON.stringify({id:'k3-response',model:JSON.parse(init.body).model,status:'completed',output:[],usage:{input_tokens:7,output_tokens:2}}),{headers:{'content-type':'application/json'}});
    }});
  t.after(()=>gateway.close());
  const request=model=>fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{Authorization:'Bearer '+gateway.token,'Content-Type':'application/json'},body:JSON.stringify({model,reasoning:{effort:'max'},service_tier:'priority',instructions:'base',input:[{role:'developer',content:'persona'},user('synthetic')],max_output_tokens:4096})});
  assert.equal((await request('k3')).status,200);
  assert.equal(calls[0].url,'https://api.kimi.com/coding/v1/responses');
  assert.equal(calls[0].headers.Authorization,'Bearer kimi-key');
  assert.equal(calls[0].body.reasoning.effort,'high');
  assert.equal(calls[0].body.input[0].role,'developer');
  assert.equal(calls[0].body.max_output_tokens,4096);assert.equal(calls[0].body.service_tier,undefined);
  assert.equal(evidence[0].provider,'kimi');assert.equal(usage[0].provider,'kimi');
  purpose={lane:'background',purpose:'native-assessment'};
  assert.equal((await request('k3')).status,502);assert.equal(calls.length,1);
  assert.equal((await request('deepseek-flash')).status,200);
  assert.equal(calls[1].url,'https://api.deepseek.com/responses');
  assert.equal(calls[1].headers.Authorization,'Bearer ds-key');assert.equal(calls[1].body.reasoning.effort,'high');
  assert.equal(usage[1].provider,'deepseek');
  purpose={lane:'background',purpose:'native-creation'};
  assert.equal((await request('k3')).status,502);assert.equal(calls.length,2);
  purpose={...purpose,mainConversation:true};
  assert.equal((await request('k3')).status,200);assert.equal(calls[2].url,'https://api.kimi.com/coding/v1/responses');
  assert.equal(usage[2].lane,'background','the delivery keeps its existing scheduling and usage attribution');
  purpose={lane:'background',purpose:'native-assessment',mainConversation:true};
  assert.equal((await request('k3')).status,502);assert.equal(calls.length,3);
});

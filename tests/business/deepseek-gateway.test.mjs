import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import {deepseekRequest,kimiRequest,applyMainReplyFormat,providerMessageDiagnostic,gatewayContracts,NEUTRAL_OWNER_NAME,startDeepSeekGateway,nativeTurnPurpose,gatewayTransportFailure} from '../../adapters/deepseek-gateway.mjs';

const source = fs.readFileSync(new URL('../../adapters/deepseek-gateway.mjs', import.meta.url), 'utf8');
const user = text => ({type: 'message', role: 'user', content: [{type: 'input_text', text}]});

test('JSON mode is DeepSeek main reply only; compaction, schemas and other providers retain their format',()=>{
 const identity={threadId:'synthetic-thread',turnId:'synthetic-turn'};
 const context={identity,attributed:{mainConversation:true,purpose:'native-chat-turn'},
  headers:{'x-codex-turn-metadata':JSON.stringify({thread_id:identity.threadId,turn_id:identity.turnId,request_kind:'turn'})}};
 for(const purpose of ['native-chat-turn','native-creation']){
  const body={model:'deepseek-flash',text:{verbosity:'low'},tools:[{type:'function',name:'read_file'}],input:[user('synthetic'),{type:'function_call_output',call_id:'synthetic',output:'synthetic'}]};
  const original=structuredClone(body);
  applyMainReplyFormat(body,{...context,attributed:{mainConversation:true,purpose}});
  assert.deepEqual(body.text,{verbosity:'low',format:{type:'json_object'}});
  assert.deepEqual(body.tools,original.tools);assert.deepEqual(body.input,original.input);
 }
 const cases=[
  [{model:'k3'},{}],[{model:'gpt-6.1-sol'},{}],
  [{model:'deepseek-flash',text:{format:{type:'json_schema',name:'existing',schema:{type:'object'}}}},{}],
  [{model:'deepseek-flash'},{headers:{'x-codex-turn-metadata':JSON.stringify({thread_id:identity.threadId,turn_id:identity.turnId,request_kind:'compaction'})}}],
  [{model:'deepseek-flash'},{headers:{}}],[{model:'deepseek-flash'},{identity:'unknown'}],
  ...['native-assessment','native-contact-draft','native-exploration','native-continuity-check'].map(purpose=>[{model:'deepseek-flash'},{attributed:{mainConversation:true,purpose}}]),
  [{model:'deepseek-flash'},{attributed:{mainConversation:false,purpose:'native-creation'}}],
  [{model:'deepseek-flash'},{profile:'exploration'}],
 ];
 for(const [body,extra]of cases){const before=structuredClone(body);applyMainReplyFormat(body,{...context,...extra});assert.deepEqual(body,before);}
});

test('authenticated native HTTP forwards main reply JSON mode without changing the parser contract',async t=>{
 let sent;const gateway=await startDeepSeekGateway({key:'synthetic',purposeFor:()=>({lane:'foreground',purpose:'native-chat-turn',mainConversation:true}),
  fetchImpl:async(_,init)=>{sent=JSON.parse(init.body);return new Response(JSON.stringify({status:'completed',output:[]}));}});t.after(()=>gateway.close());
 const id='synthetic-thread',turn='synthetic-turn';
 const response=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{authorization:'Bearer '+gateway.token,'session-id':id,'thread-id':id,'x-client-request-id':id,
  'x-codex-turn-metadata':JSON.stringify({thread_id:id,turn_id:turn,request_kind:'turn'})},body:JSON.stringify({model:'deepseek-flash',input:[user('synthetic')],prompt_cache_key:id,
   client_metadata:{session_id:id,thread_id:id,turn_id:turn,root_turn_id:turn}})});
 assert.equal(response.status,200);await response.text();assert.equal(sent.text.format.type,'json_object');
});

test('bound main creation is user work and cannot be starved by foreground classification',async t=>{
  assert.deepEqual(nativeTurnPurpose('creation',{mainConversation:true}),{lane:'user-work',purpose:'native-creation'});
  for(const kind of ['creation','assessment','contact-draft','exploration'])assert.equal(nativeTurnPurpose(kind).lane,'background');
  const lanes=[],usage=[];
  const gateway=await startDeepSeekGateway({key:'synthetic-ds',kimiKey:'synthetic-kimi',
    purposeFor:()=>({...nativeTurnPurpose('creation',{mainConversation:true}),mainConversation:true}),onUsage:r=>usage.push(r),
    lease:{acquire:async({lane})=>{lanes.push(lane);return {proceed:lane!=='background',retryAfterSeconds:30,
      detail:()=>({lane,leaseState:lane==='background'?'wait':'admitted'}),release:async()=>{}};}},
    fetchImpl:async()=>({ok:true,headers:{get:()=> 'application/json'},json:async()=>({id:'synthetic',status:'completed',output:[],usage:{}})})});
  t.after(()=>gateway.close());
  const r=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{authorization:'Bearer '+gateway.token},body:JSON.stringify({model:'k3',input:[user('synthetic work')]})});
  assert.equal(r.status,200);await r.text();assert.deepEqual(lanes,['user-work']);assert.equal(usage[0].outcome,'completed');
});

test('existing transport failures keep a useful reason without retaining private prose',async t=>{
  const marker='synthetic-private-credential-and-message';
  assert.deepEqual(gatewayTransportFailure(Object.assign(Error(marker),{cause:{code:'ECONNRESET'}})),{reason:'network-error',code:'ECONNRESET'});
  assert.equal(JSON.stringify(gatewayTransportFailure(Error(marker))).includes(marker),false);
  const stream=frames=>({ok:true,headers:{get:()=> 'text/event-stream'},body:(async function*(){for(const frame of frames)yield Buffer.from(frame);})()});
  const cases=[
    ['missing-terminal-event',async()=>stream(['data: {"type":"response.created","response":{"id":"synthetic"}}\n\n'])],
    ['malformed-stream-frame',async()=>stream(['data: {"private":"'+marker+'"\n\n'])],
    ['malformed-response-json',async()=>({ok:true,headers:{get:()=> 'application/json'},json:async()=>{throw new SyntaxError(marker);}})],
    ['network-error',async()=>{throw Object.assign(Error(marker),{cause:{code:'ECONNRESET'}});}],
    ['timeout',async(_url,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException(marker,'AbortError')),{once:true}))]
  ];
  for(const [reason,fetchImpl] of cases){
    const rows=[];const gateway=await startDeepSeekGateway({key:'synthetic',onUsage:r=>rows.push(r),fetchImpl,timeoutMs:reason==='timeout'?30:5000});
    try{
      const r=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{authorization:'Bearer '+gateway.token},body:JSON.stringify({model:'deepseek-flash',input:[user('synthetic')]})});
      const body=await r.text();assert.equal(rows.length,1,reason);assert.equal(rows[0].transportFailure.reason,reason);
      assert.equal(rows[0].usageStatus,'unknown');assert.equal(JSON.stringify(rows).includes(marker),false);assert.equal(body.includes(marker),false);
    }finally{await gateway.close();}
  }
});

test('a lane refusal gives the native retry its actual wait interval and makes no provider call',async t=>{
  const gateway=await startDeepSeekGateway({key:'synthetic',purposeFor:()=>nativeTurnPurpose('assessment'),
    lease:{acquire:async()=>({proceed:false,retryAfterSeconds:30,detail:()=>({leaseState:'wait'}),release:async()=>{}})},
    fetchImpl:async()=>assert.fail('refused admission is not a provider request')});t.after(()=>gateway.close());
  const r=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{authorization:'Bearer '+gateway.token},body:JSON.stringify({model:'deepseek-flash',input:[user('synthetic')]})});
  assert.equal(r.status,503);assert.equal(r.headers.get('retry-after'),'30');await r.text();
});

test('Kimi Code request preserves public tool history, image input and caller output budget',()=>{
 const input=[user('synthetic'),{type:'message',role:'developer',content:[{type:'input_text',text:'synthetic contract'}]},
  {type:'function_call',call_id:'call-test',name:'read_file',arguments:'{"path":"synthetic.txt"}'},
  {type:'function_call_output',call_id:'call-test',output:'synthetic result'},
  {type:'message',role:'user',content:[{type:'input_image',image_url:'data:image/png;base64,c3ludGhldGlj'}]}];
 const tools=[{type:'function',name:'read_file',parameters:{type:'object',properties:{path:{type:'string'}}}}];
 const value=kimiRequest({model:'k3',input,tools,tool_choice:'auto',reasoning:{effort:'high'},max_output_tokens:4096,stream:true,service_tier:'priority',previous_response_id:'synthetic',conversation:'synthetic'});
 assert.deepEqual(value.input,input);assert.deepEqual(value.tools,tools);assert.equal(value.max_output_tokens,4096);
 assert.deepEqual(value.reasoning,{effort:'high'});assert.equal(value.store,false);assert.equal(value.stream,true);
 for(const key of ['service_tier','previous_response_id','conversation'])assert.equal(value[key],undefined);
});

test('private provider diagnostics keep the actual reason, redact credentials and bound prose',()=>{
 assert.equal(providerMessageDiagnostic('Unsupported parameter: include').parameter,'include');
 const message='Invalid json schema: field `type`: unknown variant `array` (request_id: synthetic-id)';
 assert.equal(providerMessageDiagnostic(message).message,message);
 const redacted=providerMessageDiagnostic('credential-a Bearer unknown-token credential-b',['credential-a','credential-b']);
 assert.equal(redacted.message,'[redacted] Bearer [redacted] [redacted]');
 const long=providerMessageDiagnostic('x'.repeat(5000));
 assert.equal(long.message.length,4096);assert.equal(long.messageTruncated,true);assert.equal(long.messageUtf8Bytes,5000);
});

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

test('provider HTTP errors retain bounded machine diagnostics without private text or a retry',async t=>{
  for(const body of [JSON.stringify({error:{code:'invalid_request_error',type:'invalid_request_error',param:'max_output_tokens',message:'secret user dialogue',request:'private',headers:{Authorization:'Bearer secret'}}}),JSON.stringify({error:{message:'Invalid request: total message size 2200000 exceeds limit 2097152'}}),'<html>secret proxy error</html>',JSON.stringify({error:{code:'x'.repeat(9000)}})]){
    let calls=0;const rows=[];
    const gateway=await startDeepSeekGateway({key:'ds-key',kimiKey:'kimi-key',onUsage:r=>rows.push(r),fetchImpl:async()=>{calls++;return new Response(body,{status:400,headers:{'Content-Type':'application/json'}});}});
    try{
      const response=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{Authorization:'Bearer '+gateway.token,'Content-Type':'application/json'},body:JSON.stringify({model:'k3',input:[user('synthetic')],reasoning:{effort:'high'}})});
      assert.equal(response.status,400);const value=await response.json();
      const expected=body.includes('max_output_tokens')?{code:'invalid_request_error',type:'invalid_request_error',param:'max_output_tokens'}:body.includes('2200000')?{category:'request-size-limit',requested:2200000,limit:2097152}:null;
      assert.deepEqual(rows[0].providerError,expected);assert.deepEqual(value.error.providerError??null,expected);
      assert.equal(rows[0].usage,null);assert.equal(rows[0].usageStatus,'unknown');assert.equal(calls,1);
      assert.doesNotMatch(JSON.stringify([rows,value]),/secret|private|Bearer|dialogue|<html>/);
    }finally{await gateway.close();}
  }
});

test('provider message goes only to a private sink with known credentials removed, never to ACP or usage',async()=>{
 const privateRows=[],usage=[];
 const gateway=await startDeepSeekGateway({key:'synthetic-ds-secret',kimiKey:'synthetic-kimi-secret',onUsage:r=>usage.push(r),onProviderError:r=>privateRows.push(r),
  fetchImpl:async()=>new Response(JSON.stringify({error:{type:'invalid_request_error',message:'Unsupported parameter include; synthetic-kimi-secret synthetic-ds-secret Bearer unknown-secret',headers:{Authorization:'not retained'}}}),{status:400})});
 try{
  const response=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{Authorization:'Bearer '+gateway.token,'Content-Type':'application/json'},body:JSON.stringify({model:'k3',input:[user('private input')],reasoning:{effort:'high'}})});
  const value=await response.json();assert.equal(response.status,400);assert.equal(privateRows.length,1);
  assert.match(privateRows[0].message,/Unsupported parameter include/);
  assert.doesNotMatch(JSON.stringify(privateRows),/synthetic-.*-secret|unknown-secret|private input|not retained/);
  assert.equal(privateRows[0].requestShape.reasoningEffort,'high');assert.equal(privateRows[0].provider,'kimi');
  assert.doesNotMatch(JSON.stringify([value,usage]),/Unsupported parameter|redacted|private input/);
 }finally{await gateway.close();}
});

test('Kimi compaction is refused locally before lease or upstream; request content is not dropped or exposed',async()=>{
 let calls=0,leases=0;const input=[user('synthetic private marker'),{type:'compaction',encrypted_content:'synthetic opaque marker'}];
 assert.throws(()=>kimiRequest({model:'k3',input}),{code:'KIMI_HISTORY_INCOMPATIBLE'});
 const gateway=await startDeepSeekGateway({key:'synthetic',kimiKey:'synthetic',fetchImpl:async()=>{calls++;throw Error('must not call');},lease:{acquire:async()=>{leases++;throw Error('must not lease');}}});
 try{const response=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{Authorization:'Bearer '+gateway.token,'Content-Type':'application/json'},body:JSON.stringify({model:'k3',input})});
 const body=await response.json();assert.equal(response.status,409);assert.equal(body.error.code,'KIMI_HISTORY_INCOMPATIBLE');assert.match(body.error.message,/压缩历史/);assert.equal(JSON.stringify(body).includes('synthetic'),false);assert.equal(calls,0);assert.equal(leases,0);assert.equal(input.length,2);
 }finally{await gateway.close();}
});

test('native developer-base format passes the existing exact instruction check for Kimi and DeepSeek',async t=>{
 const {instructionTextEvidence}=await import('../../adapters/instruction-evidence.mjs');
 const base='Synthetic approved base',required=instructionTextEvidence(base),sent=[],evidence=[];
 const gateway=await startDeepSeekGateway({key:'synthetic',kimiKey:'synthetic',requiredIncomingInstructions:required,onRequestEvidence:e=>evidence.push(e),
  fetchImpl:async(_,init)=>{sent.push(JSON.parse(init.body));return new Response(JSON.stringify({id:'synthetic',status:'completed',output:[]}),{headers:{'content-type':'application/json'}});}});
 t.after(()=>gateway.close());
 const developer=text=>({type:'message',role:'developer',content:[{type:'input_text',text}]});
 const request=body=>fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{Authorization:'Bearer '+gateway.token,'Content-Type':'application/json'},body:JSON.stringify(body)});
 for(const model of ['k3','deepseek-flash'])for(const legacy of [false,true]){
  const input=[developer('Synthetic persona'),...(legacy?[]:[developer(base)]),user('Synthetic owner input')];
  assert.equal((await request({model,input,...(legacy?{instructions:base}:{})})).status,200);
  assert(sent.at(-1).instructions.startsWith(base+'\n\n'));
  assert.equal(sent.at(-1).input.filter(i=>i.role===(model==='k3'?'developer':'system')).length,1,'the base is moved once; persona retained');
  assert.equal(evidence.findLast(e=>e.stage==='terminal').incomingInstructions.sha256,required.sha256);
 }
 const before=sent.length;
 for(const body of [
  {input:[developer('wrong base'),user(base)]},
  {instructions:'wrong base',input:[developer(base)]},
  {input:[user(base)]},
 ])assert.equal((await request({model:'k3',...body})).status,400);
 assert.equal(sent.length,before,'invalid instructions never reach a provider');
});

test('chat host web functions round-trip namespaces for DeepSeek and Kimi without built-in search', async t => {
  for (const model of ['deepseek-flash','k3']) {
    let upstream;
    const gateway=await startDeepSeekGateway({key:'synthetic-ds',kimiKey:'synthetic-kimi',
      fetchImpl:async(_url,init)=>{upstream=JSON.parse(init.body);return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({id:'synthetic',status:'completed',output:[{type:'function_call',call_id:'call-public',name:'mcp__memorypalace__read_page',arguments:'{"url":"https://example.com"}'}],usage:{}})};}});
    t.after(()=>gateway.close());
    const r=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{authorization:'Bearer '+gateway.token},body:JSON.stringify({model,input:[user('Read public example.com')],tools:[{type:'web_search'},{type:'namespace',name:'mcp__memorypalace',tools:[{type:'function',name:'read_page',parameters:{type:'object',properties:{url:{type:'string'}},required:['url']}}]}]})});
    assert.equal(r.status,200);const result=await r.json();
    assert.equal(upstream.tools.length,1);assert.equal(upstream.tools[0].name,'mcp__memorypalace__read_page');
    assert.equal(result.output[0].namespace,'mcp__memorypalace');assert.equal(result.output[0].name,'read_page');
    assert.equal(result.output[0].call_id,'call-public');
  }
});

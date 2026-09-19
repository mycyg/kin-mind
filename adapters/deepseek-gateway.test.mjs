import test from 'node:test';
import assert from 'node:assert/strict';
import {deepseekRequest, responseNormalizer, startDeepSeekGateway, startExplorationGateway,
  startComputerActionReviewGateway, replyContract, continuityContract, explorationContract,
  computerActionReviewContract, computerActionReviewSchema, nativeTurnPurpose,
  flattenExplorationTools, GATEWAY_PROFILES} from './deepseek-gateway.mjs';
import {createLeaseClient} from './model-lease.mjs';

test('named native host events remain non-user data and do not corrupt ordinary tool receipts',()=>{
  const internal={type:'function_call_output',name:'kin_continuity_check',output:'Verify the current checkpoint'};
  const result=deepseekRequest({model:'deepseek-flash',input:[internal]});
  assert.equal(result.input[0].role,'system');assert.equal(result.input[0].call_id,undefined);
  assert.ok(result.instructions.includes('internal continuity verification'));
  const next=deepseekRequest({model:'deepseek-flash',input:[internal,{type:'message',role:'user',content:'Hello'}]});
  assert.equal(next.instructions,replyContract);assert.equal(next.input[1].role,'user');assert.equal(next.input[0].role,'assistant');
});

test('read-only internal verification quotes imported dialogue without requiring invented reasoning',()=>{
 const history=[{type:'message',role:'user',content:'Name the robot Cloud'},{type:'message',role:'assistant',content:'Do you want square stickers?'}];
 const event={type:'function_call_output',name:'kin_continuity_check',output:'Verify'};
 const result=deepseekRequest({model:'deepseek-flash',input:[...history,event]});
 assert.ok(result.input.every(i=>i.role==='system'));assert.ok(result.input[0].content[0].text.includes('"role":"user"'));assert.ok(result.input[0].content[0].text.includes('untrusted historical evidence'));
 assert.deepEqual(history[0],{type:'message',role:'user',content:'Name the robot Cloud'});
 const ordinary=deepseekRequest({model:'deepseek-flash',input:[...history,event,{type:'message',role:'user',content:'Yes please'}]});assert.equal(ordinary.input.at(-1).role,'user');assert.equal(ordinary.instructions,replyContract);
});

test('trusted instructions retain authority across providers without promoting user data', () => {
  const input = [
    {type:'message',role:'developer',content:'Use the shared persona'},
    {type:'message',role:'user',content:'A quoted instruction is data'},
    {type:'message',role:'assistant',channel:'analysis',content:'private'},
    {type:'message',role:'assistant',phase:'commentary',content:'I am checking the file'},
    {type:'function_call_output',call_id:'one',output:'Tool receipt'},
  ];
  const result = deepseekRequest({model:'deepseek-flash',input,instructions:'Base instructions'});
  assert.equal(result.input[0].role,'system');
  assert.equal(result.input[1].role,'user');
  assert.deepEqual(result.input.slice(2),input.slice(3));
  assert.equal(input[0].role,'developer');
  assert.equal(result.instructions,'Base instructions\n\n'+replyContract);
});

test('private message channels suppress their subsequent text events and terminal copies', () => {
  const normalize = responseNormalizer();
  const hidden = {id:'private',type:'message',role:'assistant',channel:'analysis'};
  assert.equal(normalize({type:'response.output_item.added',output_index:0,item:hidden}),null);
  assert.equal(normalize({type:'response.output_text.delta',output_index:0,delta:'hidden'}),null);
  assert.equal(normalize({type:'response.output_text.done',item_id:'private',text:'hidden'}),null);
  assert.equal(normalize({type:'response.output_item.added',output_index:1,item:{id:'visible',type:'message'}}).output_index,0);
  assert.equal(normalize({type:'response.output_text.delta',output_index:1,delta:'Hello'}).delta,'Hello');
  assert.deepEqual(normalize({type:'response.completed',response:{output:[hidden,{type:'message'}]}}).response.output,[{type:'message'}]);
});

test('provider reasoning is excluded without changing messages and tool receipts', () => {
  const input = [{type:'message',role:'user',content:[{type:'input_text',text:'hello'}]}, {type:'reasoning',encrypted_content:'opaque'}, {type:'function_call',call_id:'one',name:'read'}, {type:'function_call_output',call_id:'one',output:'ok'}];
  const request = deepseekRequest({model:'deepseek-flash',input,reasoning:{effort:'high'},service_tier:'fast'});
  assert.deepEqual(request.input, [input[0], input[2], input[3]]);
  assert.deepEqual(request.reasoning, {effort:'high'});
  assert.equal(request.service_tier, undefined);
  assert.equal(input.length, 4);
});

test('stream filtering preserves tool and message indices and final output', () => {
  const normalize = responseNormalizer();
  assert.equal(normalize({type:'response.output_item.added',output_index:0,item:{type:'reasoning'}}), null);
  assert.equal(normalize({type:'response.reasoning_text.delta',delta:'private'}), null);
  assert.equal(normalize({type:'response.output_item.added',output_index:1,item:{type:'function_call'}}).output_index,0);
  assert.equal(normalize({type:'response.function_call_arguments.delta',output_index:1,delta:'{}'}).output_index,0);
  assert.equal(normalize({type:'response.output_text.delta',output_index:2,delta:'done'}).output_index,1);
  assert.deepEqual(normalize({type:'response.completed',response:{output:[{type:'reasoning'}, {type:'message'}]}}).response.output,[{type:'message'}]);
});

test('local gateway authenticates requests and enforces the configured effort at the wire', async () => {
  let sent;
  const gateway = await startDeepSeekGateway({key:'synthetic-secret', fetchImpl:async (url, options) => {
    sent={url,options}; return new Response(JSON.stringify({id:'r',model:'deepseek-flash',output:[{type:'reasoning'}, {type:'message',content:[]}]}), {headers:{'Content-Type':'application/json'}});
  }});
  try {
    assert.equal((await fetch(gateway.baseUrl+'/responses',{method:'POST'})).status,401);
    const response=await fetch(gateway.baseUrl+'/responses',{method:'POST',headers:{Authorization:'Bearer '+gateway.token},body:JSON.stringify({model:'deepseek-flash',input:[]})});
    assert.equal(response.status,200);
    assert.equal((await response.json()).output.length,1);
    assert.equal(sent.url,'https://api.deepseek.com/responses');
    assert.equal(JSON.parse(sent.options.body).reasoning.effort,'high');
  } finally {await gateway.close();}
});

const sse = frames => new Response(new ReadableStream({start(controller){
  for(const frame of frames)controller.enqueue(new TextEncoder().encode(frame));controller.close();}}),
  {headers:{'Content-Type':'text/event-stream'}});
const turn = (gateway,body={model:'deepseek-flash',input:[]}) => fetch(gateway.baseUrl+'/responses',
  {method:'POST',headers:{Authorization:'Bearer '+gateway.token},body:JSON.stringify(body)});

test('native turns are attributed per session, and a background session yields a lane it cannot have',async()=>{
  const usage=[];let kind='creation',upstream=0;
  assert.deepEqual(nativeTurnPurpose('chat'),{lane:'foreground',purpose:'native-chat-turn'});
  for(const background of ['contact-draft','creation','exploration'])
    assert.equal(nativeTurnPurpose(background).lane,'background');
  // One ledger answer for both turns: full for background, never a wait for foreground.
  const lease=createLeaseClient({request:async()=>({state:'wait',reason:'deepseek-background-capacity',retry_after_seconds:30}),
    setTimer:()=>0,clearTimer:()=>{}});
  const gateway=await startDeepSeekGateway({key:'synthetic-secret',lease,onUsage:row=>usage.push(row),
    purposeFor:()=>nativeTurnPurpose(kind),
    fetchImpl:async()=>{upstream++;return new Response(JSON.stringify({id:'r',model:'deepseek-flash',usage:{input_tokens:7},output:[]}),{headers:{'Content-Type':'application/json'}});}});
  try {
    assert.equal((await turn(gateway)).status,503);
    assert.equal(upstream,0,'a yielded background turn never reaches the provider');
    assert.deepEqual([usage[0].lane,usage[0].purpose,usage[0].usageStatus,usage[0].outcome],
      ['background','native-creation','skipped','lane-skipped']);
    assert.equal(usage[0].usage,null);
    kind='chat';
    assert.equal((await turn(gateway)).status,200);
    assert.equal(upstream,1);
    assert.deepEqual([usage[1].lane,usage[1].purpose,usage[1].usageStatus],['foreground','native-chat-turn','reported']);
  } finally {await gateway.close();}
});

test('a provider error, a failed stream and a broken stream each leave one unknown usage row',async()=>{
  const usage=[];let reply;
  const gateway=await startDeepSeekGateway({key:'synthetic-secret',onUsage:row=>usage.push(row),fetchImpl:async()=>reply()});
  try {
    reply=()=>new Response('',{status:500});
    assert.equal((await turn(gateway)).status,500);
    reply=()=>sse(['data: '+JSON.stringify({type:'response.incomplete',response:{id:'r',model:'deepseek-flash',usage:{input_tokens:4}}})+'\n\n','data: [DONE]\n\n']);
    assert.equal((await turn(gateway)).status,200);
    reply=()=>sse(['data: '+JSON.stringify({type:'response.output_text.delta',delta:'x'})+'\n\n']);
    const broken=await turn(gateway);await broken.text();
    assert.deepEqual(usage.map(r=>[r.usageStatus,r.outcome]),
      [['unknown','provider-http-500'],['reported','incomplete'],['unknown','transport-incomplete']]);
    assert.equal(usage[0].usage,null);assert.equal(usage[2].usage,null);
    assert.deepEqual(usage[1].usage,{input_tokens:4});
    for(const row of usage)assert.ok(JSON.stringify(row).includes('"usage":'),'a usage key is always present');
  } finally {await gateway.close();}
});

test('purpose profiles are host-bound contracts, never request-declared', () => {
  const body = {model:'deepseek-flash',input:[{type:'message',role:'user',content:'Research this'}]};
  const exploration = deepseekRequest(body, 'high', 'exploration');
  assert.equal(exploration.instructions, explorationContract);
  assert.match(explorationContract,
    /host-owned computer\/kin_ui tool only when that tool returned state=observed/);
  assert.match(explorationContract, /failed, reviewed, or acted-only receipt is never a source/);
  assert.match(explorationContract, /evidence_map is claim-to-evidence, never evidence-to-description/);
  assert.match(explorationContract, /exact evidence_id or exact locator strings/);
  assert.match(explorationContract, /Never use prose, shortened ids, version hashes/);
  assert.match(explorationContract, /use null when no finding-level mapping is needed/);
  assert.ok(!exploration.instructions.includes(replyContract),
    'an exploration turn must not inherit the user-reply contract');
  // Even a trailing continuity host event cannot move a profiled instance off
  // its contract; the legacy unprofiled path still swaps as before.
  const event = {type:'function_call_output',name:'kin_continuity_check',output:'Verify'};
  assert.equal(deepseekRequest({model:'deepseek-flash',input:[event]}, 'high', 'exploration').instructions, explorationContract);
  assert.equal(deepseekRequest({model:'deepseek-flash',input:[event]}).instructions, continuityContract);
  assert.equal(deepseekRequest(body, 'high', 'chat').instructions, replyContract);
  assert.equal(deepseekRequest(body, 'high', 'contact-draft').instructions, replyContract);
  assert.equal(deepseekRequest(body, 'high', 'continuity-check').instructions, continuityContract);
  const review=deepseekRequest(body, 'high', 'computer-action-review');
  assert.equal(review.instructions,computerActionReviewContract);
  assert.deepEqual(review.text.format.schema,computerActionReviewSchema);
  assert.equal(review.text.format.strict,true);
  assert.throws(() => deepseekRequest(body, 'high', 'owner-asserted'), /unknown-gateway-profile/);
  assert.deepEqual(GATEWAY_PROFILES.exploration, {lane:'background', purpose:'native-exploration', contract:explorationContract});
});

test('computer action review has a fixed high profile, strict schema and background usage identity', async () => {
  const usage=[];let forwarded;
  const gateway=await startComputerActionReviewGateway({key:'synthetic-secret',onUsage:row=>usage.push(row),
    fetchImpl:async(_url,options)=>{forwarded=JSON.parse(options.body);return new Response(JSON.stringify({
      id:'review-request',model:'deepseek-flash',usage:{input_tokens:5},output:[],
    }),{headers:{'Content-Type':'application/json'}});}});
  try {
    const body={model:'deepseek-flash',input:[{type:'message',role:'user',content:'{"candidate":"click"}'}],
      tools:[{name:'unsafe'}],text:{format:{type:'text'}}};
    const response=await turn(gateway,body);
    assert.equal(response.status,200);
    assert.equal(gateway.profile,'computer-action-review');
    assert.equal(gateway.reasoningEffort,'high');
    assert.equal(forwarded.reasoning.effort,'high');
    assert.deepEqual(forwarded.text.format.schema,computerActionReviewSchema);
    assert.equal(forwarded.tools,undefined);
    assert.deepEqual([usage[0].lane,usage[0].purpose],['background','native-computer-action-review']);
  } finally {await gateway.close();}
});

test('exploration flattens only host-owned MCP namespaces into DeepSeek function tools', () => {
  const body={tools:[
    {type:'custom',name:'exec'},
    {type:'function',name:'request_user_input'},
    {type:'namespace',name:'collaboration',tools:[{type:'function',name:'spawn_agent',parameters:{}}]},
    {type:'namespace',name:'mcp__kin_ui',tools:[
      {type:'function',name:'open_browser_page',description:'open',parameters:{type:'object'}},
    ]},
  ],input:[{type:'function_call',name:'open_browser_page',namespace:'mcp__kin_ui',call_id:'c'}]};
  const reverse=flattenExplorationTools(body);
  assert.deepEqual(body.tools.map(tool=>[tool.type,tool.name]),[
    ['function','mcp__kin_ui__open_browser_page'],
  ]);
  assert.equal(body.input[0].name,'mcp__kin_ui__open_browser_page');
  assert.equal(body.input[0].namespace,undefined);
  const normalized=responseNormalizer(reverse)({type:'response.output_item.done',item:{
    type:'function_call',name:'mcp__kin_ui__open_browser_page',call_id:'c',arguments:'{}',
  }});
  assert.equal(normalized.item.name,'open_browser_page');
  assert.equal(normalized.item.namespace,'mcp__kin_ui');
});

test('one background slot is released after an exploration response before tool action review', async () => {
  let occupied=false;const events=[];
  const lease={acquire:async({purpose})=>{
    if(occupied)return{proceed:false};
    occupied=true;events.push('acquire:'+purpose);
    return{proceed:true,detail:()=>({}),release:async()=>{events.push('release:'+purpose);occupied=false;}};
  }};
  const answer=async()=>new Response(JSON.stringify({id:'r',model:'deepseek-flash',output:[]}),
    {headers:{'Content-Type':'application/json'}});
  const exploration=await startExplorationGateway({key:'secret',lease,fetchImpl:answer});
  const reviewer=await startComputerActionReviewGateway({key:'secret',lease,fetchImpl:answer});
  try {
    assert.equal((await turn(exploration)).status,200);
    assert.equal((await turn(reviewer,{model:'deepseek-flash',input:[
      {type:'message',role:'user',content:'{"candidate":"click"}'},
    ]})).status,200);
    assert.deepEqual(events,[
      'acquire:native-exploration','release:native-exploration',
      'acquire:native-computer-action-review','release:native-computer-action-review',
    ]);
  } finally {await exploration.close();await reviewer.close();}
});

test('a profiled gateway fixes lane and purpose for every request and never consults purposeFor', async () => {
  const usage = [];
  const gateway = await startExplorationGateway({key:'synthetic-secret', onUsage:row=>usage.push(row),
    fetchImpl:async()=>new Response(JSON.stringify({id:'r',model:'deepseek-flash',usage:{input_tokens:3},output:[]}),{headers:{'Content-Type':'application/json'}})});
  try {
    assert.equal(gateway.profile, 'exploration');
    const response = await turn(gateway);
    assert.equal(response.status, 200);
    assert.equal(usage[0].lane, 'background');
    assert.equal(usage[0].purpose, 'native-exploration');
  } finally {await gateway.close();}
  // purposeFor is not even reachable on a profiled instance.
  const profiled = await startDeepSeekGateway({key:'synthetic-secret', profile:'chat',
    purposeFor:()=>{throw Error('must-not-be-called');},
    fetchImpl:async()=>new Response(JSON.stringify({id:'r',model:'deepseek-flash',output:[]}),{headers:{'Content-Type':'application/json'}})});
  try {
    assert.equal((await turn(profiled)).status, 200);
  } finally {await profiled.close();}
});

test('the fifth concurrent background request waits; a foreground request is never queued behind exploration', async () => {
  // One shared ledger, capacity four for background. Foreground is never queued.
  const held = new Map(); let maxConcurrentBackground = 0;
  const ledger = async (operation, body) => {
    const op = operation.replace('model-leases/', '');
    if (op === 'acquire') {
      const background = [...held.values()].filter(h => h.lane === 'background').length;
      if (body.lane === 'background' && background >= 4)
        return {state:'wait', reason:'deepseek-background-capacity', retry_after_seconds:0.01};
      held.set(body.id, {lane:body.lane});
      if (body.lane === 'background') maxConcurrentBackground = Math.max(maxConcurrentBackground, background + 1);
      return {state:'admitted', lease:{renew_after_seconds:600}};
    }
    if (op === 'renew') return held.has(body.id) ? {state:'renewed', lease:{renew_after_seconds:600}} : {state:'lost'};
    if (op === 'release') {held.delete(body.id); return {state:'released'};}
    return {state:'unavailable'};
  };
  const usage = [];
  const lease = () => createLeaseClient({request:ledger, waitBudgetMs:4000});
  let upstreamCalls = 0; const pending = [];
  const slowUpstream = async () => {
    upstreamCalls++;
    return new Promise(resolve => pending.push(() =>
      resolve(new Response(JSON.stringify({id:'r'+upstreamCalls,model:'deepseek-flash',usage:{input_tokens:1},output:[]}),
        {headers:{'Content-Type':'application/json'}}))));
  };
  const exploration = await startExplorationGateway({key:'synthetic-secret', lease:lease(), onUsage:row=>usage.push(row), fetchImpl:slowUpstream});
  const chat = await startDeepSeekGateway({key:'synthetic-secret', profile:'chat', lease:lease(), onUsage:row=>usage.push(row), fetchImpl:slowUpstream});
  const until = async (condition, label) => {
    for (let i = 0; i < 200 && !condition(); i++) await new Promise(resolve => setTimeout(resolve, 10));
    assert.ok(condition(), label);
  };
  try {
    // Four explorations hold all four background slots without finishing.
    const turns = [0, 1, 2, 3].map(() => turn(exploration));
    await until(() => upstreamCalls === 4, 'four explorations reach the provider');
    const fifth = turn(exploration);
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(upstreamCalls, 4, 'the fifth background request is still waiting for a slot');
    // The owner's chat turn shares the ledger but no background slot: it answers now.
    const chatTurn = turn(chat);
    await until(() => upstreamCalls === 5, 'foreground is not queued behind exploration');
    pending.at(-1)();  // answer the chat turn
    assert.equal((await chatTurn).status, 200);
    // One exploration completes; the waiting fifth may now take its slot.
    pending[0]();
    await until(() => upstreamCalls === 6, 'the fifth request took the released slot');
    pending.at(-1)();  // answer the fifth
    assert.equal((await fifth).status, 200);
    for (const answer of pending) answer();
    await Promise.all(turns);
    assert.equal(maxConcurrentBackground, 4, 'background occupancy never exceeded the shared four');
    await until(() => held.size === 0, 'every lease was released on exit');
    const explorationRows = usage.filter(row => row.purpose === 'native-exploration');
    assert.equal(explorationRows.length, 5);
    assert.ok(explorationRows.every(row => row.lane === 'background'));
    assert.ok(explorationRows.every(row => row.leaseState === 'admitted'));
    const chatRow = usage.find(row => row.purpose === 'native-chat-turn');
    assert.equal(chatRow.lane, 'foreground');
    assert.equal(chatRow.leaseState, 'admitted');
  } finally {await exploration.close(); await chat.close();}
});

test('exploration and creation identities never share a purpose, and only deepseek-flash crosses', () => {
  // C7-16: distinct background purposes on the same lane.
  assert.equal(GATEWAY_PROFILES.exploration.purpose, 'native-exploration');
  assert.equal(nativeTurnPurpose('creation').purpose, 'native-creation');
  assert.equal(nativeTurnPurpose('contact-draft').purpose, 'native-contact-draft');
  assert.notEqual(GATEWAY_PROFILES.exploration.purpose, nativeTurnPurpose('creation').purpose);
  assert.equal(GATEWAY_PROFILES.exploration.lane, nativeTurnPurpose('creation').lane);
  // C7-2: a non-DeepSeek model never crosses the gateway, whatever the request claims.
  assert.throws(() => deepseekRequest({model:'gpt-6-astra', input:[]}), /unsupported-request/);
  assert.throws(() => deepseekRequest({model:'deepseek-v4-pro', input:[],}), /unsupported-request/);
  assert.throws(() => deepseekRequest({model:'deepseek-flash', input:[]}, 'extreme'), /unsupported-reasoning-effort/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {deepseekRequest, responseNormalizer, startDeepSeekGateway, replyContract, nativeTurnPurpose} from './deepseek-gateway.mjs';
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

import test from 'node:test';
import assert from 'node:assert/strict';
import {deepseekRequest, responseNormalizer, startDeepSeekGateway, replyContract} from './deepseek-gateway.mjs';

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
  assert.deepEqual(request.reasoning, {effort:'max'});
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
    assert.equal(JSON.parse(sent.options.body).reasoning.effort,'max');
  } finally {await gateway.close();}
});

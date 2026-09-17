import test from 'node:test';
import assert from 'node:assert/strict';
import {createMobileReviewer,REVIEWER_LANES,REVIEWER_PURPOSES} from './mobile-reviewer.mjs';
import {createLeaseClient} from './model-lease.mjs';

test('classifier uses max thinking without exposing reasoning or adding context',async()=>{
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async(url,options)=>{
    assert.equal(url,'https://api.deepseek.com/anthropic/v1/messages');
    const body=JSON.parse(options.body);
    assert.equal(body.model,'deepseek-flash');assert.deepEqual(body.thinking,{type:'enabled'});
    assert.deepEqual(body.output_config,{effort:'high'});
    assert.deepEqual(body.tool_choice,{type:'auto'});
    assert.deepEqual(JSON.parse(body.messages[0].content),{text:'hello',recent:[],task:null});
    return Response.json({content:[{type:'thinking',thinking:'private synthetic reasoning'},{type:'tool_use',name:'route_message',input:{route:'chat',reason:'casual'}}]});
  }});
  assert.equal((await reviewer.classify({text:'hello',recent:[],task:null,timeoutMs:1000})).route,'chat');
});

test('invalid or absent structured result fails closed',async()=>{
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async()=>Response.json({content:[]})});
  await assert.rejects(reviewer.classify({text:'hello'}),/invalid-result/);
  await assert.rejects(reviewer.audit({healthy:true}),/invalid-result/);
});

test('work lock review uses DeepSeek max and retains only structured judgment plus a provider receipt',async()=>{
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async(url,options)=>{
    const body=JSON.parse(options.body);assert.equal(body.model,'deepseek-flash');assert.equal(body.output_config.effort,'high');assert.equal(body.tools[0].name,'review_work_lock');assert.equal(body.max_tokens,131072);assert.deepEqual(body.tools[0].input_schema.properties.evidenceIds.items.enum,['owner-input']);assert.equal(body.tools[0].input_schema.properties.discardDraftIds.maxItems,0);
    return Response.json({id:'verified-request',model:'deepseek-flash',usage:{input_tokens:100},content:[{type:'thinking',thinking:'private synthetic reasoning'},{type:'tool_use',name:'review_work_lock',input:{disposition:'not_a_task',reason:'An optional interest',evidenceIds:['owner-input'],remaining:[],discardDraftIds:[]}}]});
  }});
  const r=await reviewer.reviewWork({inputs:[{id:'owner-input',text:'Explore when idle'}]});assert.equal(r.receipt.reasoning,'high');assert.equal(r.receipt.requestId,'verified-request');assert.equal(r.decision.disposition,'not_a_task');assert.ok(!JSON.stringify(r).includes('private synthetic reasoning'));
});

test('a budget-exhausted review never closes work even when a tool result looks complete',async()=>{
  const usage=[];
  const reviewer=createMobileReviewer({key:'synthetic',onUsage:r=>usage.push(r),fetchImpl:async()=>Response.json({id:'budget-receipt',model:'deepseek-flash',stop_reason:'max_tokens',usage:{output_tokens:65536},content:[{type:'thinking',thinking:'private synthetic reasoning'},{type:'tool_use',name:'review_work_lock',input:{disposition:'not_a_task',reason:'Optional interest',evidenceIds:['owner-input'],remaining:[],discardDraftIds:[]}}]})});
  await assert.rejects(reviewer.reviewWork({inputs:[{id:'owner-input'}]}),error=>{
    assert.equal(error.message,'deepseek-output-budget-exhausted');assert.equal(error.receipt.requestId,'budget-receipt');assert.equal(error.receipt.stopReason,'max_tokens');assert.ok(!JSON.stringify(error.receipt).includes('private synthetic reasoning'));return true;
  });
  assert.equal(usage[0].stopReason,'max_tokens');assert.equal(usage[0].usage.output_tokens,65536);
});

test('every failed exit leaves one usage row with unknown status, never a zero',async()=>{
  const usage=[];
  const failing=(name,fetchImpl)=>createMobileReviewer({key:'synthetic',onUsage:r=>usage.push({name,...r}),fetchImpl});
  await assert.rejects(failing('http',async()=>new Response('',{status:500})).classify({text:'hello',timeoutMs:1000}),/deepseek-http-500/);
  await assert.rejects(failing('timeout',async()=>{throw Object.assign(Error('The operation was aborted due to timeout'),{name:'TimeoutError'});}).audit({healthy:true}),/aborted/);
  await assert.rejects(failing('abort',async()=>{throw Object.assign(Error('This operation was aborted'),{name:'AbortError'});}).reviewWork({inputs:[{id:'owner-input'}]}),/aborted/);
  await assert.rejects(failing('unreadable',async()=>new Response('not json',{headers:{'Content-Type':'application/json'}})).classify({text:'hello',timeoutMs:1000}),/JSON/);
  assert.deepEqual(usage.map(r=>r.name),['http','timeout','abort','unreadable']);
  for(const row of usage) {
    assert.equal(row.usageStatus,'unknown');
    assert.equal(row.usage,null);
    assert.ok(JSON.stringify(row).includes('"usage":null'));
    assert.ok(!JSON.stringify(row).includes('"usage":0'));
  }
  assert.deepEqual(usage.map(r=>r.outcome),['provider-http-500','transport-failed','transport-failed','unreadable-response']);
  assert.deepEqual(usage.map(r=>r.lane),[REVIEWER_LANES.classify,REVIEWER_LANES.audit,REVIEWER_LANES.reviewWork,REVIEWER_LANES.classify]);
});

test('an answer that reports no usage is recorded as unknown rather than vanishing from the row',async()=>{
  const usage=[];
  const reviewer=createMobileReviewer({key:'synthetic',onUsage:r=>usage.push(r),
    fetchImpl:async()=>Response.json({id:'r',model:'deepseek-flash',content:[{type:'tool_use',name:'route_message',input:{route:'chat',reason:'casual',recall:{mode:'light',query:'q',reason:'r'}}}]})});
  await reviewer.classify({text:'hello',timeoutMs:1000});
  assert.equal(usage.length,1);
  assert.equal(usage[0].usageStatus,'unknown');
  assert.equal(usage[0].usage,null);
  assert.ok(JSON.stringify(usage[0]).includes('"usage":null'),'an absent usage key would disappear through JSON.stringify');
  assert.equal(usage[0].purpose,'route_message');
});

test('without a lease client the request on the wire is exactly what it was before lanes existed',async()=>{
  const sent=[];
  const capture=lease=>createMobileReviewer({key:'synthetic',lease,fetchImpl:async(url,options)=>{sent.push({url,options});
    return Response.json({id:'r',model:'deepseek-flash',usage:{input_tokens:1},content:[{type:'tool_use',name:'route_message',input:{route:'chat',reason:'casual',recall:{mode:'light',query:'q',reason:'r'}}}]});}});
  await capture(null).classify({text:'hello',recent:[],task:null,timeoutMs:1000});
  const admitting=createLeaseClient({request:async(route,body)=>route.endsWith('acquire')
    ?{state:'admitted',lease:{id:body.id,lane:body.lane,purpose:body.purpose,ttl_seconds:90,renew_after_seconds:30}}:{state:'released'},
    setTimer:()=>0,clearTimer:()=>{}});
  await capture(admitting).classify({text:'hello',recent:[],task:null,timeoutMs:1000});
  const [plain,leased]=sent;
  assert.equal(plain.url,'https://api.deepseek.com/anthropic/v1/messages');
  assert.equal(plain.url,leased.url);
  assert.equal(plain.options.method,'POST');
  assert.equal(plain.options.redirect,'error');
  assert.deepEqual(plain.options.headers,{'Content-Type':'application/json','x-api-key':'synthetic','anthropic-version':'2023-06-01'});
  assert.deepEqual(plain.options.headers,leased.options.headers);
  assert.equal(plain.options.body,leased.options.body,'a lease changes the accounting, never the request');
  assert.deepEqual(Object.keys(plain.options).sort(),['body','headers','method','redirect','signal']);
  assert.equal(REVIEWER_PURPOSES.classify,'mobile-route-message');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {createMobileReviewer} from './mobile-reviewer.mjs';

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

import test from 'node:test';
import assert from 'node:assert/strict';
import {createMobileReviewer} from './mobile-reviewer.mjs';

test('classifier uses official Anthropic endpoint without thinking or extra context',async()=>{
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async(url,options)=>{
    assert.equal(url,'https://api.deepseek.com/anthropic/v1/messages');
    const body=JSON.parse(options.body);
    assert.equal(body.model,'deepseek-flash');assert.deepEqual(body.thinking,{type:'disabled'});
    assert.deepEqual(JSON.parse(body.messages[0].content),{text:'hello',recent:[],task:null});
    return Response.json({content:[{type:'tool_use',name:'route_message',input:{route:'chat',reason:'casual'}}]});
  }});
  assert.equal((await reviewer.classify({text:'hello',recent:[],task:null,timeoutMs:1000})).route,'chat');
});

test('invalid or absent structured result fails closed',async()=>{
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async()=>Response.json({content:[]})});
  await assert.rejects(reviewer.classify({text:'hello'}),/invalid-result/);
  await assert.rejects(reviewer.audit({healthy:true}),/invalid-result/);
});

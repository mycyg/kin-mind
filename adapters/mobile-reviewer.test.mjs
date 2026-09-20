import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createMobileReviewer,REVIEWER_LANES,REVIEWER_PURPOSES,messageIntents} from './mobile-reviewer.mjs';
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

// ---- reply tail: the decision rides on the routing call, or stands alone ------
const routed=(extra={})=>Response.json({id:'request-1',model:'deepseek-flash',usage:{input_tokens:1},stop_reason:'tool_use',content:[{type:'tool_use',name:'route_message',input:{route:'chat',reason:'casual',recall:{mode:'light',query:'q',reason:'r'},...extra}}]});
const interruptedReply={reason:'new-owner-input',sent:[{text:'The first bubble.',receipt:{messageId:'om_1',acceptedAt:'2026-01-01T00:00:00.000Z'}}],unconfirmed:[],unsent:[{text:'The second bubble.'}],decisions:['continue','rewrite_remainder','supersede']};

test('with no interrupted reply the routing request is pinned to the current route and model-control schema',async()=>{
  const bodies=[];
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async(url,options)=>{bodies.push(options.body);return routed();}});
  await reviewer.classify({text:'hello',clock:{now:'2026-01-01T00:00:00.000Z'},recent:[{role:'user',text:'earlier'}],task:null,mode:'auto',workHeld:false,timeoutMs:1000});
  // Length and SHA-256 pin the exact ordinary request. Reply-tail support adds
  // nothing unless a remainder is actually supplied.
  assert.equal(bodies[0].length,4645);
  assert.equal(createHash('sha256').update(bodies[0]).digest('hex'),'001f05d7c7b5b88edf519398d2afc3f8663d2c9dfc740a6a0469379efba9de50');
  const plain=JSON.parse(bodies[0]);
  assert.doesNotMatch(plain.system,/If uncertain choose work/);
  assert.match(plain.system,/uncertainty alone never upgrades a message to work/);
  assert.deepEqual(Object.keys(plain.tools[0].input_schema.properties),['route','control','profile','force','reason','recall']);
  assert.ok(!bodies[0].includes('interruptedReply')&&!bodies[0].includes('tail'));
});

test('an interrupted reply rides on the routing call: optional input, optional tail output, one call, one usage row',async()=>{
  const bodies=[],usage=[];let answer={tail:{decision:'rewrite_remainder',reason:'Fold it into the next reply'}};
  const reviewer=createMobileReviewer({key:'synthetic',onUsage:row=>usage.push(row),fetchImpl:async(url,options)=>{bodies.push(JSON.parse(options.body));return routed(answer);}});
  const plain=await reviewer.classify({text:'hello',recent:[],task:null,timeoutMs:1000});
  const result=await reviewer.classify({text:'wait, one more thing',recent:[],task:null,timeoutMs:1000,interruptedReply});
  assert.equal(plain.tail,undefined);
  assert.deepEqual([result.route,result.tail.decision,result.tail.reason,result.tail.receipt.requestId,result.tail.receipt.provider],['chat','rewrite_remainder','Fold it into the next reply','request-1','deepseek']);
  const [before,carried]=bodies;
  assert.deepEqual(JSON.parse(carried.messages[0].content),{text:'wait, one more thing',recent:[],task:null,interruptedReply});
  assert.ok(carried.system.startsWith(before.system)&&carried.system.length>before.system.length,'the routing rules are untouched; the tail rules are appended');
  assert.deepEqual(carried.tools[0].input_schema.properties.tail.properties.decision.enum,['continue','rewrite_remainder','supersede']);
  assert.deepEqual(carried.tools[0].input_schema.required,['route','reason','recall','tail']);
  assert.deepEqual([carried.tools[0].name,carried.max_tokens,carried.model],[before.tools[0].name,before.max_tokens,before.model]);
  assert.deepEqual(usage.map(row=>[row.purpose,row.lane]),[['route_message','foreground'],['route_message','foreground']],'no extra call, no extra row');
  // Once a remainder has come back too often, the host narrows the choice and the schema follows.
  await reviewer.classify({text:'and now?',timeoutMs:1000,interruptedReply:{...interruptedReply,resurfaced:2,decisions:['continue','supersede']}});
  assert.deepEqual(bodies[2].tools[0].input_schema.properties.tail.properties.decision.enum,['continue','supersede']);
  // The route stands on its own: an unusable tail is dropped, never turned into a failed classification.
  for(const tail of [{decision:'rewrite_remainder',reason:'not among the allowed answers'},{decision:'delete_everything',reason:'x'},undefined]) {
    answer={tail};
    const kept=await reviewer.classify({text:'hm',timeoutMs:1000,interruptedReply:{...interruptedReply,decisions:['continue','supersede']}});
    assert.deepEqual([kept.route,kept.tail],['chat',undefined]);
  }
});

test('the tail decision on its own: its own tool and purpose label, the routing lane, a receipt, and a closed answer',async()=>{
  const bodies=[],usage=[],leases=[];let answer={decision:'supersede',reason:'The owner moved on'};
  const lease=createLeaseClient({request:async(route,body)=>{leases.push([route,body.lane,body.purpose]);return route.endsWith('acquire')
    ?{state:'admitted',lease:{id:body.id,lane:body.lane,purpose:body.purpose,ttl_seconds:90,renew_after_seconds:30}}:{state:'released'};},setTimer:()=>0,clearTimer:()=>{}});
  const reviewer=createMobileReviewer({key:'synthetic',lease,onUsage:row=>usage.push(row),fetchImpl:async(url,options)=>{bodies.push(JSON.parse(options.body));
    return Response.json({id:'request-tail',model:'deepseek-flash',usage:{input_tokens:3},content:[{type:'thinking',thinking:'private synthetic reasoning'},{type:'tool_use',name:'decide_reply_tail',input:answer}]});}});
  const decided=await reviewer.tail({interruptedReply,newMessage:null});
  assert.deepEqual([decided.decision,decided.reason,decided.receipt.requestId],['supersede','The owner moved on','request-tail']);
  assert.ok(!JSON.stringify(decided).includes('private synthetic reasoning'));
  assert.deepEqual([bodies[0].tools[0].name,bodies[0].tools[0].input_schema.properties.decision.enum,bodies[0].output_config.effort],['decide_reply_tail',['continue','rewrite_remainder','supersede'],'high']);
  assert.deepEqual(JSON.parse(bodies[0].messages[0].content),{interruptedReply,newMessage:null});
  assert.deepEqual(leases[0],['model-leases/acquire',REVIEWER_LANES.classify,'mobile-reply-tail']);assert.equal(REVIEWER_PURPOSES.tail,'mobile-reply-tail');
  assert.deepEqual(usage.map(row=>[row.purpose,row.lane,row.outcome,row.leaseState]),[['mobile-reply-tail','foreground','answered','admitted']],'every call of its own shows up in the usage rows under its own label');
  answer={decision:'rewrite_remainder',reason:'not allowed any more'};
  await assert.rejects(reviewer.tail({interruptedReply:{...interruptedReply,decisions:['continue','supersede']}}),/deepseek-invalid-tail-decision/);
});

// ---- intents: what else the one routing call is asked about the same message ----
const intentAnswer=(extra={})=>Response.json({id:'request-2',model:'deepseek-flash',usage:{input_tokens:1},stop_reason:'tool_use',
  content:[{type:'tool_use',name:'route_message',input:{route:'chat',reason:'casual',stop:'none',recall:{mode:'light',query:'q',reason:'r'},...extra}}]});
const plainInput={text:'hello',clock:{now:'2026-01-01T00:00:00.000Z'},recent:[{role:'user',text:'earlier'}],task:null,mode:'auto',workHeld:false,timeoutMs:1000};

test('with intents on the added prompt and schema are one bounded block, and the request is pinned too',async()=>{
  const bodies=[];
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async(url,options)=>{bodies.push(options.body);return intentAnswer();}});
  await reviewer.classify(plainInput);
  await reviewer.classify({...plainInput,intents:true});
  await reviewer.classify({...plainInput,intents:true,attachments:[{kind:'image',mimeType:'image/jpeg',name:'photo.jpg',bytes:12345}]});
  const [off,on]=bodies;
  // The request as it stands with intents on and nothing sent with the message. Length and
  // SHA-256, so that every later change to these rules has to be pinned again on purpose.
  assert.equal(on.length,5530);
  assert.equal(createHash('sha256').update(on).digest('hex'),'802abeaeddec59c6becfdd4215cf78d44a774847211c308f283c2459f8b8b817');
  assert.equal(on.length-off.length,885,'the whole cost of the intents on an ordinary message');
  const [before,after,carried]=bodies.map(body=>JSON.parse(body));
  assert.equal(after.system.length-before.system.length,434);
  assert.equal(JSON.stringify(after.tools[0].input_schema).length-JSON.stringify(before.tools[0].input_schema).length,451);
  assert.ok(after.system.startsWith(before.system),'the routing rules are untouched; the intent rules are appended');
  assert.deepEqual(Object.keys(after.tools[0].input_schema.properties),['route','control','profile','force','reason','recall','stop','file_send']);
  assert.deepEqual(after.tools[0].input_schema.properties.stop.enum,['none','current_task']);
  assert.deepEqual(after.tools[0].input_schema.required,['route','reason','recall','stop'],'only the one enum token is required of every answer');
  assert.deepEqual(after.tools[0].input_schema.properties.recall.properties.owner_words,{type:'array',maxItems:8,items:{type:'string',maxLength:24}});
  assert.deepEqual(JSON.parse(after.messages[0].content),{text:'hello',clock:{now:'2026-01-01T00:00:00.000Z'},recent:[{role:'user',text:'earlier'}],task:null,mode:'auto',workHeld:false},'the switch itself never travels');
  // The attachment rules are paid for only by a message that actually carried something.
  assert.ok(!after.system.includes('attachments is the metadata'));
  assert.equal(carried.system.length-after.system.length,179);
  assert.deepEqual(JSON.parse(carried.messages[0].content).attachments,[{kind:'image',mimeType:'image/jpeg',name:'photo.jpg',bytes:12345}]);
});

test('intents nobody asked for are never handed on, and the route never fails over one',async()=>{
  let answer={stop:'current_task',file_send:{requested:true,channel:'wechat',file_ref:'report.pdf'},recall:{mode:'light',query:'q',reason:'r',owner_words:['captain']}};
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async()=>intentAnswer(answer)});
  const unasked=await reviewer.classify(plainInput);
  assert.deepEqual([unasked.route,unasked.stop,unasked.file_send,unasked.recall.owner_words],['chat',undefined,undefined,undefined]);
  const asked=await reviewer.classify({...plainInput,intents:true});
  assert.deepEqual(messageIntents(asked),{stop:'current_task',fileSend:{requested:true,channel:'wechat',fileRef:'report.pdf'},ownerWords:['captain']});
  // Nothing about the intents can fail a classification: the route is answered either way.
  answer={stop:'delete_everything',file_send:{requested:true,channel:'sms',file_ref:'x'},recall:{mode:'light',query:'q',reason:'r',owner_words:'not a list'}};
  const kept=await reviewer.classify({...plainInput,intents:true});
  assert.deepEqual([kept.route,messageIntents(kept)],['chat',{}]);
});

test('the bounded reading of an intent drops what is malformed, oversized or unknown',()=>{
  const sha='a'.repeat(64);
  assert.deepEqual(messageIntents({stop:'none'}),{stop:'none'});
  assert.deepEqual(messageIntents({file_send:{requested:true,channel:'wechat',file_ref:' report.pdf ',candidate_sha256:sha.toUpperCase()}}),
    {fileSend:{requested:true,channel:'wechat',fileRef:'report.pdf',candidateSha256:sha}});
  for(const send of [{requested:false,channel:'wechat',file_ref:'x'},{requested:true,channel:'sms',file_ref:'x'},{requested:true,channel:'wechat',file_ref:'y'.repeat(121)},
    {requested:true,channel:'wechat',file_ref:'a'+String.fromCharCode(1)+'b'},{requested:true,channel:'wechat'}])assert.deepEqual(messageIntents({file_send:send}),{});
  // A digest that is not one is dropped on its own; what is left is still a usable request.
  assert.deepEqual(messageIntents({file_send:{requested:true,channel:'wechat',file_ref:'r',candidate_sha256:'not-a-digest'}}),{fileSend:{requested:true,channel:'wechat',fileRef:'r'}});
  assert.deepEqual(messageIntents({recall:{owner_words:['captain','captain','  ','x'.repeat(25),'kin',...Array.from({length:12},(_,i)=>'w'+i)]}}).ownerWords,
    ['captain','kin','w0','w1','w2','w3','w4','w5']);
  assert.deepEqual(messageIntents({recall:{owner_words:[]}}),{});
  assert.deepEqual(messageIntents(null),{});
});

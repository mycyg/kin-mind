import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,modeCommand,recentConversation,attachmentMetadata,CLASSIFIER_DECISION} from './mobile-router.mjs';
import {compactPrompt,publicMobileRuntime} from './mobile-controls.mjs';

function fixture(t, options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-routing-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=100,classificationCalls=0;
  const runtime={known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'gpt-6-astra',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0};
  const switched=[];
  const args={file:path.join(root,'state.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),
    classify:async({text})=>{classificationCalls++;const control={'现在是什么模型':'status','切过去给我说一声哦😯':'watch','退出正经模式':'auto','进入正经模式':'work'}[text];return control?{route:'control',control,reason:'synthetic semantic result'}:{route:/code|test/.test(text)?'work':'chat',reason:'synthetic'};},
    switchModel:async model=>{switched.push(model);runtime.model=model;return{...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},now:()=>++now,...options};
  const router=new MobileRouter(args);
  return{router,runtime,switched,args,classificationCalls:()=>classificationCalls};
}

test('automatic changes notify once per verified transition, including a delayed intermediate model',async t=>{
 const f=fixture(t);f.runtime.model='deepseek-flash';
 await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
 assert.equal(Object.values(f.router.state.notices).filter(n=>n.kind==='model-switched').length,1);
 const first=Object.values(f.router.state.notices)[0];assert.equal(first.target,'gpt-6-astra');
 f.runtime.model='deepseek-flash';await f.router.readRuntime();
 const sent=[];
 await f.router.flushNotices({send:async r=>{sent.push(r);return {state:'accepted',messageId:r.id};},lookup:async()=>null});
 assert.equal(sent.length,2);assert.match(sent[0].text,/此前已切到 GPT/);assert.match(sent[0].text,/现在用的是 DeepSeek/);
 assert.equal(f.router.lastToldModel(),'deepseek-flash','what the owner was told is the model the delivered message named as current');
 assert.equal(f.router.state.recent.some(r=>r.text===sent[0].text),false);
 const restarted=new MobileRouter(f.args);
 await restarted.flushNotices({send:async r=>{sent.push(r);},lookup:async()=>null});assert.equal(sent.length,2);
});

test('explicit switch confirmation shares the automatic transition notice',async t=>{
 const f=fixture(t);
 await f.router.requestMode({commandId:'auto-owner',mode:'auto',notify:true,reason:'owner preference'});
 await f.router.applyPendingMode();
 assert.equal(Object.values(f.router.state.notices).length,1);
 await f.router.prepareModel('deepseek-flash');assert.equal(Object.values(f.router.state.notices).length,1);
});

test('casual and unknown follow-ups preserve completion while execution remains GPT',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'delivered',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  const completion=structuredClone(task.completion),version=task.inputVersion;
  await f.router.dispatch({id:'chat',text:'haha'},async()=> 'steered');
  f.router.classify=async()=>{throw Error('classification timeout');};
  await f.router.dispatch({id:'unknown',text:'How is the switch?'},async()=> 'new-turn');
  assert.equal(task.inputVersion,version);assert.deepEqual(task.completion,completion);
  assert.equal(f.runtime.model,'gpt-6-astra');assert.deepEqual(task.contextInputIds,['chat','unknown']);
});

test('pre-submit errors can retry once, post-submit ambiguity never replays',async t=>{
  const f=fixture(t);const input={id:'one',text:'write code',submissionProtocol:'host-boundary-v1'};
  await assert.rejects(()=>f.router.dispatch(input,async()=>{throw Error('optional restore failed');}),/before native/);
  assert.equal(f.router.state.inputs.one.state,'failed-before-submit');
  let calls=0;
  await f.router.dispatch(input,async(_,started)=>{started();calls++;return 'new-turn';});
  await f.router.dispatch(input,async()=>assert.fail('duplicate'));
  assert.equal(calls,1);
  await assert.rejects(()=>f.router.dispatch({...input,id:'two'},async(_,started)=>{started();throw Error('lost receipt');}),/reconciliation/);
  await assert.rejects(()=>f.router.dispatch({...input,id:'two'},async()=>assert.fail('ambiguous retry')),/reconciliation/);
});

test('internal completion preserves an owner switch notification across restart',async t=>{
  const f=fixture(t);
  await f.router.requestMode({commandId:'owner-request',mode:'auto',reason:'Switch and tell me',notify:true});
  await f.router.requestMode({commandId:'host-finished',mode:'auto',reason:'Work verified',notify:false});
  const restored=new MobileRouter(f.args);
  await restored.applyPendingMode();
  let sends=0;
  await restored.flushNotices({send:async()=>{sends++;return{state:'accepted',messageId:'notice'};},lookup:async()=>null});
  await restored.flushNotices({send:async()=>assert.fail('replayed'),lookup:async()=>null});
  assert.equal(sends,1);assert.equal(f.runtime.model,'deepseek-flash');assert.equal(restored.sessionId,'synthetic');
});

test('internal outreach defers for work and never switches an active provider',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const before=structuredClone(f.router.currentTask());
  const result=await f.router.dispatch({id:'thought',kind:'proactive',text:'A funny thought'},()=>assert.fail('must not submit'));
  assert.equal(result.route,'deferred');assert.deepEqual(f.router.currentTask(),before);assert.deepEqual(f.switched,[]);
  assert.equal(f.router.snapshot().inputs.thought,undefined);
});

test('an idle spontaneous thought is generated by verified DeepSeek without a work task',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'thought',kind:'proactive',text:'An idle thought'},async r=>{assert.equal(r.model,'deepseek-flash');return'new-turn';});
  assert.deepEqual(f.switched,['deepseek-flash']);assert.equal(f.router.tasks().length,0);
});

test('a restarted idle auto conversation restores DeepSeek without creating or replaying input',async t=>{
  const f=fixture(t);await f.router.restoreRoutingProfile();await f.router.restoreRoutingProfile();
  assert.deepEqual(f.switched,['deepseek-flash']);assert.equal(f.router.tasks().length,0);
  assert.deepEqual(f.router.state.inputs,{});assert.equal(Object.values(f.router.state.notices).length,1);
  assert.equal(Object.values(f.router.state.notices)[0].target,'deepseek-flash');
  assert.equal(f.router.state.actual.threadId,'synthetic');
});
test('restart recovery waits for busy or uncertain work and preserves the GPT task lock',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const restarted=new MobileRouter(f.args);f.runtime.active=true;
  assert.equal((await restarted.restoreRoutingProfile()).state,'waiting');assert.deepEqual(f.switched,[]);
  f.runtime.active=false;f.runtime.model='deepseek-flash';
  assert.equal((await restarted.restoreRoutingProfile()).model,'gpt-6-astra');assert.equal(restarted.tasks().length,1);
  const again=new MobileRouter(f.args);again.state.inputs.work.state='unconfirmed';
  assert.equal((await again.restoreRoutingProfile()).state,'waiting');assert.deepEqual(f.switched,['gpt-6-astra']);
});
test('an input arriving before restart recovery selects work under the same coordinator',async t=>{
  const f=fixture(t);const work=f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await work;await f.router.restoreRoutingProfile();assert.deepEqual(f.switched,[]);assert.equal(f.router.tasks().length,1);
});

test('mode commands are explicit owner controls, not embedded instructions',()=>{
  assert.equal(modeCommand('/mode work'),'work');assert.equal(modeCommand('/mode auto'),'auto');
  assert.equal(modeCommand('进入正经模式'),null);
  assert.equal(modeCommand('文章里写着“进入正经模式”'),null);
});

test('classification history never exceeds eight owner messages or final replies',()=>{
  const input=Array.from({length:20},(_,n)=>({role:'user',text:String(n)}));
  input.push({role:'tool',text:'private log'},{role:'assistant',text:'final'});
  const recent=recentConversation(input);
  assert.equal(recent.filter(v=>v.role==='user').length,8);
  assert.equal(recent.filter(v=>v.role==='assistant').length,1);
  assert.equal(recent[0].text,'12');assert.equal(recent.some(v=>v.role==='tool'),false);
});

test('working conversation holds GPT even when receiving jokes and exit commands',async t=>{
  const f=fixture(t);let dispatched=0;
  await f.router.dispatch({id:'work',text:'write code'},async()=>{dispatched++;return'new-turn';});
  f.runtime.active=true;f.runtime.nativeStatus='active';
  await f.router.dispatch({id:'joke',text:'haha'},async result=>{assert.equal(result.model,'gpt-6-astra');dispatched++;return'steered';});
  await f.router.dispatch({id:'exit',text:'退出正经模式'},async result=>{assert.equal(result.model,'gpt-6-astra');return'steered';});
  assert.equal(f.classificationCalls(),3);assert.equal(dispatched,2);assert.deepEqual(f.switched,[]);
  assert.equal(f.router.snapshot().exitRequested,true);
});

test('model completion claim alone cannot release work or background tools',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'tests passed',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'result',state:'accepted',messageId:'synthetic-receipt'});
  f.runtime.backgroundTasks=1;
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  f.runtime.backgroundTasks=0;
  await f.router.reconcile();assert.equal(f.router.tasks().length,0);
  await f.router.dispatch({id:'chat',text:'hello'},async result=>{assert.equal(result.model,'deepseek-flash');return'new-turn';});
  assert.deepEqual(f.switched,['deepseek-flash']);
});

test('unconfirmed delivery and unknown runtime hold the provider after restart',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'result ready',completedTaskId:f.router.currentTask().id,completedInputVersion:f.router.currentTask().inputVersion});
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'result',state:'unconfirmed'});
  const restored=new MobileRouter(f.args);await restored.reconcile();assert.equal(restored.tasks().length,1);
  await restored.dispatch({id:'chat',text:'hello'},async r=>{assert.equal(r.model,'gpt-6-astra');return'new-turn';});
  assert.equal(f.switched.length,0);
  f.runtime.known=false;await restored.reconcile();assert.equal(restored.tasks().length,1);
});

test('desktop handoff and its pending result keep work locked after the native turn ends',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'desktop result pending',completedTaskId:f.router.currentTask().id,completedInputVersion:f.router.currentTask().inputVersion});
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'ack',state:'accepted',messageId:'ack-id'});
  f.runtime.handoffTasks=1;
  const restored=new MobileRouter(f.args);await restored.reconcile();
  assert.equal(restored.tasks().length,1);await restored.applyPendingMode();
  assert.equal(f.switched.length,0);
  f.runtime.handoffTasks=0;await restored.reconcile();
  assert.equal(restored.tasks().length,0);
});

test('duplicate input never resubmits or reclassifies; uncertain submission is held',async t=>{
  const f=fixture(t);let count=0;const input={id:'one',text:'hello'};
  await f.router.dispatch(input,async()=>{count++;return'new-turn';});
  await f.router.dispatch(input,async()=>{count++;return'new-turn';});
  assert.equal(count,1);assert.equal(f.classificationCalls(),1);
  await assert.rejects(f.router.dispatch({id:'two',text:'hi'},async()=>{throw Error('lost response');}),/reconciliation/);
  await assert.rejects(f.router.dispatch({id:'two',text:'hi'},async()=>{count++;}),/reconciliation/);
  assert.equal(count,1);
});

test('new input invalidates an earlier completion proposal',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'done',completedTaskId:f.router.currentTask().id,completedInputVersion:f.router.currentTask().inputVersion});
  await f.router.dispatch({id:'addition',text:'also add a test'},async()=> 'steered');
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'result',state:'accepted',messageId:'one'});
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
});

test('DeepSeek work escalation waits for its turn before changing provider',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';f.runtime.active=true;f.runtime.nativeStatus='active';
  await assert.rejects(f.router.dispatch({id:'work',text:'write code'},async()=>{throw Error('must not submit');}),/waiting/);
  assert.deepEqual(f.switched,[]);assert.equal(f.router.currentTask().status,'running');
  f.runtime.active=false;f.runtime.nativeStatus='idle';
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  assert.deepEqual(f.switched,['gpt-6-astra']);
});

test('classifier timeout fails over to work, without treating its output as chat',async t=>{
  const f=fixture(t,{classify:async()=>{throw Error('timeout');}});
  await f.router.dispatch({id:'question',text:'unclear'},async r=>{assert.equal(r.model,'gpt-6-astra');return'new-turn';});
  assert.equal(f.router.currentTask().status,'running');
});

test('switch and input acceptance share one mutex',async t=>{
  const f=fixture(t);const releases=[];let count=0;
  f.router.switchModel=async model=>{await new Promise(resolve=>{releases.push(resolve);});f.runtime.model=model;return{...f.runtime};};
  const a=f.router.dispatch({id:'chat',text:'hi'},async()=>{count++;return'new-turn';});
  while(!releases.length)await new Promise(resolve=>setImmediate(resolve));
  const b=f.router.dispatch({id:'work',text:'write code'},async()=>{count++;return'new-turn';});
  assert.equal(count,0);releases[0]();await a;
  while(releases.length<2)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(count,1);releases[1]();await b;assert.equal(count,2);
});

test('interrupted provider transition is reconciled without replaying uncertain input',async t=>{
  const f=fixture(t);
  f.router.state.transition={state:'switching',to:'deepseek-flash'};
  f.router.state.inputs.uncertain={id:'uncertain',hash:'synthetic',state:'submitting'};
  f.router.save('synthetic-crash');
  const restored=new MobileRouter(f.args);
  await restored.reconcile();
  assert.equal(restored.state.transition.state,'failed-restored');
  assert.equal(restored.state.inputs.uncertain.state,'unconfirmed');
  await restored.dispatch({id:'new',text:'hello'},async r=>{assert.equal(r.model,'deepseek-flash');return'new-turn';});
});

test('unconfirmed recovery never switches a background task and retries at most once',async t=>{
  const f=fixture(t);f.runtime.profileReady=false;f.runtime.backgroundTasks=1;
  f.router.state.transition={state:'unconfirmed',to:'deepseek-flash'};
  let attempts=0;f.router.switchModel=async()=>{attempts++;throw Error('offline');};
  await f.router.reconcile();assert.equal(attempts,0);
  f.runtime.backgroundTasks=0;
  await f.router.reconcile();await f.router.reconcile();assert.equal(attempts,1);
  assert.equal(f.router.state.transition.state,'unconfirmed');
});

test('mode tool returns pending during its own turn and host applies it after tools exit',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';f.runtime.active=true;f.runtime.nativeStatus='active';
  const receipt=await f.router.requestMode({commandId:'enter',mode:'work',reason:'new idea'});
  assert.equal(receipt.state,'pending');
  await f.router.applyPendingMode();assert.equal(f.switched.length,0);
  f.runtime.active=false;f.runtime.nativeStatus='idle';
  await f.router.applyPendingMode();assert.deepEqual(f.switched,['gpt-6-astra']);
  assert.equal(f.router.state.requests.enter.state,'applied');
});

test('observations do not stale configuration revision and invalid completion does not mutate mode',async t=>{
  const f=fixture(t);const revision=f.router.state.configRevision;
  await f.router.observe('reply',{final:true,text:'hello'});
  await f.router.requestMode({commandId:'enter',mode:'work',reason:'owner requested',expectedRevision:revision});
  const state=f.router.snapshot();
  await assert.rejects(f.router.requestMode({commandId:'bad',mode:'auto',reason:'done',completedTaskId:'missing'}),/not open/);
  assert.equal(f.router.state.exitRequested,state.exitRequested);
  assert.equal(f.router.state.configRevision,state.configRevision);
});

test('explicit stop releases work only when native execution has stopped',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  f.runtime.active=true;f.runtime.nativeStatus='active';
  await f.router.dispatch({id:'stop',text:'停止任务'},async()=> 'steered');
  await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  f.runtime.active=false;f.runtime.nativeStatus='idle';
  await f.router.reconcile();assert.equal(f.router.tasks().length,0);
});

test('natural wording is judged by DeepSeek with recent context and the independent work lock',async t=>{
  const seen=[];const f=fixture(t,{classify:async input=>{seen.push(input);return{route:'control',control:'status',reason:'semantic status inquiry'};}});
  await f.router.dispatch({id:'unfamiliar',text:'咦，你的脑袋已经换回刚才那个了没？'},async()=>{throw Error('no model turn for status');});
  assert.equal(seen.length,1);assert.equal(seen[0].text,'咦，你的脑袋已经换回刚才那个了没？');
  assert.equal(seen[0].mode,'auto');assert.equal(seen[0].workHeld,false);assert.equal(f.router.tasks().length,0);
});

test('native compact is not a work task and its command survives memory enrichment',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';
  await f.router.dispatch({id:'compact',text:'/compact'},async decision=>{
    assert.equal(decision.command,'compact');assert.equal(decision.model,'deepseek-flash');
    const pending=compactPrompt({contextToken:'source',prompt:[{type:'text',text:'synthetic memory prefix'},{type:'text',text:'/compact'}]});
    assert.equal(pending.prompt[0].text,'/compact');assert.equal(pending.contextToken,'source');return'new-turn';
  });
  assert.equal(f.classificationCalls(),0);assert.equal(f.router.tasks().length,0);
  await f.router.observeOperation('compact','start');
  await assert.rejects(f.router.dispatch({id:'code',text:'write code'},async()=>{throw Error('must wait');}),/waiting/);
  assert.deepEqual(f.switched,[]);
  await f.router.observeOperation('compact','end',{stopReason:'end_turn'});
  await f.router.dispatch({id:'code',text:'write code'},async()=> 'new-turn');
  assert.deepEqual(f.switched,['gpt-6-astra']);
});

test('compact completion returns a standalone chat to automatic routing',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';
  await f.router.dispatch({id:'compact',text:'/compact'},async()=> 'new-turn');
  await f.router.observeOperation('compact','start');await f.router.observeOperation('compact','end',{stopReason:'end_turn'});
  const restored=new MobileRouter(f.args);
  await restored.dispatch({id:'hello',text:'haha'},async d=>{assert.equal(d.model,'deepseek-flash');return'new-turn';});
  assert.equal(restored.tasks().length,0);assert.deepEqual(f.switched,[]);
});

test('ambiguous compact after restart remains held and is never replayed',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';
  await f.router.dispatch({id:'compact',text:'/compact'},async()=> 'new-turn');
  const restored=new MobileRouter(f.args);
  assert.equal(restored.state.operations.compact.state,'unconfirmed');
  assert.equal((await restored.dispatch({id:'compact',text:'/compact'},async()=>{throw Error('replay');})).route,'deduplicated');
  await assert.rejects(restored.prepareModel('gpt-6-astra'),/Work prevents/);
});

test('state queries and switch watches bypass classification without invalidating work',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask(),version=task.inputVersion;
  await f.router.requestMode({commandId:'done',mode:'auto',reason:'validated',completedTaskId:task.id,completedInputVersion:version});
  f.runtime.active=true;f.runtime.nativeStatus='active';f.runtime.backgroundTasks=1;
  for(const [id,text] of [['status','现在是什么模型'],['watch','切过去给我说一声哦😯'],['exit','退出正经模式']]) {
    assert.equal((await f.router.dispatch({id,text},async()=>{throw Error('runtime controls do not prompt a model');})).route,'host-control');
  }
  assert.equal(task.inputVersion,version);assert.ok(task.completion);assert.equal(f.classificationCalls(),4);
  await f.router.applyPendingMode();assert.deepEqual(f.switched,[]);
  const sent=[];await f.router.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'receipt-'+n.id};},lookup:async()=>null});
  assert.ok(sent.length);assert.equal(Object.keys(task.deliveries).length,0);
  f.runtime.active=false;f.runtime.nativeStatus='idle';f.runtime.backgroundTasks=0;
  await f.router.observe('prompt-end',{stopReason:'end_turn'});await f.router.reconcile();assert.equal(f.router.tasks().length,1);
  await f.router.observe('delivery',{id:'real-result',state:'accepted',messageId:'result'});
  await f.router.reconcile();await f.router.applyPendingMode();assert.deepEqual(f.switched,['deepseek-flash']);
});

test('completion proposals cannot acknowledge an older task input version',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask(),oldVersion=task.inputVersion;
  await f.router.dispatch({id:'change',text:'include more tests'},async()=> 'steered');
  await assert.rejects(f.router.requestMode({commandId:'stale',mode:'auto',reason:'done',completedTaskId:task.id,completedInputVersion:oldVersion}),/version changed/);
  assert.equal(task.completion,undefined);
});

test('switch confirmation is sent once only after native verification, with stable receipts',async t=>{
  const f=fixture(t);f.runtime.active=true;f.runtime.nativeStatus='active';
  await f.router.requestMode({commandId:'exit',mode:'auto',reason:'owner wants chat',notify:true});
  await f.router.applyPendingMode();assert.equal(Object.keys(f.router.state.notices).length,0);
  f.runtime.active=false;f.runtime.nativeStatus='idle';await f.router.applyPendingMode();
  const sent=[];const channel={send:async n=>{assert.equal(f.runtime.model,'deepseek-flash');sent.push(n);return{state:'accepted',messageId:'platform-id'};},lookup:async()=>null};
  await f.router.flushNotices(channel);await f.router.flushNotices(channel);
  await new MobileRouter(f.args).flushNotices(channel);
  assert.equal(sent.length,1);assert.match(sent[0].text,/DeepSeek Flash/);
  assert.equal(Object.values(f.router.state.notices)[0].messageId,'platform-id');
});

test('uncertain notification reconciles its original ID without another send',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{throw Error('no model');});
  let sends=0;
  await f.router.flushNotices({send:async()=>{sends++;return{state:'accepted'};},lookup:async()=>({state:'unconfirmed'})});
  const id=Object.keys(f.router.state.notices)[0];assert.equal(f.router.state.notices[id].state,'unconfirmed');
  const restored=new MobileRouter(f.args);restored.state.notices[id].nextAttemptAt=0;
  await restored.flushNotices({send:async()=>{sends++;throw Error('must not replay');},lookup:async requested=>{assert.equal(requested,id);return{state:'accepted',messageId:'reconciled'};}});
  assert.equal(sends,1);assert.equal(restored.state.notices[id].messageId,'reconciled');
});

test('known pre-send failure retries the same notification ID',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  const ids=[];const channel={send:async n=>{ids.push(n.id);if(ids.length===1)throw Error('not submitted');return{state:'accepted',messageId:'id'};},lookup:async()=>({state:'not-started'})};
  await f.router.flushNotices(channel);Object.values(f.router.state.notices)[0].nextAttemptAt=0;await f.router.flushNotices(channel);
  assert.equal(ids.length,2);assert.equal(ids[0],ids[1]);
});

test('concurrent notice drains do not send twice or mark an in-flight send retryable',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  let release,sends=0;const send=async()=>{sends++;await new Promise(r=>{release=r;});return{state:'accepted',messageId:'one'};};
  const first=f.router.flushNotices({send,lookup:async()=>({state:'not-started'})});
  while(!release)await new Promise(r=>setImmediate(r));
  await f.router.flushNotices({send,lookup:async()=>({state:'not-started'})});release();await first;
  assert.equal(sends,1);
});

test('historical transitions are distinguished from current runtime and every host switch is recorded',async t=>{
  const f=fixture(t);f.router.state.transition={state:'applied',from:'gpt-6-astra',to:'deepseek-flash'};
  const before=publicMobileRuntime(f.router.state,f.runtime,'synthetic');
  assert.equal(before.actual.model,'gpt-6-astra');assert.equal(before.lastTransition.matchesCurrentModel,false);
  await f.router.prepareModel('deepseek-flash');
  const view=await f.router.readRuntime();assert.equal(view.actual.model,'deepseek-flash');
  assert.equal(view.lastTransition.source,'probe');assert.equal(view.lastTransition.matchesCurrentModel,true);
  f.runtime.model='gpt-6-astra';const changed=await f.router.readRuntime();
  assert.equal(changed.lastTransition.source,'runtime-observation');assert.equal(changed.lastTransition.to,'gpt-6-astra');
});

test('chat in an explicitly selected work mode does not invent a work task',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'enter',text:'进入正经模式'},async()=>{throw Error('host control');});
  await f.router.applyPendingMode();
  await f.router.dispatch({id:'joke',text:'haha'},async d=>{assert.equal(d.model,'gpt-6-astra');return'new-turn';});
  assert.equal(f.router.state.mode,'work');assert.equal(f.router.tasks().length,0);
  await f.router.dispatch({id:'exit',text:'退出正经模式'},async()=>{throw Error('host control');});
  await f.router.applyPendingMode();assert.equal(f.runtime.model,'deepseek-flash');
});

test('a notification in flight holds provider changes until its send settles',async t=>{
  const f=fixture(t);await f.router.dispatch({id:'status',text:'现在是什么模型'},async()=>{});
  let release;const sending=f.router.flushNotices({send:async()=>{await new Promise(r=>{release=r;});return{state:'accepted',messageId:'notice'};},lookup:async()=>null});
  while(!release)await new Promise(r=>setImmediate(r));
  await f.router.requestMode({commandId:'exit',mode:'auto',reason:'owner exits'});
  await f.router.applyPendingMode();assert.deepEqual(f.switched,[]);
  release();await sending;await f.router.applyPendingMode();assert.deepEqual(f.switched,['deepseek-flash']);
});

// ---- durable state file: one damaged revision never costs the conversation ----

test('a damaged state file falls back to the revision the writer kept, and the bytes are quarantined',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const file=f.args.file,directory=path.dirname(file);
  assert.equal(fs.existsSync(file+'.prev'),true,'the writer keeps the revision it replaces');
  fs.writeFileSync(file,'{"schema":1,"tasks"');
  const restored=new MobileRouter(f.args);
  assert.equal(restored.tasks().length,1,'the open task is still there');
  assert.equal(restored.state.recovery.reason,'restored-from-previous-revision');
  assert.equal(restored.state.recovery.from,'previous');
  const kept=fs.readdirSync(path.join(directory,'quarantine'));
  assert.deepEqual([kept.length,restored.state.recovery.quarantined],[1,kept]);
  assert.deepEqual((await restored.readRuntime()).recovery,restored.state.recovery,'and status can show it');
  assert.equal(fs.readFileSync(path.join(directory,'quarantine',kept[0]),'utf8'),'{"schema":1,"tasks"','the damaged bytes are kept, never deleted');
  assert.deepEqual(new MobileRouter(f.args).state.recovery,restored.state.recovery,'the fact stays readable after a healthy restart');
});

test('with no readable revision left the router does not invent a fresh history',async t=>{
  const f=fixture(t);f.runtime.model='deepseek-flash';
  await f.router.dispatch({id:'one',text:'hello'},async()=> 'new-turn');
  const file=f.args.file;
  fs.writeFileSync(file,'not json');fs.writeFileSync(file+'.prev','[');
  const restored=new MobileRouter(f.args);
  assert.equal(restored.state.recovery.reason,'state-file-unreadable');
  assert.equal(restored.state.recovery.quarantined.length,2);
  assert.equal(restored.state.recovery.restoredInputs,1);
  assert.equal(restored.state.inputs.one.state,'unconfirmed','what was accepted comes back from the journal');
  await assert.rejects(restored.dispatch({id:'one',text:'hello'},async()=>assert.fail('replayed')),/reconciliation/);
  assert.equal((await restored.restoreRoutingProfile()).state,'waiting','no provider is switched under work nobody can see');
  assert.deepEqual(f.switched,[]);
  const untouched=new MobileRouter({...f.args,file:path.join(path.dirname(file),'never-written.json')});
  assert.equal(untouched.state.recovery,undefined,'a missing file is still a fresh start');
});

test('two writers never share a temporary name, and an interrupted write leaves the file loadable',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const second=new MobileRouter(f.args),temporaries=[],rename=fs.renameSync;
  fs.renameSync=(from,to)=>{temporaries.push(from);return rename(from,to);};
  try {f.router.save('one');second.save('two');} finally {fs.renameSync=rename;}
  assert.ok(temporaries.length>=4);
  assert.equal(new Set(temporaries).size,temporaries.length,'same process, same second, different names');
  fs.renameSync=(from,to)=>{if(to===f.args.file)throw Object.assign(Error('interrupted'),{code:'EIO'});return rename(from,to);};
  try {assert.throws(()=>f.router.save('interrupted'));} finally {fs.renameSync=rename;}
  fs.writeFileSync(f.args.file+'.999.abcdef.tmp','half a revision');
  const after=new MobileRouter(f.args);
  assert.equal(after.tasks().length,1);assert.equal(after.state.recovery,undefined);
  assert.deepEqual(fs.readdirSync(path.dirname(f.args.file)).filter(n=>n.endsWith('.tmp')),['state.json.999.abcdef.tmp'],'a write cleans up after itself');
});

// ---- a restart that changes nothing the owner can see says nothing ----

async function told(f,model) {
  f.runtime.model=model;
  await f.router.dispatch({id:'told-'+model,text:'现在是什么模型'},async()=>{});
  await f.router.flushNotices({send:async()=>({state:'accepted',messageId:'told-'+model}),lookup:async()=>null});
  assert.equal(f.router.lastToldModel(),model);
}

test('a restart that restores the model the owner was last told about settles without a message',async t=>{
  const f=fixture(t);
  await told(f,'deepseek-flash');
  f.runtime.model='gpt-6-astra';                     // the native runtime came back on its own default
  const restarted=new MobileRouter(f.args);
  assert.equal((await restarted.restoreRoutingProfile()).model,'deepseek-flash');
  const notice=Object.values(restarted.state.notices).find(n=>n.kind==='model-switched');
  assert.deepEqual([notice.state,notice.reason,notice.text],['suppressed','restart-restored-known-model',undefined]);
  await restarted.flushNotices({send:async()=>assert.fail('a suppressed notice is never sent'),lookup:async()=>assert.fail('and never reconciled')});
  const view=await restarted.readRuntime();
  assert.ok(view.notifications.some(n=>n.state==='suppressed'&&n.reason==='restart-restored-known-model'),'still visible in status');
  await restarted.dispatch({id:'ask',text:'现在是什么模型'},async()=>{});
  const sent=[];
  await restarted.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'answer'};},lookup:async()=>null});
  assert.equal(sent.length,1);assert.match(sent[0].text,/DeepSeek Flash/,'an explicit question is still answered');
});

test('a restart that restores a different model tells the owner, exactly as before',async t=>{
  const f=fixture(t);
  await told(f,'deepseek-flash');
  // The mode moved on without the owner hearing about it, so the restored model is news.
  await f.router.requestMode({commandId:'host-work',mode:'work',reason:'Verified work is under way',notify:false});
  const restarted=new MobileRouter(f.args);
  assert.equal((await restarted.restoreRoutingProfile()).model,'gpt-6-astra');
  const notice=Object.values(restarted.state.notices).find(n=>n.kind==='model-switched'&&n.target==='gpt-6-astra');
  assert.equal(notice.state,'pending');assert.match(notice.text,/已经切到 GPT/);
  const sent=[];
  await restarted.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'switched'};},lookup:async()=>null});
  await restarted.flushNotices({send:async()=>assert.fail('sent twice'),lookup:async()=>null});
  assert.equal(sent.length,1);
});

test('a restart in the middle of a real switch still tells the owner once',async t=>{
  const f=fixture(t);
  await told(f,'deepseek-flash');
  f.router.state.transition={id:'switch-synthetic',state:'switching',from:'deepseek-flash',to:'gpt-6-astra',source:'input',sourceId:'told-deepseek-flash'};
  f.router.save('synthetic-crash');
  f.runtime.model='gpt-6-astra';
  const restarted=new MobileRouter(f.args);
  assert.equal(restarted.state.transition.state,'unconfirmed');
  await restarted.reconcile();
  const switches=Object.values(restarted.state.notices).filter(n=>n.kind==='model-switched');
  assert.equal(switches.length,1);assert.equal(switches[0].state,'pending');
  const sent=[];
  await restarted.flushNotices({send:async n=>{sent.push(n);return{state:'accepted',messageId:'switched'};},lookup:async()=>null});
  await restarted.flushNotices({send:async()=>assert.fail('sent twice'),lookup:async()=>null});
  assert.equal(sent.length,1);assert.equal(restarted.lastToldModel(),'gpt-6-astra');
});

// ---- reply tail port: the unsent rest of an interrupted reply rides on the routing call ----
function tailPort(answers={}) {
  const calls=[];
  const method=name=>async detail=>{calls.push([name,detail]);if(answers[name] instanceof Error)throw answers[name];return typeof answers[name]==='function'?answers[name](detail):answers[name]??null;};
  return {calls,pending:method('pending'),decided:method('decided'),missed:method('missed'),stopped:method('stopped')};
}
const synthetic={reason:'new-owner-input',sent:[{text:'The first bubble.',receipt:{messageId:'om_1',acceptedAt:null}}],unconfirmed:[],unsent:[{text:'The second bubble.'}],decisions:['continue','rewrite_remainder','supersede']};

test('with no interrupted reply the classifier is asked exactly what it was always asked',async t=>{
  const seen=[],classify=async input=>{seen.push(input);return {route:'chat',reason:'synthetic',tail:{decision:'supersede',reason:'nobody asked'}};};
  const port=tailPort(),plain=fixture(t,{classify}),ported=fixture(t,{classify,replyTail:port});
  const before=await plain.router.select({id:'one',text:'hello'}),after=await ported.router.select({id:'one',text:'hello'});
  assert.deepEqual(Object.keys(seen[1]),['text','clock','recent','task','mode','workHeld','timeoutMs']);
  assert.deepEqual(Object.keys(seen[1]),Object.keys(seen[0]));
  assert.deepEqual(JSON.stringify({...seen[1],clock:null}),JSON.stringify({...seen[0],clock:null}));
  assert.deepEqual(port.calls,[['pending',{id:'one',text:'hello'}]],'the port hears about the message and is asked nothing else');
  assert.deepEqual([after.tail,before.tail,Object.keys(after)],[undefined,undefined,Object.keys(before)],'an unsolicited tail is never acted on');
});

test('an interrupted reply rides on the routing call, and what DeepSeek decided reaches the tail port with the input that caused it',async t=>{
  const seen=[],port=tailPort({pending:{key:'group-a:0123456789abcdef',reply:synthetic},decided:{state:'recorded',groupId:'group-a',intentId:'tail-1'}});
  const f=fixture(t,{replyTail:port,classify:async input=>{seen.push(input);return {route:'chat',reason:'synthetic',tail:{decision:'rewrite_remainder',reason:'Fold it in',receipt:{requestId:'request-1'}}};}});
  const record=await f.router.select({id:'two',text:'wait, one more thing'});
  assert.deepEqual(seen[0].interruptedReply,synthetic);assert.equal(Object.keys(seen[0]).at(-1),'interruptedReply');
  assert.deepEqual(port.calls.map(c=>c[0]),['pending','decided']);
  assert.deepEqual(port.calls[1][1],{inputId:'two',key:'group-a:0123456789abcdef',tail:{decision:'rewrite_remainder',reason:'Fold it in',receipt:{requestId:'request-1'}}});
  assert.deepEqual([record.route,record.tail],['chat',{carrier:'classify',decision:'rewrite_remainder',state:'recorded',groupId:'group-a',intentId:'tail-1'}]);
  assert.deepEqual(f.router.state.inputs.two.tail,record.tail,'the routing record says which decision rode on it');
  await f.router.select({id:'two',text:'wait, one more thing'});
  assert.equal(port.calls.length,2,'a duplicate input asks nobody again');
});

test('a classifier that times out, or answers without a tail, leaves the remainder to its next carrier',async t=>{
  const offer={key:'group-a:0123456789abcdef',reply:synthetic};
  const timeout=tailPort({pending:offer,missed:detail=>({state:'missed',reason:detail.reason})});
  const f=fixture(t,{replyTail:timeout,classify:async()=>{throw Error('classification-timeout');}});
  const record=await f.router.select({id:'three',text:'unclear'});
  assert.deepEqual([record.route,record.reason,record.tail],['work','classifier-unconfirmed',{carrier:'classify',state:'missed',reason:'classifier-unconfirmed'}]);
  assert.deepEqual(timeout.calls.at(-1),['missed',{inputId:'three',key:offer.key,reason:'classifier-unconfirmed'}]);
  const silent=tailPort({pending:offer});
  const g=fixture(t,{replyTail:silent,classify:async()=>({route:'chat',reason:'synthetic'})});
  assert.deepEqual((await g.router.select({id:'four',text:'haha'})).tail,{carrier:'classify',state:'missed'});
  assert.deepEqual(silent.calls.at(-1),['missed',{inputId:'four',key:offer.key,reason:'no-tail-decision'}]);
});

// ---- classify intents: stop, file sending, owner words and attachment metadata ----
const intentClassifier=answer=>{const seen=[];return {seen,classify:async input=>{seen.push(input);
  return {route:/code|test/.test(input.text)?'work':'chat',reason:'synthetic',recall:{mode:'light',query:'q',reason:'r'},...(typeof answer==='function'?answer(input):answer)};}};};

test('with intents off an attachment is still work, and the classifier is asked exactly what it always was',async t=>{
  const {seen,classify}=intentClassifier({stop:'current_task',file_send:{requested:true,channel:'wechat',file_ref:'r'}});
  const f=fixture(t,{classify});
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const record=await f.router.select({id:'file',text:'look at this',attachments:[{kind:'image',name:'photo.jpg'}]});
  assert.deepEqual([record.route,record.reason,record.fileSend,record.stop],['work','work-lock: work-input',undefined,undefined]);
  await f.router.select({id:'plain',text:'hello'});
  assert.deepEqual(Object.keys(seen[0]),['text','clock','recent','task','mode','workHeld','timeoutMs'],'no new key, and no intent asked for');
  assert.equal(f.router.currentTask().cancelRequested,undefined,'an intent nobody asked for cannot stop a task');
});

test('with intents on an attachment is classified, and only its metadata is ever sent',async t=>{
  const {seen,classify}=intentClassifier({route:'chat',reason:'a photo to look at'});
  const f=fixture(t,{classify,classifyIntents:true});
  const record=await f.router.select({id:'file',text:'look at this',attachments:[
    {kind:'image',mimeType:'image/jpeg',name:'photo.jpg',bytes:12345,path:'/private/inbox/photo.jpg',data:'BASE64',encrypt_query_param:'secret'},
    {name:'note.txt',bytes:-1,kind:'unheard-of'}]});
  assert.deepEqual([record.route,record.reason],['chat','a photo to look at'],'the model decides chat or work with the attachments in view');
  assert.deepEqual(seen[0].attachments,[{kind:'image',mimeType:'image/jpeg',name:'photo.jpg',bytes:12345},{kind:'other',name:'note.txt'}]);
  const request=JSON.stringify(seen[0]);
  for(const leaked of ['path','data','encrypt_query_param','/private/inbox','BASE64'])assert.ok(!request.includes(leaked),'no path and no content ever reaches the classifier: '+leaked);
  assert.equal(seen[0].intents,true);
  // Metadata only, whatever the caller passes: the request is built from four fields.
  assert.deepEqual(attachmentMetadata([{kind:'file',mimeType:'x/y'.padEnd(200,'z'),name:'a'.repeat(200),bytes:7,url:'https://example.test/x'}]),
    [{kind:'file',mimeType:('x/y'.padEnd(200,'z')).slice(0,100),name:'a'.repeat(120),bytes:7}]);
  assert.deepEqual(attachmentMetadata(null),[]);
  assert.equal(attachmentMetadata(Array.from({length:40},()=>({kind:'file'}))).length,24);
});

test('a natural-language stop is the literal command only for the owner with a task open, and only when the classifier answered',async t=>{
  const {seen,classify}=intentClassifier({stop:'current_task'});
  const f=fixture(t,{classify,classifyIntents:true});
  const idle=await f.router.select({id:'idle',text:'stop what you are doing'});
  assert.deepEqual([idle.route,idle.stop],['chat',undefined],'with no task open there is nothing to stop');
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  const stopped=await f.router.select({id:'stop-it',text:'ok, that is enough for now'});
  assert.deepEqual(stopped.stop,{requested:'current_task',decisionSource:CLASSIFIER_DECISION,taskIds:[task.id]});
  assert.equal(task.cancelRequested,true,'the same flag the literal command sets');
  assert.equal(f.router.state.inputs['stop-it'].stop.decisionSource,'deepseek-input-classification');
  await f.router.reconcile();
  assert.equal(f.router.state.tasks[task.id].status,'canceled');
  // Nobody but the owner is asked for intents, so nobody but the owner can stop a task.
  const g=fixture(t,{classify,classifyIntents:true});
  await g.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const other=await g.router.select({id:'other',kind:'notice',text:'stop the task'});
  assert.deepEqual([other.stop,seen.at(-1).intents],[undefined,undefined]);
  assert.equal(g.router.currentTask().cancelRequested,undefined);
});

test('a literal stop still needs no model, and an unavailable classifier cannot stop anything',async t=>{
  const f=fixture(t,{classifyIntents:true,classify:async()=>{throw Error('the classifier is never asked for a literal stop');}});
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  const stop=await f.router.select({id:'stop',text:'停止任务'});
  assert.deepEqual([stop.reason,task.cancelRequested,f.classificationCalls()],['owner-stop-command',true,0]);
  // The same words in ordinary language, with no classifier to read them: nothing happens.
  const g=fixture(t,{classifyIntents:true,classify:async()=>{throw Error('classification-timeout');}});
  await g.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const unread=await g.router.select({id:'unread',text:'ok, that is enough for now'});
  assert.deepEqual([unread.route,unread.reason,unread.stop],['work','work-lock: classifier-unconfirmed',undefined]);
  assert.equal(g.router.currentTask().cancelRequested,undefined);
});

test('a file the owner asked for is recorded as states and IDs; a malformed intent is dropped without failing the route',async t=>{
  const sha='b'.repeat(64);
  let answer={file_send:{requested:true,channel:'wechat',file_ref:'report.pdf',candidate_sha256:sha},recall:{mode:'deep',query:'q',reason:'r',owner_words:['captain','kin']}};
  const {classify}=intentClassifier(()=>answer);
  const f=fixture(t,{classify,classifyIntents:true});
  const asked=await f.router.select({id:'send-it',text:'send me the report on WeChat'});
  const {at,...recorded}=asked.fileSend;
  assert.deepEqual(recorded,{requested:true,channel:'wechat',fileRef:'report.pdf',candidateSha256:sha,decisionSource:CLASSIFIER_DECISION});
  assert.equal(typeof at,'number');
  assert.deepEqual(f.router.state.inputs['send-it'].fileSend,asked.fileSend);
  assert.deepEqual(asked.recall.owner_words,['captain','kin']);
  assert.equal(asked.recall.decisionSource,CLASSIFIER_DECISION);
  answer={file_send:{requested:true,channel:'carrier-pigeon',file_ref:'x'.repeat(400)},recall:{mode:'light',query:'q',reason:'r',owner_words:Array.from({length:40},()=>'w'.repeat(200))}};
  const dropped=await f.router.select({id:'nonsense',text:'anything'});
  assert.deepEqual([dropped.route,dropped.fileSend,dropped.recall.owner_words],['chat',undefined,undefined],'the route stands; the unusable intent is gone');
  assert.ok(!JSON.stringify(f.router.state.inputs.nonsense).includes('w'.repeat(30)));
});

test('the host can buy one message a second classification and a longer wait, and nothing more',async t=>{
  const waits=[];let fail=true;
  const f=fixture(t,{classifyIntents:true,classifyRetry:({text})=>/file/.test(text),
    classify:async input=>{waits.push(input.timeoutMs);if(fail){fail=false;throw Error('classification-timeout');}
      return {route:'chat',reason:'a file the owner asked for',recall:{mode:'light',query:'q',reason:'r'},file_send:{requested:true,channel:'wechat',file_ref:'report.pdf'}};}});
  const asked=await f.router.select({id:'ask',text:'send me that file'});
  assert.deepEqual([waits,asked.route,asked.fileSend.channel],[[15000,30000],'chat','wechat'],'twice, the second time with a longer wait');
  assert.equal(f.router.state.history.some(e=>e.kind==='classification-retried'&&e.id==='ask'),true);
  // A message the host says nothing about is asked exactly once, and fails closed as before.
  fail=true;
  const plain=await f.router.select({id:'plain',text:'hello there'});
  assert.deepEqual([plain.route,plain.reason,waits],['work','classifier-unconfirmed',[15000,30000,15000]]);
  assert.equal(plain.fileSend,undefined,'a retry the host asked for can never grant anything');
});

test('attachments and commands are never classified; a literal stop needs no model; other inputs never touch the tail port',async t=>{
  const port=tailPort({pending:{key:'group-a:0123456789abcdef',reply:synthetic},stopped:{state:'recorded'}}),f=fixture(t,{replyTail:port});
  await f.router.select({id:'file',text:'look at this',attachments:[{name:'photo'}]});
  await f.router.select({id:'mode',text:'/mode work'});
  await f.router.select({id:'compact',text:'/compact'});
  const stop=await f.router.select({id:'stop',text:'停止任务'});
  assert.deepEqual(port.calls,[['missed',{inputId:'file',reason:'not-classified'}],['missed',{inputId:'mode',reason:'not-classified'}],['missed',{inputId:'compact',reason:'not-classified'}],['stopped',{inputId:'stop'}]]);
  assert.deepEqual([f.classificationCalls(),stop.reason,stop.tail],[0,'owner-stop-command',{carrier:'owner-stop',state:'recorded'}]);
  for(const input of [{id:'thought',kind:'proactive',text:'An idle thought'},{id:'handoff:1',kind:'handoff',text:'continue'},{id:'result',kind:'work-result',text:'停止任务'}])await f.router.select(input);
  assert.equal(port.calls.length,4);
});

test('a tail port that fails never decides routing',async t=>{
  const broken=Error('tail unavailable'),port=tailPort({pending:broken,missed:broken,stopped:broken,decided:broken});
  const f=fixture(t,{replyTail:port});
  assert.deepEqual([(await f.router.select({id:'one',text:'hello'})).route,(await f.router.select({id:'stop',text:'/停'})).reason],['chat','owner-stop-command']);
  await f.router.dispatch({id:'two',text:'write code'},async()=> 'new-turn');
  assert.equal(f.router.currentTask().status,'running');
});

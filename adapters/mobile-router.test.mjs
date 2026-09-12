import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,modeCommand,recentConversation} from './mobile-router.mjs';
import {compactPrompt,publicMobileRuntime} from './mobile-controls.mjs';

function fixture(t, options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-routing-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=100,classificationCalls=0;
  const runtime={known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'gpt-6-astra',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0};
  const switched=[];
  const args={file:path.join(root,'state.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),
    classify:async({text})=>{classificationCalls++;const control={'现在是什么模型':'status','切过去给我说一声哦😯':'watch','退出正经模式':'auto','进入正经模式':'work'}[text];return control?{route:'control',control,reason:'synthetic semantic result'}:{route:text.includes('code')?'work':'chat',reason:'synthetic'};},
    switchModel:async model=>{switched.push(model);runtime.model=model;return{...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},now:()=>++now,...options};
  const router=new MobileRouter(args);
  return{router,runtime,switched,args,classificationCalls:()=>classificationCalls};
}

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

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,modeCommand,recentConversation} from './mobile-router.mjs';

function fixture(t, options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-routing-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  let now=100,classificationCalls=0;
  const runtime={known:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',model:'gpt-6-astra',active:false,backgroundTasks:0,queued:0,pendingDeliveries:0};
  const switched=[];
  const args={file:path.join(root,'state.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),
    classify:async({text})=>{classificationCalls++;return{route:text.includes('code')?'work':'chat',reason:'synthetic'};},
    switchModel:async model=>{switched.push(model);runtime.model=model;return{...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},now:()=>++now,...options};
  const router=new MobileRouter(args);
  return{router,runtime,switched,args,classificationCalls:()=>classificationCalls};
}

test('mode commands are explicit owner controls, not embedded instructions',()=>{
  assert.equal(modeCommand('进入正经模式～'),'work');assert.equal(modeCommand('退出正经模式'),'auto');
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
  assert.equal(f.classificationCalls(),1);assert.equal(dispatched,2);assert.deepEqual(f.switched,[]);
  assert.equal(f.router.snapshot().exitRequested,true);
});

test('model completion claim alone cannot release work or background tools',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'work',text:'write code'},async()=> 'new-turn');
  const task=f.router.currentTask();
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'tests passed',completedTaskId:task.id});
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
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'result ready',completedTaskId:f.router.currentTask().id});
  await f.router.observe('prompt-end',{stopReason:'end_turn'});
  await f.router.observe('delivery',{id:'result',state:'unconfirmed'});
  const restored=new MobileRouter(f.args);await restored.reconcile();assert.equal(restored.tasks().length,1);
  await restored.dispatch({id:'chat',text:'hello'},async r=>{assert.equal(r.model,'gpt-6-astra');return'new-turn';});
  assert.equal(f.switched.length,0);
  f.runtime.known=false;await restored.reconcile();assert.equal(restored.tasks().length,1);
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
  await f.router.requestMode({commandId:'finish',mode:'auto',reason:'done',completedTaskId:f.router.currentTask().id});
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

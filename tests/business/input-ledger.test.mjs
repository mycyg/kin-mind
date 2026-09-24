import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {MobileRouter} from '../../adapters/mobile-router.mjs';
import {inputSummary,inputSettled,holdsSession,SUMMARY_STATES} from '../../adapters/input-ledger.mjs';
import {safeBoundary} from '../../adapters/session-policy.mjs';

function fixture(t,options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-ledger-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const clock={now:1000};
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',
    active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const args={file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>++clock.now,
    classify:async({text})=>({route:/work/.test(text)?'work':'chat',reason:'synthetic'}),
    switchModel:async(model,profile={model})=>{Object.assign(runtime,{model,reasoningEffort:profile.reasoningEffort??runtime.reasoningEffort,serviceTierPreference:profile.serviceTierPreference??runtime.serviceTierPreference});return {...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},...options};
  return {root,clock,runtime,args,router:new MobileRouter(args)};
}

test('the eight summary states are a view over the facts, which are all kept',()=>{
  const cases=[
    [{state:'semantic-pending'},'classifying'],[{state:'selected'},'queued'],[{state:'preparing'},'queued'],[{state:'queued'},'queued'],
    [{state:'submitting'},'submitted'],[{state:'unconfirmed'},'submitted'],[{state:'fenced-unconfirmed'},'submitted'],
    [{state:'accepted'},'submitted'],[{state:'accepted',answer:{state:'accepted'}},'answered'],[{state:'accepted',answer:{state:'silent'}},'answered'],
    [{state:'accepted',route:'control'},'answered'],[{state:'failed-before-submit'},'received'],
    [{state:'failed-before-submit',ownerNotice:{state:'accepted'}},'failed-notified'],[{state:'superseded'},'superseded'],
    [{state:'accepted',canceledBy:'stop'},'canceled-by-owner'],[{state:'semantic-canceled',historical:{reason:'pre-ledger'}},'historical'],
    [{kind:'assessment',state:'accepted',turnStartedAt:1},'submitted'],[{kind:'assessment',state:'accepted',turnStartedAt:1,turnEndedAt:2},'answered'],
  ];
  for(const [record,summary] of cases)assert.equal(inputSummary(record),summary,JSON.stringify(record));
  assert.deepEqual(SUMMARY_STATES,['received','classifying','queued','submitted','answered','failed-notified','superseded','canceled-by-owner']);
  assert.equal(inputSettled({state:'accepted'}),false,'an owner input read but never answered is not settled');
  assert.equal(inputSettled({kind:'assessment',state:'accepted',turnStartedAt:1,turnEndedAt:2}),true);
  assert.equal(inputSettled({state:'selected',ownerNotice:{state:'accepted'}}),false,'anything still moving is unsettled, whatever the owner was told');
  assert.equal(inputSettled({state:'semantic-failed',historical:{reason:'pre-ledger'}}),true);
});

test('native turns and reply receipts settle owner inputs by their original ids',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'first',text:'hi'},async()=> 'new-turn');
  await f.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['first']});
  let [open]=f.router.unsettledInputs();
  assert.deepEqual([open.id,open.state,open.summary,open.inFlight],['first','accepted','submitted',true]);
  await f.router.observe('prompt-end',{taskId:null,inputVersion:null,turnFence:0,stopReason:'end_turn'});
  assert.equal(f.router.state.inputs.first.stopReason,'end_turn');
  [open]=f.router.unsettledInputs();assert.equal(open.inFlight,false,'read but not yet answered');
  await f.router.dispatch({id:'second',text:'still there?'},async()=> 'new-turn');
  await f.router.observe('reply-complete',{inputId:'second'});
  assert.deepEqual(f.router.unsettledInputs(),[],'the answer to the later message covers the earlier one');
  assert.equal(f.router.state.inputs.first.answer.state,'covered');
  const summary=f.router.summary({received:[{id:'inbox-only',at:1}]});
  assert.deepEqual([summary.answered,summary.received,summary.historical,summary.frozen],[2,1,0,null]);
  assert.deepEqual(f.router.unsettledInputs({received:[{id:'inbox-only',at:1,processing:true}]}).map(i=>[i.id,i.state,i.inFlight]),[['inbox-only','received',true]]);
});

test('inputs from before the ledger are history: never unsettled, never re-run (AD1-09, D1-12)',async t=>{
  const f=fixture(t);
  const state=JSON.parse(fs.readFileSync(f.args.file,'utf8'));delete state.ledgerVersion;
  const old=(id,extra)=>({id,kind:'owner',hash:createHash('sha256').update(JSON.stringify(['x',[]])).digest('hex'),at:1,...extra});
  Object.assign(state.inputs,{canceled:old('canceled',{state:'semantic-canceled',reason:'superseded-by-cancel'}),failed:old('failed',{state:'semantic-failed'}),
    unsent:old('unsent',{state:'failed-before-submit'}),done:old('done',{state:'accepted'}),waiting:old('waiting',{state:'semantic-pending'})});
  fs.writeFileSync(f.args.file,JSON.stringify(state));fs.rmSync(f.args.file+'.prev',{force:true});
  const router=new MobileRouter(f.args);
  assert.deepEqual(['canceled','failed','unsent'].map(id=>router.state.inputs[id].historical.reason),['pre-ledger:semantic-canceled','pre-ledger:semantic-failed','pre-ledger:failed-before-submit']);
  assert.equal(router.state.inputs.done.answer.state,'legacy');assert.equal(router.state.inputs.waiting.historical,undefined);
  assert.deepEqual(router.unsettledInputs().map(i=>i.id),['waiting'],'only what is really still in play');
  assert.equal(router.summary().historical,3);
  const refused=await router.dispatch({id:'unsent',text:'x'},async()=>assert.fail('history is never re-run')).catch(error=>error);
  assert.equal(refused.code,'input-historical');assert.equal(router.state.inputs.unsent.reason,undefined);
  assert.equal(new MobileRouter(f.args).state.inputs.canceled.historical.reason,'pre-ledger:semantic-canceled','migration runs once');
});

test('a freeze holds new dispatch, survives a restart and lifts itself, but never the owner\'s stop',async t=>{
  const f=fixture(t);
  const freeze=await f.router.freezeDispatch('release',{ttlMs:10*60000,by:'kin-deploy'});
  assert.equal(freeze.reason,'release');assert.equal(f.router.summary().frozen.reason,'release');
  const error=await f.router.dispatch({id:'chat',text:'hi'},async()=>assert.fail('frozen')).catch(e=>e);
  assert.equal(error.code,'dispatch-frozen');assert.equal(f.router.state.inputs.chat,undefined,'the input is not taken: its inbox job waits');
  for(const kind of ['proactive','assessment','handoff'])
    assert.equal((await f.router.dispatch({id:kind,kind,text:'internal'},async()=>assert.fail('frozen')).catch(e=>e)).code,'dispatch-frozen');
  let stops=0;
  await f.router.dispatch({id:'stop',text:'停止任务'},async()=>{stops++;return 'new-turn';});
  assert.equal(stops,1,'the owner\'s literal stop still acts');
  const restarted=new MobileRouter(f.args);assert.equal(restarted.frozen(),true);
  f.clock.now+=10*60000;assert.equal(restarted.frozen(),false,'a freeze nobody lifted lifts itself');
  const swap=await restarted.freezeDispatch('swap');assert.deepEqual(await restarted.thawDispatch('done'),{state:'thawed',frozenAt:swap.at});
  assert.equal(restarted.frozen(),false);assert.deepEqual(await restarted.thawDispatch(),{state:'not-frozen'});
  await assert.rejects(()=>restarted.freezeDispatch(''),/reason/);
});

test('a freeze holds mode changes nobody forced',async t=>{
  const f=fixture(t);
  await f.router.freezeDispatch('release');
  await f.router.requestMode({mode:'work',commandId:'kin-work',reason:'Kin asked for work mode'});
  assert.equal((await f.router.applyPendingMode()).state,'pending');assert.equal(f.runtime.model,'deepseek-flash');
  await f.router.thawDispatch();
  assert.equal((await f.router.applyPendingMode()).state,'applied');assert.equal(f.runtime.model,'gpt-6-sol');
});

test('a session boundary waits for inputs in play, never for history (AD1-09)',()=>{
  const runtime={known:true,active:false,nativeStatus:'idle',backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const check=input=>safeBoundary({runtime,inputs:[input]}).safe;
  assert.equal(check({state:'semantic-pending'}),false,'an input awaiting reclassification is not swallowed by a rotation');
  assert.equal(check({state:'queued'}),false);
  assert.equal(check({state:'unconfirmed'}),false);
  assert.equal(check({state:'unconfirmed',ownerNotice:{state:'accepted'}}),true);
  assert.equal(check({state:'accepted',turnStartedAt:1}),false);
  assert.equal(check({state:'accepted',turnStartedAt:1,turnEndedAt:2}),true);
  assert.equal(check({state:'semantic-canceled',historical:{reason:'pre-ledger'}}),true);
  assert.equal(check({state:'failed-before-submit'}),true);
  assert.equal(holdsSession({state:'fenced-unconfirmed',reconciliation:{state:'found'}}),false);
});

test('an input proven never submitted is retried by its own id, then reported; only the notice receipt settles it (AD1-02, H2a-03, H2b-09)',async t=>{
  const f=fixture(t);
  const fail=async()=>{throw Error('context unavailable');};
  const error=await f.router.dispatch({id:'owner-1',text:'hi',submissionProtocol:'host-boundary-v1'},fail).catch(e=>e);
  assert.deepEqual([error.code,error.inputId,Boolean(error.retryAt)],['input-not-submitted','owner-1',true]);
  const requeued=[],requeue=async id=>{requeued.push(id);return {state:'requeued'};};
  assert.deepEqual(await f.router.watch({requeue}),[],'nothing before its retry time');
  f.clock.now+=30000;
  assert.deepEqual((await f.router.watch({requeue})).map(a=>[a.kind,a.id,a.result]),[['requeue','owner-1','requeued']]);
  let submitted=0;
  await f.router.dispatch({id:'owner-1',text:'hi'},async()=>{submitted++;return 'new-turn';});
  assert.deepEqual([submitted,f.router.state.inputs['owner-1'].state,f.router.state.inputs['owner-1'].retry.attempts],[1,'accepted',1]);
  // Four failures exhaust the retries; the owner is told once, by the host, as the host.
  for(let attempt=0;attempt<4;attempt++)await f.router.dispatch({id:'owner-2',text:'again',submissionProtocol:'host-boundary-v1'},fail).catch(()=>{});
  assert.equal(f.router.state.inputs['owner-2'].retry.exhausted,true);
  const notices=[];let answer={state:'unconfirmed'};
  const notifyOwner=async(kind,id)=>{notices.push([kind,id]);return answer;};
  await f.router.watch({requeue,notifyOwner});
  assert.deepEqual(notices,[['stopped','owner-2']]);
  assert.equal(inputSummary(f.router.state.inputs['owner-2']),'received','an unconfirmed notice does not settle the input');
  f.clock.now+=120000;answer={state:'accepted',messageId:'notice-1'};
  await f.router.watch({requeue,notifyOwner});
  assert.deepEqual(notices.at(-1),['stopped','owner-2'],'the same notice, by the same input id');
  assert.equal(inputSummary(f.router.state.inputs['owner-2']),'failed-notified');
  assert.equal(f.router.unsettledInputs().some(i=>i.id==='owner-2'),false);
});

test('an uncertain submission is reconciled by its original id and never re-sent under another (AD1-10)',async t=>{
  const f=fixture(t);
  for(const id of ['found','absent','unknown'])
    await assert.rejects(f.router.dispatch({id,text:id,submissionProtocol:'host-boundary-v1'},async(_,started)=>{started();throw Error('lost response');}),/reconciliation/);
  assert.deepEqual((await f.router.restoreRoutingProfile()).state,'restored','an unconfirmed input no longer holds the restart profile');
  const reconcileInput=async id=>({state:{found:'found',absent:'not-found'}[id]??'unknown'});
  await f.router.watch({reconcileInput});
  const inputs=f.router.state.inputs;
  assert.deepEqual([inputs.found.state,inputs.absent.state,inputs.absent.retry.evidence,inputs.unknown.reconciliation.state],['accepted','failed-before-submit','reconciled-not-received','unknown']);
  const notices=[];
  await f.router.watch({reconcileInput,requeue:async()=>({state:'requeued'}),notifyOwner:async(kind,id)=>{notices.push([kind,id]);return {state:'accepted',messageId:'n'};}});
  assert.equal(inputs.absent.retry.requeuedAt>0,true,'what was proven never received goes back to its inbox under its own id');
  assert.deepEqual(notices,[['unknown','unknown']],'an unknown result is reported, and only reconciled by its own id');
  await assert.rejects(f.router.dispatch({id:'unknown',text:'unknown'},async()=>assert.fail('never re-sent')),/reconciliation/);
});

test('a queued input is accepted when its prompt begins; a dropped or restarted queue proves it unsent (H2a-02)',async t=>{
  const f=fixture(t);
  assert.equal((await f.router.dispatch({id:'q1',text:'hi'},async()=>({route:'new-turn',queued:true}))).queued,true);
  assert.equal(f.router.state.inputs.q1.state,'queued');assert.equal(inputSummary(f.router.state.inputs.q1),'queued');
  const waiting=f.router.awaitAcceptance('q1');
  await f.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['q1']});
  assert.deepEqual(await waiting,{state:'accepted'});
  await f.router.dispatch({id:'q2',text:'hey'},async()=>({route:'new-turn',queued:true}));
  const dropped=f.router.awaitAcceptance('q2');
  await f.router.observe('input-dropped',{inputId:'q2'});
  assert.deepEqual(await dropped,{state:'failed-before-submit'});
  assert.deepEqual([f.router.state.inputs.q2.retry.evidence,f.router.state.inputs.q2.submissionStartedAt],['not-submitted',undefined]);
  await f.router.dispatch({id:'q3',text:'yo'},async()=>({route:'new-turn',queued:true}));
  const released=f.router.awaitAcceptance('q3');f.router.releaseAcceptance();
  assert.deepEqual(await released,{state:'released',reason:'host-stopping'});
  const restarted=new MobileRouter(f.args);
  assert.deepEqual([restarted.state.inputs.q3.state,restarted.state.inputs.q3.reason,Boolean(restarted.state.inputs.q3.queuedSubmissionAt)],['failed-before-submit','queued-prompt-never-started',true]);
  assert.equal(restarted.state.inputs.q1.state,'accepted');
});

test('a native command that fails or ends abnormally never leaves the coordinator busy (AD1-03)',async t=>{
  const f=fixture(t),idle={...f.runtime};
  await f.router.dispatch({id:'c1',text:'/compact',submissionProtocol:'host-boundary-v1'},async()=>{throw Error('compact prompt failed');}).catch(()=>{});
  assert.equal(f.router.state.operations.c1,undefined,'no command exists before its submission');
  await f.router.dispatch({id:'c2',text:'/compact'},async()=> 'new-turn');
  assert.equal(f.router.busy(idle),true);
  await f.router.observeOperation('c2','start');await f.router.observeOperation('c2','end',{stopReason:'error'});
  assert.deepEqual([f.router.state.operations.c2.state,f.router.state.operations.c2.stopReason,f.router.busy(idle)],['failed','error',false]);
  await f.router.dispatch({id:'c3',text:'/compact'},async()=> 'new-turn');await f.router.observeOperation('c3','start');
  const restarted=new MobileRouter(f.args);
  assert.deepEqual([restarted.state.operations.c3.state,restarted.busy(idle)],['interrupted',false]);
});

test('a failed switch restored to the old profile leaves a chat a chat (AD1-04)',async t=>{
  const f=fixture(t);
  Object.assign(f.runtime,{model:'gpt-6-sol',modelProvider:'openai-15m',providerOverride:false,reasoningEffort:'medium',serviceTierPreference:'fast',fastMode:'on'});
  let calls=0;
  f.router.switchModel=async(model,profile)=>{if(++calls===1)throw Error('switch failed');Object.assign(f.runtime,{model,reasoningEffort:profile.reasoningEffort,serviceTierPreference:profile.serviceTierPreference});return {...f.runtime};};
  await f.router.dispatch({id:'chat',text:'hi'},async()=> 'new-turn');
  assert.equal(f.router.state.transition.state,'failed-restored');
  assert.deepEqual([f.router.state.inputs.chat.taskId,f.router.tasks().length],[undefined,0]);
});

test('a slow model catalog cannot leave the classification clock without a listener (AD1-05)',async t=>{
  const f=fixture(t,{modelCatalog:async()=>{await new Promise(resolve=>setTimeout(resolve,60));return [];},resolveProfile:async profile=>structuredClone(profile)});
  f.router.state.config.classifierTimeoutMs=20;
  const unhandled=[],listener=error=>unhandled.push(error);process.on('unhandledRejection',listener);t.after(()=>process.off('unhandledRejection',listener));
  await f.router.dispatch({id:'slow',text:'hi'},async()=> 'new-turn');
  await new Promise(resolve=>setTimeout(resolve,50));
  assert.deepEqual(unhandled,[]);assert.equal(f.router.state.inputs.slow.state,'accepted');
});

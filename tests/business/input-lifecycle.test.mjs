import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,NOTICE_SEND_BUDGET,NOTICE_LOOKUP_BUDGET,NOTICE_ROUND_REST_MS,REQUEUE_BUDGET,DEFERRAL_PLAN_RETRY_MS} from '../../adapters/mobile-router.mjs';
import {inputSummary} from '../../adapters/input-ledger.mjs';
import {WorkLockReview} from '../../adapters/work-lock-review.mjs';

const MINUTE=60000,HOUR=3600000;
function fixture(t,options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-lifecycle-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const clock={now:Date.parse('2026-09-25T00:00:00Z')};
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',
    active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0};
  const args={file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>clock.now,
    classify:async({text})=>({route:/work|写/.test(text)?'work':'chat',reason:'synthetic'}),
    switchModel:async(model,profile={model})=>{Object.assign(runtime,{model,reasoningEffort:profile.reasoningEffort??runtime.reasoningEffort,serviceTierPreference:profile.serviceTierPreference??runtime.serviceTierPreference});return {...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},...options};
  return {root,clock,runtime,args,router:new MobileRouter(args)};
}
const chat=(f,id,text='hi')=>f.router.dispatch({id,kind:'owner',text},async()=> 'new-turn');

test('the owner\'s literal stop cuts the running turn, keeps the result on the stop, and settles at once (CR-LIFE-01)',async t=>{
  const calls=[];
  const f=fixture(t,{interruptTurn:async request=>{calls.push(request);Object.assign(f.runtime,{active:false,nativeStatus:'idle'});return {state:'interrupted',cancel:{cancelledTurn:true}};}});
  await f.router.dispatch({id:'ask',kind:'owner',text:'写一份报告'},async()=> 'new-turn');
  const task=f.router.currentTask();
  await f.router.requestMode({commandId:'take-on',mode:'work',reason:'on it',taskOutcome:'accepted',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  await f.router.observe('prompt-start',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,inputIds:['ask']});
  Object.assign(f.runtime,{active:true,nativeStatus:'running'});
  let submitted;
  await f.router.dispatch({id:'stop',kind:'owner',text:'停止任务'},async decision=>{submitted=decision;return 'new-turn';});
  assert.deepEqual(calls.map(call=>call.sourceInputId),['stop'],'the native interrupt is asked for, by the stop input');
  const stop=f.router.state.inputs.stop;
  assert.deepEqual([stop.interrupt.state,stop.interrupt.result,stop.interrupt.cancelledTurn],['confirmed','interrupted',true]);
  assert.equal(f.router.state.tasks[task.id].status,'canceled','confirmed: the work is canceled at once, not when the coordinator is idle');
  assert.equal(inputSummary(f.router.state.inputs.ask),'canceled-by-owner');
  assert.equal(submitted.newTurn,true,'the stop reaches Kin as a turn of its own, never steered into the cut one');
});

test('an unconfirmed interruption keeps the stop\'s result open and is settled once the session is seen idle (CR-LIFE-01)',async t=>{
  const f=fixture(t,{interruptTurn:async()=>({state:'unconfirmed',reason:'native-cancel-unconfirmed'})});
  await f.router.dispatch({id:'ask',kind:'owner',text:'写一份报告'},async()=> 'new-turn');
  const task=f.router.currentTask();
  await f.router.requestMode({commandId:'take-on',mode:'work',reason:'on it',taskOutcome:'accepted',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  Object.assign(f.runtime,{active:true,nativeStatus:'running'});
  await f.router.dispatch({id:'stop',kind:'owner',text:'停止任务'},async()=> 'new-turn');
  assert.equal(f.router.state.inputs.stop.interrupt.state,'unconfirmed');
  assert.equal(f.router.state.tasks[task.id].status,'running','nothing is settled on an unknown result');
  await f.router.reconcile();
  assert.equal(f.router.state.tasks[task.id].status,'running','still running: still looked at');
  Object.assign(f.runtime,{active:false,nativeStatus:'idle'});
  await f.router.reconcile();
  assert.equal(f.router.state.inputs.stop.interrupt.state,'reconciled');assert.equal(f.router.state.tasks[task.id].status,'canceled');
  // With nothing running there is nothing to cut.
  const g=fixture(t,{interruptTurn:async()=>assert.fail('nothing runs')});
  await g.router.dispatch({id:'stop-idle',kind:'owner',text:'停止任务'},async()=> 'new-turn');
  assert.equal(g.router.state.inputs['stop-idle'].interrupt.state,'not-needed');
});

test('a submission reconciled as not received is retried under its own id, and requeues are counted (CR-LIFE-03)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'lost',kind:'owner',text:'hi'},async()=>{throw Error('connection reset');}).catch(()=>{});
  const record=f.router.state.inputs.lost;
  assert.equal(record.state,'unconfirmed');const submittedAt=record.submissionStartedAt;assert.ok(submittedAt);
  await f.router.watch({reconcileInput:async id=>({state:id==='lost'?'not-found':'unknown'})});
  assert.deepEqual([record.state,record.retry.evidence,record.reconciliation.state],['failed-before-submit','reconciled-not-received','not-found']);
  assert.equal(record.submissionStartedAt,undefined,'the old submission no longer blocks a retry');
  assert.equal(record.reconciliation.submissionStartedAt,submittedAt,'it is kept as history');
  let retried=false;
  await f.router.dispatch({id:'lost',kind:'owner',text:'hi'},async()=>{retried=true;return 'new-turn';});
  assert.equal(retried,true);assert.equal(record.state,'accepted');
  assert.equal(record.reconciliation,undefined);assert.equal(record.priorReconciliations.at(-1).state,'not-found');
  // A requeue whose replay never reaches the router is still counted: never an endless loop.
  const g=fixture(t);
  await g.router.dispatch({id:'refused',kind:'owner',text:'hi'},async()=>{throw Error('refused');},).catch(()=>{});
  g.router.state.inputs.refused.state='failed-before-submit';delete g.router.state.inputs.refused.submissionStartedAt;
  g.router.state.inputs.refused.retry={attempts:1,evidence:'not-submitted',nextAt:0};
  let requeues=0;
  for(let i=0;i<REQUEUE_BUDGET+2;i++){await g.router.watch({requeue:async()=>{requeues++;return {state:'requeued'};},notifyOwner:async()=>({state:'accepted',messageId:'n'})});g.clock.now+=HOUR;}
  assert.equal(requeues,REQUEUE_BUDGET);
  assert.equal(g.router.state.inputs.refused.retry.exhausted,true);
  assert.deepEqual([g.router.state.inputs.refused.ownerNotice.kind,inputSummary(g.router.state.inputs.refused)],['stopped','failed-notified']);
});

test('a busy or hung session never holds back one notice, and a wait for acceptance has a limit (CR-LIFE-04)',async t=>{
  const f=fixture(t);
  // A queued input behind a turn that never ends.
  await f.router.dispatch({id:'queued',kind:'owner',text:'hi'},async()=>({route:'new-turn',queued:true}));
  assert.equal(f.router.state.inputs.queued.state,'queued');
  assert.deepEqual(await f.router.awaitAcceptance('queued',{timeoutMs:5}),{state:'waiting',reason:'acceptance-deadline'});
  f.clock.now+=11*MINUTE;
  const told=[];
  await f.router.watch({sessionBusy:true,notifyOwner:async(kind,id,options)=>{told.push([kind,id,options.mayStart]);return {state:'accepted',messageId:'n1'};}});
  assert.deepEqual(told,[['unknown','queued',true]]);
  assert.equal(f.router.state.inputs.queued.state,'queued','busy: not resubmitted, still waiting for its prompt');
  // A running turn with no progress for the stuck time is reported, busy or not.
  const g=fixture(t);
  await chat(g,'hung');await g.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['hung']});
  g.clock.now+=5*MINUTE;g.router.noteProgress();
  g.clock.now+=6*MINUTE;
  const seen=[];const notify=async(kind,id)=>{seen.push([kind,id]);return {state:'accepted',messageId:'n2'};};
  await g.router.watch({sessionBusy:true,notifyOwner:notify});
  assert.deepEqual(seen,[],'streamed progress five minutes ago keeps it alive');
  g.clock.now+=5*MINUTE;
  await g.router.watch({sessionBusy:true,notifyOwner:notify});
  assert.deepEqual(seen,[['unknown','hung']]);
  assert.equal(inputSummary(g.router.state.inputs.hung),'failed-notified');
  await g.router.observe('reply-complete',{inputId:'hung'});
  assert.equal(inputSummary(g.router.state.inputs.hung),'answered','an answer after all outranks the notice');
});

test('a notice round that used its attempts rests and is taken up again; a begun one is only looked up (CR-LIFE-05)',async t=>{
  const f=fixture(t);
  await chat(f,'unanswered');f.clock.now+=11*MINUTE;
  const calls=[];let answer={state:'not-submitted',submissionStarted:false};
  const notifyOwner=async(kind,id,options)=>{if(id==='unanswered')calls.push(options.mayStart);return id==='unanswered'?answer:{state:'accepted',messageId:'other'};};
  for(let i=0;i<NOTICE_SEND_BUDGET;i++){await f.router.watch({notifyOwner});f.clock.now+=MINUTE;}
  const notice=f.router.state.inputs.unanswered.ownerNotice;
  assert.deepEqual([notice.state,notice.round,notice.attempts],['not-submitted',1,0],'the round ends, the notice does not');
  assert.ok(notice.nextAt-f.clock.now>=NOTICE_ROUND_REST_MS[0]-NOTICE_SEND_BUDGET*MINUTE);
  await f.router.watch({notifyOwner});assert.equal(calls.length,NOTICE_SEND_BUDGET,'resting');
  // Something new from the owner wakes it: her channel may be reachable again.
  await chat(f,'she-is-back');
  answer={state:'sending'};
  await f.router.watch({notifyOwner});
  assert.equal(calls.at(-1),true,'proven unsent before: it may be sent');
  assert.equal(notice.lastReceipt,'unknown');
  for(let i=0;i<NOTICE_LOOKUP_BUDGET+1;i++){f.clock.now+=3*MINUTE;await f.router.watch({notifyOwner});}
  assert.equal(calls.slice(NOTICE_SEND_BUDGET+1).every(mayStart=>mayStart===false),true,'begun: only ever looked up');
  assert.notEqual(notice.state,'exhausted');
  answer={state:'accepted',messageId:'late-receipt'};
  f.clock.now+=NOTICE_ROUND_REST_MS.at(-1);await f.router.watch({notifyOwner});
  assert.equal(notice.state,'accepted','a receipt that arrives late still settles it');
  // A notice an older host gave up on is taken up again after a restart.
  const g=fixture(t);
  await chat(g,'old');g.router.state.inputs.old.ownerNotice={kind:'unknown',id:'kin-input-notice-x',state:'exhausted',attempts:10,lastReceipt:'unknown'};g.router.save('seed');
  const restarted=new MobileRouter(g.args);
  assert.deepEqual([restarted.state.inputs.old.ownerNotice.state,restarted.state.inputs.old.ownerNotice.round],['unknown',1]);
});

test('the activity gate: nothing starts while frozen, everything started counts until released (CR-LIFE-08, CR-MIND-01)',async t=>{
  const f=fixture(t);
  const reply=f.router.beginActivity({kind:'reply',id:'bubble-1',channel:'feishu'});
  assert.equal(reply.ok,true);
  assert.equal(f.router.busy(f.runtime),true);
  const freeze=await f.router.freezeDispatch('release');
  assert.ok(freeze);
  assert.deepEqual(f.router.beginActivity({kind:'creation',id:'c1'}),{ok:false,reason:'frozen'});
  assert.deepEqual(f.router.activityList().map(a=>[a.kind,a.id,a.channel]),[['reply','bubble-1','feishu']],'what started before the freeze is still counted');
  assert.equal(reply.release(),true);assert.equal(reply.release(),false);
  assert.equal(f.router.busy(f.runtime),false);
  await f.router.thawDispatch('done');
  const creation=f.router.beginActivity({kind:'creation',id:'c2'}),assessment=f.router.beginActivity({kind:'assessment',id:'a1'});
  assert.equal(f.router.busy(f.runtime),true);
  assert.equal(f.router.busy(f.runtime,{owner:true}),true,'an assessment still holds the owner\'s switch until it is interrupted');
  assessment.release();
  assert.equal(f.router.busy(f.runtime,{owner:true}),false,'a creation in its own process never holds the owner\'s dispatch');
  assert.equal(f.router.busy(f.runtime,{internal:true}),false,'the mind orders its own runs');
  creation.release();
  // An owner notice being sent counts too.
  await chat(f,'x');f.router.state.inputs.x.ownerNotice={kind:'unknown',id:'n',state:'sending'};
  assert.equal(f.router.busy(f.runtime),true);
  delete f.router.state.inputs.x.ownerNotice;
  // The watchdog's own notices pass the gate: frozen, a new one does not start; a lookup still runs.
  f.clock.now+=11*MINUTE;await f.router.freezeDispatch('release');
  const calls=[];
  await f.router.watch({notifyOwner:async(kind,id,options)=>{calls.push(options.mayStart);return {state:'accepted',messageId:'n'};}});
  assert.deepEqual(calls,[],'no notice starts during a freeze');
  f.router.state.inputs.x.ownerNotice={kind:'unknown',id:'kin-input-notice-y',state:'unknown',attempts:1,lastReceipt:'unknown'};
  await f.router.watch({notifyOwner:async(kind,id,options)=>{calls.push(options.mayStart);return {state:'accepted',messageId:'n'};}});
  assert.deepEqual(calls,[false],'a begun notice is looked up, frozen or not');
});

test('the notice gap is checked when a send starts and runs from when it started (CR-LIFE-16)',async t=>{
  const f=fixture(t);
  await chat(f,'a');await chat(f,'b');f.clock.now+=11*MINUTE;
  const started=[];let answer={state:'not-submitted',submissionStarted:false};
  const notifyOwner=async(kind,id)=>{started.push([id,f.clock.now]);return answer;};
  await f.router.watch({notifyOwner});f.clock.now+=MINUTE;
  await f.router.watch({notifyOwner});f.clock.now+=MINUTE;
  answer={state:'accepted',messageId:'na'};const sentAt=f.clock.now;
  await f.router.watch({notifyOwner});
  assert.equal(f.router.state.lastOwnerNoticeAt,sentAt,'the gap runs from the send that started, not from when the notice was first owed');
  for(let i=0;i<9;i++){f.clock.now+=MINUTE;await f.router.watch({notifyOwner});}
  assert.deepEqual(started.filter(([id])=>id==='b'),[],'nothing else starts within the gap');
  f.clock.now+=MINUTE;await f.router.watch({notifyOwner});
  assert.deepEqual(started.filter(([id])=>id==='b').map(([,at])=>at-sentAt>=10*MINUTE),[true]);
});

test('a control is answered by its own reply, maintenance by its command; neither just by its route (CR-LIFE-07)',async t=>{
  const f=fixture(t,{classify:async()=>({route:'control',control:'status',reason:'status'})});
  await f.router.dispatch({id:'status',kind:'owner',text:'/mode status'},async()=>assert.fail('a control is not a prompt'));
  const control=f.router.state.inputs.status;
  assert.equal(inputSummary(control),'submitted','accepted, not yet answered');
  await f.router.flushNotices({send:async()=>({state:'rejected'}),lookup:async()=>({state:'rejected'})});
  assert.equal(control.answer,undefined,'a refused reply answers nothing');
  f.clock.now+=11*MINUTE;
  const told=[];await f.router.watch({notifyOwner:async(kind,id)=>{told.push([kind,id]);return {state:'accepted',messageId:'n'};}});
  assert.deepEqual(told,[['stopped','status']],'the owner is told her control got no reply');
  const g=fixture(t,{classify:async()=>({route:'control',control:'status',reason:'status'})});
  await g.router.dispatch({id:'status',kind:'owner',text:'/mode status'},async()=>assert.fail('a control is not a prompt'));
  await g.router.flushNotices({send:async()=>({state:'accepted',messageId:'m-status'}),lookup:async()=>null});
  assert.deepEqual([g.router.state.inputs.status.answer.basis,g.router.state.inputs.status.answer.messageId],['control-reply','m-status']);
  // Maintenance: a completed command answers it; a failed one is reported at once.
  const h=fixture(t);
  await h.router.dispatch({id:'compact',kind:'owner',text:'/compact'},async()=> 'new-turn');
  await h.router.observeOperation('compact','start');await h.router.observeOperation('compact','end',{stopReason:'error'});
  assert.equal(inputSummary(h.router.state.inputs.compact),'submitted');
  const said=[];await h.router.watch({notifyOwner:async(kind,id)=>{said.push([kind,id]);return {state:'accepted',messageId:'n'};}});
  assert.deepEqual(said,[['stopped','compact']]);
});

test('a proposal only hints at routing: no lock, no delivery duty, and it lapses without a turn (CR-LIFE-10)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'maybe-work',kind:'owner',text:'写个东西'},async()=>{throw Error('catalog unavailable');}).catch(()=>{});
  const task=f.router.tasks()[0];
  assert.deepEqual([task.status,task.requiresDelivery,f.router.openWork().length],['proposed',false,0]);
  assert.equal(f.router.internalHeld(f.runtime,'proactive'),false,'the mind is not held by a proposal');
  const record=f.router.state.inputs['maybe-work'];
  record.state='failed-before-submit';delete record.submissionStartedAt;record.retry={attempts:4,exhausted:true};
  await f.router.watch({notifyOwner:async()=>({state:'accepted',messageId:'n'})});
  assert.equal(f.router.state.tasks[task.id].status,'unclaimed','it lapses though no turn ever ran');
  // Taken on, it holds the lock and owes the report.
  const g=fixture(t);
  await g.router.dispatch({id:'w',kind:'owner',text:'写个东西'},async()=> 'new-turn');
  const proposal=g.router.tasks()[0];
  await g.router.requestMode({commandId:'accept',mode:'work',reason:'yes',taskOutcome:'accepted',completedTaskId:proposal.id,completedInputVersion:proposal.inputVersion});
  assert.deepEqual([proposal.status,proposal.requiresDelivery,g.router.openWork().length],['running',true,1]);
});

test('waiting is never a delivery receipt: an unproven report is looked up by id and handed back to Kin (CR-LIFE-11, CR-MIND-02)',async t=>{
  const f=fixture(t);
  await f.router.dispatch({id:'job',kind:'owner',text:'写一份报告'},async()=> 'new-turn');
  const task=f.router.tasks()[0];
  await f.router.requestMode({commandId:'accept',mode:'work',reason:'yes',taskOutcome:'accepted',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  await f.router.observe('prompt-start',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,inputIds:['job']});
  await f.router.requestMode({commandId:'done',mode:'auto',reason:'finished',completedTaskId:task.id,completedInputVersion:task.inputVersion,sourceInputId:'job'});
  await f.router.observe('delivery',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,id:'report',outboxId:'kin-frag-report',state:'unconfirmed',sourceInputId:'job'});
  await f.router.observe('prompt-end',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,stopReason:'end_turn'});
  f.clock.now+=31*MINUTE;
  await f.router.reconcile();
  assert.equal(f.router.state.tasks[task.id].status,'running','thirty minutes are not a receipt');
  assert.equal(f.router.declarationStalled(task,f.runtime),true);
  const review=new WorkLockReview({router:f.router,file:path.join(f.root,'review.json'),collect:async()=>({}),summarize:async()=>({}),now:()=>f.clock.now});
  assert.equal(review.waiting(task,f.runtime),'task-recently-active','handed back to Kin once idle, not held as her decision');
  f.clock.now+=HOUR;assert.equal(review.waiting(task,f.runtime),null);
  // Looked up by its original id: the platform had it after all.
  const looked=[];
  assert.deepEqual(await f.router.reconcileDeliveries(async id=>{looked.push(id);return {state:'accepted',messageId:'om-report'};}),{looked:1,accepted:1});
  assert.deepEqual(looked,['kin-frag-report']);
  await f.router.reconcile();
  assert.equal(f.router.state.tasks[task.id].status,'completed');
  // Her other exits stay open: a partial declaration replaces the first.
  const g=fixture(t);
  await g.router.dispatch({id:'job2',kind:'owner',text:'写一份报告'},async()=> 'new-turn');
  const other=g.router.tasks()[0];
  await g.router.requestMode({commandId:'done2',mode:'auto',reason:'finished',completedTaskId:other.id,completedInputVersion:other.inputVersion});
  await g.router.requestMode({commandId:'partial2',mode:'auto',reason:'only half',taskOutcome:'partial',completedTaskId:other.id,completedInputVersion:other.inputVersion});
  assert.equal(other.completion.outcome,'partial');
});

test('a deferral\'s plan is retried when the port cannot answer, and handed back to Kin only when it cannot be made (CR-MIND-03)',async t=>{
  const f=fixture(t);
  f.router.state.tasks.later={id:'later',status:'deferred',inputIds:[],summary:'later',deliveries:{},tools:{},deferral:{notBefore:'2026-09-26T00:00:00Z',plan:{state:'pending'}}};
  assert.deepEqual(f.router.deferredPlans().map(task=>task.id),['later']);
  await f.router.recordDeferralPlan('later',{state:'retry',reason:'plan-port-unavailable'});
  assert.deepEqual([f.router.state.tasks.later.deferral.plan.state,f.router.deferredPlans().length],['pending',0],'kept, and asked again later');
  f.clock.now+=DEFERRAL_PLAN_RETRY_MS[0];
  assert.equal(f.router.deferredPlans().length,1);
  for(let i=1;i<DEFERRAL_PLAN_RETRY_MS.length+1;i++)await f.router.recordDeferralPlan('later',{state:'retry',reason:'plan-port-unavailable'});
  assert.equal(f.router.state.tasks.later.deferral.plan.state,'needs-kin');
  assert.deepEqual(f.router.deferralsToTell().map(task=>task.id),['later']);
  assert.ok(f.router.workFacts().some(task=>task.id==='later'&&task.deferral.plan.state==='needs-kin'),'a fact for Kin');
  await f.router.markDeferralTold('later',{state:'accepted'});
  assert.deepEqual(f.router.deferralsToTell(),[]);
});

test('an input keeps when the host first received it and where from; a hand-off\'s age runs from then (CR-LIFE-18, CR-LIFE-17)',async t=>{
  const f=fixture(t);
  const received=new Date(f.clock.now-30*HOUR).toISOString();
  await f.router.dispatch({id:'old-in-inbox',kind:'owner',text:'让桌面做',receivedAt:received,channel:'wechat'},async()=> 'new-turn');
  const record=f.router.state.inputs['old-in-inbox'];
  assert.deepEqual([record.firstReceivedAt,record.channel],[Date.parse(received),'wechat']);
  assert.equal(f.router.handoffSource('old-in-inbox').reason,'source-too-old','classified just now, received thirty hours ago');
  await f.router.dispatch({id:'future',kind:'owner',text:'x',receivedAt:new Date(f.clock.now+HOUR).toISOString(),channel:'somewhere'},async()=> 'new-turn');
  assert.deepEqual([f.router.state.inputs.future.firstReceivedAt,f.router.state.inputs.future.channel],[f.clock.now,undefined],'never in the future; an unknown channel is not recorded');
});

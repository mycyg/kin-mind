import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MobileRouter,NOTICE_SEND_BUDGET,NOTICE_LOOKUP_BUDGET,NOTICE_ROUND_REST_MS,NOTICE_REJECT_RETRY_MS,REQUEUE_BUDGET,DEFERRAL_PLAN_RETRY_MS,ACTIVITY_KINDS,literalCommand,reconciliationState} from '../../adapters/mobile-router.mjs';
import {inputSummary,holdsSession} from '../../adapters/input-ledger.mjs';
import {WorkLockReview} from '../../adapters/work-lock-review.mjs';
import {MobileAudit} from '../../adapters/mobile-audit.mjs';
import {createMobileReviewer} from '../../adapters/mobile-reviewer.mjs';
import {createLeaseClient} from '../../adapters/model-lease.mjs';

const MINUTE=60000,HOUR=3600000;
function fixture(t,options={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-lifecycle-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const clock={now:Date.parse('2026-09-25T00:00:00Z')};
  const runtime={known:true,profileReady:true,sessionId:'synthetic',threadId:'synthetic',nativeSessionId:'synthetic',nativeStatus:'idle',
    model:'deepseek-flash',modelProvider:'custom-gateway',providerOverride:true,reasoningEffort:'high',serviceTierPreference:'default',fastMode:'off',
    active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0,inputCorrelation:true};
  const args={file:path.join(root,'router.json'),sessionId:'synthetic',inspect:async()=>({...runtime}),now:()=>clock.now,
    classify:async({text})=>({route:/work|写/.test(text)?'work':'chat',reason:'synthetic'}),
    switchModel:async(model,profile={model})=>{Object.assign(runtime,{model,reasoningEffort:profile.reasoningEffort??runtime.reasoningEffort,serviceTierPreference:profile.serviceTierPreference??runtime.serviceTierPreference});return {...runtime};},
    waitForIdle:async()=>{throw Error('waiting');},...options};
  return {root,clock,runtime,args,router:new MobileRouter(args)};
}
const chat=(f,id,text='hi')=>f.router.dispatch({id,kind:'owner',text},async()=> 'new-turn');
/** What the owned ACP answers when it read the whole of the submitted thread and the id is not there (CR2-INT-02). */
const absent={state:'not-found',complete:true,sessionId:'synthetic'};
const lookup=states=>async id=>(states[id]??'unknown')==='not-found'?absent:{state:states[id]??'unknown'};

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
  await f.router.watch({reconcileInput:async id=>id==='lost'?absent:{state:'unknown'}});
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

// WS8 #3: the flag that refuses a replay used to outlive the lookup that settles it.
test('an input restored from the journal is settled by its lookup: found stays answered once, proven unsent is routed afresh (WS8 #3)',async t=>{
  const f=fixture(t);
  for(const [id,text] of [['lost-a','在吗'],['lost-b','晚上吃什么'],['lost-c','明天呢']])await chat(f,id,text);
  // Every revision is lost; only the journal names the inputs.
  fs.writeFileSync(f.args.file,'{broken');fs.writeFileSync(f.args.file+'.prev','{broken');
  const router=new MobileRouter(f.args);
  assert.ok(router.state.recovery.restoredInputs>=3);
  assert.deepEqual(['lost-a','lost-b','lost-c'].map(id=>[router.state.inputs[id].state,router.state.inputs[id].recovered]),
    [['unconfirmed',true],['unconfirmed',true],['unconfirmed',true]]);
  const submitted=[];const submit=async decision=>{submitted.push(decision.inputId);return 'new-turn';};
  await assert.rejects(router.dispatch({id:'lost-b',kind:'owner',text:'晚上吃什么'},submit),/requires reconciliation/,'refused until it is looked up');
  const lookups={'lost-a':'found','lost-b':'not-found','lost-c':'unknown'};
  await router.watchOnce({reconcileInput:lookup(lookups),notifyOwner:null,requeue:null,sessionBusy:false});
  const [a,b,c]=['lost-a','lost-b','lost-c'].map(id=>router.state.inputs[id]);
  assert.deepEqual([a.state,a.recovered,a.restored],['accepted',undefined,'journal'],'found: it stands as accepted');
  assert.deepEqual([b.state,b.recovered,b.retry.evidence],['failed-before-submit',undefined,'reconciled-not-received'],'proven never received');
  assert.equal(c.recovered,true,'a lookup that could not answer settles nothing');
  assert.equal((await router.dispatch({id:'lost-a',kind:'owner',text:'在吗'},submit)).route,'deduplicated','found: a replay is a duplicate');
  await assert.rejects(router.dispatch({id:'lost-c',kind:'owner',text:'明天呢'},submit),/requires reconciliation/);
  // The inbox takes the proven-unsent one up again under its own id, and it is routed afresh.
  assert.equal((await router.received({id:'lost-b',kind:'owner',channel:'feishu'})).record.state,'preparing');
  assert.equal((await router.dispatch({id:'lost-b',kind:'owner',text:'晚上吃什么',channel:'feishu'},submit)).route,'new-turn');
  const routed=router.state.inputs['lost-b'];
  assert.deepEqual([routed.state,routed.route,typeof routed.hash,routed.restored,routed.retry.evidence],['accepted','chat','string',undefined,'reconciled-not-received']);
  assert.deepEqual(submitted,['lost-b'],'submitted once, and only the one proven never received');
});

test('an internal input whose submission is unknown is looked up by its id, and nobody is told (WS8 #2)',async t=>{
  const f=fixture(t);
  const lost=async(_,started)=>{started();throw Error('lost response');};
  for(const id of ['handoff:h1','handoff:h2'])
    await assert.rejects(f.router.dispatch({id,kind:'handoff',text:'继续 '+id,submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
  assert.equal(holdsSession(f.router.state.inputs['handoff:h1']),true,'an unknown submission holds a session boundary');
  const notices=[];
  await f.router.watch({reconcileInput:async id=>id==='handoff:h1'?{state:'found'}:absent,
    notifyOwner:async(kind,id)=>{notices.push([kind,id]);return {state:'accepted',messageId:'n'};}});
  const [h1,h2]=['handoff:h1','handoff:h2'].map(id=>f.router.state.inputs[id]);
  assert.deepEqual([h1.state,h2.state,h2.retry.evidence],['accepted','failed-before-submit','reconciled-not-received']);
  assert.equal([h1,h2].some(holdsSession),false,'settled by the lookup, neither holds one any more');
  assert.deepEqual(notices,[],'the owner is never told about Kin\'s own continuation');
});

// A journal restore used to take every id it found for the owner's, so the watchdog could
// tell her about one of Kin's own turns. Better one notice too few than a wrong one.
test('a journal restore knows whose each input was; one an older journal cannot say is only looked up, never told about',async t=>{
  // A new journal names the kind of every input it records.
  const f=fixture(t);
  await chat(f,'owner-a','在吗');
  await f.router.dispatch({id:'handoff:h1',kind:'handoff',text:'继续写'},async()=> 'new-turn');
  const events=fs.readFileSync(f.args.file+'.events.jsonl','utf8').trim().split('\n').map(line=>JSON.parse(line));
  assert.ok(events.some(e=>e.kind==='input-selected'&&e.id==='owner-a'&&e.inputKind==='owner'));
  assert.ok(events.some(e=>e.kind==='input-selected'&&e.id==='handoff:h1'&&e.inputKind==='handoff'));
  fs.writeFileSync(f.args.file,'{broken');fs.writeFileSync(f.args.file+'.prev','{broken');
  const restored=new MobileRouter(f.args);
  assert.deepEqual(['owner-a','handoff:h1'].map(id=>restored.state.inputs[id].kind),['owner','handoff']);
  const told=[];const notifyOwner=async(kind,id)=>{told.push([kind,id]);return {state:'accepted',messageId:'n'};};
  await restored.watch({reconcileInput:async()=>({state:'found'}),notifyOwner});
  f.clock.now+=11*MINUTE;
  await restored.watch({reconcileInput:async()=>({state:'found'}),notifyOwner});
  assert.deepEqual(told,[['unknown','owner-a']],'her own message, whose answer nobody can confirm now, is reported; Kin\'s continuation is not');

  // An older journal does not say whose an input was: its kind is unknown.
  const g=fixture(t);
  const old=[{kind:'input-selected',id:'inj_feishu_old'},{kind:'input-accepted',id:'inj_feishu_old',route:'chat'},{kind:'input-selected',id:'internal:draft-1'},
    {kind:'input-accepted',id:'internal:draft-1',route:'chat'},{kind:'input-reclassification-requested',id:'reclass-1',commandId:'owner-mode:x'}];
  fs.writeFileSync(g.args.file+'.events.jsonl',old.map((event,i)=>JSON.stringify({at:g.clock.now,...event,revision:i+1})).join('\n')+'\n');
  fs.writeFileSync(g.args.file,'{broken');fs.writeFileSync(g.args.file+'.prev','{broken');
  const legacy=new MobileRouter(g.args);
  assert.deepEqual(['inj_feishu_old','internal:draft-1','reclass-1'].map(id=>[legacy.state.inputs[id].kind,legacy.state.inputs[id].state]),
    [['unknown','unconfirmed'],['unknown','unconfirmed'],['unknown','unconfirmed']]);
  const heard=[],requeued=[],lookups={'inj_feishu_old':'not-found','internal:draft-1':'found','reclass-1':'not-found'};
  const watch=()=>legacy.watch({reconcileInput:lookup(lookups),
    notifyOwner:async(kind,id)=>{heard.push([kind,id]);return {state:'accepted',messageId:'n'};},
    requeue:async id=>{requeued.push(id);return {state:id==='inj_feishu_old'?'requeued':'missing'};}});
  for(let pass=0;pass<8;pass++){await watch();g.clock.now+=11*MINUTE;}
  assert.deepEqual(heard,[],'nothing is ever told about an input nobody can say was hers');
  assert.deepEqual([legacy.state.inputs['internal:draft-1'].state,holdsSession(legacy.state.inputs['internal:draft-1'])],['accepted',false],'found: settled by its lookup');
  // An older journal kept no record of the submission, so not-found proves nothing: only looked up (CR2-INT-02).
  assert.deepEqual(['inj_feishu_old','reclass-1'].map(id=>[legacy.state.inputs[id].state,legacy.state.inputs[id].reconciliation.state,legacy.state.inputs[id].reconciliation.reason]),
    [['unconfirmed','unknown','no-submission-record'],['unconfirmed','unknown','no-submission-record']]);
  assert.deepEqual(requeued,[],'never sent again');
});

// ---- Second lifecycle review (CR2-LIFE) ----
const waitFor=async(condition,what='condition')=>{for(let i=0;i<400&&!condition();i++)await new Promise(resolve=>setTimeout(resolve,5));assert.ok(condition(),what);};

test('the owner\'s stop withdraws work still being prepared: its reservation goes and a late submission is refused (CR2-LIFE-01)',async t=>{
  const f=fixture(t);
  let release;const prepared=new Promise(resolve=>{release=resolve;});const sent=[];
  const work=f.router.dispatch({id:'w',kind:'owner',text:'写一份报告',submissionProtocol:'host-boundary-v1'},
    async(decision,markSubmitted)=>{await prepared;await markSubmitted();sent.push(decision.inputId);return 'new-turn';});
  await waitFor(()=>f.router.state.inputs.w?.state==='preparing','the work is being prepared');
  assert.equal(f.router.reservations.has('w'),true);
  await f.router.dispatch({id:'stop',kind:'owner',text:'停止任务'},async()=> 'new-turn');
  assert.equal(f.router.reservations.has('w'),false,'its reservation is gone at once');
  assert.deepEqual([f.router.state.inputs.w.canceledBy,f.router.state.inputs.w.state,f.router.state.inputs.w.withdrawn.reason],['stop','failed-before-submit','canceled-by-owner']);
  release();
  assert.equal((await work).route,'canceled-by-owner','the preparation that resumes after the stop is refused at its last check');
  assert.deepEqual(sent,[],'nothing was submitted');
  assert.equal(inputSummary(f.router.state.inputs.w),'canceled-by-owner');
  assert.equal((await f.router.dispatch({id:'w',kind:'owner',text:'写一份报告'},async()=>assert.fail('never submitted'))).route,'canceled-by-owner','replayed, it stays withdrawn');
  assert.equal(f.router.state.inputs.stop.state,'accepted','the stop itself went through');
});

test('a live dispatch is judged for stalls, and one whose preparation hangs past its deadline cannot submit late (CR2-LIFE-03)',async t=>{
  const f=fixture(t);
  let release;const prepared=new Promise(resolve=>{release=resolve;});const sent=[];
  const slow=f.router.dispatch({id:'slow',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},
    async(decision,markSubmitted)=>{await prepared;await markSubmitted();sent.push(1);return 'new-turn';});
  await waitFor(()=>f.router.state.inputs.slow?.state==='preparing','preparing');
  const told=[];const watch=()=>f.router.watch({notifyOwner:async(kind,id)=>{told.push([kind,id]);return {state:'accepted',messageId:'n'};},
    requeue:async()=>assert.fail('a live dispatch is never requeued')});
  f.clock.now+=11*MINUTE;await watch();
  assert.deepEqual(told,[['stopped','slow']],'past the stall limit she is told, though it is still in flight');
  assert.equal(f.router.state.inputs.slow.state,'preparing','before its deadline it may still go');
  f.clock.now+=10*MINUTE;await watch();
  assert.deepEqual([f.router.state.inputs.slow.state,f.router.state.inputs.slow.withdrawn?.reason],['failed-before-submit','dispatch-wait-exceeded'],'past its deadline it is withdrawn');
  release();
  await assert.rejects(slow,error=>error.code==='input-not-submitted','the late submission is refused as not submitted');
  assert.deepEqual(sent,[]);
});

test('a reply settles the inputs it was formed for, never a later one steered into the same turn (CR2-LIFE-04)',async t=>{
  const f=fixture(t);
  await chat(f,'a','在吗');
  await f.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['a']});
  await f.router.dispatch({id:'b',kind:'owner',text:'还有个事'},async()=> 'steered');
  assert.equal(f.router.state.inputs.b.turnStartedAt,f.router.state.inputs.a.turnStartedAt,'one native turn');
  assert.deepEqual(await f.router.observe('reply-complete',{inputId:'a'}),['a']);
  assert.equal(f.router.state.inputs.b.answer,undefined,'A\'s reply formed before B came: B is still owed');
  await f.router.dispatch({id:'c',kind:'owner',text:'再补一句'},async()=> 'steered');
  assert.deepEqual(await f.router.observe('reply-complete',{inputId:'b',answeredInputIds:['b','c','a']}),['b','c'],'what the reply group names, and only what is still open');
  assert.deepEqual([f.router.state.inputs.c.answer.state,f.router.state.inputs.c.answer.basis],['covered','answered-input-ids']);
  // Merged into one prompt is not settled by the start time either: only what the group names.
  await chat(f,'d','一');await chat(f,'e','二');
  await f.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['d','e']});
  assert.deepEqual(await f.router.observe('reply-complete',{inputId:'e'}),['e'],'a group from before the list answers only its own input');
  assert.deepEqual(await f.router.observe('reply-choice',{inputId:'d',state:'silent',answeredInputIds:['d']}),['d']);
});

test('the requeue budget counts across every attempt, taken before the requeue is handed out (CR2-LIFE-05)',async t=>{
  const f=fixture(t);
  const lost=async(_,started)=>{started();throw Error('lost response');};
  let requeues=0;
  const notifyOwner=async()=>({state:'accepted',messageId:'n'});
  for(let cycle=0;cycle<REQUEUE_BUDGET+2;cycle++) {
    await assert.rejects(f.router.dispatch({id:'x',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
    await f.router.watch({reconcileInput:async()=>absent});
    assert.equal(f.router.state.inputs.x.retry.evidence,'reconciled-not-received');
    await f.router.watch({requeue:async()=>{requeues++;return {state:'requeued'};},notifyOwner});
    f.clock.now+=HOUR;
  }
  assert.equal(requeues,REQUEUE_BUDGET,'each not-found starts no new budget');
  // A requeue whose job is taken up again before its answer comes back is still counted.
  const g=fixture(t);
  await assert.rejects(g.router.dispatch({id:'y',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
  await g.router.watch({reconcileInput:async()=>absent});
  await g.router.watch({requeue:async id=>{await g.router.dispatch({id,kind:'owner',text:'在吗'},async()=> 'new-turn');return {state:'requeued'};},notifyOwner});
  assert.deepEqual([g.router.state.inputs.y.state,g.router.state.inputs.y.requeues],['accepted',1]);
});

test('a notice the platform refused goes again under its own id after bounded rests, then is only looked up (CR2-LIFE-06)',async t=>{
  const f=fixture(t);
  await chat(f,'owner-x','在吗');
  f.clock.now+=11*MINUTE;
  const calls=[];const notifyOwner=async(kind,id,options)=>{calls.push([kind,id,options?.mayStart]);return {state:'rejected'};};
  await f.router.watch({notifyOwner});
  for(let i=0;i<NOTICE_REJECT_RETRY_MS.length+3;i++){f.clock.now+=7*HOUR;await f.router.watch({notifyOwner});}
  const notice=f.router.state.inputs['owner-x'].ownerNotice;
  assert.ok(calls.every(([kind,id])=>kind==='unknown'&&id==='owner-x'),'one notice, under one identity');
  assert.deepEqual(calls.slice(0,NOTICE_REJECT_RETRY_MS.length+1).map(call=>call[2]),Array(NOTICE_REJECT_RETRY_MS.length+1).fill(true),'sent, then again after each rest');
  assert.ok(calls.slice(NOTICE_REJECT_RETRY_MS.length+1).every(call=>call[2]===false),'past the rests it is only looked up');
  assert.equal(notice.rejections,NOTICE_REJECT_RETRY_MS.length+1);
});

test('an input owing her no notice stays until its own id is looked up: nothing unreconciled is archived (CR2-LIFE-10)',async t=>{
  const f=fixture(t,{hotLimits:{internal:1}});
  const lost=async(_,started)=>{started();throw Error('lost response');};
  for(const id of ['facts-1','facts-2','facts-3'])
    await assert.rejects(f.router.dispatch({id,kind:'work-facts',text:'宿主事实 '+id,submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
  f.router.state.inputs['old-1']={id:'old-1',kind:'unknown',state:'failed-before-submit',restored:'journal',retry:{attempts:0,evidence:'reconciled-not-received',nextAt:0},at:1};
  f.router.prune();
  assert.deepEqual(['facts-1','facts-2','facts-3','old-1'].filter(id=>f.router.state.inputs[id]),['facts-1','facts-2','facts-3','old-1'],'none of them is settled yet');
  await f.router.watch({reconcileInput:async()=>({state:'found'}),requeue:async()=>({state:'missing'})});
  await f.router.watch({reconcileInput:async()=>({state:'found'}),requeue:async()=>({state:'missing'})});
  assert.deepEqual(['facts-1','facts-2','facts-3'].map(id=>f.router.state.inputs[id].state),['accepted','accepted','accepted']);
  assert.equal(f.router.state.inputs['old-1'].retry.exhausted,true,'no inbox job to go back to');
  f.router.prune();
  assert.deepEqual(['facts-1','facts-2','facts-3','old-1'].filter(id=>f.router.state.inputs[id]),['old-1'],'once settled they go to the archive like any other, past the hot limit');
});

test('a deferral still owed its plan, or owed to Kin, keeps its place past the closed-task limit (CR2-LIFE-11)',async t=>{
  const f=fixture(t,{hotLimits:{tasks:1}});
  const task=(id,status,plan)=>({id,status,inputIds:[],summary:id,deliveries:{},tools:{},createdAt:1,...(plan?{deferral:{notBefore:'2026-09-26T00:00:00Z',plan}}:{})});
  Object.assign(f.router.state.tasks,{pending:task('pending','deferred',{state:'pending'}),untold:task('untold','deferred',{state:'needs-kin'}),
    told:task('told','deferred',{state:'needs-kin',toldAt:5}),made:task('made','deferred',{state:'created'}),done:task('done','completed')});
  f.router.prune();
  assert.deepEqual(Object.keys(f.router.state.tasks).sort(),['done','pending','untold']);
  assert.deepEqual([f.router.deferredPlans().map(t=>t.id),f.router.deferralsToTell().map(t=>t.id)],[['pending'],['untold']],'still scheduled');
});

test('when the host first received an input is kept when routing replaces its intake record (CR2-LIFE-08)',async t=>{
  const f=fixture(t);
  const first=new Date(f.clock.now-30*HOUR).toISOString();
  await f.router.received({id:'late',kind:'owner',channel:'wechat',receivedAt:first});
  f.clock.now+=MINUTE;
  await f.router.dispatch({id:'late',kind:'owner',text:'让桌面做',receivedAt:new Date(f.clock.now).toISOString(),channel:'wechat'},async()=> 'new-turn');
  assert.equal(f.router.state.inputs.late.firstReceivedAt,Date.parse(first));
  assert.equal(f.router.handoffSource('late').reason,'source-too-old','its age runs from when it first came');
});

test('a runtime notice starts no send while dispatch is frozen, and passes the activity gate when it does (CR2-LIFE-02)',async t=>{
  const f=fixture(t);
  f.router.state.notices.n1={id:'n1',kind:'mode-failed',text:'切换没有成功。',state:'pending',attempts:0,sourceInputId:null};
  f.router.state.notices.n2={id:'n2',kind:'mode-failed',text:'早先那条',state:'unconfirmed',attempts:1,sourceInputId:null};
  const sent=[],looked=[];let during=null;
  const send=async request=>{sent.push(request.id);during=f.router.activityList();return {state:'accepted',messageId:'m-'+request.id};};
  const lookup=async id=>{looked.push(id);return {state:'accepted',messageId:'m-'+id};};
  await f.router.freezeDispatch('release');
  await f.router.flushNotices({send,lookup});
  assert.deepEqual(sent,[],'nothing starts while frozen');
  assert.deepEqual([f.router.state.notices.n1.state,f.router.state.notices.n1.attempts],['pending',0],'and no attempt is spent');
  assert.deepEqual(looked,['n2'],'a begun one is still looked up');
  await f.router.thawDispatch('released');
  await f.router.flushNotices({send,lookup});
  assert.deepEqual(sent,['n1']);
  assert.deepEqual(during.map(activity=>[activity.kind,activity.id]),[['notice','n1']],'counted in flight while it is sent');
  assert.deepEqual(f.router.activityList(),[],'and released after');
});

test('the owner\'s literal commands are read from a message of their own (CR2-LIFE-09)',()=>{
  assert.deepEqual(['停止任务','/compact',' /mode work！','/mode auto','/compact\n\n在吗','好的'].map(text=>literalCommand(text)),['stop','compact','work','auto',null,null]);
  assert.equal(literalCommand('/compact',{attachments:[{kind:'file'}]}),null,'a command with attachments is not one');
  assert.equal(literalCommand('停止任务',{attachments:[{kind:'file'}]}),'stop','her stop always is');
});

// ---- Integration review: CR2-INT-02 ----
test('not-found proves an input never arrived only on its own thread, from a runtime that recorded input ids, read in full (CR2-INT-02)',async t=>{
  const submit={sessionId:'thread-1',runtime:'bundle-2',correlation:true},absentThere={state:'not-found',complete:true,sessionId:'thread-1'};
  assert.deepEqual(reconciliationState(absentThere,submit),{state:'not-found'});
  assert.deepEqual(reconciliationState(absentThere,{...submit,correlation:false}),{state:'unknown',reason:'runtime-without-input-correlation'});
  assert.deepEqual(reconciliationState({...absentThere,sessionId:'thread-2'},submit),{state:'unknown',reason:'not-the-submitted-thread'});
  assert.deepEqual(reconciliationState({...absentThere,complete:false},submit),{state:'unknown',reason:'history-not-read-in-full'});
  assert.deepEqual(reconciliationState({state:'not-found'},submit),{state:'unknown',reason:'history-not-read-in-full'},'an older ACP\'s bare answer');
  assert.deepEqual(reconciliationState(absentThere,null),{state:'unknown',reason:'no-submission-record'});
  assert.deepEqual(reconciliationState({state:'found',sessionId:'thread-2'},null),{state:'found'},'found anywhere is receipt');
  const lost=async(_,started)=>{started();throw Error('lost response');};
  const asked=[];const answer=(id,{sessionId})=>{asked.push(sessionId);return {state:'not-found',complete:true,sessionId};};

  // 1. Submitted while an older runtime ran, which recorded no input ids; the new one cannot find it.
  const old=fixture(t,{runtimeId:'bundle-1'});delete old.runtime.inputCorrelation;
  await assert.rejects(old.router.dispatch({id:'x',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
  assert.deepEqual(old.router.state.inputs.x.submit,{sessionId:'synthetic',runtime:'bundle-1',correlation:false,at:old.clock.now},'the ledger names where it went and on what');
  old.runtime.inputCorrelation=true;
  await old.router.watch({reconcileInput:async(id,options)=>answer(id,options)});
  assert.deepEqual([old.router.state.inputs.x.state,old.router.state.inputs.x.reconciliation.reason],['unconfirmed','runtime-without-input-correlation'],'still unknown');
  await assert.rejects(old.router.dispatch({id:'x',kind:'owner',text:'在吗'},async()=>assert.fail('never sent again')),/reconciliation/);

  // 2. Its own thread, a runtime that records ids, the whole history read: proven, retried under its own id.
  const f=fixture(t,{runtimeId:'bundle-2'});
  await assert.rejects(f.router.dispatch({id:'y',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
  await f.router.watch({reconcileInput:async(id,options)=>answer(id,options)});
  assert.deepEqual([f.router.state.inputs.y.state,f.router.state.inputs.y.retry.evidence],['failed-before-submit','reconciled-not-received']);

  // 3. The binding moved to a new thread: the lookup asks the original one, and an answer about another thread proves nothing.
  const g=fixture(t,{runtimeId:'bundle-2'});
  await assert.rejects(g.router.dispatch({id:'z',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
  g.router.sessionId='thread-after-migration';
  await g.router.watch({reconcileInput:async(id,options)=>{asked.push(options.sessionId);return {state:'not-found',complete:true,sessionId:'thread-after-migration'};}});
  assert.deepEqual([g.router.state.inputs.z.state,g.router.state.inputs.z.reconciliation.reason],['unconfirmed','not-the-submitted-thread']);
  assert.deepEqual(asked,['synthetic','synthetic','synthetic'],'every lookup went to the thread the input was submitted to');
});

// ---- Third review: CR3-FLOW ----

test('a work summary, a health reading and a classification asked again each pass the activity gate where the call starts; refused, they count nothing (CR3-FLOW-01)',async t=>{
  // A summary of open work Kin has gone quiet about.
  const f=fixture(t);
  await f.router.dispatch({id:'job',kind:'owner',text:'写一份报告'},async()=> 'new-turn');
  const task=f.router.tasks()[0];
  await f.router.requestMode({commandId:'accept',mode:'work',reason:'yes',taskOutcome:'accepted',completedTaskId:task.id,completedInputVersion:task.inputVersion});
  await f.router.observe('prompt-start',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,inputIds:['job']});
  await f.router.observe('prompt-end',{taskId:task.id,inputVersion:task.inputVersion,turnFence:0,stopReason:'end_turn'});
  let read=0,during=null,busyDuring=null;const asked=[];
  const review=new WorkLockReview({router:f.router,file:path.join(f.root,'review.json'),now:()=>f.clock.now,idleMs:HOUR,
    collect:async()=>{read++;return {input:{inputs:[{id:'job',text:'写一份报告'}],outputs:[],unsentDrafts:[]},facts:{unsentDraftIds:[],undelivered:[]}};},
    summarize:async()=>{asked.push('summary');during=f.router.activityList();busyDuring=f.router.busy(f.runtime);
      return {summary:{summary:'Asked for a report; nothing delivered yet.',delivered:[],open:['the report'],unsent:[],evidenceIds:['job']},receipt:{model:'deepseek-flash',requestId:'r-1'}};}});
  f.clock.now+=2*HOUR;
  await f.router.freezeDispatch('release');
  const held=await review.tick();
  assert.deepEqual([held.state,held.reason,read,asked.length],['waiting','dispatch-frozen',0,0],'frozen: nothing is read, spent or counted');
  assert.deepEqual(f.router.activityList(),[]);
  await f.router.thawDispatch('released');
  f.clock.now+=10*MINUTE;
  const told=await review.tick();
  assert.deepEqual([told.state,told.repeats,asked.length],['told',0,1]);
  assert.deepEqual(during.map(activity=>[activity.kind,activity.id]),[['work-summary',told.id]],'the drain counts it while the model is asked');
  assert.equal(busyDuring,false,'it holds no turn, no switch and no dispatch');
  assert.deepEqual(f.router.activityList(),[],'let go once the call has ended');

  // A health reading: the freeze and its lane are both asked before anything is collected or counted.
  const leases=[];
  const lease={acquire:async({lane,purpose})=>{leases.push(purpose);return {proceed:true,lane,release:async()=>{leases.push('released');}};}};
  let reading=null;
  const audit=new MobileAudit({file:path.join(f.root,'audit.json'),now:()=>f.clock.now,lease,activity:spec=>f.router.beginActivity(spec),
    collect:async()=>({checkedAt:f.clock.now}),review:async()=>{reading=f.router.activityList();return {status:'healthy',findings:[]};}});
  const due=audit.state.nextAt;
  await f.router.freezeDispatch('release');
  assert.deepEqual(await audit.tick(),{state:'skipped',reason:'dispatch-frozen'});
  assert.deepEqual([leases.length,audit.state.status,audit.state.failures,audit.state.nextAt],[0,undefined,undefined,due],'refused before its lane is asked: no run, no failure, still due');
  await f.router.thawDispatch('released');
  assert.deepEqual(await audit.tick(),{state:'healthy'});
  assert.deepEqual(reading.map(activity=>activity.kind),['health-review'],'counted while the reading runs');
  assert.deepEqual([f.router.activityList(),leases],[[],['mobile-health-audit','released']]);
  const refusing={acquire:async({lane})=>({proceed:false,lane,state:'wait',reason:'lane-full',release:async()=>{}})};
  const skipped=new MobileAudit({file:path.join(f.root,'audit-2.json'),now:()=>f.clock.now,lease:refusing,activity:spec=>f.router.beginActivity(spec),
    collect:async()=>assert.fail('nothing is collected'),review:async()=>assert.fail('nothing is asked')});
  assert.equal((await skipped.tick()).state,'skipped');
  assert.deepEqual(f.router.activityList(),[],'a lane that refuses lets the gate go');

  // A classification asked again.
  let failing=true,classifying=null;
  const g=fixture(t,{classify:async()=>{if(failing)throw Error('deepseek-http-503');classifying=[g.router.activityList(),g.router.busy(g.runtime)];return {route:'chat',reason:'synthetic'};}});
  await g.router.select({id:'q',kind:'owner',text:'在吗'});
  const entry=g.router.state.semanticPending.q;
  assert.deepEqual([g.router.state.inputs.q.state,entry.attempts],['semantic-pending',1]);
  failing=false;
  await g.router.freezeDispatch('release');
  await g.router.reviewSemanticPending();
  assert.deepEqual([entry.state,entry.attempts,classifying],['pending',1,null],'frozen: the model is not asked and no attempt is counted');
  assert.equal(g.router.state.history.some(event=>event.kind==='semantic-retry'),false);
  await g.router.thawDispatch('released');
  await g.router.reviewSemanticPending();
  assert.deepEqual([g.router.state.inputs.q.state,entry.attempts],['selected',2]);
  assert.deepEqual(classifying[0].map(activity=>[activity.kind,activity.id]),[['classification-retry','q']],'counted while the model is asked');
  assert.equal(classifying[1],false,'holding nothing of her conversation');
  assert.deepEqual(g.router.activityList(),[]);
  for(const kind of ['work-summary','health-review','classification-retry'])assert.ok(ACTIVITY_KINDS.includes(kind),kind);
});

test('the owner\'s stop takes her stopped work out of the host\'s queue by its own ids, and a prompt that would begin with it never does (CR3-FLOW-02)',async t=>{
  // The host's queue, by input id and in order.
  const hostQueue=()=>{const queue=[];return {queue,take:ids=>{const taken=queue.filter(id=>ids.includes(id));queue.splice(0,queue.length,...queue.filter(id=>!ids.includes(id)));return taken;},
    push:id=>async()=>{queue.push(id);return {route:'new-turn',queued:true};}};};
  const q=hostQueue();
  const f=fixture(t,{withdrawQueued:q.take});
  await f.router.dispatch({id:'w',kind:'owner',text:'写一份报告'},q.push('w'));
  assert.equal(f.router.state.inputs.w.state,'queued','waiting behind a turn of its own, not begun');
  await f.router.dispatch({id:'stop',kind:'owner',text:'停止任务'},q.push('stop'));
  const w=f.router.state.inputs.w;
  assert.deepEqual(q.queue,['stop'],'her stopped work left the queue; the stop keeps its place');
  assert.deepEqual([w.id,w.state,w.canceledBy,w.withdrawn.reason,w.withdrawn.stage,w.withdrawn.fromQueue,w.submissionStartedAt,typeof w.queuedSubmissionAt],
    ['w','failed-before-submit','stop','canceled-by-owner','host-queue',true,undefined,'number'],'canceled under its own id with its receipt; the queue was never a submission');
  assert.equal(inputSummary(w),'canceled-by-owner');
  await f.router.dispatch({id:'later',kind:'owner',text:'在吗'},q.push('later'));
  assert.deepEqual(q.queue,['stop','later'],'what came after the stop is untouched');
  assert.equal((await f.router.dispatch({id:'w',kind:'owner',text:'写一份报告'},async()=>assert.fail('never submitted'))).route,'canceled-by-owner','replayed, it stays withdrawn');
  assert.equal(await f.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['stop']}),undefined,'the stop\'s own prompt begins');

  // The queue did not hold it — it was the message about to begin — and a message sent after
  // the stop was merged into it: that prompt never begins, and the later message is not lost.
  const g=fixture(t,{withdrawQueued:()=>[]});
  await g.router.dispatch({id:'w',kind:'owner',text:'写一份报告'},async()=>({route:'new-turn',queued:true}));
  await g.router.dispatch({id:'stop',kind:'owner',text:'停止任务'},async()=>({route:'new-turn',queued:true}));
  assert.deepEqual([g.router.state.inputs.w.state,g.router.state.inputs.w.withdrawn.fromQueue],['failed-before-submit',false]);
  await g.router.dispatch({id:'after',kind:'owner',text:'在吗'},async()=>({route:'merged-before-start',queued:true}));
  assert.deepEqual(await g.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['w','after']}),
    {state:'refused',canceled:['w'],notSubmitted:['after']},'refused where it would have begun');
  assert.equal(g.router.state.turn,undefined,'no native turn is recorded');
  const after=g.router.state.inputs.after;
  assert.deepEqual([after.state,after.canceledBy,after.failureStage,after.retry.evidence],['failed-before-submit',undefined,'prompt-start','not-submitted']);
  const requeued=[];
  await g.router.watch({requeue:async id=>{requeued.push(id);return {state:'requeued'};},notifyOwner:async()=>({state:'accepted',messageId:'n'})});
  assert.deepEqual(requeued,['after'],'the later message goes again under its own id; the stopped work never does');

  // Stopped while it was being handed over: its prompt start refuses it, and the handover keeps that.
  const h=fixture(t,{withdrawQueued:()=>[]});
  let begin=null;
  const handing=h.router.dispatch({id:'w',kind:'owner',text:'写一份报告',submissionProtocol:'host-boundary-v1'},async(decision,markSubmitted)=>{
    await markSubmitted();
    await h.router.dispatch({id:'stop',kind:'owner',text:'停止任务'},async()=>({route:'new-turn',queued:true}));
    begin=await h.router.observe('prompt-start',{taskId:decision.taskId,inputVersion:decision.inputVersion,turnFence:0,inputIds:['w']});
    return {route:'new-turn',queued:true};
  });
  assert.equal((await handing).route,'canceled-by-owner');
  assert.deepEqual(begin,{state:'refused',canceled:['w'],notSubmitted:[]});
  assert.deepEqual([h.router.state.inputs.w.state,h.router.state.inputs.w.canceledBy,h.router.state.inputs.w.withdrawn.stage],['failed-before-submit','stop','prompt-start']);

  // Handed to the queue just after the stop: taken back the moment the handover ends.
  const k=hostQueue();
  const late=fixture(t,{withdrawQueued:k.take});
  const lateWork=late.router.dispatch({id:'w',kind:'owner',text:'写一份报告',submissionProtocol:'host-boundary-v1'},async(decision,markSubmitted)=>{
    await markSubmitted();
    await late.router.dispatch({id:'stop',kind:'owner',text:'停止任务'},k.push('stop'));
    return k.push('w')();
  });
  assert.equal((await lateWork).route,'canceled-by-owner');
  assert.deepEqual(k.queue,['stop']);
  assert.deepEqual([late.router.state.inputs.w.canceledBy,late.router.state.inputs.w.withdrawn.stage,late.router.state.inputs.w.withdrawn.fromQueue],['stop','host-queue',true]);
});

test('a runtime notice whose write fails before its send lets the permit go and waits, unsent, under its own id (CR3-FLOW-09)',async t=>{
  const f=fixture(t);
  f.router.state.notices.n1={id:'n1',kind:'mode-failed',text:'切换没有成功。',state:'pending',attempts:0,sourceInputId:null};
  const sent=[];const send=async request=>{sent.push(request.id);return {state:'accepted',messageId:'m-'+request.id};};
  const save=f.router.save;
  f.router.save=function(kind,detail){if(kind==='notice-sending')throw Error('disk full');return save.call(this,kind,detail);};
  await assert.rejects(f.router.flushNotices({send,lookup:async()=>assert.fail('nothing was sent, so nothing is looked up')}),/disk full/);
  assert.deepEqual(f.router.activityList(),[],'the permit is let go');
  assert.equal(f.router.busy(f.runtime),false,'nothing holds busy or the drain');
  const n=f.router.state.notices.n1;
  assert.deepEqual([n.id,n.state,n.attempts,n.stage,n.nextAction],['n1','pending',0,'not-submitted','resend-same-id'],'not submitted, under its own id, no attempt spent');
  assert.deepEqual(sent,[]);
  f.router.save=save;
  f.clock.now+=MINUTE;
  await f.router.flushNotices({send,lookup:async id=>({state:'accepted',messageId:'m-'+id})});
  assert.deepEqual(sent,['n1'],'taken up again under the same id once the write succeeds');
  assert.deepEqual([f.router.state.notices.n1.state,f.router.state.notices.n1.attempts],['accepted',1]);
  assert.deepEqual(f.router.activityList(),[]);
});

test('an input requeued while it was taken in keeps that count once it is routed (CR3-FLOW-10)',async t=>{
  const f=fixture(t);let requeues=0;
  const requeue=async()=>{requeues++;return {state:'requeued'};},notifyOwner=async()=>({state:'accepted',messageId:'n'});
  const lost=async(_,started)=>{started();throw Error('lost response');};
  // Its intake failed and it went back to the inbox once.
  await f.router.received({id:'z',kind:'owner',channel:'wechat'});
  await f.router.intakeFailed('z','attachment-unreadable');
  f.clock.now+=HOUR;await f.router.watch({requeue,notifyOwner});
  assert.deepEqual([requeues,f.router.state.inputs.z.requeues],[1,1]);
  // Taken in again and routed; each submission is lost and then proven never received.
  await f.router.received({id:'z',kind:'owner',channel:'wechat'});
  for(let cycle=0;cycle<REQUEUE_BUDGET+1;cycle++) {
    await assert.rejects(f.router.dispatch({id:'z',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
    if(!cycle)assert.deepEqual([f.router.state.inputs.z.state,f.router.state.inputs.z.requeues],['unconfirmed',1],'routing never starts the count again');
    await f.router.watch({reconcileInput:async()=>absent});
    await f.router.watch({requeue,notifyOwner});
    f.clock.now+=HOUR;
  }
  assert.equal(requeues,REQUEUE_BUDGET,'three in all, across its intake and its routing');
  // A classification that fails after an intake requeue keeps the count on its pending record too.
  const g=fixture(t,{classify:async()=>{throw Error('deepseek-http-503');}});
  await g.router.received({id:'s',kind:'owner',channel:'wechat'});
  await g.router.intakeFailed('s','attachment-unreadable');
  g.clock.now+=HOUR;await g.router.watch({requeue:async()=>({state:'requeued'}),notifyOwner});
  await g.router.received({id:'s',kind:'owner',channel:'wechat'});
  await g.router.select({id:'s',kind:'owner',text:'在吗'});
  assert.deepEqual([g.router.state.inputs.s.state,g.router.state.inputs.s.requeues,g.router.state.inputs.s.retry.evidence],['semantic-pending',1,'not-submitted'],'the count and this attempt\'s evidence are kept apart');
});

// ---- The owner's stop kept through a restore from the journal alone (§0) ----

test('a restore from the journal alone keeps the owner\'s stop: what she stopped comes back canceled under its own id, and what came after is untouched',async t=>{
  const queue=[];
  const take=ids=>{const taken=queue.filter(id=>ids.includes(id));queue.splice(0,queue.length,...queue.filter(id=>!ids.includes(id)));return taken;};
  let f;
  f=fixture(t,{withdrawQueued:take,interruptTurn:async()=>{Object.assign(f.runtime,{active:false,nativeStatus:'idle'});return {state:'interrupted'};}});
  const lost=async(_,started)=>{started();throw Error('lost response');};
  // A turn the stop cut: the input it answered had reached the native session.
  await f.router.dispatch({id:'running',kind:'owner',text:'写一份报告'},async()=> 'new-turn');
  const first=f.router.tasks()[0];
  await f.router.observe('prompt-start',{taskId:first.id,inputVersion:first.inputVersion,turnFence:0,inputIds:['running']});
  Object.assign(f.runtime,{active:true,nativeStatus:'active'});
  await f.router.dispatch({id:'stop-0',kind:'owner',text:'停止任务'},async()=> 'new-turn');
  // Stopped in the host's queue (CR3-FLOW-02), and while still being prepared (CR2-LIFE-01).
  await f.router.dispatch({id:'queued-w',kind:'owner',text:'写第二份'},async()=>{queue.push('queued-w');return {route:'new-turn',queued:true};});
  let release;const prepared=new Promise(resolve=>{release=resolve;});
  const prep=f.router.dispatch({id:'prep',kind:'owner',text:'再写一份',submissionProtocol:'host-boundary-v1'},async(decision,markSubmitted)=>{await prepared;await markSubmitted();return 'new-turn';});
  await waitFor(()=>f.router.state.inputs.prep?.state==='preparing','being prepared');
  await f.router.dispatch({id:'stop-1',kind:'owner',text:'停止任务'},async()=> 'new-turn');
  release();assert.equal((await prep).route,'canceled-by-owner');
  // Stopped while it was being handed over: refused where its prompt would have begun.
  const handing=await f.router.dispatch({id:'handing',kind:'owner',text:'写第三份',submissionProtocol:'host-boundary-v1'},async(decision,markSubmitted)=>{
    await markSubmitted();
    await f.router.dispatch({id:'stop-2',kind:'owner',text:'停止任务'},async()=> 'new-turn');
    await f.router.observe('prompt-start',{taskId:decision.taskId,inputVersion:decision.inputVersion,turnFence:0,inputIds:['handing']});
    return {route:'new-turn',queued:true};
  });
  assert.equal(handing.route,'canceled-by-owner');
  // Unrelated and after every stop: a chat whose submission was lost.
  await assert.rejects(f.router.dispatch({id:'after',kind:'owner',text:'在吗',submissionProtocol:'host-boundary-v1'},lost),/reconciliation/);
  // The journal names each stop and how far the input had got.
  const canceling=fs.readFileSync(f.args.file+'.events.jsonl','utf8').trim().split('\n').map(line=>JSON.parse(line)).filter(event=>event.canceledBy);
  assert.deepEqual(canceling.map(event=>[event.id,event.kind,event.canceledBy,event.scope]).sort(),[
    ['handing','input-canceled','stop-2','native-session'],['handing','input-dispatch-withdrawn','stop-2','prompt-start'],
    ['prep','input-dispatch-withdrawn','stop-1','preparation'],['queued-w','input-dispatch-withdrawn','stop-1','host-queue'],
    ['running','input-canceled','stop-0','native-session']]);

  // Every revision is lost; only the journal is left.
  fs.writeFileSync(f.args.file,'{broken');fs.writeFileSync(f.args.file+'.prev','{broken');
  const router=new MobileRouter(f.args);
  const restored=id=>router.state.inputs[id];
  assert.deepEqual(['running','queued-w','prep','handing'].map(id=>[id,restored(id).state,restored(id).canceledBy,restored(id).cancelScope,restored(id).recovered,inputSummary(restored(id))]),[
    ['running','accepted','stop-0','native-session',undefined,'canceled-by-owner'],
    ['queued-w','failed-before-submit','stop-1','host-queue',undefined,'canceled-by-owner'],
    ['prep','failed-before-submit','stop-1','preparation',undefined,'canceled-by-owner'],
    ['handing','failed-before-submit','stop-2','prompt-start',undefined,'canceled-by-owner']],'each comes back canceled by the stop that did it, under its own id');
  assert.deepEqual([restored('after').state,restored('after').recovered,restored('after').canceledBy],['unconfirmed',true,undefined],'what came after comes back as any input does');
  // The watchdog looks up and requeues only what she did not stop; nobody is told about what she did.
  const stops=['stop-0','stop-1','stop-2'],looked=[],requeued=[],told=[];
  const notifyOwner=async(kind,id)=>{told.push(id);return {state:'accepted',messageId:'n'};};
  await router.watch({reconcileInput:async id=>{looked.push(id);return stops.includes(id)?{state:'found'}:absent;},notifyOwner});
  await router.watch({requeue:async id=>{requeued.push(id);return {state:'requeued'};},notifyOwner});
  assert.deepEqual(looked.sort(),['after',...stops].sort(),'nothing she stopped is looked up');
  assert.deepEqual(requeued,['after'],'proven unsent, what came after goes again under its own id; nothing she stopped does');
  assert.deepEqual(told,[]);
  // Taken in or replayed under their own ids, the stopped inputs stay stopped.
  assert.deepEqual((await router.received({id:'prep',kind:'owner',channel:'wechat'})).record.state,'failed-before-submit','taken in again, it is not prepared afresh');
  for(const [id,text] of [['queued-w','写第二份'],['prep','再写一份'],['handing','写第三份']])
    assert.equal((await router.dispatch({id,kind:'owner',text},async()=>assert.fail('never submitted'))).route,'canceled-by-owner',id);
  assert.equal((await router.dispatch({id:'running',kind:'owner',text:'写一份报告'},async()=>assert.fail('never submitted'))).route,'deduplicated');
  assert.deepEqual(['running','queued-w','prep','handing'].map(id=>inputSummary(restored(id))),Array(4).fill('canceled-by-owner'));
});

// ---- Fourth review: CR4-FLOW ----

test('a classification asked again holds its activity until the model call has ended: a late lease starts nothing, a request past the deadline is canceled (CR4-FLOW-01)',async t=>{
  // The real reviewer and lane client; the ledger and the provider answer when the test says so.
  let slowLease=null;const leaseCalls=[];
  const lease=createLeaseClient({holder:'test',request:async route=>{
    leaseCalls.push(route.split('/').pop());
    if(route.endsWith('/acquire')&&slowLease)await slowLease;
    return route.endsWith('/acquire')?{state:'admitted',lease:{renew_after_seconds:30}}:{state:'released'};
  }});
  let phase='fail',endRequest=null;const requests=[],usage=[];
  const fetchImpl=async(url,init)=>{
    requests.push(phase);
    if(phase==='fail')return {ok:false,status:503};
    // A slow request: like fetch it refuses a signal already fired; otherwise it ends only
    // after it is canceled, and when the test lets it.
    return new Promise((_,reject)=>{
      if(init.signal.aborted)return reject(init.signal.reason);
      init.signal.addEventListener('abort',()=>{endRequest=()=>reject(init.signal.reason??Error('aborted'));},{once:true});
    });
  };
  const reviewer=createMobileReviewer({key:'synthetic-key',fetchImpl,lease,onUsage:row=>usage.push(row)});
  const f=fixture(t,{classify:(input,options)=>reviewer.classify(input,options)});
  f.router.state.config.classifierTimeoutMs=20;
  await f.router.select({id:'q',kind:'owner',text:'在吗'});
  assert.equal(f.router.state.inputs.q.state,'semantic-pending');
  const retrying=()=>f.router.activityList().map(activity=>[activity.kind,activity.id]);
  // 1. The lane answers after the deadline: then a freeze, and the drain.
  let grant;slowLease=new Promise(resolve=>{grant=resolve;});phase='slow';
  const first=f.router.reviewSemanticPending();
  await waitFor(()=>leaseCalls.filter(call=>call==='acquire').length===2,'the lane is asked');
  await new Promise(resolve=>setTimeout(resolve,80));
  await f.router.freezeDispatch('release');
  assert.deepEqual(retrying(),[['classification-retry','q']],'past the deadline the drain still counts it: its lane has not answered');
  grant();slowLease=null;
  await first;
  assert.deepEqual(requests,['fail'],'the lease that came late started no request');
  assert.equal(leaseCalls.at(-1),'release','and was let go');
  assert.deepEqual(f.router.activityList(),[],'held until then');
  // 2. The request is slow: at the deadline it is canceled; then a freeze, and the drain.
  await f.router.thawDispatch('released');
  f.clock.now+=MINUTE;
  const second=f.router.reviewSemanticPending();
  await waitFor(()=>endRequest!==null,'the request is canceled at the deadline');
  await f.router.freezeDispatch('release');
  assert.deepEqual(retrying(),[['classification-retry','q']],'canceled but not yet ended: the drain still counts it');
  endRequest();
  await second;
  assert.deepEqual(f.router.activityList(),[],'let go once the request has ended and its lease is released');
  assert.equal(leaseCalls.at(-1),'release');
  assert.deepEqual([requests,usage.at(-1).outcome],[['fail','slow'],'caller-deadline'],'the canceled request is accounted to the caller\'s deadline');
  assert.deepEqual([f.router.state.semanticPending.q.attempts,f.router.state.semanticPending.q.lastFailure.class],[3,'timeout']);
});

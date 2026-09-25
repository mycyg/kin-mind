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
    active:false,backgroundTasks:0,queued:0,pendingDeliveries:0,handoffTasks:0,inputCorrelation:true};
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
    // A control is answered by its own reply's receipt, not by being a control (CR-LIFE-07).
    [{state:'accepted',route:'control'},'submitted'],[{state:'accepted',route:'control',answer:{state:'accepted',basis:'control-reply'}},'answered'],
    [{state:'accepted',route:'maintenance'},'submitted'],[{state:'failed-before-submit'},'received'],
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

test('native turns and reply receipts settle owner inputs by their original ids; a later reply never covers an earlier input (CR-LIFE-06)',async t=>{
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
  assert.deepEqual(f.router.unsettledInputs().map(input=>input.id),['first'],'a reply to a later message answers only that message');
  assert.equal(f.router.state.inputs.first.answer,undefined);
  await f.router.observe('reply-choice',{inputId:'first',state:'silent'});
  assert.equal(f.router.state.inputs.first.answer.state,'silent','Kin chose, for this input');
  // Two messages merged into one native turn are answered by its one reply, which names them both (CR2-LIFE-04).
  await f.router.dispatch({id:'third',text:'a'},async()=> 'new-turn');
  await f.router.dispatch({id:'fourth',text:'b'},async()=> 'new-turn');
  await f.router.observe('prompt-start',{taskId:null,inputVersion:null,turnFence:0,inputIds:['third','fourth']});
  await f.router.observe('reply-complete',{inputId:'fourth',answeredInputIds:['third','fourth']});
  await f.router.observe('prompt-end',{taskId:null,inputVersion:null,turnFence:0,stopReason:'end_turn'});
  assert.deepEqual([f.router.state.inputs.third.answer.state,f.router.state.inputs.third.answer.basis],['covered','answered-input-ids']);
  const summary=f.router.summary({received:[{id:'inbox-only',at:1}]});
  assert.deepEqual([summary.answered,summary.received,summary.historical,summary.frozen],[4,1,0,null]);
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

test('the first release hands over the inputs that raced the old host\'s stop, and the freeze the new router starts under (WS7)',async t=>{
  const f=fixture(t);
  const state=JSON.parse(fs.readFileSync(f.args.file,'utf8'));delete state.ledgerVersion;
  const old=(id,extra)=>({id,kind:'owner',hash:'h',at:1,...extra});
  Object.assign(state.inputs,{answeredBefore:old('answeredBefore',{state:'accepted'}),oldFailure:old('oldFailure',{state:'failed-before-submit'}),
    cut:old('cut',{state:'accepted',acceptedAt:5}),preparing:old('preparing',{state:'preparing'}),submitting:old('submitting',{state:'submitting'}),
    selected:old('selected',{state:'selected'}),raced:old('raced',{state:'failed-before-submit'}),mind:{...old('mind',{state:'preparing'}),kind:'internal'}});
  fs.writeFileSync(f.args.file,JSON.stringify(state));fs.rmSync(f.args.file+'.prev',{force:true});
  const until=f.clock.now+3600000;
  fs.writeFileSync(path.join(f.root,'release-carryover.json'),JSON.stringify({schema:1,releaseId:'r1',inputs:[{id:'cut'},{id:'preparing'},{id:'submitting'},
    {id:'selected'},{id:'raced'},{id:'lost',create:true,at:900},{id:'mind'},{id:'../x',create:true}],freeze:{reason:'kin-deploy r1',at:1000,until}}));
  const router=new MobileRouter(f.args),inputs=router.state.inputs;
  assert.equal(inputs.answeredBefore.answer.state,'legacy');
  assert.equal(inputs.oldFailure.historical.reason,'pre-ledger:failed-before-submit','what is not handed over is history, as before');
  for(const id of ['cut','preparing','submitting','selected','raced','lost'])assert.ok(inputs[id].carriedOver&&!inputs[id].historical,id);
  assert.equal(inputs.mind.carriedOver,undefined,'only the owner\'s inputs are handed over');
  assert.equal(inputs.cut.answer,undefined,'a turn the stop may have cut is not taken as answered');
  assert.deepEqual(['preparing','selected','raced','lost'].map(id=>inputs[id].state),Array(4).fill('failed-before-submit'));
  assert.ok(['raced','lost'].every(id=>inputs[id].retry&&!inputs[id].retry.exhausted),'provably unsubmitted: retried by its own id');
  assert.equal(inputs.submitting.state,'unconfirmed');
  assert.equal(inputs['../x'],undefined,'an id that is not an input id is ignored');
  assert.deepEqual([router.frozen(),router.state.freeze.by,router.state.freeze.until],[true,'kin-deploy',until],'the router starts under the release freeze');
  assert.deepEqual(router.unsettledInputs().filter(i=>i.inFlight).map(i=>i.id),[],'nothing in flight: the release can verify while frozen');
  const requeued=[],requeue=async id=>{requeued.push(id);return {state:'requeued'};};
  await router.watch({requeue});
  assert.deepEqual(requeued,[],'nothing goes out while frozen');
  await router.thawDispatch('verified');
  await router.watch({requeue});
  assert.deepEqual(requeued.sort(),['lost','preparing','raced','selected']);
  assert.equal(new MobileRouter(f.args).state.inputs.cut.historical,undefined,'the hand-over is read once, by the upgrade');
});

test('a release renews its freeze beside the state file before each start: taken up, extended, never shortened, never over another holder (WS7, CR2-OPS-04)',async t=>{
  const f=fixture(t);
  await f.router.freezeDispatch('kin-deploy r2',{ttlMs:10*60000,by:'control'});
  const handover=until=>fs.writeFileSync(path.join(f.root,'release-carryover.json'),JSON.stringify({schema:1,releaseId:'k',id:'r2',inputs:[],freeze:{reason:'kin-deploy r2',at:1000,until}}));
  // The freeze ran out while the host was down; the release renewed it before this start.
  f.clock.now+=3*3600000;
  const renewed=f.clock.now+90*60000;
  handover(renewed);
  let router=new MobileRouter(f.args);
  assert.deepEqual([router.frozen(),router.state.freeze.reason,router.state.freeze.until],[true,'kin-deploy r2',renewed],'taken up at a start, ledger or not');
  const until=router.state.freeze.until;
  handover(until-60000);
  assert.equal(new MobileRouter(f.args).state.freeze.until,until,'never shortened');
  // Lifted by the release: the same hand-over does not freeze a restarted host again; a renewed one does.
  router=new MobileRouter(f.args);await router.thawDispatch('verified');
  assert.equal(new MobileRouter(f.args).frozen(),false);
  handover(until+60000);
  assert.equal(new MobileRouter(f.args).frozen(),true);
  // Another holder's freeze is not taken over.
  router=new MobileRouter(f.args);await router.thawDispatch('done');
  await router.freezeDispatch('migration m1',{ttlMs:60*60000,migrationId:'m1'});
  handover(f.clock.now+5*3600000);
  assert.equal(new MobileRouter(f.args).state.freeze.reason,'migration m1');
  // An expired hand-over is nothing.
  router=new MobileRouter(f.args);await router.thawDispatch('done');
  handover(f.clock.now-1);
  assert.equal(new MobileRouter(f.args).frozen(),false);
});

test('a deployment\'s hold does not lift itself: past its end it is overdue and waits for an explicit thaw; every other freeze keeps its TTL (WS7, CR3-FLOW-03)',async t=>{
  const f=fixture(t);
  const handover=freeze=>fs.writeFileSync(path.join(f.root,'release-carryover.json'),JSON.stringify({schema:1,releaseId:'k',id:'r3',inputs:[],freeze}));
  // A first release held for a forward fix: its hold is taken up at the start.
  handover({reason:'kin-deploy r3',at:f.clock.now,until:f.clock.now+12*3600000,hold:true});
  let router=new MobileRouter(f.args);
  assert.deepEqual([router.frozen(),router.state.freeze.reason,router.state.freeze.hold],[true,'kin-deploy r3',true]);
  // Twelve hours and more: overdue, and still closed -- dispatch, activities, a restart.
  f.clock.now+=13*3600000;
  assert.equal(router.frozen(),true,'past its end it is overdue, not lifted');
  assert.equal(router.beginActivity({kind:'notice',id:'n1'}).ok,false);
  assert.equal(router.summary().frozen.hold,true);
  assert.equal(new MobileRouter(f.args).frozen(),true,'a restart does not lift it either');
  // A forward fix freezes under its own name, and the hold goes with it: its TTL lifts nothing.
  router=new MobileRouter(f.args);
  const fix=await router.freezeDispatch('kin-deploy runtime-cand-2',{ttlMs:90*60000,by:'control'});
  assert.deepEqual([fix.reason,fix.hold],['kin-deploy runtime-cand-2',true]);
  f.clock.now+=2*3600000;
  assert.equal(router.frozen(),true,'the fix\'s own TTL does not lift the hold');
  // Only an explicit thaw ends it: the fix's once its candidate passed, or a person's.
  await router.thawDispatch('verified');
  assert.equal(router.frozen(),false);
  assert.equal(new MobileRouter(f.args).frozen(),false,'the spent hand-over does not freeze a restart again');
  // Every other freeze keeps its TTL, asked for or handed over.
  router=new MobileRouter(f.args);
  const plain=await router.freezeDispatch('kin-deploy r4',{ttlMs:10*60000,by:'control'});
  assert.equal(plain.hold,undefined);
  f.clock.now+=11*60000;
  assert.equal(router.frozen(),false);
  handover({reason:'kin-deploy r5',at:f.clock.now,until:f.clock.now+60*60000});
  router=new MobileRouter(f.args);
  assert.deepEqual([router.frozen(),router.state.freeze.hold],[true,undefined]);
  f.clock.now+=61*60000;
  assert.equal(router.frozen(),false,'a release\'s ordinary freeze still lifts itself');
  // A held release whose services did not come up, started by hand past the hold's end: it starts held.
  handover({reason:'kin-deploy r6',at:f.clock.now,until:f.clock.now+60000,hold:true});
  f.clock.now+=2*3600000;
  router=new MobileRouter(f.args);
  assert.deepEqual([router.frozen(),router.state.freeze.reason,router.state.freeze.hold],[true,'kin-deploy r6',true],'a late start still starts held');
  await router.thawDispatch('operator');
  assert.equal(new MobileRouter(f.args).frozen(),false,'and once thawed it is not taken up again');
  // A release left pending asks the running router for its hold outright (CR3-REL-01/02): its own
  // freeze, asked for again with the hold, becomes one; asked for again without it, it stays one.
  router=new MobileRouter(f.args);
  const running=await router.freezeDispatch('kin-deploy r7',{ttlMs:90*60000,by:'control'});
  assert.equal(running.hold,undefined,'a release in progress keeps its TTL');
  const held=await router.freezeDispatch('kin-deploy r7',{ttlMs:12*3600000,by:'control',hold:true});
  assert.deepEqual([held.reason,held.at,held.hold],['kin-deploy r7',running.at,true]);
  assert.equal((await router.freezeDispatch('kin-deploy r7',{ttlMs:90*60000,by:'control'})).hold,true,'asked again without it, it stays');
  f.clock.now+=13*3600000;
  assert.deepEqual([router.frozen(),new MobileRouter(f.args).frozen()],[true,true],'past every end, and across a restart');
  router=new MobileRouter(f.args);
  await router.thawDispatch('operator');
  assert.equal(router.frozen(),false,'a thaw ends it');
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

test('a migration may ask for its freeze again while it drains; nothing changes, and it lifts after two hours',async t=>{
  const f=fixture(t);
  const first=await f.router.freezeDispatch('kin-home-migration',{migrationId:'m1'});
  assert.equal(first.until-first.at>=2*3600000-5,true,'two hours unless asked otherwise');
  const revision=f.router.state.revision;
  assert.deepEqual(await f.router.freezeDispatch('kin-home-migration',{migrationId:'m1'}),first);
  assert.equal(f.router.state.revision,revision,'a repeated freeze writes nothing');
  const release=await f.router.freezeDispatch('release',{migrationId:'deploy-7'});
  assert.equal(release.at,first.at,'another reason refreshes the freeze without restarting its clock');
  assert.deepEqual(await f.router.thawDispatch('kin-home-migration',{migrationId:'m1'}),{state:'thawed',frozenAt:first.at,migrationId:'deploy-7'});
  await f.router.freezeDispatch('kin-home-migration',{migrationId:'m2'});
  f.clock.now+=2*3600000;assert.equal(f.router.frozen(),false);
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
  assert.equal(f.router.state.inputs['owner-2'].ownerNotice.id,'kin-input-notice-'+createHash('sha256').update(JSON.stringify(['owner-2','stopped'])).digest('hex').slice(0,32),
    'the ledger names the notice by the identity its sender uses');
  assert.equal(inputSummary(f.router.state.inputs['owner-2']),'failed-notified');
  assert.equal(f.router.unsettledInputs().some(i=>i.id==='owner-2'),false);
});

test('an uncertain submission is reconciled by its original id and never re-sent under another (AD1-10)',async t=>{
  const f=fixture(t);
  for(const id of ['found','absent','unknown'])
    await assert.rejects(f.router.dispatch({id,text:id,submissionProtocol:'host-boundary-v1'},async(_,started)=>{started();throw Error('lost response');}),/reconciliation/);
  assert.deepEqual((await f.router.restoreRoutingProfile()).state,'restored','an unconfirmed input no longer holds the restart profile');
  const reconcileInput=async id=>id==='absent'?{state:'not-found',complete:true,sessionId:'synthetic'}:{state:{found:'found'}[id]??'unknown'};
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

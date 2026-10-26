import test from 'node:test';
import assert from 'node:assert/strict';
import {ParallelTasks} from '../../adapters/parallel-tasks.mjs';
import {parsePrivateReply} from '../../adapters/private-notes.mjs';

const pause=()=>new Promise(r=>setImmediate(r));
const final=text=>JSON.stringify({private_note:'合成私密札记，不能外发',presentation:{mode:'text',style:''},action:'send',bubbles:[text]});
export function fixture(options={}) {
  let clock=1000,stored;
  const state={tasks:{task:{id:'task',status:'proposed',inputVersion:1,executionEpoch:0,inputIds:['owner-1']}},inputs:{'owner-1':{text:'合成任务'}},recent:[],executionEpoch:0};
  let chain=Promise.resolve();
  const router={state,locked:fn=>{const result=chain.then(fn);chain=result.catch(()=>{});return result;},save:()=>{stored=structuredClone(state);}};
  const calls=[],sent=[],notes=[];let native={state:'running',threadId:'worker-thread',events:[]};
  const runtime=async r=>{calls.push(r);if(r.op==='start')return {state:'running',threadId:'worker-thread'};if(r.op==='cancel')return {state:'interrupted'};return structuredClone(native);};
  const ports={router,runtime,now:()=>clock,parseFinal:parsePrivateReply,saveNote:async n=>notes.push(n),
    deliver:async item=>{sent.push(item);return {state:'accepted',id:item.id};},...options};
  const host=new ParallelTasks(ports);
  return {host,ports,router,calls,sent,notes,setNative:x=>native=x,advance:ms=>clock+=ms,disk:()=>stored,
    async start(){await host.start({taskId:'task',inputVersion:1,commandId:'start-1',brief:'合成任务：读取、核对、交付；不要访问真实数据'},'owner-1');await host.tick();},
    async drain(){for(let i=0;i<5;i++){await host.tick();await pause();}}};
}

test('long task, independent chat, progress, fresh status and final use distinct receipts',async()=>{
  const f=fixture();await f.start();
  const source=f.host.caller(f.router.state.tasks.task.parallel.capability);
  // Chat is its own input/turn, not a new worker dispatch or task revision.
  const version=f.router.state.tasks.task.inputVersion;
  const chat=Promise.resolve({reply:'刚泡好茶，你呢？',inputId:'chat-2'});
  assert.equal((await chat).inputId,'chat-2');assert.equal(f.calls.filter(x=>x.op==='start').length,1);
  await f.host.publish({commandId:'finding-1',publicText:'找到两处需要一起核对的地方。'},source);
  f.setNative({state:'running',threadId:'worker-thread',events:[{seq:1,type:'tool',id:'read-1',name:'read',status:'completed',at:1200}]});
  await f.drain();
  assert.equal(f.sent[0].kind,'progress');assert.equal(f.router.state.tasks.task.status,'running');
  assert.deepEqual(f.host.snapshot('task').completed.map(x=>x.id),['read-1']);
  assert.equal(f.router.state.tasks.task.inputVersion,version);
  f.setNative({state:'completed',threadId:'worker-thread',turnId:'turn-worker',events:[],text:final('两处已核对，结果在这里。')});await f.drain();
  assert.deepEqual(f.sent.map(x=>x.kind),['progress','final']);assert.equal(f.router.state.tasks.task.status,'completed');
  assert.equal(f.notes.length,1);assert.ok(!JSON.stringify(f.sent).includes('合成私密札记'));
  assert.equal(f.host.snapshot('task').result.verification,'model-reported');
  await f.drain();assert.equal(f.sent.length,2);
});

test('same expression identity deduplicates and conflicting/hidden fields are refused',async()=>{
  const f=fixture();await f.start();const src=f.host.caller(f.router.state.tasks.task.parallel.capability);
  const a=await f.host.publish({commandId:'a',publicText:'嗯，我看到了。'},src);
  const b=await f.host.publish({commandId:'a',publicText:'嗯，我看到了。'},src);assert.equal(a.id,b.id);
  await assert.rejects(f.host.publish({commandId:'a',publicText:'改写'},src),/conflicts/);
  await assert.rejects(f.host.publish({commandId:'b',publicText:'text',private_note:'secret'},src),/explicit public/);
  await assert.rejects(f.host.publish({commandId:'c',publicText:'text',presentation:{mode:'voice',style:'secret'}},src),/style/);
  await f.drain();assert.equal(f.sent.length,1);
});

test('slow optional voice and MEME never block work; final fences their late preparations',async()=>{
  const resolvers=[];const f=fixture({prepare:item=>item.kind==='progress'?new Promise(r=>resolvers.push(r)):Promise.resolve(null)});
  await f.start();const src=f.host.caller(f.router.state.tasks.task.parallel.capability);
  await f.host.publish({commandId:'voice',publicText:'这一段有点绕。',presentation:{mode:'voice'}},src);
  await f.host.publish({commandId:'meme',meme:{meme_id:'m1',inspection_id:'i1'}},src);await f.drain();
  assert.equal(resolvers.length,2);assert.equal(f.sent.length,0);
  f.setNative({state:'completed',threadId:'worker-thread',turnId:'final',events:[],text:final('已经完成。')});await f.drain();
  assert.deepEqual(f.sent.map(x=>x.kind),['final']);resolvers.forEach(r=>r({}));await f.drain();
  assert.deepEqual(f.sent.map(x=>x.kind),['final']);
  assert.equal(Object.values(f.router.state.visibleItems).filter(i=>i.state==='dropped').length,2);
});

test('important voice preparation failure falls back to text; optional failure stays local',async()=>{
  const f=fixture({prepare:async()=>{throw Error('synthetic TTS unavailable');}});await f.start();const src=f.host.caller(f.router.state.tasks.task.parallel.capability);
  await f.host.publish({commandId:'optional',publicText:'有点绕。',presentation:{mode:'voice'}},src);
  await f.host.publish({commandId:'important',publicText:'发现输入文件缺了一页。',presentation:{mode:'voice'},important:true},src);
  await f.drain();assert.equal(f.sent.length,1);assert.equal(f.sent[0].presentation.mode,'text');assert.equal(f.router.state.tasks.task.parallel.state,'running');
});

test('cancel revokes capability and drops pending expressions; no false final',async()=>{
  const f=fixture();await f.start();const cap=f.router.state.tasks.task.parallel.capability;
  await f.host.publish({commandId:'pending',publicText:'还在核对。'},f.host.caller(cap));
  f.router.state.tasks.task.cancelRequested=true;await f.drain();
  assert.ok(f.calls.some(r=>r.op==='cancel'));assert.equal(f.sent.length,0);
  assert.equal(f.host.snapshot('task').state,'canceled');assert.throws(()=>f.host.caller(cap),/expired/);
});

test('revised goal interrupts old turn then continues owned thread with new version',async()=>{
  const f=fixture();await f.start();const old=f.host.caller(f.router.state.tasks.task.parallel.capability);
  await f.host.publish({commandId:'old',publicText:'旧计划。'},old);
  const t=f.router.state.tasks.task;t.inputVersion=2;t.inputIds.push('owner-2');f.router.state.inputs['owner-2']={text:'改成三份'};
  await f.host.tick();await f.host.tick();
  const starts=f.calls.filter(r=>r.op==='start');assert.equal(starts.length,2);assert.equal(starts[1].threadId,'worker-thread');assert.match(starts[1].input,/改成三份/);
  assert.equal(f.sent.length,0);await assert.rejects(f.host.publish({commandId:'late',publicText:'迟到旧结果'},old),/stale/);
});

test('restart keeps accepted/unknown delivery identities and does not replay worker',async()=>{
  let sends=0,lookups=0;
  const f=fixture({deliver:async(item,p,g,{lookupOnly})=>{if(lookupOnly){lookups++;return {state:'accepted'};}sends++;return {state:'unknown'};}});
  await f.start();await f.host.publish({commandId:'x',publicText:'发现一处变化。'},f.host.caller(f.router.state.tasks.task.parallel.capability));
  await f.host.tick();await pause();await f.host.tick();
  const restarted=new ParallelTasks(f.ports);await restarted.recover();await restarted.tick();
  assert.equal(sends,1);assert.equal(lookups,1);assert.equal(f.calls.filter(r=>r.op==='start').length,1);
  assert.equal(restarted.snapshot('task').state,'blocked');
});

test('duplicate tool events never inflate completed steps; timeout drops optional message',async()=>{
  const f=fixture();await f.start();f.setNative({state:'running',threadId:'worker-thread',events:[{seq:1,type:'tool',id:'x',name:'read',status:'completed',at:1001}]});
  await f.drain();assert.equal(f.host.snapshot('task').completed.length,1);
  await f.host.publish({commandId:'late',publicText:'随口话'},f.host.caller(f.router.state.tasks.task.parallel.capability));
  f.advance(60001);await f.drain();assert.equal(f.sent.length,0);
});

test('freeze defers new execution, concurrent pumps start once, and unknown notification is not replayed',async()=>{
  let frozen=true,resolveStart,starts=0,notifications=0;
  const f=fixture({runtime:async r=>{if(r.op==='start'){starts++;return new Promise(resolve=>resolveStart=resolve);}return {state:'running',events:[]};},onChange:async()=>{notifications++;}});
  f.router.frozen=()=>frozen;
  await f.host.start({taskId:'task',inputVersion:1,commandId:'start',brief:'synthetic'},'owner-1');
  await f.host.tick();assert.equal(starts,0);
  frozen=false;const t=f.router.state.tasks.task;
  const first=f.host.runTask(t),second=f.host.runTask(t);await pause();assert.equal(starts,1);
  resolveStart({state:'running',threadId:'worker'});await Promise.all([first,second]);
  t.parallel.notification='submitting';t.parallel.notificationKey='blocked:unsettled';
  await f.host.recover();await f.host.tick();assert.equal(t.parallel.notification,'unknown');assert.equal(notifications,0);
});

test('important media has a deadline and falls back to text; failure delivery reaches main without replay',async()=>{
  const seen=[];let sends=0;
  const f=fixture({prepareTimeoutMs:5,prepare:()=>new Promise(()=>{}),onChange:async s=>{seen.push(s);return {state:'accepted'};},
    deliver:async()=>{sends++;return {state:'rejected'};}});
  await f.start();f.setNative({state:'completed',text:final('核对结果。'),events:[]});await f.host.tick();
  await new Promise(r=>setTimeout(r,12));await f.drain();await Promise.all([...f.host.notifying.values()]);
  const item=Object.values(f.router.state.visibleItems)[0];assert.equal(item.presentation.mode,'text');assert.equal(item.state,'failed');
  assert.equal(seen.length,1);assert.ok(seen[0].blockers.includes('final-delivery-unconfirmed'));
  assert.equal(f.router.state.tasks.task.status,'running');await f.drain();assert.equal(sends,1);
});

test('unconfirmed tool cancellation remains pending; new goal never starts before actual stop',async()=>{
  const f=fixture();await f.start();let confirmed=false;
  f.host.runtime=async r=>{f.calls.push(r);return r.op==='cancel'?{state:confirmed?'interrupted':'running'}:{state:'running',events:[]};};
  const t=f.router.state.tasks.task;t.inputVersion=2;
  await f.drain();assert.equal(f.host.snapshot('task').state,'cancel_requested');
  assert.equal(f.host.snapshot('task').inputVersion,1);assert.equal(f.host.snapshot('task').requestedInputVersion,2);
  assert.equal(f.calls.filter(x=>x.op==='start').length,1);
  confirmed=true;await f.host.tick();await f.host.tick();assert.equal(f.calls.filter(x=>x.op==='start').length,2);
});

test('canceling an admitted frozen task needs no nonexistent native stop receipt',async()=>{
  const f=fixture();f.router.frozen=()=>true;
  await f.host.start({taskId:'task',inputVersion:1,commandId:'waiting',brief:'synthetic'},'owner-1');
  f.router.state.tasks.task.cancelRequested=true;await f.host.tick();
  assert.equal(f.host.snapshot('task').state,'canceled');assert.equal(f.calls.length,0);
});

test('a confirmed failed native run can accept a revised goal without waiting for a second interruption',async()=>{
  const f=fixture();await f.start();f.setNative({state:'failed',reason:'synthetic failure',events:[]});await f.drain();
  const t=f.router.state.tasks.task;assert.equal(t.parallel.state,'failed');t.inputVersion=2;
  f.host.runtime=async r=>{f.calls.push(r);return r.op==='cancel'?{state:'failed'}:{state:'running',threadId:'worker-thread'};};
  await f.host.runTask(t);await f.host.tick();assert.equal(t.parallel.inputVersion,2);assert.equal(t.parallel.state,'running');
});

test('a force switch elsewhere does not stale the worker final; a dropped final is a blocker told once',async()=>{
  const f=fixture();await f.start();
  f.router.state.executionEpoch=1; // the main session was switched; the worker's task kept its own epoch
  f.setNative({state:'completed',threadId:'worker-thread',turnId:'t1',events:[],text:final('第一份结果。')});await f.drain();
  assert.deepEqual(f.sent.map(x=>x.kind),['final']);assert.equal(f.router.state.tasks.task.status,'completed');

  // The task itself is fenced while its final is being prepared.
  const seen=[];const g=fixture({onChange:async s=>{seen.push(s);return {state:'accepted'};},
    prepare:async item=>{if(item.kind==='final')g.router.state.tasks.task.executionEpoch=1;return null;}});await g.start();
  g.setNative({state:'completed',threadId:'worker-thread',turnId:'t2',events:[],text:final('第二份结果。')});await g.drain();
  await Promise.all([...g.host.notifying.values()]);await g.drain();
  const t=g.router.state.tasks.task,item=Object.values(g.router.state.visibleItems).find(i=>i.kind==='final');
  assert.equal(item.state,'dropped');assert.equal(t.status,'running');
  assert.equal(seen.length,1);assert.ok(seen[0].blockers.includes('final-dropped:stale-expression'));
});

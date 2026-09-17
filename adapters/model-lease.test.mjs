import test from 'node:test';
import assert from 'node:assert/strict';
import {createLeaseClient,usageRow,LANES} from './model-lease.mjs';
import {createMobileReviewer,REVIEWER_LANES} from './mobile-reviewer.mjs';

const settle=async(rounds=6)=>{for(let i=0;i<rounds;i++)await new Promise(resolve=>setImmediate(resolve));};

/** An injected clock and timer queue: nothing in these tests waits on real time. */
function clock(start=0) {
  let now=start,seq=0;const timers=new Map();
  return {
    now:()=>now,
    setTimer:(fn,ms)=>{const id=++seq;timers.set(id,{at:now+ms,fn});return id;},
    clearTimer:id=>timers.delete(id),
    pending:()=>timers.size,
    async advance(ms) {
      const target=now+ms;
      for(;;) {
        const due=[...timers.entries()].filter(([,t])=>t.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];
        if(!due)break;
        timers.delete(due[0]);now=due[1].at;due[1].fn();
        await settle();
      }
      now=target;await settle();
    },
  };
}

/** A scripted ledger. `answers` maps an operation to a queue or a single answer. */
function ledger(answers={}) {
  const seen=[];
  const request=async(route,body)=>{
    const op=route.split('/')[1];
    seen.push({op,...body});
    const scripted=answers[op];
    const answer=Array.isArray(scripted)?(scripted.length>1?scripted.shift():scripted[0]):scripted;
    if(typeof answer==='function')return answer(body);
    if(answer===undefined)throw Error('lease-service-unreachable');
    return answer;
  };
  return {request,seen,of:op=>seen.filter(s=>s.op===op)};
}

const admitted=(renewAfter=30)=>body=>({state:'admitted',lease:{id:body.id,lane:body.lane,purpose:body.purpose,
  expires_at:0,ttl_seconds:body.ttl_seconds,renew_after_seconds:renewAfter},capacity:{limit:4,source:'configured',held:1}});
const answer=(content,usage)=>Response.json({id:'synthetic-request',model:'deepseek-flash',...(usage?{usage}:{}),
  content:[{type:'tool_use',name:content,input:content==='route_message'?{route:'chat',reason:'casual',recall:{mode:'light',query:'q',reason:'r'}}
    :content==='review_mobile_health'?{status:'healthy',findings:[]}
    :{disposition:'keep',reason:'Unfinished work remains',evidenceIds:['owner-input'],remaining:['A result is pending'],discardDraftIds:[]}}]});

test('an unreachable ledger never stops the owner: classification proceeds and says so in its usage row',async()=>{
  const usage=[],c=clock();
  // Every lease call fails: no script at all, so the transport throws.
  const lease=createLeaseClient({request:ledger().request,...c});
  const reviewer=createMobileReviewer({key:'synthetic',lease,onUsage:row=>usage.push(row),
    fetchImpl:async()=>answer('route_message',{input_tokens:12})});
  assert.equal((await reviewer.classify({text:'hello',timeoutMs:1000})).route,'chat');
  assert.equal(usage.length,1);
  assert.equal(usage[0].lane,'foreground');
  assert.equal(usage[0].leaseState,'degraded');
  assert.equal(usage[0].leaseReason,'lease-service-unreachable');
  assert.equal(usage[0].usageStatus,'reported');
});

test('the work lock review asks for the reserved user-work lane, never the background pool',async()=>{
  const c=clock(),book=ledger({acquire:[admitted()],renew:[{state:'renewed',lease:{renew_after_seconds:30}}],release:[{state:'released'}]});
  const lease=createLeaseClient({request:book.request,...c});
  const reviewer=createMobileReviewer({key:'synthetic',lease,fetchImpl:async()=>answer('review_work_lock',{input_tokens:5})});
  await reviewer.reviewWork({inputs:[{id:'owner-input'}]});
  assert.equal(REVIEWER_LANES.reviewWork,'user-work');
  assert.deepEqual(book.of('acquire').map(a=>[a.lane,a.purpose]),[['user-work','mobile-work-lock-review']]);
  assert.equal(book.of('release').length,1);
  // The three verdicts never share a lane. The reply-tail decision is the routing call's own
  // question asked alone: same lane, its own purpose label.
  assert.equal(new Set(['classify','reviewWork','audit'].map(entry=>REVIEWER_LANES[entry])).size,3);
  assert.equal(REVIEWER_LANES.tail,REVIEWER_LANES.classify);
});

test('a lost user-work lease aborts the paid call and records its usage as unknown',async()=>{
  const usage=[],c=clock();
  const book=ledger({acquire:[admitted()],renew:[{state:'lost',id:'x'}],release:[{state:'lost'}]});
  const lease=createLeaseClient({request:book.request,...c});
  const reviewer=createMobileReviewer({key:'synthetic',lease,onUsage:row=>usage.push(row),
    fetchImpl:(url,options)=>new Promise((resolve,reject)=>{
      options.signal.addEventListener('abort',()=>reject(Error('aborted')),{once:true});
    })});
  const outcome=reviewer.reviewWork({inputs:[{id:'owner-input'}]}).then(()=>null,error=>error);
  await settle();
  await c.advance(30000);
  assert.match((await outcome)?.message,/aborted/);
  assert.equal(usage.length,1);
  assert.equal(usage[0].usageStatus,'unknown');
  assert.equal(usage[0].usage,null);
  assert.equal(usage[0].outcome,'lease-lost');
  assert.equal(usage[0].leaseLost,true);
});

test('a lost foreground lease is only recorded: the owner keeps the answer already paid for',async()=>{
  const usage=[],c=clock();
  const notes=[];
  const book=ledger({acquire:[admitted()],renew:[{state:'lost',id:'x'}],release:[{state:'lost'}]});
  const lease=createLeaseClient({request:book.request,note:n=>notes.push(n),...c});
  let release;
  const reviewer=createMobileReviewer({key:'synthetic',lease,onUsage:row=>usage.push(row),
    fetchImpl:(url,options)=>new Promise(resolve=>{release=()=>resolve(answer('route_message',{input_tokens:3}));
      options.signal.addEventListener('abort',()=>resolve(answer('route_message',{input_tokens:3})),{once:true});})});
  const pending=reviewer.classify({text:'hello',timeoutMs:1000});
  await settle();
  await c.advance(30000);
  release();
  assert.equal((await pending).route,'chat');
  assert.equal(usage[0].leaseLost,true);
  assert.equal(usage[0].usageStatus,'reported');
  assert.equal(notes.filter(n=>n.event==='model-lease-lost').length,1);
});

test('renewal follows the cadence the ledger returns, and releasing stops it',async()=>{
  const c=clock();
  const book=ledger({acquire:[admitted(30)],renew:[{state:'renewed',lease:{renew_after_seconds:12}}],release:[{state:'released'}]});
  const lease=createLeaseClient({request:book.request,...c});
  const held=await lease.acquire({lane:'background',purpose:'synthetic-background'});
  assert.equal(held.state,'admitted');
  await c.advance(29000);assert.equal(book.of('renew').length,0);
  await c.advance(1000);assert.equal(book.of('renew').length,1);
  await c.advance(11000);assert.equal(book.of('renew').length,1);
  await c.advance(1000);assert.equal(book.of('renew').length,2);
  await held.release();
  await c.advance(600000);
  assert.equal(book.of('renew').length,2);
  assert.equal(c.pending(),0);
});

test('a waiting background lane retries after the time the ledger asks for, inside its own budget',async()=>{
  const c=clock();
  const script=[{state:'wait',reason:'deepseek-background-capacity',retry_after_seconds:30,capacity:{limit:4,held:4}},
    {state:'wait',reason:'deepseek-background-capacity',retry_after_seconds:30,capacity:{limit:4,held:4}},admitted()];
  const book=ledger({acquire:script,release:[{state:'released'}]});
  const lease=createLeaseClient({request:book.request,...c});
  const pending=lease.acquire({lane:'background',purpose:'synthetic-background',budgetMs:120000});
  await settle();assert.equal(book.of('acquire').length,1);
  await c.advance(30000);assert.equal(book.of('acquire').length,2);
  await c.advance(30000);
  const held=await pending;
  assert.equal(held.state,'admitted');
  assert.equal(book.of('acquire').length,3);
  await held.release();
});

test('a lane that stays full past the budget is refused, and foreground is never asked to wait',async()=>{
  const c=clock();
  const waiting={state:'wait',reason:'deepseek-background-capacity',retry_after_seconds:30,capacity:{limit:4,held:4}};
  const book=ledger({acquire:[waiting],release:[{state:'released'}]});
  const lease=createLeaseClient({request:book.request,...c});
  const full=await lease.acquire({lane:'background',purpose:'synthetic-background'});
  assert.equal(full.state,'wait');assert.equal(full.proceed,false);assert.equal(full.retryAfterSeconds,30);
  const front=await lease.acquire({lane:'foreground',purpose:'synthetic-foreground'});
  assert.equal(front.proceed,true);
  assert.equal(book.of('acquire').length,2);
});

test('switched-off lanes carry on exactly as before: no lease is held, renewed or released',async()=>{
  const usage=[],c=clock();
  const book=ledger({acquire:[{state:'disabled'}]});
  const lease=createLeaseClient({request:book.request,...c});
  const reviewer=createMobileReviewer({key:'synthetic',lease,onUsage:row=>usage.push(row),
    fetchImpl:async()=>answer('review_mobile_health',{output_tokens:9})});
  assert.equal((await reviewer.audit({healthy:true})).status,'healthy');
  assert.deepEqual(book.seen.map(s=>s.op),['acquire']);
  assert.equal(c.pending(),0);
  assert.equal(usage[0].leaseState,'disabled');
  assert.equal(usage[0].usageStatus,'reported');
});

test('a lease answer nobody can read is a degraded ledger, not an admission',async()=>{
  const c=clock();
  const book=ledger({acquire:[{error:'ValueError'}]});
  const lease=createLeaseClient({request:book.request,...c});
  const held=await lease.acquire({lane:'background',purpose:'synthetic-background'});
  assert.equal(held.state,'degraded');
  assert.equal(held.proceed,false);
  assert.equal(held.reason,'lease-service-unreadable');
  await assert.rejects(lease.acquire({lane:'nowhere',purpose:'synthetic'}),/unknown-model-lane/);
  await assert.rejects(lease.acquire({lane:'background',purpose:'not a label'}),/invalid-lease-purpose/);
  assert.deepEqual(LANES,['foreground','user-work','background']);
});

test('unknown usage is written as unknown and never survives as a dropped key or a zero',()=>{
  for(const value of [undefined,null,{}]) {
    const row=usageRow({purpose:'synthetic',usage:value});
    assert.equal(row.usageStatus,'unknown');
    assert.equal(row.usage,null);
    assert.ok(JSON.stringify(row).includes('"usage":null'));
    assert.ok(!JSON.stringify(row).includes('"usage":0'));
  }
  assert.equal(usageRow({usage:{input_tokens:0}}).usageStatus,'reported');
});

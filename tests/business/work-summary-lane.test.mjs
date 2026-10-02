import test from 'node:test';
import assert from 'node:assert/strict';
import {createMobileReviewer} from '../../adapters/mobile-reviewer.mjs';
import {createLeaseClient} from '../../adapters/model-lease.mjs';

// An open task owns the foreground lease. Its facts summary must be able to run
// without admitting unrelated maintenance or declaring the task completed.
function fixture({occupied=false}={}) {
  let calls=0;const releases=[],acquires=[];
  const lease=createLeaseClient({request:async(route,body)=>{
    if(route.endsWith('/release')){releases.push(body.id);return {state:'released'};}
    if(route.endsWith('/acquire')) {
      acquires.push(body);
      if(body.lane==='background')return {state:'wait',reason:'deepseek-foreground-priority'};
      if(body.lane==='user-work'&&occupied)return {state:'wait',reason:'deepseek-user-work-capacity'};
      return {state:'admitted',lease:{id:body.id,ttl_seconds:90,expires_at:Date.now()/1000+90}};
    }
    throw Error('unexpected operation');
  }});
  const reviewer=createMobileReviewer({key:'fixture',lease,fetchImpl:async()=>{
    calls++;return {ok:true,json:async()=>({id:'summary-fixture',model:'deepseek-flash',stop_reason:'tool_use',usage:{output_tokens:20},content:[{type:'tool_use',name:'summarize_open_work',input:{summary:'One delivered result; task still open.',delivered:['result'],open:[],unsent:[],evidenceIds:['output-1']}}]})};
  }});
  return {reviewer,acquires,releases,calls:()=>calls};
}

test('open foreground work does not starve its own facts summary; ordinary audit still yields',async()=>{
  const f=fixture();const result=await f.reviewer.summarizeWork({outputs:[{id:'output-1'}]});
  assert.equal(result.summary.summary,'One delivered result; task still open.');
  assert.equal('outcome' in result.summary,false);
  assert.equal(f.calls(),1);assert.equal(f.releases.length,1);
  await assert.rejects(f.reviewer.audit({}),e=>e.leaseSkipped===true);
  assert.equal(f.calls(),1);
});

test('a second work summary waits for the reserved slot without making a provider request',async()=>{
  const f=fixture({occupied:true});
  await assert.rejects(f.reviewer.summarizeWork({}),e=>e.leaseSkipped===true);
  assert.equal(f.calls(),0);
  assert.equal(f.acquires[0].lane,'user-work');
});

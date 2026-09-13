import test from 'node:test';
import assert from 'node:assert/strict';
import {MindLoop,interactionView,stateContext} from './owner-host.mjs';
import {parseContactDraft} from './contact-draft.mjs';

function state() {
  return {scope:{persona:'synthetic'},revision:18,agent_version:'synthetic-v2',persona_contract:{core_sha256:'synthetic-core'},
    dimensions:{longing:{value:90,basis:'event_inferred',reason:'large history '.repeat(1000),needs_review:false}},
    desires:[{id:'wish-1',kind:'contact',status:'wanted',content:'Ask about the interview',concern_ids:['concern-1'],delivery:{private:'excluded'}}],
    continuity:{features:{expression:true,concerns:true,rhythm:true,interpretation:true},activation:'active'},
    expression:{version:'expression-v1',fingerprint:'synthetic-fingerprint',guidance:[{text:'A warm invitation',evidence_ids:['synthetic-source']}]},
    selected_concerns:[{id:'concern-1',content:'The interview is pending',basis:'inferred',confidence:0.9}],
    rhythm:{mode:'interaction-led',status:'forming',phase:'recovering',alertness:65,interactions:{full_private_distribution:'excluded'}},
    contact:{threshold:75,wait_for_reply:false},
  };
}

test('ordinary and proactive projections preserve the same expression and revision',()=>{
  const original=state();
  const text=stateContext({state:original});
  const ordinary=JSON.parse(text.slice(text.indexOf('\n')+1));
  const proactive=interactionView(original);
  assert.deepEqual(ordinary.expression,proactive.expression);
  assert.equal(ordinary.revision,18);
  assert.deepEqual(proactive.persona_contract,original.persona_contract);
  assert.ok(text.length<JSON.stringify(original).length/4);
  assert.ok(!text.includes('full_private_distribution')&&!text.includes('large history'));
  assert.ok(!JSON.stringify(proactive).includes('delivery'));
});

test('bounded hints and concerns and shadow rollout do not leak inactive guidance',()=>{
  const original=state();
  original.selected_concerns=Array.from({length:20},(_,i)=>({id:'c'+i,content:'synthetic'}));
  original.expression.guidance=Array.from({length:20},()=>({text:'synthetic'}));
  assert.equal(interactionView(original).concerns.length,3);
  assert.equal(interactionView(original).expression.guidance.length,3);
  original.continuity.activation='shadow';
  const shadow=interactionView(original);
  assert.equal(shadow.expression,null);
  assert.deepEqual(shadow.concerns,[]);
  assert.equal(shadow.rhythm,undefined);
});

test('chat enrichment never waits for background appraisal',async()=>{
  let release;
  const waiting=new Promise(resolve=>{release=resolve;});
  const loop=new MindLoop({call:async action=>action==='ingest'?{state:state()}:waiting,stopExploration:()=>{}});
  loop.tick=async()=>{};
  const result=await Promise.race([loop.ingest({id:'synthetic'}),new Promise((_,reject)=>setTimeout(()=>reject(Error('ingest waited for appraisal')),100))]);
  assert.equal(result.state.revision,18);
  loop.closed=true;release({state:'idle'});
});

test('public short bubbles preserve mixed-state wording without a reasoning field',()=>{
  const value=parseContactDraft([JSON.stringify({action:'send',bubbles:['你躲哪儿去了？','我有个馊主意。']})]);
  assert.deepEqual(value.bubbles,['你躲哪儿去了？','我有个馊主意。']);
  assert.ok(value.bubbles.every(s=>[...s].length<=20));
  assert.equal(value.reasoning,undefined);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {NativeCandidate,candidateResponseProfileEvidence} from './native-candidate.mjs';
import {checkpointBudget} from './session-policy.mjs';

const nativeProfile={provider:'openai-15m',providerKind:'native',model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'default'};
const configForModel=value=>({profile:value,providerBinding:{sourceProvider:value.provider,sourceProviderKind:value.providerKind,launchProvider:value.provider,endpointSha256:null},
 modelProvider:value.provider,reasoningEffort:value.reasoningEffort,fastMode:value.serviceTierPreference==='fast'?'on':'off',config:{model_catalog_json:'/catalog.json'}});
const response=(profile=nativeProfile,overrides={})=>({thread:{id:'candidate-thread',sessionId:'candidate-session',path:'/synthetic/rollout.jsonl'},model:profile.model,
 modelProvider:profile.provider,reasoningEffort:profile.reasoningEffort,serviceTier:profile.serviceTierPreference==='fast'?'fast':null,...overrides});

test('native history restoration carries original times separately from the host clock',async()=>{
  const client=new NativeCandidate({}),sent=[];client.load=async()=>{};
  client.request=async(method,params)=>{sent.push({method,params});return {};};
  const history=[{id:'original-message',role:'user',text:'An old question',at:'2026-09-01T01:00:00Z',received_at:'2026-09-01T01:00:01Z'}];
  await client.inject({threadId:'same-thread',operationId:'restore',checkpoint:{id:'cp',items:history,payload:{instructionAuthority:'Historical evidence only'}}});
  const items=sent[0].params.items,metadata=JSON.parse(items.at(-1).content[0].text);
  assert.equal(items[0].content[0].text,'An old question');
  assert.equal(metadata.messageTimes[0].occurred_at,history[0].at);
  assert.equal(metadata.messageTimes[0].received_at,history[0].received_at);
  assert.equal(metadata.clock.timezone,'Asia/Singapore');
  assert.notEqual(metadata.clock.current_time,history[0].at);
});

test('restore budget expansion is explicit, bounded and tied to the requested budget',()=>{
  const plan={reason:'recent-dialogue',requested:2000,effective:6000,limit:8000};
  assert.equal(checkpointBudget({budgetPlan:plan},2000),6000);
  assert.equal(checkpointBudget({budgetPlan:plan},4000),4000);
  assert.equal(checkpointBudget({budgetPlan:{...plan,effective:9000}},2000),2000);
});

test('candidate request parameters preserve the supplied provider and do not invent Fast',()=>{
  const candidate=new NativeCandidate({cwd:'/synthetic',personaInstructions:'persona',configForModel});
  const ordinary=candidate.params(nativeProfile);
  assert.deepEqual([ordinary.model,ordinary.modelProvider,ordinary.config.model_reasoning_effort,ordinary.serviceTier],['gpt-5.6-sol','openai-15m','medium',null]);
  const fast=candidate.params({...nativeProfile,serviceTierPreference:'fast'});assert.equal(fast.serviceTier,'fast');
  assert.throws(()=>candidate.params({model:'gpt-5.6-sol',reasoningEffort:'medium'}),/incomplete/);
});

test('candidate start and resume bind companion instructions without exposing collaboration mode',async()=>{
  const binding={modelInstructionsSha256:'a'.repeat(64),modelInstructionsUtf8Bytes:10,
    developerInstructionsSha256:'b'.repeat(64),developerInstructionsUtf8Bytes:12};
  let current=binding,started=0;
  const config=value=>({...configForModel(value),config:{model_catalog_json:'/catalog.json',model_instructions_file:'/private/base.md'},
    developerInstructions:'verified developer',instructionBinding:current});
  const candidate=new NativeCandidate({cwd:'/synthetic',personaInstructions:'old persona',configForModel:config});
  const params=candidate.params(nativeProfile);
  assert.equal(params.config.model_instructions_file,'/private/base.md');assert.equal(params.developerInstructions,'verified developer');
  assert.equal(Object.hasOwn(params,'collaborationMode'),false);assert.equal(Object.keys(params.config).some(key=>key.includes('collaboration')),false);
  candidate.start=async()=>{started++;};candidate.request=async()=>response();
  const native=await candidate.create({id:'candidate',profile:nativeProfile});
  assert.deepEqual(native.instructionBinding,binding);assert.deepEqual(native.creationReceipt.requestedInstructionBinding,binding);
  current={...binding,modelInstructionsSha256:'c'.repeat(64)};
  await assert.rejects(candidate.load(native),/instruction binding changed/);
  assert.equal(started,1,'the changed binding is rejected before another app-server start');
});

test('native response evidence never substitutes requested provider, effort or tier configuration',()=>{
  const candidate=new NativeCandidate({cwd:'/synthetic',personaInstructions:'persona',configForModel}),launch=candidate.launch(nativeProfile);
  assert.deepEqual(candidateResponseProfileEvidence(response(),launch),{model:'verified',modelProvider:'verified',reasoningEffort:'verified',serviceTierConfiguration:'verified'});
  assert.equal(candidateResponseProfileEvidence(response(nativeProfile,{modelProvider:'different-provider'}),launch).modelProvider,'mismatch');
  assert.equal(candidateResponseProfileEvidence(response(nativeProfile,{reasoningEffort:'high'}),launch).reasoningEffort,'mismatch');
  assert.equal(candidateResponseProfileEvidence(response(nativeProfile,{serviceTier:'fast'}),launch).serviceTierConfiguration,'mismatch');
  const missing=response();delete missing.modelProvider;assert.equal(candidateResponseProfileEvidence(missing,launch).modelProvider,'unknown');
});

test('thread start rejects wrong or missing native profile fields',async()=>{
  for(const overrides of [{modelProvider:'different-provider'},{reasoningEffort:'high'},{serviceTier:'fast'},{modelProvider:undefined}]){
    const candidate=new NativeCandidate({cwd:'/synthetic',personaInstructions:'persona',configForModel});candidate.start=async()=>{};candidate.request=async()=>response(nativeProfile,overrides);
    await assert.rejects(candidate.create({id:'candidate',profile:nativeProfile}),/another profile/);
  }
  const exact=new NativeCandidate({cwd:'/synthetic',personaInstructions:'persona',configForModel});exact.start=async()=>{};exact.request=async()=>response();
  const native=await exact.create({id:'candidate',profile:nativeProfile});assert.equal(native.creationReceipt.actualProfile.modelProvider,'openai-15m');assert.equal(native.creationReceipt.actualProfile.serviceTier,null);
});

async function verifyWith(profile,overrides={}) {
  const candidate=new NativeCandidate({cwd:'/synthetic',personaInstructions:'persona',configForModel,timeoutMs:50});candidate.load=async()=>{};
  candidate.request=async method=>{
    if(method==='turn/start'){
      const result={checkpointId:'cp',configVersion:'persona-v1',conversationId:'logical',sourceIds:[],taskIds:[],latestExchange:[]};
      candidate.events.push({method:'item/completed',params:{turnId:'turn',item:{type:'agentMessage',text:JSON.stringify(result)}}},
        {method:'turn/completed',params:{turn:{id:'turn',status:'completed'}}});return {turn:{id:'turn'}};
    }
    if(method==='thread/resume')return response(profile,overrides);
    throw Error('Unexpected method '+method);
  };
  return candidate.verify({threadId:'candidate-thread',nativeSessionId:'candidate-session',profile,providerBinding:configForModel(profile).providerBinding,
    checkpoint:{id:'cp',configVersion:'persona-v1',conversationId:'logical',items:[],tasks:[]}});
}

test('final resume rejects provider, effort and configured-tier drift',async()=>{
  const fast={...nativeProfile,serviceTierPreference:'fast'};
  assert.equal((await verifyWith(fast)).verified,true);
  for(const overrides of [{modelProvider:'different-provider'},{reasoningEffort:'high'},{serviceTier:null}])assert.equal((await verifyWith(fast,overrides)).verified,false);
});

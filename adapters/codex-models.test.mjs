import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeModelCatalog,resolveModelProfile,runtimeProfile,profileMatches,switchCodexModel} from './codex-models.mjs';

const catalog=[
  {id:'deepseek-flash',providerId:'openai-15m',providerKind:'gateway',supported_reasoning_levels:[{effort:'high'}],default_reasoning_level:'high',supported_service_tiers:['default']},
  {id:'gpt-5.6-sol',providerId:'custom-gateway',providerKind:'native',supported_reasoning_levels:[{effort:'low'},{effort:'medium'},{effort:'high'},{effort:'xhigh'},{effort:'max'}],default_reasoning_level:'medium',additional_speed_tiers:['fast']},
  {id:'gpt-6-astra',display_name:'GPT-6 Astra',aliases:['ASTRA-6'],providerId:'custom-gateway',providerKind:'native',reasoningEfforts:['medium','high'],defaultReasoningEffort:'medium',serviceTiers:[{id:'default'},{id:'priority',name:'Fast'}]},
];

test('the live catalog keeps exact capabilities and normalizes priority as a Fast preference',()=>{
  const models=normalizeModelCatalog({models:catalog}),sol=models.find(model=>model.id==='gpt-5.6-sol'),astra=models.find(model=>model.id==='gpt-6-astra');
  assert.deepEqual(sol.serviceTiers,['fast'],'additional_speed_tiers is authoritative even without service_tiers');
  assert.deepEqual(astra.serviceTiers,['default','fast']);
  assert.deepEqual(resolveModelProfile({model:'ASTRA-6',reasoningEffort:'medium',serviceTierPreference:'fast'},models),
    {provider:'custom-gateway',providerKind:'native',model:'gpt-6-astra',reasoningEffort:'medium',serviceTierPreference:'fast'});
});

test('catalog resolution never substitutes a nearby model, effort or unknown optional tier',()=>{
  assert.throws(()=>resolveModelProfile({model:'gpt-6-astra',reasoningEffort:'max',serviceTierPreference:'default'},catalog),/Unsupported reasoning effort/);
  assert.throws(()=>resolveModelProfile({model:'gpt-7-imaginary',reasoningEffort:'medium',serviceTierPreference:'default'},catalog),/Unsupported mobile model/);
  assert.throws(()=>resolveModelProfile({model:'deepseek-flash',reasoningEffort:'high',serviceTierPreference:'fast'},catalog),/Unsupported service tier/);
  assert.throws(()=>resolveModelProfile({model:'gpt-5.6-sol',reasoningEffort:'__unsupported__',serviceTierPreference:'fast'},catalog),/Unsupported reasoning effort/);
  assert.throws(()=>resolveModelProfile({provider:'different-provider',providerKind:'native',model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'},catalog),/Unsupported model provider/);
});

test('configured Fast preference stays distinct from the actual response service tier',()=>{
  const runtime={model:'gpt-5.6-sol',modelProvider:'custom-gateway',providerOverride:false,reasoningEffort:'medium',fastMode:'on',serviceTier:'default',serviceTierVerified:true};
  const profile=runtimeProfile(runtime);
  assert.deepEqual([profile.serviceTierPreference,profile.serviceTier,profile.serviceTierVerified],['fast','default',true]);
  assert.equal(profileMatches(runtime,{model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'}),true,'configured preference matches');
  assert.equal(profileMatches(runtime,{model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'},{actualServiceTier:true}),false,'actual default is never relabeled Fast');
  assert.equal(profileMatches({model:'gpt-5.6-sol'},{model:'gpt-5.6-sol'}),true,'legacy model-only checks remain model-only');
  assert.equal(profileMatches({model:'gpt-5.6-sol',fastMode:'on'},{model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'}),false,'missing effort is not verified');
  assert.equal(profileMatches({model:'gpt-5.6-sol',reasoningEffort:'medium',fastMode:'on'},{model:'gpt-5.6-sol',provider:'custom-gateway',reasoningEffort:'medium',serviceTierPreference:'fast'}),false,'missing provider is not verified');
  assert.equal(profileMatches({model:'gpt-5.6-sol',modelProvider:'custom-gateway',reasoningEffort:'medium'},{model:'gpt-5.6-sol',provider:'custom-gateway',reasoningEffort:'medium',serviceTierPreference:'fast'}),false,'missing Fast configuration is not verified');
});

test('a confirmed force boundary bypasses stale coordinator activity but never native identity or idle checks',async()=>{
  const calls=[];
  const actual={known:true,sessionId:'s',threadId:'s',nativeSessionId:'s',nativeStatus:'idle',active:true,backgroundTasks:1,model:'deepseek-flash',modelProvider:'openai-15m',providerOverride:true,providerBaseUrl:'http://gateway.invalid',reasoningEffort:'high',fastMode:'off'};
  const connection={
    async extMethod(method,args){calls.push([method,args]);if(method==='_kin/runtime')return{...actual};if(method==='providers/disable'){actual.providerOverride=false;actual.modelProvider='custom-gateway';delete actual.providerBaseUrl;}return{};},
    async setSessionConfigOption({configId,value}){calls.push([configId,value]);if(configId==='model')actual.model=value;if(configId==='reasoning_effort')actual.reasoningEffort=value;if(configId==='fast-mode')actual.fastMode=value;return{};},
  };
  const profile={model:'gpt-5.6-sol',reasoningEffort:'medium',serviceTierPreference:'fast'};
  await assert.rejects(switchCodexModel({connection,sessionId:'s',profile,gateway:{baseUrl:'http://gateway.invalid',token:'x'},modelCatalog:catalog}),/active or unconfirmed/);
  const switched=await switchCodexModel({connection,sessionId:'s',profile,gateway:{baseUrl:'http://gateway.invalid',token:'x'},modelCatalog:catalog,forceBoundary:{receipt:{state:'interrupted'}}});
  assert.deepEqual([switched.model,switched.reasoningEffort,switched.fastMode,switched.serviceTier,switched.serviceTierVerified],['gpt-5.6-sol','medium','on',null,false]);
  assert.ok(calls.some(([name,value])=>name==='fast-mode'&&value==='on'));
  actual.threadId='other';
  await assert.rejects(switchCodexModel({connection,sessionId:'s',profile,gateway:{baseUrl:'http://gateway.invalid',token:'x'},modelCatalog:catalog,forceBoundary:{receipt:{state:'idle'}}}),/active or unconfirmed/);
  actual.threadId='s';actual.nativeStatus='busy';
  await assert.rejects(switchCodexModel({connection,sessionId:'s',profile,gateway:{baseUrl:'http://gateway.invalid',token:'x'},modelCatalog:catalog,forceBoundary:{receipt:{state:'idle'}}}),/active or unconfirmed/);
});

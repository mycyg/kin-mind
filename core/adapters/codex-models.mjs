import {checkModelHistory} from './provider-history.mjs';
export {checkModelHistory} from './provider-history.mjs';
const text=value=>typeof value==='string'&&value.trim()?value.trim():null;
/** One mapping for every layer: Codex 0.153+ may report a Fast thread as
 * `priority`, and the owned ACP's fast-mode patch reads it the same way. */
export const FAST_SERVICE_TIERS=Object.freeze(['fast','priority']);
export const preferenceTier=value=>{const tier=text(value);return FAST_SERVICE_TIERS.includes(tier)?'fast':['standard','default'].includes(tier)?'default':tier;};
/** A service tier counts as verified only when the runtime explicitly says so. */
export const serviceTierVerified=runtime=>Boolean(text(runtime?.serviceTier)&&runtime?.serviceTierVerified===true);
const effortList=model=>(model.reasoningEfforts??model.supportedReasoningEfforts??model.supported_reasoning_levels??[])
  .map(value=>text(typeof value==='string'?value:value?.effort)).filter(Boolean);
const tierList=model=>{
  const source=model.serviceTiers??model.supportedServiceTiers??model.supported_service_tiers;
  const additional=model.additionalSpeedTiers??model.additional_speed_tiers;
  if(!Array.isArray(source)&&!Array.isArray(additional))return null;
  return [...(Array.isArray(source)?source:[]),...(Array.isArray(additional)?additional:[])]
    .map(value=>preferenceTier(typeof value==='string'?value:value?.id??value?.tier??value?.value)).filter(Boolean);
};

/** A private host may obtain this list from app-server `model/list`, ACP config
 * choices, or a checked-in mobile catalog. Only capability facts survive this
 * boundary; descriptions, credentials and provider endpoints do not. */
export function normalizeModelCatalog(value) {
  const models=Array.isArray(value)?value:Array.isArray(value?.models)?value.models:[];
  return models.map(model=>{
    const id=text(model.id??model.slug??model.value);
    if(!id)return null;
    const tiers=tierList(model);
    return {id,
      provider:text(model.provider??model.providerId??model.modelProvider),
      providerKind:['gateway','native'].includes(model.providerKind)?model.providerKind:null,
      aliases:[...new Set([model.displayName,model.display_name,...(model.aliases??[])].map(text).filter(Boolean))],
      defaultReasoningEffort:text(model.defaultReasoningEffort??model.default_reasoning_level),
      reasoningEfforts:[...new Set(effortList(model))],
      defaultServiceTier:preferenceTier(model.defaultServiceTier??model.default_service_tier),
      // null is deliberately different from []: null means the host cannot prove
      // whether an optional tier is available.
      serviceTiers:tiers===null?null:[...new Set(tiers)]};
  }).filter(Boolean);
}

const aliasKey=value=>text(value)?.toLocaleLowerCase().replace(/[\s_]+/g,'-')??null;
/** A runtime the private host reports as its own native engine (dsh). Identity is
 * repeated in sessionId/threadId/nativeSessionId because those are three names for the
 * same actual native session. There is no provider override and no background count in
 * this shape, so each is read only when the runtime actually carries it. */
export const NATIVE_ENGINE='dsh-native';
export const nativeEngine=runtime=>text(runtime?.engine)===NATIVE_ENGINE;
/** Only an explicit boolean is a provider override; absent is unknown, never `false`. */
const providerOverride=value=>value?.providerOverride===true?true:value?.providerOverride===false?false:null;
/** The native reasoning effort: absent means the model's own default, which is not an
 * effort anyone may claim to verify; an explicit value is kept exactly as given. */
export const nativeReasoningEffort=runtime=>{
  const effort=text(runtime?.reasoningEffort);
  return effort&&effort!=='__default__'?effort:null;
};

/** Resolve only against capabilities the current host actually advertised.
 * There is no nearest-model or nearest-effort fallback. */
export function resolveModelProfile(request,catalog) {
  if(!request||typeof request!=='object')throw Error('Invalid model profile');
  const models=normalizeModelCatalog(catalog),asked=aliasKey(request.model);
  if(!asked||asked==='--unsupported--')throw Error('Unsupported mobile model');
  const matches=models.filter(model=>[model.id,...model.aliases].some(value=>aliasKey(value)===asked));
  if(matches.length!==1)throw Error(matches.length?'Ambiguous mobile model':'Unsupported mobile model');
  const model=matches[0];
  const requestedProvider=text(request.provider),requestedProviderKind=text(request.providerKind);
  if(requestedProvider&&requestedProvider!==model.provider)throw Error('Unsupported model provider');
  if(requestedProviderKind&&requestedProviderKind!==model.providerKind)throw Error('Unsupported model provider kind');
  let reasoningEffort=text(request.reasoningEffort);
  if(!reasoningEffort||reasoningEffort==='__default__')reasoningEffort=model.defaultReasoningEffort;
  if(!reasoningEffort||reasoningEffort==='__unsupported__'||!model.reasoningEfforts.includes(reasoningEffort))throw Error('Unsupported reasoning effort');
  let serviceTierPreference=preferenceTier(request.serviceTierPreference??request.serviceTier);
  if(!serviceTierPreference||serviceTierPreference==='__default__')serviceTierPreference=model.defaultServiceTier??'default';
  if(serviceTierPreference==='__unsupported__')throw Error('Unsupported service tier');
  if(serviceTierPreference!=='default') {
    if(model.serviceTiers===null)throw Error('Model service tier availability is unknown');
    if(!model.serviceTiers.includes(serviceTierPreference))throw Error('Unsupported service tier');
  }
  return {provider:model.provider,providerKind:model.providerKind,model:model.id,reasoningEffort,serviceTierPreference};
}

/** Full non-secret profile observed from the canonical native session. Actual
 * service tier is kept separate from the configured preference: `fast-mode=on`
 * is not evidence that a request was processed on Fast. */
export function runtimeProfile(runtime={}) {
  const serviceTier=text(runtime.serviceTier);
  const serviceTierPreference=preferenceTier(runtime.serviceTierPreference)??(runtime.fastMode==='on'||runtime.fastMode===true?'fast':runtime.fastMode==='off'||runtime.fastMode===false?'default':null);
  // The native engine reports its route directly: there is no provider override to read,
  // and the actual route is the native one, so its provider kind is native.
  const native=nativeEngine(runtime);
  return {provider:text(runtime.modelProvider),providerKind:native?'native':providerOverride(runtime)===true?'gateway':providerOverride(runtime)===false?'native':null,
    model:text(runtime.model),reasoningEffort:native?nativeReasoningEffort(runtime):text(runtime.reasoningEffort),serviceTier,
    serviceTierVerified:serviceTierVerified(runtime),serviceTierPreference,...(native?{engine:NATIVE_ENGINE,
      ...(Object.hasOwn(runtime,'serviceTierConfigured')?{serviceTierConfigured:runtime.serviceTierConfigured}:{})}:{})};
}

export function profileMatches(runtime,expected,{actualServiceTier=false}={}) {
  if(typeof expected==='string')expected={model:expected};
  if(!expected||runtime.model!==expected.model)return false;
  // The native engine may omit the effort: omitted or `__default__` means the model's own
  // default, so only an explicitly asked effort must appear exactly as asked.
  const native=nativeEngine(runtime);
  if(native){if(Object.hasOwn(expected,'reasoningEffort')&&expected.reasoningEffort!==undefined&&nativeReasoningEffort(runtime)!==nativeReasoningEffort(expected))return false;}
  else if(expected.reasoningEffort&&runtime.reasoningEffort!==expected.reasoningEffort)return false;
  if(expected.provider&&runtime.modelProvider!==expected.provider)return false;
  if(expected.providerKind==='gateway'&&providerOverride(runtime)!==true)return false;
  if(expected.providerKind==='native'&&!(native||providerOverride(runtime)===false))return false;
  const preference=expected.serviceTierPreference??expected.serviceTier;
  if(preference){
    if(native&&Object.hasOwn(runtime,'serviceTierConfigured')){
      // A native provider's tier is fixed at launch. Default inherits it; an
      // explicit preference must match, without claiming API confirmation.
      const configured=runtime.serviceTierConfigured;
      const matches=preference==='default'||(preference==='standard'?!configured
        :['fast','priority'].includes(preference)?configured==='priority':configured===preference);
      if(!matches)return false;
      return !actualServiceTier||preferenceTier(runtime.serviceTier)===preferenceTier(preference);
    }
    const configured=preferenceTier(runtime.serviceTierPreference)??(runtime.fastMode==='on'||runtime.fastMode===true?'fast':runtime.fastMode==='off'||runtime.fastMode===false?'default':null);
    if(configured!==preference)return false;
    if(actualServiceTier&&preferenceTier(runtime.serviceTier)!==preference)return false;
  }
  return true;
}

export function profileReady(runtime,gateway,expected=null) {
  // The native engine is ready without a stated effort: the model runs its own default,
  // which profileMatches compares only when the caller asked for one explicitly.
  if(!runtime?.known||!runtime.model||!(nativeEngine(runtime)||runtime.reasoningEffort))return false;
  if(providerOverride(runtime)===true&&runtime.providerBaseUrl!==gateway?.baseUrl)return false;
  return expected?profileMatches(runtime,expected):true;
}

/** Apply one already-resolved profile. The legacy `model`/`workFast` form remains
 * accepted for older private hosts; new callers pass `profile` and a live catalog. */
export async function switchCodexModel({connection,sessionId,profile=null,model=null,gateway,workFast,modelCatalog=null,forceBoundary=null}) {
  const before=await connection.extMethod('_kin/runtime',{sessionId});
  const forced=['interrupted','idle'].includes(forceBoundary?.receipt?.state);
  // A confirmed force boundary is the private host's proof that the native turn
  // was interrupted (or was already idle) and that hot switching is safe. The
  // coordinator can still report processing while an old sender/tool ledger is
  // settling; that state must not undo the confirmed interrupt. Identity and the
  // native stopped receipt are always rechecked. A failed native turn may remain
  // systemError after cancel; only a confirmed owner force with no running work
  // may switch that terminal state.
  if(!before.known||before.sessionId!==sessionId||before.threadId!==sessionId||before.nativeSessionId!==sessionId||!(before.nativeStatus==='idle'||forced&&before.nativeStatus==='systemError'&&before.active===false&&before.backgroundTasks===0)||(!forced&&(before.active||before.backgroundTasks)))throw Error('Native task is active or unconfirmed');
  let target;
  if(profile)target=modelCatalog?resolveModelProfile(profile,modelCatalog):{...profile};
  else {
    if(!['deepseek-flash','gpt-6-sol','gpt-5.6-sol','gpt-6-astra'].includes(model))throw Error('Unsupported mobile model');
    target={provider:model==='deepseek-flash'?(before.providerOverride?before.modelProvider:null):before.providerOverride?null:before.modelProvider,
      providerKind:model==='deepseek-flash'?'gateway':'native',model,
      reasoningEffort:model==='deepseek-flash'?(gateway.reasoningEffort??'high'):'medium',
      serviceTierPreference:model!=='deepseek-flash'&&workFast===true?'fast':'default'};
  }
  if(!target.model||!target.reasoningEffort)throw Error('Incomplete model profile');
  await checkModelHistory(target,before);
  const gatewayTarget=target.providerKind==='gateway'||(!target.providerKind&&target.model==='deepseek-flash');
  if(gatewayTarget) {
    if(!gateway?.baseUrl||!gateway?.token)throw Error('Gateway profile unavailable');
    if(before.providerOverride!==true||before.providerBaseUrl!==gateway.baseUrl)
      await connection.extMethod('providers/set',{providerId:'openai',apiType:'openai',baseUrl:gateway.baseUrl,headers:{Authorization:'Bearer '+gateway.token}});
  } else await connection.extMethod('providers/disable',{providerId:'openai'});
  await connection.setSessionConfigOption({sessionId,configId:'model',value:target.model});
  await connection.setSessionConfigOption({sessionId,configId:'reasoning_effort',value:target.reasoningEffort});
  if(target.serviceTierPreference)await connection.setSessionConfigOption({sessionId,configId:'fast-mode',value:target.serviceTierPreference==='fast'?'on':'off'});
  const actual=await connection.extMethod('_kin/runtime',{sessionId});
  actual.serviceTierPreference??=actual.fastMode==='on'||actual.fastMode===true?'fast':actual.fastMode==='off'||actual.fastMode===false?'default':null;
  actual.serviceTier??=null;actual.serviceTierVerified=serviceTierVerified(actual);
  actual.profileReady=profileReady(actual,gateway,target);
  if(!actual.known||!actual.profileReady||actual.threadId!==sessionId||actual.nativeSessionId!==sessionId)throw Error('Mobile model verification failed');
  actual.requestedProfile=target;
  return actual;
}

const text=value=>typeof value==='string'&&value.trim()?value.trim():null;
const preferenceTier=value=>{const tier=text(value);return ['priority','fast'].includes(tier)?'fast':['standard','default'].includes(tier)?'default':tier;};
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
  return {provider:text(runtime.modelProvider),providerKind:runtime.providerOverride===true?'gateway':runtime.providerOverride===false?'native':null,
    model:text(runtime.model),reasoningEffort:text(runtime.reasoningEffort),serviceTier,
    serviceTierVerified:Boolean(serviceTier&&runtime.serviceTierVerified!==false),serviceTierPreference};
}

export function profileMatches(runtime,expected,{actualServiceTier=false}={}) {
  if(typeof expected==='string')expected={model:expected};
  if(!expected||runtime.model!==expected.model)return false;
  if(expected.reasoningEffort&&runtime.reasoningEffort!==expected.reasoningEffort)return false;
  if(expected.provider&&runtime.modelProvider!==expected.provider)return false;
  if(expected.providerKind==='gateway'&&runtime.providerOverride!==true)return false;
  if(expected.providerKind==='native'&&runtime.providerOverride!==false)return false;
  const preference=expected.serviceTierPreference??expected.serviceTier;
  if(preference){
    const configured=preferenceTier(runtime.serviceTierPreference)??(runtime.fastMode==='on'||runtime.fastMode===true?'fast':runtime.fastMode==='off'||runtime.fastMode===false?'default':null);
    if(configured!==preference)return false;
    if(actualServiceTier&&preferenceTier(runtime.serviceTier)!==preference)return false;
  }
  return true;
}

export function profileReady(runtime,gateway,expected=null) {
  if(!runtime?.known||!runtime.model||!runtime.reasoningEffort)return false;
  if(runtime.providerOverride===true&&runtime.providerBaseUrl!==gateway?.baseUrl)return false;
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
  // native idle receipt are always rechecked.
  if(!before.known||before.sessionId!==sessionId||before.threadId!==sessionId||before.nativeSessionId!==sessionId||before.nativeStatus!=='idle'||(!forced&&(before.active||before.backgroundTasks)))throw Error('Native task is active or unconfirmed');
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
  const gatewayTarget=target.providerKind==='gateway'||(!target.providerKind&&target.model==='deepseek-flash');
  if(gatewayTarget) {
    if(!gateway?.baseUrl||!gateway?.token)throw Error('Gateway profile unavailable');
    await connection.extMethod('providers/set',{providerId:'openai',apiType:'openai',baseUrl:gateway.baseUrl,headers:{Authorization:'Bearer '+gateway.token}});
  } else await connection.extMethod('providers/disable',{providerId:'openai'});
  await connection.setSessionConfigOption({sessionId,configId:'model',value:target.model});
  await connection.setSessionConfigOption({sessionId,configId:'reasoning_effort',value:target.reasoningEffort});
  if(target.serviceTierPreference)await connection.setSessionConfigOption({sessionId,configId:'fast-mode',value:target.serviceTierPreference==='fast'?'on':'off'});
  const actual=await connection.extMethod('_kin/runtime',{sessionId});
  actual.serviceTierPreference??=actual.fastMode==='on'||actual.fastMode===true?'fast':actual.fastMode==='off'||actual.fastMode===false?'default':null;
  actual.serviceTier??=null;actual.serviceTierVerified=Boolean(actual.serviceTier&&actual.serviceTierVerified!==false);
  actual.profileReady=profileReady(actual,gateway,target);
  if(!actual.known||!actual.profileReady||actual.threadId!==sessionId||actual.nativeSessionId!==sessionId)throw Error('Mobile model verification failed');
  actual.requestedProfile=target;
  return actual;
}

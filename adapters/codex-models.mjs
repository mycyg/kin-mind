export function profileReady(runtime,gateway) {
  if(runtime.model==='deepseek-flash')return runtime.providerOverride===true&&runtime.providerBaseUrl===gateway.baseUrl&&runtime.reasoningEffort==='none';
  if(runtime.model==='gpt-6-astra')return runtime.providerOverride===false&&runtime.reasoningEffort==='medium';
  return false;
}
export async function switchCodexModel({connection,sessionId,model,gateway,workFast}) {
  if(!['deepseek-flash','gpt-6-astra'].includes(model))throw Error('Unsupported mobile model');
  const before=await connection.extMethod('_kin/runtime',{sessionId});
  if(!before.known||before.active||before.backgroundTasks||before.nativeStatus!=='idle')throw Error('Native task is active or unconfirmed');
  if(model==='deepseek-flash') {
    await connection.extMethod('providers/set',{providerId:'openai',apiType:'openai',baseUrl:gateway.baseUrl,headers:{Authorization:'Bearer '+gateway.token}});
  } else await connection.extMethod('providers/disable',{providerId:'openai'});
  await connection.setSessionConfigOption({sessionId,configId:'model',value:model});
  await connection.setSessionConfigOption({sessionId,configId:'reasoning_effort',value:model==='deepseek-flash'?'none':'medium'});
  if(model==='gpt-6-astra'&&typeof workFast==='boolean')await connection.setSessionConfigOption({sessionId,configId:'fast-mode',value:workFast?'on':'off'});
  const actual=await connection.extMethod('_kin/runtime',{sessionId});
  actual.profileReady=profileReady(actual,gateway);
  if(!actual.known||!actual.profileReady||actual.model!==model||actual.threadId!==sessionId||actual.nativeSessionId!==sessionId)throw Error('Mobile model verification failed');
  return actual;
}

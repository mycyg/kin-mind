export function publicMobileRuntime(state, runtime, sessionId, loaded=true) {
  const canonical=runtime.known?runtime.sessionId===sessionId&&runtime.threadId===sessionId&&runtime.nativeSessionId===(state.nativeSessionId??sessionId):null;
  const actual={model:runtime.model,provider:runtime.modelProvider,reasoningEffort:runtime.reasoningEffort,fastMode:runtime.fastMode,
    verified:Boolean(runtime.known&&runtime.profileReady!==false&&canonical),loaded,canonicalMatch:canonical,
    checkedAt:runtime.checkedAt,active:runtime.active,backgroundTasks:runtime.backgroundTasks,handoffTasks:runtime.handoffTasks};
  const lastTransition=state.transition?{...state.transition,recordKind:'historical-transition',
    matchesCurrentModel:Boolean(actual.verified&&state.transition.to===actual.model),runtimeCheckedAt:actual.checkedAt}:null;
  return {mode:state.mode,exitRequested:state.exitRequested,actual,sessionId,conversationId:state.conversationId,generation:state.generation,nativeSessionId:state.nativeSessionId??sessionId,lastTransition,
    // Compatibility alias; actual remains the only current-model evidence.
    transition:lastTransition,
    tasks:Object.values(state.tasks).filter(t=>!['completed','canceled'].includes(t.status)).map(t=>({id:t.id,status:t.status,summary:t.summary,inputVersion:t.inputVersion,completionRequested:Boolean(t.completion),handoff:t.handoff?.state})),
    requests:Object.values(state.requests).slice(-8).map(({hash,...r})=>r),
    notifications:Object.values(state.notices??{}).slice(-8).map(({text,...n})=>n),
    // What became of a damaged state file, if one was ever found: codes, counts and names only.
    ...(state.recovery?{recovery:state.recovery}:{})};
}

export function runtimeReply(view,{pending=false,switched=false}={}) {
  const actual=view.actual;
  if(!actual.verified)return '我还在核对当前模型，连接确认后再告诉你。';
  const model=actual.model==='deepseek-flash'?'DeepSeek Flash':actual.model==='gpt-6-astra'?'GPT‑6':actual.model;
  const label=model+(actual.reasoningEffort?'（'+actual.reasoningEffort+'）':'');
  if(pending)return '切换请求已经登记。当前是 '+label+'，切好后我会告诉你。';
  return (switched?'已经切到 ':'我现在用的是 ')+label+'。'+(view.tasks.length&&actual.model==='gpt-6-astra'?'当前任务继续使用 GPT‑6。':'');
}

// Keep the native command first: ACP parses only the first text block.
export function compactPrompt(pending) {
  return {...pending,prompt:[{type:'text',text:'/compact'}]};
}

import {runtimeProfile} from './codex-models.mjs';

export function publicMobileRuntime(state, runtime, sessionId, loaded=true) {
  const canonical=runtime.known?runtime.sessionId===sessionId&&runtime.threadId===sessionId&&runtime.nativeSessionId===(state.nativeSessionId??sessionId):null;
  const profile=runtimeProfile(runtime);
  const actual={model:runtime.model,provider:runtime.modelProvider,reasoningEffort:runtime.reasoningEffort,fastMode:runtime.fastMode,
    serviceTier:profile.serviceTier,serviceTierVerified:profile.serviceTierVerified,serviceTierPreference:profile.serviceTierPreference,
    verified:Boolean(runtime.known&&runtime.profileReady!==false&&canonical),loaded,canonicalMatch:canonical,
    checkedAt:runtime.checkedAt,active:runtime.active,backgroundTasks:runtime.backgroundTasks,handoffTasks:runtime.handoffTasks};
  const lastTransition=state.transition?{...state.transition,recordKind:'historical-transition',
    matchesCurrentModel:Boolean(actual.verified&&state.transition.to===actual.model),runtimeCheckedAt:actual.checkedAt}:null;
  return {mode:state.mode,requestedMode:state.requestedMode??null,exitRequested:state.exitRequested,actual,sessionId,conversationId:state.conversationId,generation:state.generation,executionEpoch:state.executionEpoch??0,nativeSessionId:state.nativeSessionId??sessionId,lastTransition,
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
  const model=actual.model==='deepseek-flash'?'DeepSeek Flash':actual.model==='gpt-6-astra'?'GPT‑6 Astra':actual.model==='gpt-5.6-sol'?'GPT‑5.6 Sol':actual.model;
  const mode=view.mode==='manual'?'手动模式':'自动模式';
  if(pending)return '切换正在处理。当前仍是 '+model+(actual.reasoningEffort?'（'+actual.reasoningEffort+'）':'')+'。';
  if(switched)return '已切换到 '+model+'（'+mode+'）。';
  return '我现在用的是 '+model+(actual.reasoningEffort?'（'+actual.reasoningEffort+'，'+mode+'）':'（'+mode+'）')+'。'+(view.tasks.length?'当前任务会继续保留。':'');
}

// Keep the native command first: ACP parses only the first text block.
export function compactPrompt(pending) {
  return {...pending,prompt:[{type:'text',text:'/compact'}]};
}

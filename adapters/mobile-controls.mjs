import {runtimeProfile} from './codex-models.mjs';

/** A task holds the work lock until it is one of these. A `proposed` task is open:
 * a request labelled work keeps its own turn on the work profile, and lapses when
 * that turn ends unless Kin takes it on. */
export const TASK_CLOSED=Object.freeze(['completed','canceled','partial','deferred','unclaimed']);
export const openTask=task=>Boolean(task)&&!TASK_CLOSED.includes(task.status);
/** Only for a model the live catalog gives no display name (AD1-15). */
const FALLBACK_NAMES={'deepseek-flash':'DeepSeek Flash','gpt-6-astra':'GPT‑6 Astra','gpt-6-sol':'GPT‑6 Sol','gpt-5.6-sol':'GPT‑5.6 Sol'};
export const displayName=(model,names=null)=>names?.[model]??FALLBACK_NAMES[model]??model;

export function publicMobileRuntime(state, runtime, sessionId, loaded=true) {
  const canonical=runtime.known?runtime.sessionId===sessionId&&runtime.threadId===sessionId&&runtime.nativeSessionId===(state.nativeSessionId??sessionId):null;
  const profile=runtimeProfile(runtime);
  const actual={model:runtime.model,provider:runtime.modelProvider,reasoningEffort:runtime.reasoningEffort,fastMode:runtime.fastMode,
    serviceTier:profile.serviceTier,serviceTierVerified:profile.serviceTierVerified,serviceTierPreference:profile.serviceTierPreference,
    verified:Boolean(runtime.known&&runtime.profileReady!==false&&canonical),loaded,canonicalMatch:canonical,
    checkedAt:runtime.checkedAt,nativeStatus:runtime.nativeStatus,active:runtime.active,backgroundTasks:runtime.backgroundTasks,handoffTasks:runtime.handoffTasks};
  const lastTransition=state.transition?{...state.transition,recordKind:'historical-transition',
    matchesCurrentModel:Boolean(actual.verified&&state.transition.to===actual.model),runtimeCheckedAt:actual.checkedAt}:null;
  return {mode:state.mode,requestedMode:state.requestedMode??null,exitRequested:state.exitRequested,actual,sessionId,conversationId:state.conversationId,generation:state.generation,executionEpoch:state.executionEpoch??0,nativeSessionId:state.nativeSessionId??sessionId,lastTransition,
    // Compatibility alias; actual remains the only current-model evidence.
    transition:lastTransition,
    tasks:Object.values(state.tasks).filter(openTask).map(t=>({id:t.id,status:t.status,summary:t.summary,inputVersion:t.inputVersion,completionRequested:Boolean(t.completion),...(t.completion?.outcome?{taskOutcome:t.completion.outcome}:{}),handoff:t.handoff?.state})),
    requests:Object.values(state.requests).slice(-8).map(({hash,sourceHash,...r})=>r),
    notifications:Object.values(state.notices??{}).slice(-8).map(({text,...n})=>n),
    // What became of a damaged state file, if one was ever found: codes, counts and names only.
    ...(state.recovery?{recovery:state.recovery}:{})};
}

export function runtimeReply(view,{pending=false,switched=false,names=null}={}) {
  const actual=view.actual;
  if(!actual.verified)return '我还在核对当前模型，连接确认后再告诉你。';
  const model=displayName(actual.model,names);
  const mode=view.mode==='manual'?'手动模式':'自动模式';
  if(pending)return '切换正在处理。当前仍是 '+model+(actual.reasoningEffort?'（'+actual.reasoningEffort+'）':'')+'。';
  if(switched)return '已切换到 '+model+(actual.reasoningEffort?' · '+actual.reasoningEffort:'')+(actual.serviceTierPreference==='fast'?' · Fast 配置已开启':'')+'（'+mode+'）。';
  return '我现在用的是 '+model+(actual.reasoningEffort?'（'+actual.reasoningEffort+'，'+mode+'）':'（'+mode+'）')+'。'+(view.tasks.length?'当前任务会继续保留。':'');
}

/** The owner's literal `/compact` as the native session must receive it (WS8 #1). ACP
 * reads only the first text block as a command and drops every other block, so the
 * command goes alone: the host shapes it after every fact it adds, it is a turn of its
 * own, and nothing joins its turn. */
export function compactPrompt(pending) {
  return {...pending,prompt:[{type:'text',text:'/compact'}],ownTurn:true,nativeCommand:'compact'};
}

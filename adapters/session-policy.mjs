/** Session health is measured per native window, never by rollout file size or
 * cumulative billing. Compression is the first pressure remedy. */
export const SESSION_DEFAULTS=Object.freeze({version:'compact-first-v1',observe:true,compact:true,prepare:false,rotate:false,
  pausedTaskHandover:false,preparePressure:0.65,criticalPressure:0.85,rotationCooldownMs:1800000,
  compactCooldownMs:1800000,outputReserve:32768,toolReserve:8192,restoreBudget:2000});

export function windowPressure(runtime,config=SESSION_DEFAULTS) {
  const window=runtime.modelContextWindow;
  const measured=runtime.lastTokenUsage?.inputTokens;
  const next=runtime.expectedInputTokens??measured;
  const output=runtime.outputReserve??config.outputReserve,tools=runtime.toolReserve??config.toolReserve;
  const known=[window,next,output,tools].every(v=>Number.isFinite(v)&&v>=0)&&window>0;
  const ratio=known?(next+output+tools)/window:null;
  return {known,effectiveWindow:window??null,inputTokens:measured??null,expectedInputTokens:next??null,outputReserve:output,toolReserve:tools,ratio,
    level:!known?'unknown':ratio>=config.criticalPressure?'critical':ratio>=config.preparePressure?'elevated':'normal'};
}

/** Advice references observations, not the model's own confidence. An error
 * must still be present after the latest completed native compaction. */
export function rotationEligibility(state,advice,now=Date.now()) {
  const compact=state.compactions.findLast(c=>c.state==='complete'&&c.generation===state.binding.generation);
  if(!compact||advice.compactionId!==compact.id)return {eligible:false,reason:'compact-first'};
  const evidence=(advice.evidenceIds??[]).map(id=>state.evidence[id]).filter(Boolean);
  const valid=evidence.filter(e=>e.generation===state.binding.generation&&e.at>compact.completedAt&&!e.needsReview&&!e.resolved&&
    ['reference-error','task-omission','repeat-share','context-degradation','sustained-context-pressure'].includes(e.kind)&&
    ['owner-statement','tool-observation','verified-replay'].includes(e.basis)&&e.sourceId&&e.revision);
  if(!valid.length)return {eligible:false,reason:'post-compaction-evidence-required'};
  if((advice.evidenceIds??[]).some(id=>!state.evidence[id]||state.evidence[id].needsReview||state.evidence[id].resolved))return {eligible:false,reason:'stale-advice-evidence'};
  const last=state.segments.findLast(s=>s.generation===state.binding.generation);
  if(last?.activatedAt&&now-last.activatedAt<state.config.rotationCooldownMs&&state.observation?.pressure.level!=='critical')return {eligible:false,reason:'rotation-cooldown'};
  return {eligible:true,compactionId:compact.id,evidenceIds:valid.map(e=>e.id)};
}

export function safeBoundary({runtime,tasks=[],inputs=[],notices=[],contactRunning=false}) {
  if(!runtime?.known||runtime.active||runtime.nativeStatus!=='idle'||runtime.backgroundTasks||runtime.queued||runtime.pendingDeliveries||runtime.handoffTasks||contactRunning)return {safe:false,reason:'native-or-delivery-busy'};
  if(inputs.some(i=>['submitting','unconfirmed','selected'].includes(i.state)))return {safe:false,reason:'input-awaiting-dispatch-or-reconciliation'};
  if(notices.some(n=>['sending','unconfirmed'].includes(n.state)))return {safe:false,reason:'notification-unconfirmed'};
  if(tasks.some(t=>Object.values(t.tools??{}).some(tool=>!['completed','failed'].includes(tool.status))))return {safe:false,reason:'unfinished-tool'};
  return {safe:true};
}

export function checkpointBudget(checkpoint,budget) {
  const plan=checkpoint?.budgetPlan;
  if(plan?.reason==='recent-dialogue'&&plan.requested===budget&&Number.isInteger(plan.effective)&&plan.effective>=budget&&plan.effective<=8000&&plan.limit===8000)return plan.effective;
  return budget;
}

export function validateCheckpoint(checkpoint,{binding,cursors,configVersion,budget}) {
  if(!checkpoint||checkpoint.conversationId!==binding.conversationId||checkpoint.generation!==binding.generation)return 'checkpoint-identity-mismatch';
  if(checkpoint.configVersion!==configVersion)return 'checkpoint-config-changed';
  if(JSON.stringify(checkpoint.cursors)!==JSON.stringify(cursors))return 'checkpoint-increments-pending';
  if(!checkpoint.complete||checkpoint.needsReview||!checkpoint.sourceRevisions||!checkpoint.items?.length)return 'checkpoint-coverage-incomplete';
  if(!Number.isFinite(checkpoint.tokens)||checkpoint.tokens>checkpointBudget(checkpoint,budget))return 'checkpoint-budget';
  if(checkpoint.items.some(i=>!['user','assistant'].includes(i.role)||!i.id||!i.revision||typeof i.text!=='string'||i.channel&&i.channel!=='final'))return 'checkpoint-public-history-only';
  if((checkpoint.pendingQuestions??[]).some(q=>!checkpoint.items.some(i=>i.id===q.sourceId)))return 'checkpoint-reference-missing';
  return null;
}

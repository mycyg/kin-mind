import {randomUUID} from 'node:crypto';
import {portableHistory,handoffCheckpoint} from './portable-history.mjs';
import {candidateVerificationReady} from './session-manager.mjs';
import {runtimeProfile} from './codex-models.mjs';
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
export function needsModelHandoff(target,runtime) {
  const source=runtimeProfile(runtime??{});
  return target?.providerKind==='gateway'||source.providerKind==='gateway';
}
const canceled=()=>Object.assign(Error('Context handoff canceled'),{code:'HANDOFF_CANCELED',beforeMutation:true});
const unchanged=(message)=>Object.assign(Error(message),{code:'HANDOFF_NOT_COMMITTED',beforeMutation:true});

/** Called inside the router's existing serialized model transition. The normal
 * registry commit and promotion recovery also own these candidates; no second
 * binding or independent session store is introduced. No model turn is needed. */
export class ModelHandoff {
  constructor({manager,native,collect,inspect,checkpoint,validate=async()=>true}) {
    Object.assign(this,{manager,native,collect,inspect,checkpoint,validate});
  }
  async switchProfile(profile,{signal,forceBoundary}={}) {
    const m=this.manager;
    if(m.running)throw unchanged('Session maintenance is still running');
    if(m.frozen())throw unchanged('Dispatch is frozen');
    const unresolved=m.state.candidate;
    if(unresolved&&['committing','unconfirmed'].includes(unresolved.state)&&unresolved.native?.threadId===m.fence().threadId)
      throw Error('Committed handoff is awaiting recovery');
    m.running=true;
    let candidate,committed=false,stage='preparation';
    const checkCancel=()=>{if(signal?.aborted)throw canceled();};
    try{
      checkCancel();const previous=structuredClone(m.fence()),before=await this.inspect();
      const stopped=value=>value.nativeStatus==='idle'||value.nativeStatus==='systemError'&&['interrupted','idle'].includes(forceBoundary?.receipt?.state);
      if(!stopped(before)||!before.known||before.threadId!==previous.threadId||before.active||before.backgroundTasks||before.handoffTasks||before.pendingDeliveries||before.queued)
        throw unchanged('Source session is not idle');
      const context=await this.collect();
      const cp=await this.checkpoint(context,previous);
      const history=await portableHistory(before);
      const id='model-handoff:'+randomUUID();
      const checkpoint=handoffCheckpoint(cp,history,{id,profile});
      if(!await this.validate(checkpoint))throw unchanged('Checkpoint sources changed');
      checkCancel();
      if(unresolved&&!['retired','failed','stale','canceled'].includes(unresolved.state)){
        unresolved.state='stale';unresolved.staleReason='explicit-model-handoff';m.state.retiredCandidates??=[];m.state.retiredCandidates.push(structuredClone(unresolved));
      }
      candidate={id,kind:'model-handoff',state:'creating',generation:previous.generation,previousBinding:previous,
        sourceProfile:runtimeProfile(before),profile:structuredClone(profile),checkpoint,at:m.now()};
      m.state.candidate=candidate;m.save('model-handoff-creating');
      stage='creation';candidate.native=await this.native.create({id,profile,name:'Kin continuity'});candidate.state='created';m.save('model-handoff-created');checkCancel();
      candidate.injectionId=id;candidate.state='injecting';m.save('model-handoff-injecting');
      stage='injection';candidate.injection=await this.native.inject({...candidate.native,checkpoint,operationId:id});
      if(!candidate.injection?.verified)throw unchanged('Context injection unconfirmed');
      stage='verification';candidate.verification=await this.native.verifyHandoff({...candidate.native,checkpoint,operationId:id});
      if(!candidateVerificationReady(candidate.verification,candidate))throw unchanged('Candidate context or profile is unverified');
      candidate.state='ready';m.save('model-handoff-ready');checkCancel();
      const current=await this.inspect(),latest=await this.collect();
      if(!stopped(current)||!same(m.fence(),previous)||current.threadId!==previous.threadId||current.active||current.backgroundTasks||current.handoffTasks||current.pendingDeliveries||current.queued||m.frozen())
        throw unchanged('Source session changed during handoff');
      if(!same(context.cursors,latest.cursors)||context.configVersion!==latest.configVersion||!await this.validate(checkpoint))
        throw unchanged('Continuity sources changed during handoff');
      // The immutable source fingerprint includes appends after the first read.
      const fresh=await portableHistory(current);
      if(fresh.source.sha256!==history.source.sha256)throw unchanged('Source history changed during handoff');
      checkCancel();
      const next={conversationId:previous.conversationId,generation:previous.generation+1,
        threadId:candidate.native.threadId,nativeSessionId:candidate.native.nativeSessionId};
      stage='commit';candidate.state='committing';m.state.binding=next;m.state.segments.at(-1).state='retired';
      m.state.segments.push({...next,state:'active',activatedAt:m.now(),checkpointId:checkpoint.id,
        sourceThreadId:previous.threadId,handoffId:id,profile:structuredClone(profile)});
      m.state.handoffs??=[];
      m.state.handoffs.push({id,previousBinding:previous,binding:structuredClone(next),sourceProfile:candidate.sourceProfile,
        targetProfile:structuredClone(profile),source:history.source,coverage:history.coverage,checkpointId:checkpoint.id,committedAt:m.now()});
      m.state.handoffs=m.state.handoffs.slice(-64);
      // Restore work belonged to the previous native window. The candidate
      // already contains the prepared checkpoint; never replay that old job.
      m.state.restorePending=false;m.state.restoreRequired=false;
      delete m.state.restoreCheckpoint;delete m.state.restoration;
      delete m.state.rollingCheckpoint;delete m.state.rollingCursor;
      // A failed save may have completed its atomic rename. Treat this boundary
      // as indeterminate and use promotion recovery, never claim no mutation.
      committed=true;m.save('binding-committed',{handoffId:id});
      stage='promotion';const receipt=await m.promote({previous,binding:next,candidate});
      if(!receipt?.verified||receipt.threadId!==next.threadId)throw Error('Promotion unconfirmed');
      candidate.state='retired';candidate.promotion=receipt;m.state.sessionAdvice=null;m.save('model-handoff-complete');
      const actual=await this.inspect();
      return {...actual,contextHandoff:{id,sourceThreadId:previous.threadId,checkpointId:checkpoint.id,
        coverage:history.coverage,checkpointCoverage:checkpoint.coverage,sourceRetained:true}};
    }catch(error){
      const diagnostic=error.nativeDiagnostic;
      const category=['mcp-invalid-transport','config-invalid','process-exited','spawn-failed'].includes(diagnostic?.category)?diagnostic.category:null;
      const failure={stage:category&&diagnostic.stage==='startup'?'startup':stage,category:category??'operation-failed',
        exitCode:Number.isInteger(diagnostic?.exitCode)?diagnostic.exitCode:null};
      if(candidate){candidate.state=committed?'unconfirmed':signal?.aborted?'canceled':'failed';candidate.error=error.code??error.name;candidate.failure=failure;m.save('model-handoff-ended');}
      if(!committed&&!error.beforeMutation){
        const detail=category==='mcp-invalid-transport'?'候选启动时 MCP 配置无效':category==='config-invalid'?'候选启动配置无效':
          category&&diagnostic.stage==='startup'?'候选进程启动失败':({preparation:'上下文准备失败',creation:'候选会话创建失败',injection:'上下文注入失败',verification:'候选会话核验失败',commit:'交接提交失败'}[stage]??'候选交接失败');
        throw unchanged('上下文交接未完成（'+detail+'）；原会话已保留。');
      }
      throw error;
    }finally{await this.native.close().catch(()=>{});m.running=false;}
  }
}

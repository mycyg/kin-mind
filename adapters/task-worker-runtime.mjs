export const TASK_WORKER_MARKER='// KIN_TASK_WORKER_V1';

/** Embedded in the owned ACP; uses its configured provider, persona and sandbox.
 * A separate native thread keeps the main session runnable. No assessment fork
 * is promoted to an executor, no raw notifications are forwarded to a user. */
export async function kinTaskWorker(params) {
  const parent=this.sessions.get(params.sessionId);
  if(!parent)throw Error('Worker parent session is unavailable');
  this.kinTaskRuns??=new Map();
  const runs=this.kinTaskRuns,api=this.codexAcpClient.appServerClient;
  const view=r=>({runId:r.runId,threadId:r.threadId??null,turnId:r.turnId??null,state:r.state,
    events:r.events??[],...(r.state==='completed'?{text:r.text??''}:{}),...(r.reason?{reason:r.reason}:{})});
  let run=runs.get(params.runId);
  if(run&&run.parent!==params.sessionId)throw Error('Worker belongs to another session');
  if(params.op==='status')return run?view(run):{state:'missing'};
  if(params.op==='cancel') {
    if(!run)return {state:'missing'};
    run.cancelRequested=true;
    if(run.turnId&&!['completed','failed','interrupted'].includes(run.state)) {
      // Acceptance of interrupt is not proof the tool or turn ended.
      try {await api.turnInterrupt({threadId:run.threadId,turnId:run.turnId});}catch{}
    }
    return view(run);
  }
  if(params.op!=='start'||typeof params.runId!=='string'||!params.runId||typeof params.input!=='string'||!params.input.trim()||
    typeof params.capability!=='string'||!params.capability)throw Error('Invalid task worker request');
  if(run)return view(run);
  if([...runs.values()].some(r=>!['completed','failed','interrupted'].includes(r.state)))throw Error('Task worker slot occupied');
  run={runId:params.runId,parent:params.sessionId,state:'starting',events:[],seq:0,cancelRequested:false};runs.set(params.runId,run);
  // Keep terminal receipts bounded; active execution is never evicted.
  if(runs.size>32)for(const [id,r] of runs)if(r!==run&&['completed','failed','interrupted'].includes(r.state)){runs.delete(id);if(runs.size<=32)break;}
  try {
    const config=await this.codexAcpClient.createSessionConfig(parent.cwd,parent.additionalDirectories??[],[]);
    const servers=await this.codexAcpClient.getConfigMcpServerNames(parent.cwd);
    for(const name of servers)if(name!=='memorypalace')config[`mcp_servers.${name}.enabled`]=false;
    Object.assign(config,{'mcp_servers.memorypalace.env.KIN_MCP_MODE':'task-worker',
      'mcp_servers.memorypalace.env.KIN_TASK_CAPABILITY':params.capability,'features.multi_agent':false});
    if(params.workerMark)Object.assign(config,{'mcp_servers.memorypalace.env.KIN_WORKER_MARK':params.workerMark,
      'shell_environment_policy.set':{KIN_WORKER_MARK:params.workerMark}});
    const modelId=ModelId.fromString(parent.currentModelId);
    const common={cwd:parent.cwd,model:modelId.model,modelProvider:await this.codexAcpClient.getResumeModelProvider(),
      approvalPolicy:'never',sandbox:parent.agentMode.sandboxMode,config};
    if(params.threadId) {
      // Only a thread this ACP created for this parent's completed run may resume.
      if(![...runs.values()].some(r=>r!==run&&r.parent===params.sessionId&&r.threadId===params.threadId&&['interrupted','completed','failed'].includes(r.state)))throw Error('Worker continuation is not owned');
      await api.threadResume({...common,threadId:params.threadId});run.threadId=params.threadId;
    } else {const opened=await api.threadStart(common);run.threadId=opened.thread.id;}
    let finalText='';
    api.onServerNotification(run.threadId,n=>{
      if(!['item/started','item/completed'].includes(n.method))return;
      const item=n.params?.item;if(!item)return;
      if(item.type==='agentMessage'&&n.method==='item/completed'&&(!item.phase||item.phase==='final_answer'))finalText=item.text??'';
      if(!['commandExecution','mcpToolCall','webSearch','fileChange','dynamicToolCall'].includes(item.type))return;
      run.events.push({seq:++run.seq,type:'tool',id:item.id,name:item.tool??item.type,
        status:n.method==='item/started'?'running':item.status??'completed',at:Date.now()});
      if(run.events.length>1024)run.events.shift();
    });
    if(run.cancelRequested){run.state='interrupted';api.clearThreadHandlers(run.threadId);return view(run);}
    run.state='running';
    run.done=api.runTurn({threadId:run.threadId,input:[{type:'text',text:params.input,text_elements:[]}],cwd:parent.cwd,
      approvalPolicy:'never',sandboxPolicy:parent.agentMode.sandboxPolicy,model:modelId.model,effort:modelId.effort,summary:'none'},id=>{
      run.turnId=id;if(run.cancelRequested)void api.turnInterrupt({threadId:run.threadId,turnId:id}).catch(()=>{});
    }).then(result=>{
      run.turnId=result.turn.id;run.state=result.turn.status==='completed'?'completed':result.turn.status==='interrupted'?'interrupted':'failed';
      // K3 can omit phase. Only a completed native turn is eligible, and the
      // host still requires the private-note/public JSON envelope before send.
      const message=(result.turn.items??[]).findLast(i=>i.type==='agentMessage'&&(!i.phase||i.phase==='final_answer'));
      if(run.state==='completed')run.text=message?.text??finalText;
    }).catch(()=>{run.state='failed';run.reason='native-turn-failed';}).finally(()=>api.clearThreadHandlers(run.threadId));
    return view(run);
  } catch(error) {run.state='failed';run.reason='worker-start-failed';return view(run);}
}

export function patchTaskWorker(source) {
  if(source.includes(TASK_WORKER_MARKER))throw Error('Task worker already patched');
  const once=(needle,replacement)=>{if(source.split(needle).length!==2)throw Error('Task worker ACP anchor changed: '+needle);source=source.replace(needle,replacement);};
  once('  async kinLastReply(sessionId) {','  async kinTaskWorker(params) { return kinTaskWorker.call(this, params); }\n  async kinLastReply(sessionId) {');
  once('    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);',
    '    if (method === "_kin/task-worker") return await this.kinTaskWorker(params);\n    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);');
  once('.onRequest("_kin/runtime",','.onRequest("_kin/task-worker", external_exports.object({sessionId: external_exports.string(), op: external_exports.string(), runId: external_exports.string(), input: external_exports.string().optional(), capability: external_exports.string().optional(), threadId: external_exports.string().optional(), workerMark: external_exports.string().optional()}), (ctx) => getAgent().extMethod("_kin/task-worker", ctx.params)).onRequest("_kin/runtime",');
  const helper=TASK_WORKER_MARKER+'\n'+kinTaskWorker.toString()+'\n';
  const offset=source.startsWith('#!')?source.indexOf('\n')+1:0;
  return source.slice(0,offset)+helper+source.slice(offset);
}

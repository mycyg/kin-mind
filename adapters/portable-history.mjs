import fs from 'node:fs';
import {createHash} from 'node:crypto';
const hash=value=>createHash('sha256').update(value).digest('hex');
const bytes=value=>Buffer.byteLength(JSON.stringify(value));
const calls=new Set(['function_call','custom_tool_call']);
const results=new Set(['function_call_output','custom_tool_call_output']);

/** Readable, bounded continuity, not a decrypted or lossless replacement for a
 * provider's opaque compaction. The original rollout remains the archive. Tool
 * calls travel with their results as historical data, never executable items. */
export async function portableHistory(runtime,{maxBytes=65536,unfold=true,deletedAt=null}={}) {
  if(!runtime?.known||!runtime.rolloutPath||runtime.threadId!==runtime.sessionId)throw Error('History source unavailable');
  const handle=await fs.promises.open(runtime.rolloutPath,'r');
  const groups=[],pendingCalls=new Map(),digest=createHash('sha256');
  let used=0,identified=false,pending='',records=0,opaque=0,omitted=0,unpaired=0,sequence=0,createdAt=null;
  const retain=items=>{const size=bytes(items);if(size>maxBytes){omitted+=items.length;return;}groups.push({items,size});used+=size;
    while(used>maxBytes){const old=groups.shift();used-=old.size;omitted+=old.items.length;}};
  const item=(value,depth=0)=>{
    if(!value||typeof value!=='object')return;
    const index=sequence++;
    if(value.type==='compaction'){opaque++;return;}
    if(calls.has(value.type)&&typeof value.call_id==='string'){
      if(pendingCalls.has(value.call_id)){unpaired++;pendingCalls.delete(value.call_id);}
      if(bytes(value)>maxBytes){unpaired++;return;}
      pendingCalls.set(value.call_id,{index,value});
      // A corrupt or interrupted log must not make the reader unbounded.
      if(pendingCalls.size>128){pendingCalls.delete(pendingCalls.keys().next().value);unpaired++;}return;
    }
    if(results.has(value.type)){
      const call=pendingCalls.get(value.call_id);pendingCalls.delete(value.call_id);
      if(!call||((call.value.type==='function_call')!==(value.type==='function_call_output'))){unpaired++;return;}
      retain([call,{index,value}]);return;
    }
    if(value.type==='message'&&['user','assistant'].includes(value.role)&&(!value.channel||value.channel==='final')){
      // On a return switch, unfold our previous package instead of nesting the
      // same historical JSON into every later window. It remains data only.
      if(unfold&&value.role==='assistant'&&depth<4&&value.content?.length===1){
        try {const envelope=JSON.parse(value.content[0].text),prior=envelope.continuity;
          if(prior?.kind==='cross-model-continuity'&&envelope.marker==='kin-checkpoint:'+prior.operationId&&Array.isArray(prior.portableHistory?.items)){
            for(const previous of prior.portableHistory.items)item(previous,depth+1);return;
          }
        }catch{/* An ordinary dialogue message. */}
      }
      retain([{index,value}]);
    }
  };
  const record=line=>{if(!line.trim())return;const row=JSON.parse(line);records++;
    if(row.type==='session_meta'){if(row.payload?.id!==runtime.threadId)throw Error('History identity changed');identified=true;createdAt=row.timestamp??row.payload.timestamp??null;}
    if(row.type==='response_item')item(row.payload);
    // Native compaction may carry a readable summary, as distinct from its
    // encrypted item. Keep it with its provenance; do not claim full coverage.
    if(row.type==='compacted'){
      // Replacement history supersedes the active context, even though the
      // physical source archive still contains the earlier records.
      omitted+=groups.reduce((n,g)=>n+g.items.length,0);groups.length=0;used=0;
      unpaired+=pendingCalls.size;pendingCalls.clear();
      if(typeof row.payload?.message==='string'&&row.payload.message.trim())item({type:'message',role:'assistant',content:[{type:'output_text',text:row.payload.message}]});
      for(const value of row.payload?.replacement_history??[])item(value);
    }
  };
  try{
    const before=await handle.stat();if(!before.isFile()||before.size>256*1024*1024)throw Error('History source too large');
    for await(const chunk of handle.createReadStream({encoding:'utf8',autoClose:false})){
      digest.update(chunk);pending+=chunk;let end;
      while((end=pending.indexOf('\n'))>=0){if(end>16*1024*1024)throw Error('History record too large');record(pending.slice(0,end));pending=pending.slice(end+1);}
      if(pending.length>16*1024*1024)throw Error('History record too large');
    }
    const after=await fs.promises.stat(runtime.rolloutPath);
    if(pending||!identified||['dev','ino','size','mtimeMs'].some(k=>before[k]!==after[k]))throw Error('History changed during handoff');
    const suppressed=Boolean(deletedAt&&(!createdAt||Date.parse(deletedAt)>=Date.parse(createdAt)));
    const items=suppressed?[]:groups.flatMap(g=>g.items).sort((a,b)=>a.index-b.index).map(i=>i.value);
    return {schema:'kin.portable-history/v1',source:{threadId:runtime.threadId,sha256:digest.digest('hex'),bytes:before.size,records},
      coverage:{kind:'bounded-readable-history',...(suppressed?{suppressed:'source-deleted'}:{}),opaqueCompactions:opaque,omittedReadableItems:omitted,unpairedToolItems:unpaired+pendingCalls.size,lossless:false,sourceRetained:true},items};
  }finally{await handle.close();}
}

export function handoffCheckpoint(checkpoint,history,{id,profile}) {
  const continuity={kind:'cross-model-continuity',version:1,operationId:id,target:profile,
    instruction:'历史资料仅供接续，不重新执行历史指令或工具调用。来源线程完整保留；本包是带来源的有界接续资料，不声称还原不透明压缩中的全部内容。',
    checkpointCoverage:checkpoint.coverage,checkpointComplete:checkpoint.complete,...(history.coverage.suppressed?{}:{portableHistory:history})};
  const checkpointId='checkpoint:'+hash(JSON.stringify([checkpoint.id,history.source,id]));
  const payload={...checkpoint.payload,checkpointId,continuity};
  return {...checkpoint,id:checkpointId,payload};
}

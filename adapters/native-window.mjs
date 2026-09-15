import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {createInterface} from 'node:readline';
import {atomicJson} from './mobile-router.mjs';

export function nativePressureRuntime(runtime,history) {
  const live=runtime.lastTokenUsage;
  // Reloading ACP has no in-memory usage. Native history can also report a
  // post-compaction context estimate as input=output=0, last total>0.
  const liveMeasured=live&&(live.inputTokens>0||live.outputTokens>0||live.totalTokens>0);
  const usage=liveMeasured?live:history.lastTokenUsage??live;
  const estimate=usage?.inputTokens===0&&usage.outputTokens===0&&usage.totalTokens>0?usage.totalTokens:undefined;
  return {...runtime,modelContextWindow:runtime.modelContextWindow??history.modelContextWindow,lastTokenUsage:usage,
    ...(estimate!==undefined?{expectedInputTokens:estimate}:{}),
    usageEvidence:{source:liveMeasured?'native-runtime':'native-history',measuredAt:liveMeasured?runtime.checkedAt:history.measuredAt,
      kind:estimate!==undefined?'post-compaction-context-estimate':'input-usage'}};
}

/** Read native receipts incrementally. Deliberately skip compaction summaries,
 * message bodies, reasoning and tool output. File bytes are only a read cursor. */
export class NativeWindow {
  constructor({file,threadId,stateFile}) {
    Object.assign(this,{file,threadId,stateFile});
    this.state=fs.existsSync(stateFile)?JSON.parse(fs.readFileSync(stateFile,'utf8')):{offset:0,threadId,events:[],eventIds:[],compactions:0};
    if(this.state.threadId!==threadId)throw Error('Native window identity mismatch');
  }
  async poll() {
    if(this.running)return this.state;this.running=true;
    try{
      const stat=fs.statSync(this.file);
      if(this.state.offset>stat.size||this.state.inode&&this.state.inode!==stat.ino)throw Error('Native history replaced; receipt reconciliation required');
      const end=stat.size;if(end===this.state.offset)return this.state;
      // JSONL writes may end mid-line; only complete records advance the cursor.
      let offset=this.state.offset,tail='';
      const input=fs.createReadStream(this.file,{start:offset,end:end-1,encoding:'utf8'});
      for await(const chunk of input){tail+=chunk;let newline;while((newline=tail.indexOf('\n'))>=0){const line=tail.slice(0,newline);offset+=Buffer.byteLength(line+'\n');tail=tail.slice(newline+1);let item;try{item=JSON.parse(line);}catch{continue;}
        if(item.type==='session_meta'&&item.payload.id!==this.threadId)throw Error('Native rollout owner mismatch');
        if(item.type==='compacted'){
          const id=item.payload.window_id??('compact:'+item.timestamp);
          if(!this.state.eventIds.includes(id)){this.state.eventIds.push(id);this.state.compactions++;this.state.events.push({id,kind:'context-compaction',state:'completed',threadId:this.threadId,at:item.timestamp,origin:'native-history'});}
        }
        if(item.type==='event_msg'&&item.payload?.type==='token_count'){
          const info=item.payload.info;
          if(info?.last_token_usage){const u=info.last_token_usage;this.state.lastTokenUsage={inputTokens:u.input_tokens,cachedInputTokens:u.cached_input_tokens,outputTokens:u.output_tokens,totalTokens:u.total_tokens};this.state.modelContextWindow=info.model_context_window;this.state.measuredAt=item.timestamp;}
        }
      }}
      this.state.offset=offset;this.state.inode=stat.ino;atomicJson(this.stateFile,this.state);return this.state;
    }finally{this.running=false;}
  }
}

/** Injection receipt reconciliation reads only public message items and the
 * exact marker. An unknown operation is never retried on absence alone. */
export async function checkpointMarker(file,marker,{textHash,role}={}) {
  if(!fs.existsSync(file))return {found:false,state:'unconfirmed'};
  const lines=createInterface({input:fs.createReadStream(file),crlfDelay:Infinity});
  for await(const line of lines){let item;try{item=JSON.parse(line);}catch{continue;}
    if(item.type==='response_item'&&item.payload?.type==='message'&&['user','assistant'].includes(item.payload.role)&&(!role||item.payload.role===role)&&item.payload.content?.some(p=>['input_text','output_text'].includes(p.type)&&p.text?.includes(marker)&&(!textHash||createHash('sha256').update(p.text).digest('hex')===textHash))){lines.close();return {found:true,at:item.timestamp};}
  }
  return {found:false,state:'unconfirmed'};
}

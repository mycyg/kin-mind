import fs from 'node:fs';
import path from 'node:path';
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

/** How many injection and input receipts the window keeps. A receipt is looked
 * up right after its own append; older ones only matter while still unsettled. */
export const RECEIPTS_KEPT=512;
const INJECTION=/kin-(?:context|checkpoint|effect):[A-Za-z0-9._:-]+/g;
const INPUT=/<kin-host-event>([a-f0-9]{32})<\/kin-host-event>/g;
const INDEXED=/^kin-(?:context|checkpoint|effect):[A-Za-z0-9._:-]+$/;
const sha256=value=>createHash('sha256').update(value).digest('hex');
const windows=new Map();
/** The incremental reader this process keeps for a native history file, if any. */
export function nativeWindowFor(file){return typeof file==='string'&&file?windows.get(path.resolve(file))??null:null;}

/** Complete JSONL lines of `[start,end)` with the byte offset each begins at. */
async function readLines(file,start,end,visit) {
  if(end<=start)return start;
  let offset=start,tail='';
  const input=fs.createReadStream(file,{start,end:end-1,encoding:'utf8'});
  for await(const chunk of input){tail+=chunk;let newline;while((newline=tail.indexOf('\n'))>=0){const line=tail.slice(0,newline);const at=offset;offset+=Buffer.byteLength(line)+1;tail=tail.slice(newline+1);if(await visit(line,at)===false){input.destroy();return offset;}}}
  return offset;
}
const publicParts=item=>item?.type==='response_item'&&item.payload?.type==='message'&&['user','assistant'].includes(item.payload.role)&&Array.isArray(item.payload.content)
  ?item.payload.content.filter(p=>['input_text','output_text'].includes(p?.type)&&typeof p.text==='string'):[];
/** What a public message proves without keeping its words: the exact hash of each
 * text part that carries an injection marker, and the host event of an input. */
function receiptsOf(item,offset,turnContext) {
  const found=[];
  for(const part of publicParts(item)) {
    const markers=[...new Set(part.text.match(INJECTION)??[])].slice(0,4);
    if(markers.length)found.push({h:sha256(part.text),m:markers,r:item.payload.role,o:offset,at:item.timestamp??null});
    if(item.payload.role==='user')for(const [,event] of part.text.matchAll(INPUT))found.push({e:event,r:'user',o:offset,tc:turnContext,at:item.timestamp??null});
  }
  return found;
}

/** Read native receipts incrementally. Deliberately skip compaction summaries,
 * message bodies, reasoning and tool output. File bytes are only a read cursor;
 * the receipt index keeps hashes, markers, offsets and times, never text. */
export class NativeWindow {
  constructor({file,threadId,stateFile}) {
    Object.assign(this,{file,threadId,stateFile});
    this.state=fs.existsSync(stateFile)?JSON.parse(fs.readFileSync(stateFile,'utf8')):{offset:0,threadId,events:[],eventIds:[],compactions:0};
    if(this.state.threadId!==threadId)throw Error('Native window identity mismatch');
    // A window written before receipts were indexed covers only what it reads from now on.
    this.state.receipts??={from:this.state.offset,items:[]};
    windows.set(path.resolve(file),this);
  }
  /** True when a marker missing from the index is missing from the history. */
  covers(){return this.state.receipts.from===0||Boolean(this.state.receipts.legacy);}
  index(entries) {
    const items=this.state.receipts.items;
    for(const entry of entries)if(!items.some(r=>r.o===entry.o&&r.h===entry.h&&r.e===entry.e))items.push(entry);
    // Oldest indexed first out: a receipt found late by reconciliation is kept as long as a fresh one.
    if(items.length>RECEIPTS_KEPT)items.splice(0,items.length-RECEIPTS_KEPT);
  }
  /** The receipt index (interface `nativeWindow.receipts`): by exact text hash, by
   * marker, or by host input event. */
  get receipts() {
    const items=this.state.receipts.items,from=this.state.receipts.from,legacy=this.state.receipts.legacy??null;
    return {from,legacy,covers:this.covers(),
      get:hash=>items.findLast(r=>r.h===hash)??null,
      find:({marker,textHash,role}={})=>items.findLast(r=>r.m&&(!marker||r.m.includes(marker))&&(!textHash||r.h===textHash)&&(!role||r.r===role))??null,
      input:event=>items.findLast(r=>r.e===event)??null};
  }
  save(){atomicJson(this.stateFile,this.state);}
  async poll() {
    if(this.running)return this.running;
    this.running=(async()=>{
      const stat=fs.statSync(this.file);
      if(this.state.offset>stat.size||this.state.inode&&this.state.inode!==stat.ino)throw Error('Native history replaced; receipt reconciliation required');
      const end=stat.size;if(end===this.state.offset)return this.state;
      // JSONL writes may end mid-line; only complete records advance the cursor.
      // Lines are parsed only when their structure can matter here.
      const found=[];
      this.state.offset=await readLines(this.file,this.state.offset,end,(line,at)=>{
        const turn=line.includes('"type":"turn_context"'),injection=line.includes('kin-context:')||line.includes('kin-checkpoint:')||line.includes('kin-effect:')||line.includes('<kin-host-event>');
        if(!turn&&!injection&&!line.includes('"type":"compacted"')&&!line.includes('"type":"token_count"')&&!line.includes('"type":"session_meta"'))return;
        let item;try{item=JSON.parse(line);}catch{return;}
        if(item.type==='session_meta'&&item.payload.id!==this.threadId)throw Error('Native rollout owner mismatch');
        if(item.type==='turn_context'){this.state.turnContext=at;return;}
        if(item.type==='compacted'){
          const id=item.payload.window_id??('compact:'+item.timestamp);
          if(!this.state.eventIds.includes(id)){this.state.eventIds.push(id);this.state.compactions++;this.state.events.push({id,kind:'context-compaction',state:'completed',threadId:this.threadId,at:item.timestamp,origin:'native-history'});}
        }
        if(item.type==='event_msg'&&item.payload?.type==='token_count'){
          const info=item.payload.info;
          if(info?.last_token_usage){const u=info.last_token_usage;this.state.lastTokenUsage={inputTokens:u.input_tokens,cachedInputTokens:u.cached_input_tokens,outputTokens:u.output_tokens,totalTokens:u.total_tokens};this.state.modelContextWindow=info.model_context_window;this.state.measuredAt=item.timestamp;}
        }
        if(injection)found.push(...receiptsOf(item,at,this.state.turnContext??null));
      });
      this.index(found);
      this.state.inode=stat.ino;this.save();return this.state;
    })().finally(()=>{this.running=null;});
    return this.running;
  }
}

/** One-time, bounded reconciliation for identities left unsettled before the
 * window indexed receipts: a single pass over at most `maxBytes` of the history
 * before the index begins. What is found is indexed like any other receipt; what
 * is not stays unknown and is never injected again. Afterwards the index answers
 * for the whole history. */
export async function reconcileLegacyInjections(window,markers,{maxBytes=Infinity,clock=()=>new Date().toISOString()}={}) {
  const receipts=window.state.receipts;
  if(window.covers())return {state:'covered',found:[],unknown:[]};
  const wanted=[...new Set((markers??[]).filter(marker=>typeof marker==='string'&&INDEXED.test(marker)))];
  const end=receipts.from,start=Number.isFinite(maxBytes)?Math.max(0,end-maxBytes):0,found=[];
  if(wanted.length&&fs.existsSync(window.file)) {
    let turnContext=null;
    await readLines(window.file,start,end,(line,at)=>{
      if(line.includes('"type":"turn_context"')){turnContext=at;return;}
      if(!wanted.some(marker=>line.includes(marker)))return;
      let item;try{item=JSON.parse(line);}catch{return;}
      found.push(...receiptsOf(item,at,turnContext).filter(r=>r.m?.some(marker=>wanted.includes(marker))));
    });
  }
  window.index(found);
  const settled=new Set(found.flatMap(r=>r.m));
  receipts.legacy={checkedAt:clock(),from:start,markers:wanted.length,found:settled.size};
  window.save();
  return {state:'reconciled',found:found.map(r=>({markers:r.m,hash:r.h,offset:r.o,at:r.at})),unknown:wanted.filter(marker=>!settled.has(marker))};
}

/** AD1-10: whether a prompt whose submission is uncertain reached the native
 * history, by the host-event ids it carried (`<kin-host-event>`). The receipt
 * index answers first; otherwise one pass over at most `maxBytes` at the end of
 * the history reads only lines that can carry an event. `not-found` needs that
 * stretch to begin before the prompt was submitted (`submittedAt`, ms); short of
 * that the answer is `unknown`, which is never read as absence. */
export async function reconcileHostInput(file,events,{submittedAt=null,maxBytes=64*1024*1024}={}) {
  const wanted=[...new Set((events??[]).filter(event=>typeof event==='string'&&/^[a-f0-9]{32}$/.test(event)))];
  if(!wanted.length)return {state:'unknown',reason:'no-host-event'};
  if(typeof file!=='string'||!fs.existsSync(file))return {state:'unknown',reason:'native-history-unavailable'};
  const window=nativeWindowFor(file);
  if(window) {
    try{await window.poll();}catch{/* The pass below reads the file itself. */}
    for(const event of wanted){const hit=window.receipts.input(event);if(hit)return {state:'found',at:hit.at,offset:hit.o};}
  }
  const size=fs.statSync(file).size,start=Number.isFinite(maxBytes)?Math.max(0,size-maxBytes):0;
  let found=null,earliest=null,turnContext=null,partial=start>0;
  await readLines(file,start,size,(line,at)=>{
    // A pass that starts inside the file skips the line it starts in.
    if(partial){partial=false;return;}
    if(earliest===null){const stamp=/"timestamp":"([^"]+)"/.exec(line);if(stamp)earliest=Date.parse(stamp[1]);}
    if(line.includes('"type":"turn_context"')){turnContext=at;return;}
    if(!line.includes('<kin-host-event>'))return;
    let item;try{item=JSON.parse(line);}catch{return;}
    const hit=receiptsOf(item,at,turnContext).find(r=>r.e&&wanted.includes(r.e));
    if(hit){found=hit;return false;}
  });
  if(found){if(window){window.index([found]);window.save();}return {state:'found',at:found.at,offset:found.o};}
  const covered=start===0||Number.isFinite(earliest)&&Number.isFinite(submittedAt)&&earliest<submittedAt;
  return covered?{state:'not-found'}:{state:'unknown',reason:'history-before-submission-unread'};
}

/** Injection receipt reconciliation reads only public message items and the
 * exact marker. An unknown operation is never retried on absence alone. With an
 * incremental reader for this file, an injection marker is answered from its
 * receipt index; otherwise one pass reads only lines that can contain it. */
export async function checkpointMarker(file,marker,{textHash,role}={}) {
  if(!fs.existsSync(file))return {found:false,state:'unconfirmed'};
  const window=nativeWindowFor(file);
  if(window&&INDEXED.test(marker)) {
    await window.poll();
    const hit=window.receipts.find({marker,textHash,role});
    if(hit)return {found:true,at:hit.at,offset:hit.o};
    if(window.covers())return {found:false,state:'unconfirmed'};
  }
  const lines=createInterface({input:fs.createReadStream(file),crlfDelay:Infinity});
  for await(const line of lines){if(!line.includes(marker))continue;let item;try{item=JSON.parse(line);}catch{continue;}
    if(item.type==='response_item'&&item.payload?.type==='message'&&['user','assistant'].includes(item.payload.role)&&(!role||item.payload.role===role)&&item.payload.content?.some(p=>['input_text','output_text'].includes(p.type)&&p.text?.includes(marker)&&(!textHash||sha256(p.text)===textHash))){lines.close();return {found:true,at:item.timestamp};}
  }
  return {found:false,state:'unconfirmed'};
}

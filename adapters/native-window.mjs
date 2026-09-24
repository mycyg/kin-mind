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

/** How many injection receipts the window keeps. A receipt is looked up right
 * after its own append; older ones only matter while still unsettled. */
export const RECEIPTS_KEPT=512;
/** How many identities the legacy pass remembers having searched for. */
export const LEGACY_MARKERS_KEPT=4096;
const INJECTION=/kin-(?:context|checkpoint|effect):[A-Za-z0-9._:-]+/g;
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
 * text part that carries an injection marker. */
function receiptsOf(item,offset) {
  const found=[];
  for(const part of publicParts(item)) {
    const markers=[...new Set(part.text.match(INJECTION)??[])].slice(0,4);
    if(markers.length)found.push({h:sha256(part.text),m:markers,r:item.payload.role,o:offset,at:item.timestamp??null});
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
  /** True when a marker missing from the index is missing from the history: the
   * index reads the history from its first byte, or the legacy pass searched for
   * this very marker from there. A pass over a bounded stretch proves nothing about
   * what lies before it, and a pass for other markers nothing about this one. */
  covers(marker) {
    const receipts=this.state.receipts;
    if(receipts.from===0)return true;
    return typeof marker==='string'&&receipts.legacy?.markers?.[marker]?.from===0;
  }
  /** Every identity left unsettled before the index began has had its legacy pass. */
  migrated(){return this.state.receipts.from===0||this.state.receipts.legacy?.complete===true;}
  index(entries) {
    const items=this.state.receipts.items;
    for(const entry of entries)if(!items.some(r=>r.o===entry.o&&r.h===entry.h))items.push(entry);
    // Oldest indexed first out: a receipt found late by reconciliation is kept as long as a fresh one.
    if(items.length>RECEIPTS_KEPT)items.splice(0,items.length-RECEIPTS_KEPT);
  }
  /** The receipt index (interface `nativeWindow.receipts`): by exact text hash or by marker. */
  get receipts() {
    const items=this.state.receipts.items,from=this.state.receipts.from,legacy=this.state.receipts.legacy??null;
    return {from,legacy,covers:this.covers(),migrated:this.migrated(),
      get:hash=>items.findLast(r=>r.h===hash)??null,
      find:({marker,textHash,role}={})=>items.findLast(r=>r.m&&(!marker||r.m.includes(marker))&&(!textHash||r.h===textHash)&&(!role||r.r===role))??null};
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
        const injection=line.includes('kin-context:')||line.includes('kin-checkpoint:')||line.includes('kin-effect:');
        if(!injection&&!line.includes('"type":"compacted"')&&!line.includes('"type":"token_count"')&&!line.includes('"type":"session_meta"'))return;
        let item;try{item=JSON.parse(line);}catch{return;}
        if(item.type==='session_meta'&&item.payload.id!==this.threadId)throw Error('Native rollout owner mismatch');
        if(item.type==='compacted'){
          const id=item.payload.window_id??('compact:'+item.timestamp);
          if(!this.state.eventIds.includes(id)){this.state.eventIds.push(id);this.state.compactions++;this.state.events.push({id,kind:'context-compaction',state:'completed',threadId:this.threadId,at:item.timestamp,origin:'native-history'});}
        }
        if(item.type==='event_msg'&&item.payload?.type==='token_count'){
          const info=item.payload.info;
          if(info?.last_token_usage){const u=info.last_token_usage;this.state.lastTokenUsage={inputTokens:u.input_tokens,cachedInputTokens:u.cached_input_tokens,outputTokens:u.output_tokens,totalTokens:u.total_tokens};this.state.modelContextWindow=info.model_context_window;this.state.measuredAt=item.timestamp;}
        }
        if(injection)found.push(...receiptsOf(item,at));
      });
      this.index(found);
      this.state.inode=stat.ino;this.save();return this.state;
    })().finally(()=>{this.running=null;});
    return this.running;
  }
}

/** Bounded reconciliation for identities left unsettled before the window indexed
 * receipts (CR-LIFE-09): one pass over at most `maxBytes` (required, finite) of the
 * history before the index begins, for the markers given that were not searched
 * for before. What is found is indexed like any other receipt; what is not stays
 * unknown and is never injected again. Each marker keeps what was searched for it;
 * `complete` says the caller has now handed over every identity it holds, and only
 * then is the window migrated. */
export async function reconcileLegacyInjections(window,markers,{maxBytes,complete=false,clock=()=>new Date().toISOString()}={}) {
  if(!Number.isSafeInteger(maxBytes)||maxBytes<0)throw Error('A legacy receipt pass needs an explicit, finite byte budget');
  const receipts=window.state.receipts;
  if(receipts.from===0)return {state:'covered',found:[],unknown:[]};
  const legacy=receipts.legacy&&typeof receipts.legacy.markers==='object'?receipts.legacy:(receipts.legacy={markers:{}});
  const wanted=[...new Set((markers??[]).filter(marker=>typeof marker==='string'&&INDEXED.test(marker)&&!legacy.markers[marker]))];
  const end=receipts.from,start=Math.max(0,end-maxBytes),found=[];
  if(wanted.length&&fs.existsSync(window.file)) {
    await readLines(window.file,start,end,(line,at)=>{
      if(!wanted.some(marker=>line.includes(marker)))return;
      let item;try{item=JSON.parse(line);}catch{return;}
      found.push(...receiptsOf(item,at).filter(r=>r.m.some(marker=>wanted.includes(marker))));
    });
  }
  window.index(found);
  const settled=new Set(found.flatMap(r=>r.m)),at=clock();
  for(const marker of wanted)legacy.markers[marker]={from:start,found:settled.has(marker),at};
  const kept=Object.keys(legacy.markers);
  for(const marker of kept.slice(0,Math.max(0,kept.length-LEGACY_MARKERS_KEPT)))delete legacy.markers[marker];
  legacy.checkedAt=at;if(complete)legacy.complete=true;
  window.save();
  return {state:'reconciled',found:found.map(r=>({markers:r.m,hash:r.h,offset:r.o,at:r.at})),unknown:wanted.filter(marker=>!settled.has(marker))};
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
    // Unknown, never absent. Once the legacy pass is done this reads no history from its
    // first byte; a checkpoint's own operation state guards what that cannot prove.
    if(window.covers(marker)||window.migrated())return {found:false,state:'unconfirmed'};
  }
  const lines=createInterface({input:fs.createReadStream(file),crlfDelay:Infinity});
  for await(const line of lines){if(!line.includes(marker))continue;let item;try{item=JSON.parse(line);}catch{continue;}
    if(item.type==='response_item'&&item.payload?.type==='message'&&['user','assistant'].includes(item.payload.role)&&(!role||item.payload.role===role)&&item.payload.content?.some(p=>['input_text','output_text'].includes(p.type)&&p.text?.includes(marker)&&(!textHash||sha256(p.text)===textHash))){lines.close();return {found:true,at:item.timestamp};}
  }
  return {found:false,state:'unconfirmed'};
}

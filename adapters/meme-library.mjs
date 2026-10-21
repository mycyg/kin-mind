/** Private, owner-scoped MEME library. No network, model calls, timers or persona writes.
 * Only explicit collection or a visual turn's interesting-image choice creates an index.
 * Single index transaction + immutable content files; a crash may leave an unreferenced
 * content file, never a ready index pointing at half-written bytes. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {inflateSync} from 'node:zlib';
import {readJsonFile,writeJsonAtomic,createFileExclusive,claimPidLock,releasePidLock} from './atomic-json.mjs';
export const MEME_LIMITS=Object.freeze({bytes:5*1024*1024,pixels:16*1024*1024,items:500,packs:64,candidates:3});
export const memeHash=bytes=>createHash('sha256').update(bytes).digest('hex');
const ID=/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$/;
const SHA=/^[a-f0-9]{64}$/;
const err=code=>Object.assign(Error(code),{code});
const requireId=id=>{if(typeof id!=='string'||!ID.test(id))throw err('meme-invalid-id');return id;};
const str=(x,max=1200)=>{if(typeof x!=='string'||x.length>max||/[\u0000-\u0008]/u.test(x))throw err('meme-invalid-text');return x.trim();};
const tags=x=>{if(!Array.isArray(x)||x.length>24)throw err('meme-invalid-tags');return [...new Set(x.map(t=>str(t,100)).filter(Boolean))];};
export function memeMetadata(value={}) {
  const uses=value.uses??[];
  if(!Array.isArray(uses)||uses.length>8)throw err('meme-invalid-uses');
  return {description:str(value.description??''),ocr:str(value.ocr??'',2400),tags:tags(value.tags??[]),contexts:tags(value.contexts??[]),aliases:tags(value.aliases??[]),emotions:tags(value.emotions??[]),
    ambiguity:str(value.ambiguity??'',600),uses:uses.map(u=>{
      if(!u||typeof u!=='object')throw err('meme-invalid-use');
      const when=str(u.when??'',300),reading=str(u.reading??'',300);
      if(!when||!reading)throw err('meme-invalid-use');return {when,reading};
    })};
}
const normalize=text=>text.normalize('NFKC').toLowerCase();
const terms=text=>[...new Set((text.match(/[a-z0-9]+|[\p{Script=Han}]+/gu)??[]).flatMap(t=>/^[\p{Script=Han}]+$/u.test(t)&&t.length>1?Array.from({length:t.length-1},(_,i)=>t.slice(i,i+2)):[t]))];
const relevance=(corpus,text)=>text?terms(text).reduce((n,t)=>n+(corpus.includes(t)?1:0),0)+(corpus.includes(text)?3:0):0;
const crc32=b=>{let c=0xffffffff;for(const x of b){c^=x;for(let j=0;j<8;j++)c=(c>>>1)^((c&1)?0xedb88320:0);}return (c^0xffffffff)>>>0;};
/** First version accepts static PNGs and structurally checked JPEGs. Animation and unvalidated formats are
 * deliberately unsupported; callers preserve ordinary input handling independently. */
export function inspectMemeImage(data) {
  if(!Buffer.isBuffer(data)||!data.length||data.length>MEME_LIMITS.bytes)throw err('meme-image-size');
  if(data[0]===0xff&&data[1]===0xd8) {
    let p=2,width=0,height=0,scan=false,ended=false;
    while(p<data.length){if(data[p++]!==0xff)throw err('meme-image-corrupt');while(data[p]===0xff)p++;const marker=data[p++];
      if(marker===0xd9){ended=true;break;}
      if(marker===0x00||marker===0xd8||marker>=0xd0&&marker<=0xd7)throw err('meme-image-corrupt');
      if(p+2>data.length)throw err('meme-image-corrupt');const length=data.readUInt16BE(p);if(length<2||p+length>data.length)throw err('meme-image-corrupt');
      if([0xc0,0xc1,0xc2].includes(marker)){if(length<8)throw err('meme-image-corrupt');height=data.readUInt16BE(p+3);width=data.readUInt16BE(p+5);}
      p+=length;
      if(marker===0xda){scan=true;while(p<data.length){if(data[p]!==0xff){p++;continue;}if(data[p+1]===0x00||data[p+1]>=0xd0&&data[p+1]<=0xd7){p+=2;continue;}break;}}
    }
    if(!ended||p!==data.length||!scan||!width||!height||width*height>MEME_LIMITS.pixels)throw err('meme-image-corrupt');
    return {mime:'image/jpeg',extension:'jpg',width,height,animated:false,bytes:data.length};
  }
  if(!data.subarray(0,8).equals(Buffer.from('89504e470d0a1a0a','hex')))throw err('meme-static-image-required');
  let pos=8,width,height,depth,color,ended=false,header=false,idats=[];
  while(pos+12<=data.length) {
    const n=data.readUInt32BE(pos),end=pos+12+n;if(end>data.length)throw err('meme-image-corrupt');
    const type=data.toString('ascii',pos+4,pos+8),bytes=data.subarray(pos+8,pos+8+n);
    if(crc32(data.subarray(pos+4,pos+8+n))!==data.readUInt32BE(pos+8+n))throw err('meme-image-corrupt');
    if(!header&&type!=='IHDR')throw err('meme-image-corrupt');
    if(type==='acTL')throw err('meme-animation-unsupported');
    if(type==='IHDR') {if(header||n!==13)throw err('meme-image-corrupt');header=true;width=bytes.readUInt32BE(0);height=bytes.readUInt32BE(4);depth=bytes[8];color=bytes[9];if(depth!==8||![0,2,4,6].includes(color)||bytes[10]||bytes[11]||bytes[12])throw err('meme-png-layout-unsupported');}
    if(type==='IDAT')idats.push(bytes);
    pos=end;if(type==='IEND'){if(n!==0)throw err('meme-image-corrupt');ended=true;break;}
  }
  if(!ended||pos!==data.length||!width||!height||width*height>MEME_LIMITS.pixels||!idats.length)throw err('meme-image-corrupt');
  const channels={0:1,2:3,4:2,6:4}[color],stride=width*channels+1,total=height*stride;
  let raw;try{raw=inflateSync(Buffer.concat(idats),{maxOutputLength:total+1});}catch{throw err('meme-image-corrupt');}
  if(raw.length!==total)throw err('meme-image-corrupt');
  for(let y=0;y<height;y++)if(raw[y*stride]>4)throw err('meme-image-corrupt');
  return {mime:'image/png',extension:'png',width,height,animated:false,bytes:data.length};
}
function publicItem(item) {const {file,...value}=item;return structuredClone(value);}
export class MemeLibrary {
  constructor(directory,{scope='owner',clock=()=>Date.now()}={}) {this.directory=path.resolve(directory);this.scope=str(scope,200);this.clock=clock;this.file=path.join(this.directory,'index.json');}
  read() {
    const r=readJsonFile(this.file);if(r.state==='missing')return {schema:1,scope:this.scope,packs:{},items:{},receipts:{}};
    if(r.state!=='ok'||r.value.schema!==1||r.value.scope!==this.scope||!r.value.items||!r.value.packs||!r.value.receipts)throw err('meme-index-unavailable');
    return r.value;
  }
  transaction(fn,afterCommit) {
    const lockFile=path.join(this.directory,'index.lock'),lock=claimPidLock(lockFile);if(lock.state!=='held')throw err('meme-library-busy');
    try {const state=this.read(),value=fn(state);writeJsonAtomic(this.file,state,{previous:false});return afterCommit?afterCommit(state,value):value;}finally{releasePidLock(lockFile,lock.claim);}
  }
  groups(){return Object.values(this.read().packs).map(p=>({...p}));}
  group({id,name,aliases=[],remove=false}) {
    requireId(id);return this.transaction(s=>{if(remove){delete s.packs[id];for(const item of Object.values(s.items))item.groups=item.groups.filter(g=>g!==id);return {state:'removed'};}
      if(!s.packs[id]&&Object.keys(s.packs).length>=MEME_LIMITS.packs)throw err('meme-pack-budget');
      return s.packs[id]={id,name:str(name,100)||id,aliases:tags(aliases)};});
  }
  collect({data,source,metadata={},group='collected',intent}) {
    // Classification alone never collects, never writes an index or invokes a model.
    if(!intent||!['explicit','interesting'].includes(intent.kind))return {state:'skipped',reason:'not-chosen'};
    // Choosing this basis already expresses interest. Utility is not an admission gate;
    // legacy worthKeeping is accepted but ignored. Unknown meaning can still be a MEME.
    if(intent.kind==='interesting'&&(intent.visual!==true||intent.isMeme!==true))return {state:'skipped',reason:'not-chosen'};
    if(!source||!['wechat','feishu','pack'].includes(source.channel)||typeof source.id!=='string'||!source.id||source.id.length>300)throw err('meme-source-required');
    const info=inspectMemeImage(data),hash=memeHash(data),meta=memeMetadata(metadata);requireId(group);
    return this.transaction(s=>this.insert(s,{data,source:{channel:source.channel,id:source.id},hash,info,meta,group}));
  }
  insert(s,{data,source,hash,info,meta,group,preferredId}) {
    if(!s.packs[group]){if(Object.keys(s.packs).length>=MEME_LIMITS.packs)throw err('meme-pack-budget');s.packs[group]={id:group,name:group,aliases:[]};}
    let item=Object.values(s.items).find(i=>i.hash===hash);
    if(item?.state==='ready'){if(!item.sources.some(x=>x.channel===source.channel&&x.id===source.id))item.sources.push(source);if(item.sources.length>100)item.sources.splice(0,item.sources.length-100);if(!item.groups.includes(group))item.groups.push(group);return {state:'saved',duplicate:true,item:publicItem(item)};}
    if(Object.values(s.items).filter(i=>i.state==='ready').length>=MEME_LIMITS.items)throw err('meme-item-budget');
    if(!s.packs[group]){if(Object.keys(s.packs).length>=MEME_LIMITS.packs)throw err('meme-pack-budget');s.packs[group]={id:group,name:group,aliases:[]};}
    const id=item?.id??preferredId??'meme-'+randomUUID();requireId(id);if(s.items[id]&&s.items[id]!==item)throw err('meme-id-conflict');
    const file=hash+'.'+info.extension,target=path.join(this.directory,'originals',file);
    createFileExclusive(target,data);if(memeHash(fs.readFileSync(target))!==hash)throw err('meme-content-conflict');
    item={id,hash,file,...info,...meta,groups:[group],sources:[source],state:'ready',createdAt:item?.createdAt??this.clock(),lastUsedAt:item?.lastUsedAt??null,useCount:item?.useCount??0};s.items[id]=item;
    return {state:'saved',duplicate:false,item:publicItem(item)};
  }
  search({query='',context='',group=null,limit=3,exclude=[]}={}) {
    query=normalize(str(query,400));context=normalize(str(context,1200));if(!query&&!context)return [];
    if(group!==null)requireId(group);if(!Array.isArray(exclude)||exclude.length>100)throw err('meme-invalid-exclude');
    const state=this.read();return Object.values(state.items).filter(i=>i.state==='ready'&&(!group||i.groups.includes(group))&&!exclude.includes(i.id)).map(i=>{
      const corpus=normalize([i.description,i.ocr,...i.tags,...i.contexts,...i.aliases,...i.emotions,i.ambiguity??''].join(' '));
      // Uses are contextual possibilities, never intrinsic facts or evidence of owner emotion.
      // Rank each alternative independently: accumulating contradictory readings adds no bonus.
      const matched_uses=(i.uses??[]).map(u=>({...u,score:relevance(normalize(u.when+' '+u.reading),query)+relevance(normalize(u.when+' '+u.reading),context)})).filter(u=>u.score>0).sort((a,b)=>b.score-a.score).slice(0,3);
      const score=relevance(corpus,query)+relevance(corpus,context)+(matched_uses[0]?.score??0);
      return {...publicItem(i),matched_uses,score};}).filter(i=>i.score>0).sort((a,b)=>b.score-a.score||(a.lastUsedAt??0)-(b.lastUsedAt??0)||a.id.localeCompare(b.id)).slice(0,Math.max(1,Math.min(MEME_LIMITS.candidates,Number.isSafeInteger(limit)?limit:3)));
  }
  readImage(id){requireId(id);const item=this.read().items[id];if(!item||item.state!=='ready')throw err('meme-unavailable');const target=path.join(this.directory,'originals',item.file),real=fs.realpathSync(target),root=fs.realpathSync(path.join(this.directory,'originals'));if(!real.startsWith(root+path.sep))throw err('meme-path-outside-library');const data=fs.readFileSync(real);if(memeHash(data)!==item.hash)throw err('meme-content-changed');inspectMemeImage(data);return {item:publicItem(item),data};}
  edit(id,{metadata,groups,remove=false}={}) {
    requireId(id);return this.transaction(s=>{const i=s.items[id];if(!i||i.state!=='ready')throw err('meme-unavailable');
      if(remove){i.state='retired';Object.assign(i,memeMetadata());i.groups=[];i.sources=[];return {state:'retired',id};}
      // Partial corrections replace only supplied metadata fields. []/'' explicitly clear them.
      if(metadata)Object.assign(i,memeMetadata({...i,...metadata}));if(groups){if(!Array.isArray(groups)||groups.length>16||groups.some(g=>!s.packs[g]))throw err('meme-unknown-group');i.groups=[...new Set(groups)];}return publicItem(i);},(s,result)=>{if(!remove)return result;const item=s.items[id];
      // Remove content only after the durable tombstone, under the same short index lock.
      // Existing delivery journals remain evidence; they are not collection/search entries.
      try {if(!Object.values(s.items).some(i=>i.state==='ready'&&i.hash===item.hash))fs.rmSync(path.join(this.directory,'originals',path.basename(item.file)),{force:true});return {...result,bytesRemoved:true};}
      catch{return {...result,bytesRemoved:false};}});
  }
  recordUse(id,receipt) {
    if(receipt?.state!=='accepted'||!receipt.messageId||!receipt.id)return {state:'not-recorded'};
    return this.transaction(s=>{const previous=s.receipts[receipt.id];if(previous){if(previous.memeId!==id)throw err('meme-receipt-conflict');return {state:'recorded',duplicate:true};}
      const i=s.items[id];if(!i)throw err('meme-unavailable');s.receipts[receipt.id]={memeId:id,messageId:receipt.messageId,at:this.clock()};i.lastUsedAt=this.clock();i.useCount++;return {state:'recorded'};});
  }
  importPack(manifest,root,{dryRun=false}={}) {
    if(manifest?.schema!=='kin-meme-pack/v1'||manifest.original!==true||!str(manifest.source??'',400)||!Array.isArray(manifest.items)||!manifest.items.length||manifest.items.length>40)throw err('meme-invalid-pack');
    if(manifest.id?.length>40||manifest.items.some(x=>x.id?.length>40))throw err('meme-invalid-pack-id');
    const pack={id:requireId(manifest.id),name:str(manifest.name,100),aliases:tags(manifest.aliases??[])};
    if(!pack.name)throw err('meme-invalid-pack');
    const base=fs.realpathSync(root),seen=new Set();
    const prepared=manifest.items.map(x=>{requireId(x.id);if(seen.has(x.id)||!SHA.test(x.sha256??''))throw err('meme-invalid-pack-item');seen.add(x.id);
      if(typeof x.file!=='string'||path.isAbsolute(x.file)||x.file.split(/[\\/]/).includes('..'))throw err('meme-pack-path');
      const real=fs.realpathSync(path.join(base,x.file));if(!real.startsWith(base+path.sep)||!fs.statSync(real).isFile())throw err('meme-pack-path');
      const data=fs.readFileSync(real),info=inspectMemeImage(data);if(info.mime!=='image/png'||info.width<256||info.height<256||info.width!==info.height||memeHash(data)!==x.sha256)throw err('meme-pack-image');
      const meta=memeMetadata(x);if(!meta.description)throw err('meme-pack-index-required');
      return {data,info,hash:x.sha256,meta,source:{channel:'pack',id:pack.id+':'+x.id},group:pack.id,preferredId:pack.id+'-'+x.id};});
    if(dryRun)return {state:'valid',pack:pack.id,count:prepared.length};
    return this.transaction(s=>{if(!s.packs[pack.id]&&Object.keys(s.packs).length>=MEME_LIMITS.packs)throw err('meme-pack-budget');s.packs[pack.id]=pack;const items=prepared.map(p=>this.insert(s,p));return {state:'imported',pack:pack.id,count:items.length,items:items.map(x=>x.item.id)};});
  }
}

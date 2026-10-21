import {randomUUID} from 'node:crypto';
import {MemeLibrary,MEME_LIMITS,memeHash,inspectMemeImage} from './meme-library.mjs';
const fail=reason=>({state:'skipped',reason});
export const memeVisionPath=context=>context?.visionPath??(context?.lane==='proactive'?'native-assess-image/v1':'mcp-image-content/v1');
const identity=x=>JSON.stringify([x?.sourceId,x?.sessionId,x?.model,x?.provider,memeVisionPath(x),x?.lane]);
/** Ephemeral pixel offers are bound to the actual turn and backend, not names supplied
 * by model arguments. A persistent capability proof must come from native validation,
 * never the model catalog. Each lane is checked separately. */
export function verifiedMemeVision(context,proofs=[]) {
  return Boolean(context?.sourceId&&context?.sessionId&&context?.model&&context?.provider&&['chat','proactive'].includes(context.lane)&&
    proofs.some(p=>p?.state==='verified'&&p.mode==='pixels'&&p.model===context.model&&p.provider===context.provider&&p.path===memeVisionPath(context)&&p.lane===context.lane&&typeof p.nativeTurnId==='string'&&p.nativeTurnId&&p.fixtureSha256?.match(/^[a-f0-9]{64}$/)));
}
export class MemeSession {
  constructor({library,proofs=()=>[],clock=()=>Date.now()}={}){if(!(library instanceof MemeLibrary))throw Error('meme-library-required');this.library=library;this.proofs=proofs;this.clock=clock;this.inputs=new Map();this.views=new Map();}
  prune(){for(const m of [this.inputs,this.views]){for(const [id,v] of m)if(v.expires<this.clock())m.delete(id);while(m.size>32)m.delete(m.keys().next().value);}
    let bytes=0;for(const entry of [...this.inputs.values()].reverse())bytes+=entry.images.reduce((n,i)=>n+i.data.length,0);
    while(bytes>32*1024*1024&&this.inputs.size){const id=this.inputs.keys().next().value;bytes-=this.inputs.get(id).images.reduce((n,i)=>n+i.data.length,0);this.inputs.delete(id);}}
  receive({sourceId,channel,images}) {
    this.prune();if(!sourceId||!['feishu','wechat'].includes(channel))return [];
    const retained=[];for(const [index,image] of (images??[]).slice(0,6).entries())try {
      const data=Buffer.from(image.data,'base64');inspectMemeImage(data);retained.push({id:sourceId+':'+index,data,source:{channel,id:sourceId+':'+index}});
    }catch{/* Normal attachment handling continues for unsupported/large images. */}
    if(retained.length)this.inputs.set(sourceId,{images:retained,expires:this.clock()+30*60*1000});
    this.prune();return (this.inputs.get(sourceId)?.images??[]).map(i=>({attachment_id:i.id}));
  }
  inputImages(sourceId){this.prune();return (this.inputs.get(sourceId)?.images??[]).map(i=>({attachment_id:i.id}));}
  collect(args,context) {
    this.prune();const image=this.inputs.get(context?.sourceId)?.images.find(i=>i.id===args.attachment_id);
    if(!image)return fail('input-image-unavailable');
    if(args.choice!=='collect')return fail('not-chosen');
    // Explicit intent is a declared conversation action, not inferred from OCR. The
    // host limits the source to the authenticated current owner turn.
    const explicit=args.basis==='explicit';
    if(!explicit&&!verifiedMemeVision({...context,visionPath:'owner-image-input/v1'},this.proofs()))return fail('vision-unverified');
    return this.library.collect({data:image.data,source:image.source,metadata:args.metadata??{},group:args.group??'collected',intent:explicit?{kind:'explicit'}:{kind:'interesting',visual:true,isMeme:args.is_meme===true}});
  }
  inspect(ids,context) {
    this.prune();if(!verifiedMemeVision(context,this.proofs()))return fail('vision-unverified');
    if(!Array.isArray(ids)||!ids.length||ids.length>MEME_LIMITS.candidates||new Set(ids).size!==ids.length)return fail('invalid-candidates');
    const images=ids.map(id=>this.library.readImage(id));
    const token='view-'+randomUUID();this.views.set(token,{identity:identity(context),images:images.map(i=>({id:i.item.id,hash:i.item.hash})),expires:this.clock()+10*60*1000});
    return {state:'pixels',inspection_id:token,images:images.map(({item,data})=>({id:item.id,sha256:item.hash,mimeType:item.mime,data:data.toString('base64')}))};
  }
  resolve(reference,context) {
    this.prune();if(!verifiedMemeVision(context,this.proofs()))return null;
    const view=this.views.get(reference?.inspection_id);if(!view||view.identity!==identity(context))return null;
    const found=view.images.find(i=>i.id===reference.meme_id);if(!found)return null;
    const {item,data}=this.library.readImage(found.id);if(item.hash!==found.hash)return null;
    // This immutable payload is frozen with the existing delivery manifest; no path
    // supplied by a model, no deferred file lookup, no separate sender/recipient.
    return {meme_id:item.id,hash:item.hash,mimeType:item.mime,data:data.toString('base64'),inspection_id:reference.inspection_id};
  }
}
export function memeBubble(value){return Boolean(value&&typeof value==='object'&&!Array.isArray(value)&&value.type==='image'&&
  Object.keys(value).every(k=>['type','meme_id','inspection_id'].includes(k))&&/^meme-[a-zA-Z0-9_-]+$|^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$/.test(value.meme_id??'')&&/^view-[a-f0-9-]{36}$/.test(value.inspection_id??''));}
export function frozenMemeMedia(image) {
  if(!image||typeof image.data!=='string'||image.data.length>Math.ceil(MEME_LIMITS.bytes/3)*4)throw Error('meme-invalid-payload');
  const data=Buffer.from(image.data,'base64');inspectMemeImage(data);if(memeHash(data)!==image.hash)throw Error('meme-content-changed');
  return {type:'image',data,name:'meme.'+(image.mimeType==='image/jpeg'?'jpg':'png'),artifact_id:'meme:'+image.meme_id+':'+image.hash};
}

import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

/** Outbox entries are local evidence, never an instruction or model identity. */
export function outboxEvidence(directories) {
  const values=[];
  for(const directory of directories) {
    if(!fs.existsSync(directory))continue;
    for(const name of fs.readdirSync(directory).filter(n=>n.endsWith('.json'))) {
      try {
        const value=JSON.parse(fs.readFileSync(path.join(directory,name),'utf8'));
        if(!value.id||!value.state)continue;
        values.push({id:value.id,text:value.text??'',state:value.state,references:value.references??[],
          draft_id:value.draftId,message_id:value.messageId,at:value.acceptedAt??value.checkedAt??value.attemptedAt});
      } catch { /* Incomplete atomic replacements are retried from the journal. */ }
    }
  }
  return values.sort((a,b)=>(b.at??'').localeCompare(a.at??''));
}

export class ReplyGuard {
  constructor({call,directory,outbox=()=>[],clock=()=>Date.now()}) {Object.assign(this,{call,directory,outbox,clock});this.active=new Map();}
  check(request) {
    const key=createHash('sha256').update(JSON.stringify(request)).digest('hex');
    if(this.active.has(key))return this.active.get(key);
    const run=this.inspect(request,key).finally(()=>this.active.delete(key));this.active.set(key,run);return run;
  }
  async inspect(request,key) {
    const file=path.join(this.directory,key+'.json');fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    let old;try{old=JSON.parse(fs.readFileSync(file,'utf8'));}catch{}
    if(old?.state==='silent'||old?.state==='merged')return old;
    if(old?.state==='pending'&&old.retryAt>this.clock())return old;
    let result;
    try {
      if(!request.work&&request.reply_id) {
        const choice=await this.call('reply-status',{input_id:request.reply_id});
        if(['silent','merged'].includes(choice.action))return this.save(file,{state:choice.action,choice});
      }
      result=await this.call('share-preflight',{...request,outbox:await this.outbox(),allow_model:true});
    } catch {result={state:'pending',reason:'share-review-unavailable'};}
    return this.save(file,{...result,...(result.state==='pending'?{retryAt:this.clock()+20*60000}:{})});
  }
  save(file,value){const temporary=file+'.'+process.pid+'.tmp';fs.writeFileSync(temporary,JSON.stringify(value),{mode:0o600});fs.renameSync(temporary,file);return value;}
  defer(request,delivery,ownerEpoch) {
    fs.mkdirSync(this.directory,{recursive:true,mode:0o700});
    const file=path.join(this.directory,createHash('sha256').update(delivery.id).digest('hex')+'.pending.json');
    let prior;try{prior=JSON.parse(fs.readFileSync(file,'utf8'));}catch{}
    if(prior)return prior;
    return this.save(file,{state:'pending',request,delivery,ownerEpoch,at:this.clock(),retryAt:this.clock()+20*60000});
  }
  async resumeDue({guard,send,limit=2}) {
    if(this.resuming||!fs.existsSync(this.directory))return {state:'idle'};
    this.resuming=true;let handled=0;
    try {
      const files=fs.readdirSync(this.directory).filter(n=>n.endsWith('.pending.json'));
      for(const name of files) {
        const file=path.join(this.directory,name);let entry=JSON.parse(fs.readFileSync(file,'utf8'));
        if(!['pending','prepared'].includes(entry.state)||entry.retryAt>this.clock())continue;
        if(handled++>=limit)break;
        let permission=await guard(entry);
        if(permission==='wait')continue;
        if(permission==='cancel'){await this.call('share-cancel',{draft_id:entry.request.draft_id});this.save(file,{...entry,state:'canceled'});continue;}
        const checked=entry.state==='prepared'?entry.checked:await this.check(entry.request);
        permission=await guard(entry);
        if(permission!=='send')continue;
        if(checked.state==='pending'){this.save(file,{...entry,retryAt:this.clock()+20*60000});continue;}
        if(checked.state!=='ready'){this.save(file,{...entry,state:checked.state});continue;}
        // Freeze body and references before transport. A crash resumes the
        // same platform ID; an uncertain receipt never creates a new send.
        entry=this.save(file,{...entry,state:'prepared',checked,delivery:{...entry.delivery,text:checked.text??entry.request.text,references:checked.references??[]}});
        try {
          const receipt=await send(entry.delivery);
          this.save(file,{...entry,state:receipt.state==='accepted'&&receipt.messageId?'accepted':'unconfirmed',receipt});
        } catch {this.save(file,{...entry,state:'unconfirmed'});}
      }
      return {state:handled?'checked':'idle',checked:Math.min(handled,limit)};
    } finally {this.resuming=false;}
  }
}

/** Test doubles only: a platform that never leaves the process and a transport
 * that keeps the same receipt discipline as the two real channels. Nothing
 * here can reach a phone. `flavour:'feishu'` checkpoints `submissionStarted`
 * the way the Feishu sender does; `flavour:'wechat'` writes a bare `pending`
 * receipt right before its request, the way the WeChat receipt fetch does. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {readJsonFile,writeJsonAtomic} from '../atomic-json.mjs';
import {classifyReceipt} from '../channel-contract.mjs';

const sha256=value=>createHash('sha256').update(value).digest('hex');
const take=(rules,subject)=>{const rule=rules.find(r=>r.times>0&&r.when(subject));if(rule)rule.times--;return rule?.outcome;};

/** Scripted outcomes: accept (default) | reject | timeout (landed, answer
 * lost) | lost (never landed) | no-id (accepted without a message ID). The
 * platform deduplicates one idempotency key inside its window. */
export function createFakePlatform({clock=()=>Date.now(),windowMs=3600000}={}) {
  const calls=[],delivered=[],rules=[],watchers=[],seen=new Map();let counter=0;
  const land=request=>{
    const prior=seen.get(request.key);
    if(prior&&clock()-prior.at<windowMs)return prior.messageId;
    const messageId='om_'+(++counter);seen.set(request.key,{at:clock(),messageId});delivered.push({...request,messageId});return messageId;
  };
  return {calls,delivered,
    script(outcome,{when=()=>true,times=1}={}){rules.push({outcome,when,times});},
    onSubmit(fn){watchers.push(fn);},
    async submit(request) {
      for(const watch of watchers)await watch(request);
      calls.push({...request,at:clock()});
      const outcome=take(rules,request)??'accept';
      if(outcome==='reject')return {code:230099};
      if(outcome==='lost')throw Object.assign(Error('Network unreachable'),{name:'FetchError'});
      if(outcome==='timeout'){land(request);throw Object.assign(Error('Request timed out'),{name:'TimeoutError'});}
      if(outcome==='no-id'){land(request);return {code:0,data:{}};}
      return {code:0,data:{message_id:land(request)}};
    }};
}

/** Faults before the network: before-receipt (nothing written), before-submit
 * (a receipt that proves nothing was submitted), upload-failed (`not-submitted`),
 * crash-mid-submit (the receipt says submission began; no outcome). */
export function createFakeTransport({directory,platform=createFakePlatform(),clock=()=>Date.now(),flavour='feishu'}) {
  const rules=[],events=[],sends=[];
  const file=id=>path.join(directory,id+'.json');
  const read=id=>{const result=readJsonFile(file(id));return result.state==='ok'?result.value:null;};
  const write=record=>{fs.mkdirSync(directory,{recursive:true});writeJsonAtomic(file(record.id),record,{previous:false});return record;};
  const stamp=()=>new Date(clock()).toISOString();
  async function send(delivery) {
    const {id,text='',media,memory,resend}=delivery;sends.push(delivery);
    if(!/^[A-Za-z0-9_-]{1,120}$/.test(id??'')||(!text.trim()&&!media))throw Error('Message ID and content are required');
    const digest=sha256(text+'\0'+(media?media.name+'\0'+sha256(media.data):''));
    const previous=read(id),known=classifyReceipt(previous);
    if(previous?.digest&&previous.digest!==digest)throw Error('Message ID already belongs to different content');
    if(known==='accepted')return previous;
    // Only a receipt proving nothing was submitted may start over; an unknown
    // one only on an explicit resend, which reuses the same idempotency key.
    if(previous&&known!=='never-started'&&!(resend&&known==='unknown'&&flavour==='feishu'))throw Object.assign(Error('Previous send must be reconciled before retry'),{code:'KIN_SEND_NEEDS_RECONCILE'});
    const fault=take(rules,delivery);
    if(fault==='before-receipt'||(fault==='before-submit'&&flavour==='wechat'))throw Error('Transport unavailable before any receipt');
    const remember=record=>{if(memory!==false)events.push({id,state:record.state});return record;};
    let record=remember(write({id,digest,state:'pending',attemptedAt:stamp(),...(flavour==='feishu'?{submissionStarted:false}:{}),...(media?{media:{type:media.type,name:media.name,bytes:media.data.length}}:{})}));
    if(fault==='before-submit')throw Error('Transport failed before submission');
    if(fault==='upload-failed'){remember(write({...record,state:'not-submitted',stage:'upload-failed',checkedAt:stamp()}));throw Error('Upload failed');}
    if(flavour==='feishu')record=write({...record,stage:'message-submitting',submissionStarted:true});
    if(fault==='crash-mid-submit')throw Object.assign(Error('Process stopped mid-submit'),{name:'SimulatedCrash'});
    let answer;
    try{answer=await platform.submit({key:sha256(id).slice(0,32),id,body:media?'[file '+media.name+']':text});}
    catch(error){remember(write({...record,state:'unconfirmed',stage:'message-unconfirmed',checkedAt:stamp()}));throw error;}
    if(answer?.code!==0){remember(write({...record,state:'rejected',stage:'platform-rejected',checkedAt:stamp()}));throw Error('Platform rejected send');}
    if(!answer.data?.message_id){remember(write({...record,state:'unconfirmed',stage:'message-unconfirmed',checkedAt:stamp()}));throw Error('Platform returned no message ID');}
    return remember(write({...record,state:'accepted',stage:'platform-accepted',messageId:answer.data.message_id,acceptedAt:stamp()}));
  }
  return {send,receipt:async id=>read(id),platform,events,sends,flavour,
    fault(outcome,{when=()=>true,times=1}={}){rules.push({outcome,when,times});}};
}

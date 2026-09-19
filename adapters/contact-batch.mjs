import {createHash} from 'node:crypto';
import {splitChatText} from './chat-bubbles.mjs';
import {CHANNEL_CONTRACTS,channelContract,classifyReceipt,normalizeReceipt} from './channel-contract.mjs';
import {fragmentText,fileFragment} from './text-fragments.mjs';
import {transportId} from './transport-manifest.mjs';

/** Verdicts that refuse a whole group. None of them applies to one bubble. */
const REFUSED=['duplicate','silent','merged'];
const BEGUN=['pending','accepted','unconfirmed'];
const REVIEW_STATES=new Set(['ready','pending','needs-review',...REFUSED]);
const HOLD_BASE_MS=60000,HOLD_MAX_MS=15*60000;
const FAILURE_CATEGORIES=new Set(['contract','model-unavailable','source-changed','semantic-hold','model-output','unknown']);
const RETRY_CONDITIONS=new Set(['repair-input','backoff','source-change','deepseek-decision','reconcile','none']);
/** A platform's own refusal code travels as a code and never as a message. */
const platformCode=record=>{const value=String(record?.platformCode??record?.code??'');return /^[A-Za-z0-9_.:-]{1,64}$/.test(value)?value:undefined;};
const staticToken=(value,fallback)=>{value=String(value??'');return /^[A-Za-z0-9_.:-]{1,96}$/.test(value)?value:fallback;};

/** Only provider/accounting facts cross the contact receipt. Prompts, provider
 * bodies and exception prose are deliberately not part of this projection. */
function modelReceipt(record,depth=0) {
  if(!record||typeof record!=='object'||Array.isArray(record)||depth>2)return undefined;
  const value={};
  for(const key of ['provider','model','reasoning','purpose','request_id','outcome','usage_status','verified_at']) {
    if(typeof record[key]==='string'&&record[key].length<=200)value[key]=record[key];
  }
  for(const key of ['elapsed_ms','model_requests'])if(Number.isFinite(record[key])&&record[key]>=0)value[key]=record[key];
  if(typeof record.cache_hit==='boolean')value.cache_hit=record.cache_hit;
  if(record.usage===null)value.usage=null;
  else if(record.usage&&typeof record.usage==='object'&&!Array.isArray(record.usage)) {
    const usage=Object.fromEntries(Object.entries(record.usage).filter(([key,amount])=>/^[A-Za-z0-9_.:-]{1,64}$/.test(key)&&Number.isFinite(amount)&&amount>=0));
    if(Object.keys(usage).length)value.usage=usage;
  }
  if(Array.isArray(record.chunks))value.chunks=record.chunks.slice(0,16).map(item=>modelReceipt(item,depth+1)).filter(Boolean);
  if(record.schema_repair&&typeof record.schema_repair==='object') {
    const repair={};
    if(Number.isSafeInteger(record.schema_repair.attempts)&&record.schema_repair.attempts>=0)repair.attempts=record.schema_repair.attempts;
    const rejected=modelReceipt(record.schema_repair.rejected_call,depth+1);if(rejected)repair.rejected_call=rejected;
    if(Object.keys(repair).length)value.schema_repair=repair;
  }
  return Object.keys(value).length?value:undefined;
}

function reviewFailure(verdict={},fallback={}) {
  const supplied=verdict.failure&&typeof verdict.failure==='object'?verdict.failure:{};
  const category=FAILURE_CATEGORIES.has(supplied.category)?supplied.category:(fallback.category??(verdict.transient===true?'unknown':'semantic-hold'));
  const stage=staticToken(supplied.stage,fallback.stage??(category==='semantic-hold'?'contact-review-semantic':'contact-review-model'));
  const code=staticToken(supplied.code,staticToken(verdict.reason,fallback.code??'contact-review-unclassified'));
  const wanted=supplied.retry_condition??supplied.retryCondition??fallback.retry_condition??({
    contract:'repair-input','model-unavailable':'backoff','source-changed':'source-change','semantic-hold':'deepseek-decision','model-output':'backoff',unknown:'backoff',
  }[category]);
  const retry_condition=RETRY_CONDITIONS.has(wanted)?wanted:'backoff';
  const receipt=modelReceipt(supplied.model_receipt??supplied.modelReceipt??verdict.receipt);
  const model_invoked=typeof supplied.model_invoked==='boolean'?supplied.model_invoked:undefined;
  return {category,stage,code,retry_condition,...(model_invoked===undefined?{}:{model_invoked}),...(receipt?{model_receipt:receipt}:{})};
}

/** The journal freezes content and IDs before sending. Unknown sends only reconcile. */
export function createContactBatch({read,write,send,receipt=()=>null,eligible=()=>true,
  preflight=async request=>({state:'ready',checked:request.entries.map(entry=>({state:'ready',draft_id:entry.draft_id,text:entry.text,references:entry.references??[]}))}),
  verifyFile=async()=>{throw Error('File delivery is not configured');},contracts={},maxHolds=6,maxReviewFailures=6,maxFailures=6,now=()=>Date.now()}) {
  const active=new Map();
  const digest=value=>createHash('sha256').update(value).digest('hex');
  // Proactive contact leaves through the Feishu bridge; a channel with no
  // published contract is measured by that one, with injected corrections.
  const textLimit=channel=>channelContract(CHANNEL_CONTRACTS[channel]?channel:'feishu',contracts[channel]).text;
  /** One entry per fragment, each with its own frozen ID: ordinary journal
   * entries, so the previous batch code replays the same bodies in the same
   * order. Content that no cut can fit stays whole and says why. */
  function cut(item,limit) {
    const plan=fragmentText(item.text,limit);
    if(plan.kind!=='text')return [{...item,oversize:{reason:plan.reason,name:fileFragment(item.text).name}}];
    if(plan.fragments.length<2)return [item];
    return plan.fragments.map(part=>({...item,id:transportId(item.id,part.index,part.body_sha256),text:item.text.slice(part.start,part.end),
      references:part.index?[]:item.references,part:{of:item.id,index:part.index,count:plan.fragments.length,body_sha256:part.body_sha256}}));
  }
  /** Freeze the reviewed bodies and references, then cut what the channel
   * cannot carry. A group that has already begun is never cut again. */
  function freeze(batch,reviewed,verdict) {
    const limit=textLimit(batch.channel),begun=batch.items.some(x=>BEGUN.includes(x.state)),items=[];
    for(const item of batch.items) {
      const index=reviewed.indexOf(item);
      if(index<0){items.push(item);continue;}
      const checked=verdict.checked?.[index]??{};
      if(Array.isArray(checked.references))item.references=checked.references;
      if(checked.text?.trim()&&checked.text!==item.text){item.originalText=item.text;item.text=checked.text;}
      items.push(...(begun?[item]:cut(item,limit)));
    }
    batch.items=items;batch.review={at:now(),...(verdict.review_id?{id:verdict.review_id}:{})};
    delete batch.reason;delete batch.failure;delete batch.reviewNotBefore;
  }
  /** Old journals did not persist the review identity separately from the
   * transport identity. A wholly unsent, unreviewed group can safely adopt its
   * already-frozen item IDs. Anything reviewed or exposed keeps the old group
   * draft ID; pending/unconfirmed transports therefore never change identity. */
  async function identities(batch,id) {
    const preserveLegacy=Boolean(batch.review)||batch.items.some(item=>BEGUN.includes(item.state));
    let changed=false,bubbleIndex=0;
    for(const item of batch.items) {
      if(!item.file) {
        if(!Number.isSafeInteger(item.bubbleIndex)||item.bubbleIndex<0){item.bubbleIndex=bubbleIndex;changed=true;}
        bubbleIndex++;
      }
      if(typeof item.draftId!=='string'||!item.draftId) {item.draftId=preserveLegacy?batch.id:item.id;changed=true;}
      if(BEGUN.includes(item.state)&&!batch.deliveryStarted){batch.deliveryStarted=true;changed=true;}
    }
    if(!batch.identity||batch.identity.version!==2) {batch.identity={version:2,groupId:batch.id,legacyGroupDraftIds:preserveLegacy};changed=true;}
    if(changed)await write(id,batch); // migration is durable before review or send
  }
  function checkedContract(reviewed,verdict) {
    if(!Array.isArray(verdict.checked)||verdict.checked.length!==reviewed.length)return false;
    return verdict.checked.every((checked,index)=>checked&&typeof checked==='object'&&!Array.isArray(checked)
      &&checked.draft_id===reviewed[index].draftId&&typeof checked.text==='string'&&Boolean(checked.text.trim())
      &&Array.isArray(checked.references)&&(checked.state===undefined||checked.state==='ready'));
  }
  async function run({id,text,bubbles,files=[],references=[],channel,guard=()=>true,superseded=false}) {
    let batch=await read(id);
    const parts=bubbles??(text===undefined?null:splitChatText(text));
    const contentDigest=parts?digest(JSON.stringify(files.length?{parts,files}:parts)):null;
    if(batch) {
      if(parts&&batch.digest!==contentDigest)throw Error('contact-batch-content-conflict');
      if(references.length&&batch.referenceDigest&&batch.referenceDigest!==digest(JSON.stringify(references)))throw Error('contact-batch-reference-conflict');
    } else {
      if(!Array.isArray(parts)||!parts.length||parts.some(x=>typeof x!=='string'||!x.trim()))throw Error('contact-batch-empty');
      if(!Array.isArray(files)||files.length>24)throw Error('contact-batch-invalid-files');
      batch={id,channel,referenceDigest:digest(JSON.stringify(references)),digest:contentDigest,state:'pending',identity:{version:2,groupId:id,legacyGroupDraftIds:false},items:parts.map((text,index)=>{
        const itemId='kin-bubble-'+digest(id+'\0'+index).slice(0,48);
        return{id:itemId,draftId:itemId,bubbleIndex:index,text,references:references[index]??[],state:'unsent'};
      })};
      batch.items.push(...files.map((file,index)=>{const itemId='kin-file-'+digest(id+'\0'+index+'\0'+file.sha256).slice(0,48);return{id:itemId,draftId:itemId,file,state:'unsent'};}));
      await write(id,batch);
    }
    const transportIds=batch.items.map(item=>item?.id);
    if(transportIds.some(value=>typeof value!=='string'||!value)||new Set(transportIds).size!==transportIds.length)
      return needsReview(batch,id,reviewFailure({},
        {category:'contract',stage:'contact-batch-journal',code:'contact-transport-identities-invalid',retry_condition:'repair-input'}));
    await identities(batch,id);
    // Reconcile first: what the transport wrote down outranks the journal, and
    // the transport's own rule (`classifyReceipt`) decides what it means, so
    // this file and the sender can never disagree about one receipt.
    for(const item of batch.items) {
      if(['accepted','canceled'].includes(item.state))continue;
      if(item.state==='unsent'&&superseded){Object.assign(item,{state:'canceled',reason:'superseded-before-send',submission:'never-started'});await write(id,batch);continue;}
      if(item.state!=='unsent') {
        const known=await receipt(item.id,batch.channel),outcome=classifyReceipt(known);
        if(outcome==='accepted')Object.assign(item,{state:'accepted',messageId:normalizeReceipt(known).messageId});
        // A receipt that proves nothing was submitted: this pass sends the same
        // frozen ID again, under the verdict the group already has. A bubble that
        // never starts is not free of the host's time, so it is offered the same
        // bounded number of attempts the transport gives a group, and then given
        // up on under a static reason. The rest of the group still goes out.
        else if(outcome==='never-started') {
          item.restarts=(item.restarts??0)+1;
          if(item.restarts>maxFailures)Object.assign(item,{state:'canceled',reason:'transport-never-started',submission:'never-started'});
          else item.state='unsent';
        }
        // A refusal is final for this bubble alone; the rest of the group goes on.
        else if(outcome==='rejected')Object.assign(item,{state:'canceled',reason:'platform-rejected',...(platformCode(known)?{platformCode:platformCode(known)}:{})});
        // Unknown, or no receipt where the reader looked: nothing is re-sent and
        // nothing is given up on. An operator reconciles this one group.
        else {batch.state='unconfirmed';await write(id,batch);return result(batch);}
        await write(id,batch);
      }
    }
    if(batch.state==='needs-review'&&!superseded)return result(batch);
    // One review of the whole group, before its first bubble is exposed: the
    // group is released, held with a visible reason, or refused as a whole. A
    // verdict never cancels a remainder the owner was already promised, and a
    // group that is already reviewed is never charged for a second one.
    if(!batch.review&&batch.items.some(x=>x.state==='unsent')) {
      if(!await eligible()||!await guard()){batch.state='pending';await write(id,batch);return result(batch);}
      const legacyFailures=batch.reviewFailures??(!batch.holds?(batch.backoffs??0):0);
      if(legacyFailures>=maxReviewFailures)return needsReview(batch,id,reviewFailure({},
        {category:'unknown',stage:'contact-review-model',code:'legacy-review-failures-exhausted',retry_condition:'deepseek-decision'}));
      if(batch.reviewNotBefore&&now()<batch.reviewNotBefore){batch.state='pending';return result(batch);}
      for(const item of batch.items.filter(x=>x.file&&x.state==='unsent')) {
        let checked;
        try {checked=await verifyFile(item.file);}
        catch {return needsReview(batch,id,reviewFailure({},
          {category:'source-changed',stage:'contact-review-source',code:'contact-file-verification-failed',retry_condition:'source-change'}));}
        if(checked.state!=='ready')return hold(batch,id,checked,false);   // a local check, free to repeat
      }
      const reviewed=batch.items.filter(x=>!x.file&&x.state==='unsent');
      const entries=reviewed.map(item=>({draft_id:item.draftId,text:item.text,references:item.references??[],bubble_index:item.bubbleIndex}));
      if(new Set(entries.map(entry=>entry.draft_id)).size!==entries.length)return needsReview(batch,id,reviewFailure({},
        {category:'contract',stage:'contact-review-contract',code:'contact-review-identities-invalid',retry_condition:'repair-input'}));
      const body=entries.map(e=>e.text).join('\n\n');
      // `text` and `references` describe the same group as `entries`: a reviewer
      // that only knows the older shape still judges the whole body, never a part.
      let verdict;
      try {verdict=entries.length?await preflight({batch_id:batch.id,draft_id:batch.id,channel:batch.channel,entries,text:body,batch_text:body,
        references:entries.flatMap(e=>e.references)}):{state:'ready',checked:[]};}
      catch(error) {const failure=reviewFailure(error,
        {category:'unknown',stage:'contact-review-model',code:'contact-review-call-failed',retry_condition:'backoff'});
        verdict={state:'pending',reason:failure.code,failure};}
      if(!verdict||typeof verdict!=='object'||Array.isArray(verdict)||!REVIEW_STATES.has(verdict.state))return needsReview(batch,id,reviewFailure({},
        {category:'contract',stage:'contact-review-contract',code:'contact-review-verdict-invalid',retry_condition:'repair-input'}));
      if(REFUSED.includes(verdict.state))return refuse(batch,id,verdict.reason??verdict.state);
      if(verdict.state!=='ready')return hold(batch,id,verdict,true);
      if(!checkedContract(reviewed,verdict))return needsReview(batch,id,reviewFailure({},
        {category:'contract',stage:'contact-review-contract',code:'contact-review-response-mismatch',retry_condition:'repair-input'}));
      freeze(batch,reviewed,verdict);await write(id,batch);
    }
    for(const item of batch.items) {
      if(item.state==='unsent') {
        // A new owner input can arrive while the semantic review is running.
        if(!await eligible()||!await guard()){batch.state='pending';await write(id,batch);return result(batch);}
        item.state='pending';delete item.submission;batch.deliveryStarted=true;await write(id,batch);
        try {
          const sent=await send({id:item.id,text:item.text,file:item.file,channel:batch.channel,memoryBatchId:batch.id,expectedBubbles:batch.items.length,references:item.references,draftId:item.draftId,
            // Unsplittable: the whole body as one file, with no words of the host's own.
            // A sender that understands `media` delivers the file INSTEAD of the text, never both;
            // `text` rides along only so that a sender that does not still has a body.
            ...(item.oversize?{media:{type:'file',name:item.oversize.name,data:Buffer.from(item.text,'utf8')}}:{})});
          if(sent?.state!=='accepted'||!sent.messageId)throw Error('receipt-unconfirmed');
          Object.assign(item,{state:'accepted',messageId:sent.messageId});
        } catch {item.state='unconfirmed';batch.state='unconfirmed';await write(id,batch);return result(batch);}
      }
      await write(id,batch);
    }
    const allNeverStarted=batch.items.length>0&&batch.items.every(item=>item.state==='canceled'&&item.submission==='never-started');
    if(!superseded&&allNeverStarted&&batch.items.some(item=>item.reason==='transport-never-started')) {
      batch.state='needs-review';batch.safeToRelease=true;
      batch.failure=reviewFailure({},
        {category:'unknown',stage:'contact-delivery',code:'transport-never-started-exhausted',retry_condition:'deepseek-decision'});
      batch.lastFailure=batch.failure;
    } else batch.state=batch.items.some(x=>x.state==='accepted')?'accepted':'canceled';
    if(batch.state==='accepted')delete batch.reason;
    else if(batch.state==='canceled'&&!batch.deliveryStarted&&!batch.items.some(x=>BEGUN.includes(x.state)))batch.safeToRelease=true;
    await write(id,batch);return result(batch);
  }
  /** Held as a whole: semantic holds and operational failures have independent
   * finite budgets. Exhaustion parks a wholly unsent group for DS/operator review
   * instead of pretending the content was rejected or retrying it forever. */
  async function hold(batch,id,verdict,paid) {
    const failure=reviewFailure(verdict);
    batch.failure=failure;batch.lastFailure=failure;
    if(['contract','source-changed'].includes(failure.category)||verdict.state==='needs-review')return needsReview(batch,id,failure,verdict.reason);
    const judged=paid&&failure.category==='semantic-hold';
    if(judged&&(batch.holds=(batch.holds??0)+1)>=maxHolds)return needsReview(batch,id,{...failure,code:'contact-review-held-too-long',retry_condition:'deepseek-decision'},'contact-review-held-too-long');
    if(paid&&!judged&&(batch.reviewFailures=(batch.reviewFailures??0)+1)>=maxReviewFailures)
      return needsReview(batch,id,{...failure,code:'contact-review-failures-exhausted',retry_condition:'deepseek-decision'},'contact-review-failures-exhausted');
    if(paid)batch.backoffs=(batch.backoffs??0)+1;
    batch.reviewNotBefore=verdict.retryAt??now()+(verdict.retryAfterMs??Math.min(HOLD_MAX_MS,HOLD_BASE_MS*2**Math.max(0,(batch.backoffs??1)-1)));
    batch.reason=verdict.reason;batch.state='pending';await write(id,batch);return result(batch);
  }
  async function needsReview(batch,id,failure,reason) {
    if(batch.items.some(item=>BEGUN.includes(item.state))||batch.deliveryStarted) {
      batch.failure={...failure,retry_condition:'reconcile'};batch.reason=reason??failure.code;batch.state='unconfirmed';
      await write(id,batch);return result(batch);
    }
    batch.failure=failure;batch.lastFailure=failure;batch.reason=reason??failure.code;
    batch.state='needs-review';batch.safeToRelease=true;delete batch.reviewNotBefore;
    await write(id,batch);return result(batch);
  }
  /** Refused as a whole: every bubble that was never exposed is canceled under
   * one reason, and that reason stays on the finished batch. What was already
   * sent stays sent; the review precedes the first bubble, so usually nothing was. */
  async function refuse(batch,id,reason) {
    const semanticReason=typeof reason==='string'&&reason.trim()?reason.trim().slice(0,1200):'Whole-group semantic review declined the unsent draft';
    for(const item of batch.items.filter(x=>x.state==='unsent'))Object.assign(item,{state:'canceled',reason});
    batch.reason=reason;batch.decision={action:'abandon',reason:semanticReason};batch.state=batch.items.some(x=>x.state==='accepted')?'accepted':'canceled';
    batch.safeToRelease=!batch.deliveryStarted&&!batch.items.some(x=>BEGUN.includes(x.state));delete batch.failure;
    await write(id,batch);return result(batch);
  }
  function result(batch){const ids=batch.items.filter(x=>x.state==='accepted').map(x=>x.messageId);return {
    id:batch.id,state:batch.state,reason:batch.reason,channel:batch.channel,messageId:batch.state==='accepted'?ids[0]:undefined,
    messageIds:ids,acceptedBubbles:ids.length,totalBubbles:batch.items.length,
    canceledBubbles:batch.items.filter(x=>x.state==='canceled').length,
    partial:batch.items.some(x=>x.state==='canceled'),visibility:'unverified',
    ...(batch.failure?{failure:batch.failure}:{}),...(batch.decision?{decision:batch.decision}:{}),
    ...(batch.safeToRelease?{safeToRelease:true}: {})};}
  return request=>{
    if(active.has(request.id))return active.get(request.id);
    const pending=run(request).finally(()=>active.delete(request.id));active.set(request.id,pending);return pending;
  };
}

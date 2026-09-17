/** What each phone channel promises, and how one of its send receipts reads.
 * A declared capability is not evidence: the transport contract tests
 * (`testing/transport-contract.mjs`) decide what a channel really does. */
import path from 'node:path';
import {readJsonFile} from './atomic-json.mjs';

/** Text measures. A fragment must fit the platform limit in the unit the
 * platform counts, and every measure is additive over whole characters.
 * `json2-utf8` is the size a text takes inside a Feishu request: the body
 * carries `content` as a JSON string that itself holds JSON, so a newline
 * costs three bytes and a quote or backslash four. */
const MEASURES=Object.freeze({
  utf16:text=>text.length,
  utf8:text=>Buffer.byteLength(text,'utf8'),
  'json2-utf8':text=>Buffer.byteLength(JSON.stringify(JSON.stringify(text)),'utf8')-6,
});
export function measureText(kind) {
  if(typeof kind==='function')return kind;
  if(!Object.hasOwn(MEASURES,kind))throw Error('Unknown text measure');
  return MEASURES[kind];
}

export const CHANNEL_CONTRACTS=Object.freeze({
  // open.feishu.cn im-v1/message/create, read 2026-09-17: a text request body
  // is at most 150 KB; one uuid succeeds at most once within an hour (50
  // characters at most); 5 requests a second per user. What a duplicate uuid
  // returns is not documented, so a repeated submit is never assumed safe.
  feishu:Object.freeze({channel:'feishu',text:Object.freeze({limit:120*1024,measure:'json2-utf8'}),file:Object.freeze({maxBytes:50*1024*1024}),
    idempotencyKey:'uuid',idempotencyKeyMaxLength:50,idempotencyWindowMs:3600000,duplicateResponse:'undocumented',
    resendWithinWindow:false,resendSafetyMs:60000,maxSendsPerSecond:5}),
  // 4000 UTF-16 units is what the bridge library cuts at. Nothing is known
  // about duplicate client IDs, so there is no resend window at all.
  wechat:Object.freeze({channel:'wechat',text:Object.freeze({limit:4000,measure:'utf16'}),file:Object.freeze({maxBytes:10*1024*1024}),
    idempotencyKey:'client_id',idempotencyWindowMs:0,duplicateResponse:'unknown',
    resendWithinWindow:false,resendSafetyMs:60000,maxSendsPerSecond:2}),
});

/** The contract for a channel, with caller-injected corrections on top. */
export function channelContract(channel,overrides={}) {
  const base=CHANNEL_CONTRACTS[channel];
  if(!base)throw Error('Unknown delivery channel');
  return Object.freeze({...base,...overrides,text:Object.freeze({...base.text,...overrides.text}),file:Object.freeze({...base.file,...overrides.file})});
}

const token=value=>typeof value==='string'&&/^[A-Za-z0-9_.:-]{1,64}$/.test(value)?value:undefined;
/** One shape for both channels' receipt files and for outbox evidence rows.
 * It carries states, times and platform IDs only, never message text. */
export function normalizeReceipt(record) {
  if(!record||typeof record!=='object'||typeof record.state!=='string')return null;
  const literal=typeof record.responseText==='string'?record.responseText.match(/"message_id"\s*:\s*("[0-9]+"|[0-9]+)/)?.[1]?.replaceAll('"',''):undefined;
  const messageId=record.messageId??record.message_id??literal;
  return {state:record.state,...(messageId?{messageId:String(messageId)}:{}),
    ...(typeof (record.submissionStarted??record.submission_started)==='boolean'?{submissionStarted:record.submissionStarted??record.submission_started}:{}),
    ...(record.acceptedAt?{acceptedAt:record.acceptedAt}:{}),...(record.checkedAt?{checkedAt:record.checkedAt}:{}),
    ...(record.attemptedAt?{attemptedAt:record.attemptedAt}:{}),...(token(record.stage??record.reason)?{reason:token(record.stage??record.reason)}:{})};
}

/** The reconcile rule, from what the transport wrote down before and after it
 * touched the network:
 *   absent        no receipt: the send never began
 *   never-started a receipt that proves nothing was submitted
 *   accepted      the platform took it and named a message
 *   rejected      the platform definitively refused it
 *   unknown       anything else; the network effect cannot be told */
export function classifyReceipt(record) {
  const receipt=normalizeReceipt(record);
  if(!receipt)return 'absent';
  if(receipt.state==='accepted')return receipt.messageId?'accepted':'unknown';
  if(receipt.state==='rejected')return 'rejected';
  if(receipt.submissionStarted!==true&&(receipt.state==='not-submitted'||(receipt.state==='pending'&&receipt.submissionStarted===false)))return 'never-started';
  return 'unknown';
}

/** Error codes with which a transport says "this ID already has a receipt I
 * cannot resolve; I will not submit it again". A caller whose receipt reader
 * sees nothing for that ID is looking in the wrong place: the outcome is
 * unknown, not unsent. */
export const RECONCILE_FIRST_CODES=Object.freeze(['KIN_SEND_NEEDS_RECONCILE','WECHAT_SEND_UNCONFIRMED']);

/** An unknown fragment may be submitted again under the same transport ID only
 * while the platform's own deduplication provably still covers the first
 * attempt, only once, and only when the injected contract allows it. */
export function mayResend(contract,fragment,now) {
  if(contract?.resendWithinWindow!==true||!(contract.idempotencyWindowMs>0)||fragment.resentAt)return false;
  if(!Number.isFinite(fragment.firstSubmitAt)||now<fragment.firstSubmitAt)return false;
  return now-fragment.firstSubmitAt<contract.idempotencyWindowMs-(contract.resendSafetyMs??60000);
}

/** Receipts both channels keep as `<directory>/<transport id>.json`. */
export function fileReceipts(directories) {
  return async id=>{
    if(!/^[A-Za-z0-9_-]{1,128}$/.test(id))return null;
    for(const directory of directories) {
      const result=readJsonFile(path.join(directory,id+'.json'));
      if(result.state==='ok'&&normalizeReceipt(result.value))return normalizeReceipt(result.value);
      if(result.state==='corrupt')return {state:'unreadable'};
    }
    return null;
  };
}

/** What the host itself tells the owner when it could not finish something.
 *
 * The host speaks as the system: never in Kin's voice and never as her
 * judgment. The words are fixed and chosen by the state of the facts alone
 * (SPEC-v2 §1, 失败说明). A notice counts as told only once the platform gave
 * this notice itself a receipt; whoever keeps the ledger reads that receipt. */
import {createHash} from 'node:crypto';

export const OWNER_NOTICE_KINDS=Object.freeze(['stopped','unknown','partial']);
export const OWNER_NOTICE_TEXTS=Object.freeze({
  stopped:'刚才那条暂时没处理成功，消息还留着。我会先核对，不用重复发。',
  unknown:'刚才那条的结果还没确认，我正在按原编号核对，先别重复发。',
  partial:'刚才只送达了一部分，我会核对剩余内容；已送达的不会重发。'});
/** The system's own label, so the owner can always tell these from Kin. */
export const SYSTEM_NOTICE_LABEL='【系统提示】';
const SCOPES=Object.freeze(['input','reply']);
const digest=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');

/** One notice: a transport identity per subject and kind, and its words. The
 * identity for an input is the one the router's ledger records for it
 * (`kin-input-notice-` + the first 32 hex of sha256 of `[inputId, kind]`), so
 * the ledger entry and the send receipt name the same message. */
export function ownerNotice(kind,subject,{scope='input'}={}) {
  if(!OWNER_NOTICE_KINDS.includes(kind))throw Error('Unsupported owner notice');
  if(typeof subject!=='string'||!subject)throw Error('An owner notice names what it is about');
  if(!SCOPES.includes(scope))throw Error('Unsupported owner notice scope');
  return {id:'kin-'+scope+'-notice-'+digest([subject,kind]).slice(0,32),kind,text:SYSTEM_NOTICE_LABEL+OWNER_NOTICE_TEXTS[kind]};
}
/** Notices are the host's words: they never become Kin's delivered words in memory. */
export const isSystemNoticeId=id=>typeof id==='string'&&/^kin-(?:input|reply)-notice-[a-f0-9]{32}$/.test(id);

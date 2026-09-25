/** The one reading of the router's input facts.
 *
 * The router keeps what actually happened to every input: `semantic-pending`,
 * `queued`, `unconfirmed`, `fenced-unconfirmed`, reply receipts, owner notices.
 * None of those facts is merged away here. This module only reads them into the
 * eight states people and deploy tools talk about, and says which inputs still
 * have no outcome. Every other "is anything unsettled" check calls this. */
export const SUMMARY_STATES=Object.freeze(['received','classifying','queued','submitted','answered','failed-notified','superseded','canceled-by-owner']);
/** The three things the host may tell the owner about an input it could not finish.
 * The host speaks as the system, never as Kin. */
export const NOTICE_KINDS=Object.freeze(['stopped','unknown','partial']);
export const NOTICE_TEXTS=Object.freeze({
  stopped:'刚才那条暂时没处理成功，消息还留着。我会先核对，不用重复发。',
  unknown:'刚才那条的结果还没确认，我正在按原编号核对，先别重复发。',
  partial:'刚才只送达了一部分，我会核对剩余内容；已送达的不会重发。'});
/** Before native submission: a retry of the same id is safe only on this evidence. */
export const RETRY_EVIDENCE=Object.freeze(['not-submitted','platform-rejected','reconciled-not-received']);
/** Backoff for an input proven never submitted: the same id, then a notice. */
export const SUBMIT_RETRY_MS=Object.freeze([30000,120000,600000]);
const PRE_NATIVE=new Set(['semantic-pending','selected','preparing','queued']);

export const ownerInput=record=>!record?.kind||record.kind==='owner';
/** A router input whose prompt has begun in the native session and whose turn has not ended. */
export const turnRunning=record=>Boolean(record?.turnStartedAt&&!(record.turnEndedAt>=record.turnStartedAt));
export const answered=record=>Boolean(record?.answer&&['accepted','silent','merged','covered','legacy'].includes(record.answer.state));

/** Still moving through the host: classification, dispatch, the native queue or
 * an unfinished native turn. A deploy waits for these; it never waits for a
 * notice or a reconciliation, which do not change by waiting. */
export function inputInFlight(record) {
  if(!record||record.historical)return false;
  if(PRE_NATIVE.has(record.state)||record.state==='submitting')return true;
  return record.state==='accepted'&&turnRunning(record);
}

/** One of SUMMARY_STATES, or `historical` for a record that predates the ledger.
 * An answer outranks a notice: an input that was told about and then answered after
 * all reads as answered. */
export function inputSummary(record) {
  if(!record)return null;
  if(record.historical)return 'historical';
  if(record.state==='superseded')return 'superseded';
  // A control or maintenance input is answered by its own receipt like any other (CR-LIFE-07).
  if(record.state==='accepted'&&answered(record))return 'answered';
  if(record.canceledBy)return 'canceled-by-owner';
  if(record.ownerNotice?.state==='accepted')return 'failed-notified';
  if(record.state==='semantic-pending')return 'classifying';
  if(['selected','preparing','queued'].includes(record.state))return 'queued';
  if(['submitting','unconfirmed','fenced-unconfirmed'].includes(record.state))return 'submitted';
  if(record.state==='accepted') {
    if(!ownerInput(record))return turnRunning(record)?'submitted':'answered';
    return 'submitted';
  }
  // failed-before-submit and the retired classification states: nothing reached
  // the native session, and the durable inbox job (or its retry) still holds it.
  return 'received';
}

/** True when this input needs nothing more from the host. Anything still in flight
 * is unsettled whatever else is known about it. Internal inputs (the mind's own
 * turns) settle when they leave the native session: their failures are the mind's
 * to retry, and no owner notice is ever owed for them. Owing her no notice is not
 * being settled, though: one whose submission is still to be looked up by its own id,
 * or (restored, of unknown kind) still on its way back to an inbox, stays (CR2-LIFE-10). */
export function inputSettled(record) {
  if(!record)return true;
  if(inputInFlight(record))return false;
  if(!ownerInput(record)) {
    if(record.historical)return true;
    if(record.recovered||['unconfirmed','fenced-unconfirmed'].includes(record.state))return false;
    // What the owner's stop settled is not on its way anywhere.
    return !(record.kind==='unknown'&&record.state==='failed-before-submit'&&!record.canceledBy&&!record.retry?.exhausted);
  }
  return ['answered','failed-notified','superseded','canceled-by-owner','historical'].includes(inputSummary(record));
}

/** What a native-session boundary (compaction, a segment swap) waits for: inputs
 * still moving, and submissions whose outcome is not yet reconciled or reported.
 * A release waits only for `inputInFlight`; neither ever waits on history. */
export function holdsSession(record) {
  if(!record||record.historical)return false;
  if(inputInFlight(record))return true;
  return ['unconfirmed','fenced-unconfirmed'].includes(record.state)&&record.reconciliation?.state!=='found'&&record.ownerNotice?.state!=='accepted';
}

/** The unsettled view of one record: its original id and the facts that explain it. */
export function unsettledView(record,now=Date.now()) {
  return {id:record.id,kind:record.kind??'owner',state:record.state,summary:inputSummary(record),inFlight:inputInFlight(record),
    at:record.at??null,ageMs:Number.isFinite(record.at)?Math.max(0,now-record.at):null,
    ...(record.reason?{reason:record.reason}:{}),...(record.failureStage?{failureStage:record.failureStage}:{}),
    ...(record.retry?{retry:{attempts:record.retry.attempts,nextAt:record.retry.nextAt??null,exhausted:Boolean(record.retry.exhausted)}}:{}),
    ...(record.ownerNotice?{notice:{kind:record.ownerNotice.kind,state:record.ownerNotice.state,attempts:record.ownerNotice.attempts??0}}:{}),
    ...(record.reconciliation?{reconciliation:record.reconciliation.state}:{})};
}

export function emptySummary() {return Object.fromEntries(SUMMARY_STATES.map(state=>[state,0]));}

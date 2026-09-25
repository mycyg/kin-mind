import {feishuAnswer,platformErrorCode} from './channel-contract.mjs';

/** The transport owns each submission boundary, using one persistent UUID.
 * `beforeSubmit(stage)` is asked, and waited for, before the upload ('upload') and before the
 * message request ('message'); throwing or rejecting there stops the send while nothing has
 * been submitted, so the receipt proves it unsent (CR3-FLOW-04: a sender outside the host
 * whose admission was lost; CR4-FLOW-02: it asks the host that granted the admission, so its
 * answer comes later). */
export async function submitPayload({media,uuid,upload,create,checkpoint,beforeSubmit=()=>{}}) {
  let content;
  if(media) {
    await beforeSubmit('upload');
    checkpoint({stage:'uploading',submissionStarted:false});
    content=await upload(media);
    if(!content||!Object.values(content).every(v=>typeof v==='string'&&v))throw Error('Upload returned no file key');
    checkpoint({stage:'uploaded',submissionStarted:false,uploadedAt:new Date().toISOString(),uploadKeys:content});
  }
  await beforeSubmit('message');
  checkpoint({stage:'message-submitting',submissionStarted:true});
  // What the answer proves is the channel contract's to say (`feishuAnswer`). Only a platform
  // error code is a definitive refusal: the sender can record `rejected`. An answer without
  // one -- no code, a code of another type, no answer at all -- says nothing about a request
  // that has left, and neither does one without a message ID: both stay an unknown outcome,
  // reconciled under this UUID (CR5-FLOW-02).
  let answer,status=200,thrown;
  try {answer=feishuAnswer({status,body:await create({content,uuid})});}
  catch(error) {
    // A non-2xx answer: the SDK's HTTP client rejects it and the SDK throws that error on as
    // it is, the answer on it (`response`: the status and the parsed body). A 4xx with the
    // platform's own code is its refusal, as on WeChat. A timeout or a lost connection has no
    // answer on it, and stays the unknown it is -- the sender records the error as it came.
    const response=error?.response;
    if(!response||typeof response!=='object')throw error;
    status=Number.isInteger(response.status)?response.status:0;
    answer=feishuAnswer({status,body:response.data});thrown=error;
  }
  if(answer.state==='answered')return answer.messageId;
  if(answer.state==='rejected')throw Object.assign(Error('Platform rejected send: '+answer.code),{code:'PLATFORM_REJECTED',platformCode:answer.code});
  // Unknown. A message ID the answer named -- beside a refusal (CL6-FLOW-01), in a 5xx, beside
  // a code nobody can read -- is what the reconciliation has to go on: it stays on the receipt.
  if(answer.messageId!==undefined)
    try {checkpoint({answer:{status,reason:answer.reason,code:answer.code??null,messageId:String(answer.messageId).slice(0,128)}});}
    catch {/* the error below still says the outcome is unknown */}
  if(answer.reason==='contradictory-answer')throw Object.assign(Error('Platform answer contradicts itself'),{code:'PLATFORM_ANSWER_UNKNOWN',reason:'contradictory-answer'});
  if(thrown)throw thrown;
  if(answer.reason==='missing-message-id')throw Object.assign(Error('Platform returned no message ID'),{code:'PLATFORM_NO_MESSAGE_ID'});
  throw Object.assign(Error('Platform answer has no usable code'),{code:'PLATFORM_ANSWER_UNKNOWN'});
}

/** The platform error code the contract defines, where earlier callers import it from. */
export {platformErrorCode};

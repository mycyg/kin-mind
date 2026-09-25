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
  const result=await create({content,uuid});
  // Only a platform error code is a definitive refusal: the sender can record `rejected`.
  // An answer without one -- no code, a code of another type, no answer at all -- says
  // nothing about a request that has left, and neither does one without a message ID:
  // both stay an unknown outcome, reconciled under this UUID (CR5-FLOW-02).
  const code=result?.code;
  if(code!==0) {
    if(platformErrorCode(code))throw Object.assign(Error('Platform rejected send: '+code),{code:'PLATFORM_REJECTED',platformCode:code});
    throw Object.assign(Error('Platform answer has no usable code'),{code:'PLATFORM_ANSWER_UNKNOWN'});
  }
  if(!result.data?.message_id)throw Object.assign(Error('Platform returned no message ID'),{code:'PLATFORM_NO_MESSAGE_ID'});
  return result.data.message_id;
}

/** A platform error code: a whole number other than zero. Nothing else a platform answers
 * with -- a string, a fraction, `null`, a missing field -- is read as its refusal. */
export const platformErrorCode=code=>Number.isSafeInteger(code)&&code!==0;

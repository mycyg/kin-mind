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
  // A platform answer with an error code is a definitive refusal: the sender can
  // record `rejected`. An answer without a message ID stays an unknown outcome.
  if(result?.code!==0)throw Object.assign(Error('Platform rejected send: '+result?.code),{code:'PLATFORM_REJECTED',platformCode:result?.code});
  if(!result.data?.message_id)throw Object.assign(Error('Platform returned no message ID'),{code:'PLATFORM_NO_MESSAGE_ID'});
  return result.data.message_id;
}

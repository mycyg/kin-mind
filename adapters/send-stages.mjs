/** The transport owns each submission boundary, using one persistent UUID. */
export async function submitPayload({media,uuid,upload,create,checkpoint}) {
  let content;
  if(media) {
    checkpoint({stage:'uploading',submissionStarted:false});
    content=await upload(media);
    if(!content||!Object.values(content).every(v=>typeof v==='string'&&v))throw Error('Upload returned no file key');
    checkpoint({stage:'uploaded',submissionStarted:false,uploadedAt:new Date().toISOString(),uploadKeys:content});
  }
  checkpoint({stage:'message-submitting',submissionStarted:true});
  const result=await create({content,uuid});
  // A platform answer with an error code is a definitive refusal: the sender can
  // record `rejected`. An answer without a message ID stays an unknown outcome.
  if(result?.code!==0)throw Object.assign(Error('Platform rejected send: '+result?.code),{code:'PLATFORM_REJECTED',platformCode:result?.code});
  if(!result.data?.message_id)throw Object.assign(Error('Platform returned no message ID'),{code:'PLATFORM_NO_MESSAGE_ID'});
  return result.data.message_id;
}

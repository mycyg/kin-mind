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
  if(result?.code!==0)throw Error('Platform rejected send: '+result?.code);
  if(!result.data?.message_id)throw Error('Platform returned no message ID');
  return result.data.message_id;
}

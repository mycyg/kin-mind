import fs from 'node:fs';

export const KIMI_HISTORY_MESSAGE='没有切换到 Kimi：当前会话含 Kimi 不支持的压缩历史，不能在保留完整上下文的前提下安全交接。原会话和内容已保留；请继续使用 GPT，或另开独立 Kimi 会话并明确提供所需上下文。';
export const KIMI_HISTORY_UNKNOWN_MESSAGE='没有切换到 Kimi：无法完整核验当前会话历史，已保留当前模型和全部内容。请继续使用当前会话，待历史核验恢复后再试。';
export const historyError=(unknown=false)=>Object.assign(Error(unknown?KIMI_HISTORY_UNKNOWN_MESSAGE:KIMI_HISTORY_MESSAGE),{code:unknown?'KIMI_HISTORY_UNVERIFIED':'KIMI_HISTORY_INCOMPATIBLE',beforeMutation:true});
export const isHistoryRefusal=error=>error?.beforeMutation===true&&['KIMI_HISTORY_UNVERIFIED','KIMI_HISTORY_INCOMPATIBLE'].includes(error.code);
export function assertKimiInputCompatible(input){
  if(Array.isArray(input)&&input.some(item=>item?.type==='compaction'))throw historyError();
}

/** A conservative, read-only preflight. Never reconstruct, delete, decrypt or
 * summarize history. Any persisted opaque compaction blocks handoff, including
 * an earlier one: later readable items do not prove that all its context survived.
 * Incomplete, changing or oversized logs fail closed. No content leaves this reader. */
export async function checkModelHistory(profile,runtime){
  if(!["kimi-for-coding","kimi-for-coding-highspeed","k3","k3-256k","k3.1-pro","k3.1-pro-highspeed"].includes(profile?.model))return;
  if(!runtime?.known||!runtime.rolloutPath||!runtime.threadId||runtime.threadId!==runtime.sessionId)throw historyError(true);
  let handle;
  try{
    handle=await fs.promises.open(runtime.rolloutPath,'r');
    const before=await handle.stat();
    if(!before.isFile()||before.size===0||before.size>256*1024*1024)throw historyError(true);
    let pending='',identified=false,bytesRead=0;
    const record=line=>{
      if(!line.trim())return;
      const row=JSON.parse(line);
      if(row.type==='session_meta'){
        if(row.payload?.id!==runtime.threadId)throw historyError(true);
        identified=true;
      }
      if(row.type==='response_item')assertKimiInputCompatible([row.payload]);
      if(row.type==='compacted'){
        if(!Array.isArray(row.payload?.replacement_history))throw historyError(true);
        assertKimiInputCompatible(row.payload.replacement_history);
      }
    };
    const stream=handle.createReadStream({encoding:'utf8',autoClose:false});
    try{for await(const chunk of stream){
      bytesRead+=Buffer.byteLength(chunk);if(bytesRead>256*1024*1024)throw historyError(true);
      pending+=chunk;
      let end;
      while((end=pending.indexOf('\n'))>=0){if(end>16*1024*1024)throw historyError(true);record(pending.slice(0,end));pending=pending.slice(end+1);}
      if(pending.length>16*1024*1024)throw historyError(true);
    }}finally{stream.destroy();}
    // A non-newline-terminated record may still be an in-flight write.
    if(pending||!identified)throw historyError(true);
    const after=await fs.promises.stat(runtime.rolloutPath);
    if(before.ino!==after.ino||before.dev!==after.dev||before.size!==after.size||before.mtimeMs!==after.mtimeMs)throw historyError(true);
  }catch(error){if(isHistoryRefusal(error))throw error;throw historyError(true);}
  finally{await handle?.close().catch(()=>{});}
}

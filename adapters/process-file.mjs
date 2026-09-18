/** The file that says which process is the service. One number, claimed
 * exclusively, so a second starter stands down instead of running beside the
 * first — and an identity beside it, so a number handed out again is recognised
 * as the stranger it is rather than mistaken for the service that has died. */
import fs from 'node:fs';
import {processGone,processIdentity,probeProcess,readJsonFile,writeJsonAtomic} from './atomic-json.mjs';
const alive=pid=>{try{process.kill(pid,0);return true;}catch(error){if(error.code==='ESRCH')return false;throw error;}};
// Beside the pid file, never inside it: the pid file stays one plain number,
// which is the shape every other reader of it already expects.
const identityFile=file=>file+'.owner.json';

/** Whether the pid in the file belongs to somebody else now. Where the previous
 * owner recorded what it was — a start time, a command — that is the question
 * asked, so a reused number no longer blocks a start-up for good. Where it
 * recorded nothing, as a file written by older code did, the only question left
 * is whether the number is alive, which is what this always used to ask. */
function ownerGone(pid,file,{isAlive,probe}) {
  const recorded=readJsonFile(identityFile(file));
  const holder=recorded.state==='ok'&&recorded.value?.pid===pid?recorded.value:null;
  if(holder?.started||holder?.command)return processGone(pid,{started:holder.started,command:holder.command,probe});
  return !isAlive(pid);
}

export function claimProcessFile(file,{pid=process.pid,isAlive=alive,probe=probeProcess}={}) {
  const write=()=>{
    const fd=fs.openSync(file,'wx',0o600);try{fs.writeFileSync(fd,String(pid));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
    const holder=processIdentity(pid,{probe});
    // A claim without its identity is the claim this file always made. It is
    // worth less than one with it, and still worth more than no claim at all.
    try{writeJsonAtomic(identityFile(file),holder,{previous:false});}catch{/* the number alone still holds */}
    return {pid,holder};
  };
  try{return write();}catch(error){if(error.code!=='EEXIST')throw error;}
  const owner=fs.readFileSync(file,'utf8'),other=Number(owner);
  // An empty/partial file belongs to a concurrent starter; never steal it.
  if(!Number.isSafeInteger(other)||other<=1)return null;
  if(!ownerGone(other,file,{isAlive,probe}))return null;
  if(fs.readFileSync(file,'utf8')!==owner)return null;
  fs.unlinkSync(file);fs.rmSync(identityFile(file),{force:true});
  try{return write();}catch(error){if(error.code==='EEXIST')return null;throw error;}
}

export function releaseProcessFile(file,claim) {
  try{if(claim&&fs.readFileSync(file,'utf8')===String(claim.pid)){fs.unlinkSync(file);fs.rmSync(identityFile(file),{force:true});}}
  catch(error){if(error.code!=='ENOENT')throw error;}
}

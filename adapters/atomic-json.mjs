/** Durable JSON state files: a unique temporary name per write, fsync before
 * the rename, the replaced revision kept as `<file>.prev`, and unreadable
 * files moved aside so one bad file never stops the others from loading.
 *
 * It also holds the lock those files are written under, because the two
 * questions are the same one: a state file is only as trustworthy as the claim
 * that nobody else is writing it, and a lock file is only as trustworthy as the
 * evidence that its holder is still alive. */
import fs from 'node:fs';
import path from 'node:path';
import {randomBytes} from 'node:crypto';
import {execFileSync} from 'node:child_process';

const unique=()=>process.pid+'.'+randomBytes(8).toString('hex');
function syncDirectory(directory) {
  let fd;
  try{fd=fs.openSync(directory,'r');fs.fsyncSync(fd);}
  catch{/* Some filesystems refuse a directory fsync; the rename stays atomic. */}
  finally{if(fd!==undefined)fs.closeSync(fd);}
}
function writeTemporary(file,body,mode) {
  const temporary=file+'.'+unique()+'.tmp',fd=fs.openSync(temporary,'wx',mode);
  try{fs.writeFileSync(fd,body);fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
  return temporary;
}

/** `{state:'ok',value,body}`, `{state:'missing'}` or `{state:'corrupt'}`; never throws for file contents. */
export function readJsonFile(file) {
  let body;
  try{body=fs.readFileSync(file,'utf8');}catch(error){if(error.code==='ENOENT')return {state:'missing'};return {state:'corrupt',reason:error.code??'unreadable'};}
  try{return {state:'ok',value:JSON.parse(body),body};}catch{return {state:'corrupt',reason:'invalid-json'};}
}

function replaceAtomically(file,body,mode,keepPrevious) {
  const directory=path.dirname(file);
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const temporary=writeTemporary(file,body,mode);
  try {
    if(keepPrevious()) {
      const link=file+'.'+unique()+'.prev.tmp';
      try{fs.linkSync(file,link);}catch{fs.copyFileSync(file,link);}
      // Renaming a link onto the same inode is a no-op after an interrupted write,
      // when `.prev` already names this revision: drop the link rather than leak it.
      fs.renameSync(link,file+'.prev');fs.rmSync(link,{force:true});
    }
    fs.renameSync(temporary,file);
  } catch(error){fs.rmSync(temporary,{force:true});throw error;}
  syncDirectory(directory);
}

/** Replace `file` with these bytes, whole or not at all: a unique temporary
 * name, the bytes on the disk before the rename, and the directory entry on the
 * disk after it. For state that is not JSON — a rendered page, an image, a
 * profile — where there is no earlier revision worth keeping. */
export function writeFileAtomic(file,bytes,{mode=0o600}={}) {
  replaceAtomically(file,bytes,mode,()=>false);
  return bytes;
}

/** Replace `file` atomically. With `previous`, the revision being replaced
 * survives as `<file>.prev` (a hard link, so nothing is copied); an unreadable
 * current file never overwrites a good `.prev`. Returns the written body. */
export function writeJsonAtomic(file,value,{previous=true,pretty=false,mode=0o600}={}) {
  const body=JSON.stringify(value,null,pretty?2:undefined);
  replaceAtomically(file,body,mode,()=>previous&&readJsonFile(file).state==='ok');
  return body;
}

/** Put `bytes` at `file` only where nothing is there yet, and make sure that what
 * lands is all of them. This is how a file named after its own digest has to be
 * written: it is written once and then read for ever after, so half of one left
 * behind by a crash would be taken for the whole by every reader that follows —
 * its name already says what it is meant to contain. False means another writer
 * was first; their contents are left untouched. */
export function createFileExclusive(file,bytes,{mode=0o600}={}) {
  const directory=path.dirname(file);
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const temporary=writeTemporary(file,bytes,mode);
  try{fs.linkSync(temporary,file);}
  catch(error){if(error.code==='EEXIST')return false;throw error;}
  finally{fs.rmSync(temporary,{force:true});}
  syncDirectory(directory);
  return true;
}

/** Create `file` only when it does not exist yet. False means another writer
 * was first; their contents are left untouched. */
export function createJsonExclusive(file,value,{pretty=false,mode=0o600}={}) {
  return createFileExclusive(file,JSON.stringify(value,null,pretty?2:undefined),{mode});
}

/** Move a file out of the way, keeping its bytes as evidence. Null when it is already gone. */
export function quarantineFile(file,directory,{reason='corrupt'}={}) {
  try {
    fs.mkdirSync(directory,{recursive:true,mode:0o700});
    const target=path.join(directory,path.basename(file)+'.'+reason+'.'+unique());
    fs.renameSync(file,target);syncDirectory(path.dirname(file));
    return target;
  } catch(error){if(error.code==='ENOENT')return null;throw error;}
}

/** Load with a per-file guard. `validate` may reject a parsed value. A bad
 * current file falls back to `.prev`; bad files go to `quarantine` when given.
 * Returns `{value,source:'current'|'previous'|'none',quarantined:[…]}`. */
export function loadJson(file,{validate=()=>true,quarantine}={}) {
  const usable=result=>{try{return result.state==='ok'&&validate(result.value)===true;}catch{return false;}};
  const current=readJsonFile(file),quarantined=[];
  if(usable(current))return {value:current.value,source:'current',quarantined};
  const previous=readJsonFile(file+'.prev');
  const aside=(name,result)=>{if(quarantine&&result.state!=='missing'){const target=quarantineFile(name,quarantine);if(target)quarantined.push(target);}};
  aside(file,current);
  if(usable(previous))return {value:previous.value,source:'previous',quarantined};
  aside(file+'.prev',previous);
  return {value:undefined,source:'none',quarantined};
}

// ---------------------------------------------------------------------------
// Who holds a file, and whether they are still there
// ---------------------------------------------------------------------------

/** The absolute path first, the way the hosts' own process checks resolve it: a
 * `ps` found on PATH is a `ps` somebody else gets to choose for us. */
function psCommand(){try{return fs.existsSync('/bin/ps')?'/bin/ps':'ps';}catch{return 'ps';}}
const UNKNOWN=Object.freeze({alive:true,known:false,started:null,command:null});
const GONE=Object.freeze({alive:false,known:true,started:null,command:null});

/** What the operating system says about one pid: whether it exists, when it
 * started, and what it is running.
 *
 * `known:false` means the question could not be asked at all — no `ps`, or one
 * that failed — and every caller here reads that as "still running". An
 * unanswerable question is not a dead process, and the cost of the two mistakes
 * is not symmetric: waiting for a holder that has already gone loses time,
 * while overrunning one that is still working loses the work. */
export function probeProcess(pid,{ps=psCommand()}={}) {
  const number=Number(String(pid).trim());
  // 0 and 1 are the kernel and the init process. Neither is ever a holder of
  // ours, so a lock naming one names nothing that can still be running.
  if(!Number.isSafeInteger(number)||number<=1)return GONE;
  let line;
  try{line=execFileSync(ps,['-p',String(number),'-o','lstart=,command='],{encoding:'utf8',timeout:10000,stdio:['ignore','pipe','ignore']});}
  catch(error) {
    // `ps` answers an absent pid with an empty listing and a non-zero status.
    // That is an answer. A command that could not be run at all is not.
    if(typeof error.status==='number'&&!String(error.stdout??'').trim())return GONE;
    return UNKNOWN;
  }
  if(!line.trim())return GONE;
  // `lstart` is exactly five fields; everything after them is the command line.
  const fields=line.trim().split(/\s+/);
  if(fields.length<5)return UNKNOWN;
  return {alive:true,known:true,started:fields.slice(0,5).join(' '),command:fields.slice(5).join(' ')};
}

/** What a process must leave behind so that a later one can tell whether it is
 * still running. The pid on its own cannot say: pids are handed out again.
 *
 * Our own identity is asked for once. It cannot change while we are here to ask,
 * and every lock taken would otherwise cost a `ps`. */
let own=null;
export function processIdentity(pid=process.pid,{probe=probeProcess}={}) {
  const mine=Number(pid)===process.pid&&probe===probeProcess;
  if(mine&&own)return own;
  const seen=probe(pid),identity={pid:Number(pid),started:seen.known?seen.started:null,command:seen.known?seen.command:null};
  // An unanswerable `ps` is asked again next time: it may be a passing failure.
  if(mine&&seen.known)own=identity;
  return identity;
}

/** True only where the process can be *shown* to be gone.
 *
 * Gone means: no such pid, or a pid that now belongs to somebody else. A reused
 * pid keeps the number and loses the start time, which is the whole reason the
 * start time is recorded. The command is the weaker witness — a process may
 * rewrite its own name — so it decides only where no start time was captured.
 * Everything else, including a record with nothing to identify it by, is "still
 * there". */
export function processGone(pid,{started=null,command=null,probe=probeProcess}={}) {
  return identityVerdict(pid,{started,command,probe}).gone;
}

function identityVerdict(pid,{started=null,command=null,probe=probeProcess}={}) {
  const seen=probe(pid);
  if(!seen.known)return {gone:false,reason:'unanswerable',seen};
  if(!seen.alive)return {gone:true,reason:'holder-exited',seen};
  if(started)return seen.started&&seen.started!==started?{gone:true,reason:'pid-reused',seen}:{gone:false,reason:'holder-running',seen};
  if(command)return seen.command!==null&&!seen.command.includes(command)?{gone:true,reason:'command-changed',seen}:{gone:false,reason:'holder-running',seen};
  return {gone:false,reason:'unidentified',seen};
}

// ---------------------------------------------------------------------------
// The lock a state file is written under
// ---------------------------------------------------------------------------

/** Who holds `file` now: `free`, `held` by a process that is still there, or
 * `stale`. A lock nobody can read is stale too — a lock file that can never be
 * broken would wedge the work it guards for good, which is the failure this
 * whole module exists to prevent. */
export function readPidLock(file,{probe=probeProcess}={}) {
  const result=readJsonFile(file);
  if(result.state==='missing')return {state:'free',owner:null,reason:null};
  const owner=result.state==='ok'?result.value:null;
  if(!owner?.nonce||!owner?.holder?.pid)return {state:'stale',owner,reason:'unreadable-lock'};
  const verdict=identityVerdict(owner.holder.pid,{started:owner.holder.started,command:owner.holder.command,probe});
  return verdict.gone?{state:'stale',owner,reason:verdict.reason}:{state:'held',owner,reason:verdict.reason};
}

/** Whether this claim is still the one in the lock file. A holder that is about
 * to do something it cannot take back can ask first. */
export function pidLockHeld(file,claim) {
  // A claim without a nonce identifies nobody, and must never match a lock file
  // that happens to have none either.
  if(!claim?.nonce)return false;
  const result=readJsonFile(file);
  return result.state==='ok'&&result.value?.nonce===claim.nonce;
}

/** Give the lock back. Only the holder may: a lock that has already been broken
 * and retaken belongs to somebody else now, and removing it would hand the work
 * to a third process. */
export function releasePidLock(file,claim) {
  if(!pidLockHeld(file,claim))return false;
  fs.rmSync(file,{force:true});
  syncDirectory(path.dirname(file));
  return true;
}

/** A stale lock is moved aside, not removed. It is the only record that an
 * attempt was interrupted, and the reconciliation may be the thing that needs
 * to read it. */
const breakPidLock=file=>quarantineFile(file,path.join(path.dirname(file),'broken'),{reason:'stale'});

/** Run `work` while holding `file`, and never run it on the strength of a lock
 * that was broken.
 *
 * A lock says that somebody took the work, never that they finished it. A holder
 * that died half way through leaves behind a lock that looks exactly like a
 * holder still at it — so "the holder is gone" can only ever mean "find out what
 * they left behind", never "do it again". Where the work is a message to the
 * owner, doing it again means they receive it twice, which is the worst outcome
 * available here and is worse than not sending at all.
 *
 * That rule is the shape of this function rather than a warning next to it:
 * `work` is reachable only where no lock had to be broken, and a broken lock
 * reaches `reconcile` or nothing.
 *
 * Returns `{state}` of `ran` (the work ran, `value` is its result),
 * `reconciled` (the lock was stale and `reconcile` ran instead, `value` is its
 * result), `stale` (the lock was stale and no `reconcile` was given — settle the
 * previous attempt before asking again) or `busy` (somebody is still holding
 * it). Anything but `ran` means the work did not happen. */
export async function withPidLock(file,{work,reconcile,probe=probeProcess,identity=processIdentity,mode=0o600}={}) {
  fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});
  const claim={holder:identity(process.pid,{probe}),nonce:unique(),at:new Date().toISOString()};
  let broken=null,held=false;
  for(let attempt=0;attempt<3&&!held;attempt++) {
    if(createJsonExclusive(file,claim,{mode})){held=true;break;}
    const seen=readPidLock(file,{probe});
    if(seen.state==='held')return {state:'busy',owner:seen.owner,reason:seen.reason,ran:false};
    // `free` means the holder released it between the two calls: go round again.
    if(seen.state==='stale')broken??={owner:seen.owner,reason:seen.reason,kept:breakPidLock(file)};
  }
  // Two contenders can break the same stale lock; only one of them ends up
  // holding it, and the other waits rather than working beside the winner.
  if(!held)return {state:'busy',owner:broken?.owner??null,reason:'lock-contended',ran:false};
  try {
    if(broken) {
      // The previous holder died with the work in flight. What happens now is an
      // accounting of that attempt. A second attempt is not on offer.
      const outcome={state:'stale',owner:broken.owner,reason:broken.reason,kept:broken.kept,ran:false};
      return reconcile?{...outcome,state:'reconciled',value:await reconcile({owner:broken.owner,reason:broken.reason,kept:broken.kept,claim,file})}:outcome;
    }
    return {state:'ran',owner:null,reason:null,ran:true,value:await work({claim,file})};
  } finally{releasePidLock(file,claim);}
}

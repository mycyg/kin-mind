/** A renewable lease over one piece of file state, shared by the host service
 * and its CLI tools. Each acquisition wins a new generation through an
 * exclusive `mkdir <id>.takeover.g<N>`, so a generation has exactly one owner
 * and works as a fencing token. Expiry only ever frees the lease: it says
 * nothing about what the previous holder already put on the network. */
import fs from 'node:fs';
import path from 'node:path';
import {randomBytes} from 'node:crypto';
import {readJsonFile,writeJsonAtomic} from './atomic-json.mjs';

export const LEASE_DEFAULTS=Object.freeze({ttlMs:90000,graceMs:30000,marginMs:15000});
const SAFE_ID=/^[A-Za-z0-9_-]{1,200}$/;

export class LeaseLost extends Error {
  constructor(reason){super('State lease is no longer held');this.name='LeaseLost';this.code='STATE_LEASE_LOST';this.reason=reason;}
}

const leaseFile=(directory,id)=>path.join(directory,id+'.lease.json');
const markerPath=(directory,id,generation)=>path.join(directory,id+'.takeover.g'+generation);
function markers(directory,id) {
  const prefix=id+'.takeover.g';let names=[];
  try{names=fs.readdirSync(directory);}catch(error){if(error.code!=='ENOENT')throw error;}
  return names.filter(name=>name.startsWith(prefix)).map(name=>Number(name.slice(prefix.length))).filter(Number.isSafeInteger);
}
const highest=(directory,id)=>Math.max(0,...markers(directory,id));
function readLease(directory,id) {
  const result=readJsonFile(leaseFile(directory,id));
  return result.state==='ok'&&Number.isSafeInteger(result.value?.generation)?result.value:null;
}
/** When the winner of a generation has not written its lease yet, its marker
 * stands in for one: claimed at `claim.at`, or at the marker's mtime. */
function claimedAt(directory,id,generation) {
  const marker=markerPath(directory,id,generation),claim=readJsonFile(path.join(marker,'claim.json'));
  if(claim.state==='ok'&&Number.isFinite(claim.value?.at))return claim.value.at;
  try{return fs.statSync(marker).mtimeMs;}catch{return 0;}
}

/** Who holds the lease now. Read-only; used by status and by `acquireLease`. */
export function inspectLease({directory,id,clock=Date.now,ttlMs=LEASE_DEFAULTS.ttlMs,graceMs=LEASE_DEFAULTS.graceMs}) {
  const lease=readLease(directory,id),top=highest(directory,id),now=clock();
  if(top>(lease?.generation??0)) {
    const busy=now<claimedAt(directory,id,top)+ttlMs+graceMs;
    return {state:busy?'claiming':'expired',generation:top,owner:null,expiresAt:null};
  }
  if(!lease)return {state:'free',generation:0,owner:null,expiresAt:null};
  const state=lease.released?'released':now<lease.expiresAt+graceMs?'held':'expired';
  return {state,generation:lease.generation,owner:lease.owner,expiresAt:lease.expiresAt};
}

/** Returns a lease handle, or null while somebody else may still be writing.
 * `floor` is the highest generation the protected state has already seen, so a
 * lost lease file can never make generations run backwards. */
export function acquireLease({directory,id,role='service',clock=Date.now,floor=0,pid=process.pid,heartbeat=false,
  ttlMs=LEASE_DEFAULTS.ttlMs,graceMs=LEASE_DEFAULTS.graceMs,marginMs=LEASE_DEFAULTS.marginMs}) {
  if(!SAFE_ID.test(id))throw Error('Lease ID must be a plain file name');
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const seen=inspectLease({directory,id,clock,ttlMs,graceMs});
  if(seen.state==='held'||seen.state==='claiming')return null;
  const generation=Math.max(seen.generation,floor)+1,marker=markerPath(directory,id,generation);
  try{fs.mkdirSync(marker,{mode:0o700});}catch(error){if(error.code==='EEXIST')return null;throw error;}
  const owner={pid,nonce:randomBytes(12).toString('hex'),role},now=clock();
  try{writeJsonAtomic(path.join(marker,'claim.json'),{owner,at:now},{previous:false});}catch{/* The marker's mtime stands in. */}
  // A contender that slept since its first read may have won a generation that
  // is already history. It must notice that before it writes anything.
  if((readLease(directory,id)?.generation??0)>=generation||highest(directory,id)>generation) {
    fs.rmSync(marker,{recursive:true,force:true});return null;
  }
  const lease=new StateLease({directory,id,owner,generation,clock,ttlMs,graceMs,marginMs,now});
  lease.write();
  for(const old of markers(directory,id))if(old<generation)fs.rmSync(markerPath(directory,id,old),{recursive:true,force:true});
  if(heartbeat)lease.keepAlive();
  return lease;
}

export class StateLease {
  constructor({directory,id,owner,generation,clock,ttlMs,graceMs,marginMs,now}) {
    Object.assign(this,{directory,id,owner,generation,clock,ttlMs,graceMs,marginMs,acquiredAt:now,renewedAt:now,expiresAt:now+ttlMs,released:false,timer:null});
  }
  write(extra={}) {
    writeJsonAtomic(leaseFile(this.directory,this.id),{id:this.id,owner:this.owner,generation:this.generation,acquiredAt:this.acquiredAt,
      renewedAt:this.renewedAt,expiresAt:this.expiresAt,ttlMs:this.ttlMs,...extra},{previous:false});
  }
  /** Why this holder may no longer write, or null while it still may. */
  lost() {
    if(this.released)return 'released';
    if(highest(this.directory,this.id)>this.generation)return 'taken-over';
    const lease=readLease(this.directory,this.id);
    if(!lease||lease.generation!==this.generation||lease.owner?.nonce!==this.owner.nonce)return 'lease-file-changed';
    if(this.clock()>=this.expiresAt)return 'expired';
    return null;
  }
  /** Extend the lease. Only a holder that has not expired may renew: after
   * expiry a contender can already be past its own checks. */
  renew() {
    if(this.lost())return false;
    this.renewedAt=this.clock();this.expiresAt=this.renewedAt+this.ttlMs;this.write();
    return true;
  }
  renewIfDue(){return this.clock()-this.renewedAt>=this.ttlMs/3?this.renew():true;}
  /** `{ok:true}` when a write may go ahead now: still ours, and not so close
   * to expiry that the write could land after a takeover. */
  check() {
    this.renewIfDue();
    const reason=this.lost()??(this.expiresAt-this.clock()<this.marginMs?'expiring':null);
    return reason?{ok:false,reason}:{ok:true};
  }
  assertHeld(){const state=this.check();if(!state.ok)throw new LeaseLost(state.reason);}
  keepAlive() {
    if(this.timer)return;
    this.timer=setInterval(()=>{try{this.renew();}catch{/* The next write is refused instead. */}},Math.max(1000,Math.floor(this.ttlMs/3)));
    this.timer.unref?.();
  }
  /** Give the lease back. The generation marker stays, so the next owner
   * still has to win a higher one. */
  release() {
    if(this.timer){clearInterval(this.timer);this.timer=null;}
    if(this.released)return;
    // Once expired, a contender may already be claiming: leave the file alone.
    const mine=!this.lost();this.released=true;
    if(mine){this.expiresAt=this.clock();try{this.write({released:true});}catch{/* An unreleased lease simply expires. */}}
  }
}

/** Forget a finished piece of state. Its generations restart from the floor the caller keeps. */
export function removeLease({directory,id}) {
  fs.rmSync(leaseFile(directory,id),{force:true});
  for(const generation of markers(directory,id))fs.rmSync(markerPath(directory,id,generation),{recursive:true,force:true});
}

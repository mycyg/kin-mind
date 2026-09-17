/** Durable JSON state files: a unique temporary name per write, fsync before
 * the rename, the replaced revision kept as `<file>.prev`, and unreadable
 * files moved aside so one bad file never stops the others from loading. */
import fs from 'node:fs';
import path from 'node:path';
import {randomBytes} from 'node:crypto';

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

/** Replace `file` atomically. With `previous`, the revision being replaced
 * survives as `<file>.prev` (a hard link, so nothing is copied); an unreadable
 * current file never overwrites a good `.prev`. Returns the written body. */
export function writeJsonAtomic(file,value,{previous=true,pretty=false,mode=0o600}={}) {
  const directory=path.dirname(file);
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const body=JSON.stringify(value,null,pretty?2:undefined),temporary=writeTemporary(file,body,mode);
  try {
    if(previous&&readJsonFile(file).state==='ok') {
      const link=file+'.'+unique()+'.prev.tmp';
      try{fs.linkSync(file,link);}catch{fs.copyFileSync(file,link);}
      // Renaming a link onto the same inode is a no-op after an interrupted write,
      // when `.prev` already names this revision: drop the link rather than leak it.
      fs.renameSync(link,file+'.prev');fs.rmSync(link,{force:true});
    }
    fs.renameSync(temporary,file);
  } catch(error){fs.rmSync(temporary,{force:true});throw error;}
  syncDirectory(directory);
  return body;
}

/** Create `file` only when it does not exist yet. False means another writer
 * was first; their contents are left untouched. */
export function createJsonExclusive(file,value,{pretty=false,mode=0o600}={}) {
  const directory=path.dirname(file);
  fs.mkdirSync(directory,{recursive:true,mode:0o700});
  const temporary=writeTemporary(file,JSON.stringify(value,null,pretty?2:undefined),mode);
  try{fs.linkSync(temporary,file);}
  catch(error){if(error.code==='EEXIST')return false;throw error;}
  finally{fs.rmSync(temporary,{force:true});}
  syncDirectory(directory);
  return true;
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

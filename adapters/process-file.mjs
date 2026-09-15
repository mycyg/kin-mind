import fs from 'node:fs';
const alive=pid=>{try{process.kill(pid,0);return true;}catch(error){if(error.code==='ESRCH')return false;throw error;}};

export function claimProcessFile(file,{pid=process.pid,isAlive=alive}={}) {
  const write=()=>{const fd=fs.openSync(file,'wx',0o600);try{fs.writeFileSync(fd,String(pid));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}return {pid};};
  try{return write();}catch(error){if(error.code!=='EEXIST')throw error;}
  const owner=fs.readFileSync(file,'utf8'),other=Number(owner);
  // An empty/partial file belongs to a concurrent starter; never steal it.
  if(!Number.isSafeInteger(other)||other<=1||isAlive(other))return null;
  if(fs.readFileSync(file,'utf8')!==owner)return null;
  fs.unlinkSync(file);
  try{return write();}catch(error){if(error.code==='EEXIST')return null;throw error;}
}

export function releaseProcessFile(file,claim) {
  try{if(claim&&fs.readFileSync(file,'utf8')===String(claim.pid))fs.unlinkSync(file);}
  catch(error){if(error.code!=='ENOENT')throw error;}
}

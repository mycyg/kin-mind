import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

const SHA256=/^[a-f0-9]{64}$/;
export const MAX_COMPANION_INSTRUCTION_BYTES=64*1024;
const BINDING_KEYS=['modelInstructionsSha256','modelInstructionsUtf8Bytes','developerInstructionsSha256','developerInstructionsUtf8Bytes'];

export const sha256=value=>createHash('sha256').update(value).digest('hex');

export const isCompanionInstructionBinding=value=>Boolean(value&&
  SHA256.test(value.modelInstructionsSha256??'')&&Number.isSafeInteger(value.modelInstructionsUtf8Bytes)&&value.modelInstructionsUtf8Bytes>0&&value.modelInstructionsUtf8Bytes<=MAX_COMPANION_INSTRUCTION_BYTES&&
  SHA256.test(value.developerInstructionsSha256??'')&&Number.isSafeInteger(value.developerInstructionsUtf8Bytes)&&value.developerInstructionsUtf8Bytes>0&&value.developerInstructionsUtf8Bytes<=MAX_COMPANION_INSTRUCTION_BYTES);

export const sameInstructionBinding=(left,right)=>left==null&&right==null||Boolean(left&&right&&
  BINDING_KEYS.every(key=>left[key]===right[key]));

/** Hash-only evidence for request-level instructions. Prompt bytes must never
 * cross this boundary into logs or private host state. */
export function instructionTextEvidence(value) {
  if(typeof value!=='string')return {present:false,sha256:null,utf8Bytes:0};
  const bytes=Buffer.from(value,'utf8');
  return {present:true,sha256:sha256(bytes),utf8Bytes:bytes.length};
}

/** Codex 0.155 native HTTP contract, observed on start, resume and restart.
 * These fields come from the authenticated native HTTP request, not model
 * text or the router's guess about the current turn. Unknown versions/shapes
 * remain unbound. An independent host still checks the actual session. */
export function nativeInstructionRequestIdentity(headers,body) {
  const meta=body?.client_metadata;
  const ids=[headers?.['session-id'],headers?.['thread-id'],headers?.['x-client-request-id'],body?.prompt_cache_key,meta?.session_id,meta?.thread_id];
  const valid=v=>typeof v==='string'&&/^[a-zA-Z0-9_-]{1,128}$/.test(v);
  if(!ids.every(valid)||!ids.every(v=>v===ids[0])||!valid(meta?.turn_id)||meta.root_turn_id!==meta.turn_id)return 'unknown';
  return {basis:'native-http-metadata/v1',sessionId:ids[0],threadId:ids[0],turnId:meta.turn_id};
}

export function developerInstructionEvidence(body) {
  return (Array.isArray(body?.input)?body.input:[]).filter(item=>item?.role==='developer')
    .flatMap(item=>Array.isArray(item.content)?item.content:[]).filter(c=>typeof c?.text==='string')
    // Native collaboration mode wraps developer instructions without changing
    // their body. Recognize that exact envelope, not arbitrary substrings.
    .map(c=>instructionTextEvidence(c.text.startsWith('<collaboration_mode>')&&c.text.endsWith('</collaboration_mode>')
      ?c.text.slice('<collaboration_mode>'.length,-'</collaboration_mode>'.length):c.text));
}

/** Callback failures are observational failures. They cannot change provider
 * dispatch or make a caller retry an already-issued model request. */
export function emitInstructionEvidence(callback,event) {
  if(typeof callback!=='function')return;
  try {
    // Give the observer its own copy: even a mutating callback cannot corrupt
    // the terminal event or the usage row retained by the request handler.
    const pending=callback(structuredClone(event));
    if(pending&&typeof pending.then==='function')pending.catch(()=>{});
  } catch {}
}

const within=(root,file)=>file===root||file.startsWith(root+path.sep);
const validUtf8=bytes=>{
  try{return new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch{throw Error('Companion instruction file is not UTF-8');}
};

/** Verify the exact base/developer pair used by the main mobile session and by
 * maintenance candidates. This validates bytes before app-server starts. */
export function verifyCompanionInstructions(value={}) {
  if(value?.enabled!==true)return {enabled:false};
  const file=value.modelInstructionsFile,expected=value.modelInstructionsSha256;
  const allowedRoot=value.allowedRoot,developer=value.developerInstructions;
  const expectedDeveloper=value.developerInstructionsSha256;
  if(typeof file!=='string'||!path.isAbsolute(file)||typeof allowedRoot!=='string'||!path.isAbsolute(allowedRoot)||
    !SHA256.test(expected??'')||typeof developer!=='string'||!SHA256.test(expectedDeveloper??''))
    throw Error('Companion instruction configuration is incomplete');
  const root=fs.realpathSync(allowedRoot);
  if(!fs.statSync(root).isDirectory())throw Error('Companion instruction root is unverified');
  const real=fs.realpathSync(file),linkStat=fs.lstatSync(file);
  if(!linkStat.isFile()||linkStat.isSymbolicLink()||!within(root,real))throw Error('Companion instruction file is unverified');
  let descriptor;
  try {
    descriptor=fs.openSync(file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
    if(!fs.fstatSync(descriptor).isFile())throw Error('Companion instruction file is unverified');
    const bytes=fs.readFileSync(descriptor);
    if(bytes.length===0||bytes.length>MAX_COMPANION_INSTRUCTION_BYTES)throw Error('Companion instruction file size is unverified');
    const text=validUtf8(bytes);
    if(text.includes('\0')||text.includes('\r'))throw Error('Companion instruction file normalization is unverified');
    const actual=sha256(bytes),developerEvidence=instructionTextEvidence(developer);
    if(developerEvidence.utf8Bytes===0||developerEvidence.utf8Bytes>MAX_COMPANION_INSTRUCTION_BYTES||
      developer.includes('\0')||developer.includes('\r'))throw Error('Companion developer instructions are unverified');
    if(actual!==expected||developerEvidence.sha256!==expectedDeveloper)
      throw Error('Companion instruction hash mismatch');
    return Object.freeze({enabled:true,modelInstructionsFile:real,developerInstructions:developer,
      evidence:Object.freeze({modelInstructionsSha256:actual,modelInstructionsUtf8Bytes:bytes.length,
        developerInstructionsSha256:developerEvidence.sha256,developerInstructionsUtf8Bytes:developerEvidence.utf8Bytes})});
  } finally {if(descriptor!==undefined)fs.closeSync(descriptor);}
}

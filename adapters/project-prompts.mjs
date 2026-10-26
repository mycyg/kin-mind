import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
const MAX_PREVIOUS_TEXTS=16,cache=new Map();
/** Parsed JSON, reread only when the file changed; undefined when it is absent. */
function readCached(file){
  let stat;try{stat=fs.statSync(file);}catch(error){if(error.code==='ENOENT'){cache.delete(file);return undefined;}throw error;}
  const hit=cache.get(file);
  if(hit&&hit.mtimeMs===stat.mtimeMs&&hit.size===stat.size&&hit.ino===stat.ino)return hit.value;
  const value=JSON.parse(fs.readFileSync(file,'utf8'));cache.set(file,{mtimeMs:stat.mtimeMs,size:stat.size,ino:stat.ino,value});return value;
}
/** A document that overrides its code default; `edited: false` follows the default again. */
export const editedPrompt=document=>document?.edited===true||document?.edited!==false&&document?.version>1;
/** System texts assembled per call from context: an edit freezes the one variant it saw. */
export const assembledPrompt=name=>/^(?:assessment|provider)\/[^/]+$/.test(name);
/** Current code defaults and KIN edits have separate files; requests load edits anew. */
export function projectPrompt(name,fallback,{root=process.env.KIN_PROMPT_ROOT,register=true}={}) {
  if(!root)return fallback;
  const file=path.join(root,name+'.json'), defaults=path.join(root,'.defaults',name+'.json');
  const document=readCached(file),current=readCached(defaults);
  if(register&&current?.text!==fallback){
    fs.mkdirSync(path.dirname(defaults),{recursive:true});
    const temporary=defaults+'.'+randomUUID()+'.tmp';
    const previous=[...(current?.previous_texts??[]).filter(text=>text!==current?.text),...(current?.text?[current.text]:[])].slice(-MAX_PREVIOUS_TEXTS);
    fs.writeFileSync(temporary,JSON.stringify({name,version:1,source:'default',text:fallback,previous_texts:previous,updated_at:new Date().toISOString()}));
    fs.renameSync(temporary,defaults);
  }
  return editedPrompt(document)?document.text:fallback;
}

/** Edited documents whose code default changed after the edit was saved or kept. */
export function stalePrompts(root) {
  const found=[];
  const walk=dir=>{for(const entry of fs.readdirSync(dir,{withFileTypes:true})) {
    if(entry.name.startsWith('.'))continue;
    const file=path.join(dir,entry.name);
    if(entry.isDirectory()){walk(file);continue;}
    if(!entry.name.endsWith('.json'))continue;
    const name=path.relative(root,file).slice(0,-5).split(path.sep).join('/'),document=readCached(file);
    if(assembledPrompt(name)||!editedPrompt(document))continue;
    const current=readCached(path.join(root,'.defaults',name+'.json'));
    if(current&&Date.parse(current.updated_at)>Date.parse(document.default_seen_at??document.updated_at))
      found.push({name,version:document.version,defaultAt:current.updated_at});
  }};
  try{walk(root);}catch(error){if(error.code!=='ENOENT')throw error;}
  return found;
}

/** Read the canonical text without registering a stale native-session copy as a default. */
export function currentProjectPrompt(name,fallback,{root=process.env.KIN_PROMPT_ROOT}={}) {
  if(!root)return fallback;
  for(const candidate of [path.join(root,name+'.json'),path.join(root,'.defaults',name+'.json')]) {
    const document=readCached(candidate);
    if(document&&(candidate.includes(path.sep+'.defaults'+path.sep)||editedPrompt(document)))return document.text;
  }
  return fallback;
}

/** Edit explanatory schema text while retaining the tool's executable structure. */
export function projectSchema(name,schema,options={}) {
  if(Array.isArray(schema))return schema.map((value,index)=>projectSchema(name+'/'+index,value,options));
  if(!schema||typeof schema!=='object')return schema;
  return Object.fromEntries(Object.entries(schema).map(([key,value])=>[key,
    ['description','title'].includes(key)&&typeof value==='string'
      ?projectPrompt(name+'/'+key,value,options):projectSchema(name+'/'+key,value,options)]));
}

/** Replace the named MCP fragment even when the native session cached an older revision. */
export function refreshMcpInstructions(text,{root}={}) {
  if(!root||typeof text!=='string')return text;
  const file=path.join(root,'.defaults/mcp/instructions.json');
  if(!fs.existsSync(file))return text;
  const registered=JSON.parse(fs.readFileSync(file,'utf8'));
  const current=projectPrompt('mcp/instructions',registered.text,{root});
  const begin='[KIN_PROJECT_PROMPT:mcp/instructions]',end='[/KIN_PROJECT_PROMPT:mcp/instructions]';
  if(text.includes(begin)&&text.includes(end)) {
    const start=text.indexOf(begin),stop=text.indexOf(end,start)+end.length;
    return text.slice(0,start)+begin+'\n'+current+'\n'+end+text.slice(stop);
  }
  const oldTexts=new Set([registered.text,...(registered.previous_texts??[])]);
  const history=path.join(root,'.history/mcp/instructions');
  if(fs.existsSync(history))for(const name of fs.readdirSync(history)) {
    if(name.endsWith('.json'))oldTexts.add(JSON.parse(fs.readFileSync(path.join(history,name),'utf8')).text);
  }
  // Sessions created before named fragments retain the registered text in their cache.
  for(const old of [...oldTexts].filter(Boolean).sort((a,b)=>b.length-a.length)) {
    if(text.includes(old))return text.replaceAll(old,begin+'\n'+current+'\n'+end);
  }
  return text;
}

/** Match complete registered revisions, never a position or a prose substring.
 * Native can put the persona beside permission text in one developer message. */
function refreshRegisteredPrompt(name,text,{root}={}) {
  if(!root||typeof text!=='string')return text;
  const file=path.join(root,'.defaults',name+'.json');
  let registered;
  try{registered=JSON.parse(fs.readFileSync(file,'utf8'));}catch(error){if(error.code==='ENOENT')return text;throw error;}
  const current=currentProjectPrompt(name,registered.text,{root});
  const known=[registered.text,current,...(registered.previous_texts??[])];
  const history=path.join(root,'.history',name);
  if(fs.existsSync(history))for(const file of fs.readdirSync(history))if(file.endsWith('.json'))
    known.push(JSON.parse(fs.readFileSync(path.join(history,file),'utf8')).text);
  return known.some(old=>typeof old==='string'&&text.trim()===old.trim())?current:text;
}

export function refreshPersonaPrompt(text,options={}) {
  if(typeof text!=='string')return text;
  const begin='<collaboration_mode>',end='</collaboration_mode>';
  if(text.startsWith(begin)&&text.endsWith(end)) {
    const body=text.slice(begin.length,-end.length),current=refreshRegisteredPrompt('persona',body,options);
    return current===body?text:begin+current+end;
  }
  return refreshRegisteredPrompt('persona',text,options);
}

/** Native compaction caches its launch-time prompt. Refresh only the exact
 * registered instruction part; quoted text and historical summaries remain data. */
export function refreshSessionSummaryPrompt(text,options={}) {
  return refreshRegisteredPrompt('summary/session',text,options);
}

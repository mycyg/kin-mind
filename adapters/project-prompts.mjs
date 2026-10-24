import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
/** Current code defaults and KIN edits have separate files; requests load edits anew. */
export function projectPrompt(name,fallback,{root=process.env.KIN_PROMPT_ROOT,register=true}={}) {
  if(!root)return fallback;
  const file=path.join(root,name+'.json'), defaults=path.join(root,'.defaults',name+'.json');
  let document;
  try{document=JSON.parse(fs.readFileSync(file,'utf8'));}catch(error){if(error.code!=='ENOENT')throw error;}
  let current;
  try{current=JSON.parse(fs.readFileSync(defaults,'utf8'));}catch(error){if(error.code!=='ENOENT')throw error;}
  if(register&&current?.text!==fallback){
    fs.mkdirSync(path.dirname(defaults),{recursive:true});
    const temporary=defaults+'.'+randomUUID()+'.tmp';
    fs.writeFileSync(temporary,JSON.stringify({name,version:1,source:'default',text:fallback,previous_texts:[...new Set([...(current?.previous_texts??[]),...(current?.text?[current.text]:[])])],updated_at:new Date().toISOString()}));
    fs.renameSync(temporary,defaults);
  }
  return document?.edited===true||document?.version>1?document.text:fallback;
}

/** Read the canonical text without registering a stale native-session copy as a default. */
export function currentProjectPrompt(name,fallback,{root=process.env.KIN_PROMPT_ROOT}={}) {
  if(!root)return fallback;
  for(const candidate of [path.join(root,name+'.json'),path.join(root,'.defaults',name+'.json')]) {
    try {
      const document=JSON.parse(fs.readFileSync(candidate,'utf8'));
      if(candidate.includes(path.sep+'.defaults'+path.sep)||document.edited===true||document.version>1)return document.text;
    } catch(error) {if(error.code!=='ENOENT')throw error;}
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

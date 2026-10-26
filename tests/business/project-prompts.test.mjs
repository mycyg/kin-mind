import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
import {projectPrompt,currentProjectPrompt,refreshMcpInstructions} from '../../adapters/project-prompts.mjs';
const fixture=t=>{const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-prompt-current-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));return root;};
test('the canonical persona cannot be overwritten by an old native session copy',t=>{
 const root=fixture(t);projectPrompt('persona','当前人格：我记得小光和一一。',{root});
 assert.equal(currentProjectPrompt('persona','原生会话缓存的旧文本。',{root}),'当前人格：我记得小光和一一。');
 assert.equal(JSON.parse(fs.readFileSync(path.join(root,'.defaults/persona.json'))).text,'当前人格：我记得小光和一一。');
 fs.writeFileSync(path.join(root,'persona.json'),JSON.stringify({name:'persona',edited:true,version:1,text:'我现在想这样和她们相处。'}));
 for(const mode of ['reply','assessment','creation'])assert.equal(currentProjectPrompt('persona',mode+' 的旧文本',{root}),'我现在想这样和她们相处。');
});
test('MCP cached instructions use the named current fragment without changing user content',t=>{
 const root=fixture(t),old='旧的工具用法。',fresh='新的工具用法。';projectPrompt('mcp/instructions',old,{root});
 fs.mkdirSync(path.join(root,'mcp'),{recursive:true});fs.writeFileSync(path.join(root,'mcp/instructions.json'),JSON.stringify({name:'mcp/instructions',edited:true,version:2,text:fresh}));
 const begin='[KIN_PROJECT_PROMPT:mcp/instructions]',end='[/KIN_PROJECT_PROMPT:mcp/instructions]';
 assert.equal(refreshMcpInstructions('前文\n'+begin+'\n'+old+'\n'+end+'\n后文',{root}),'前文\n'+begin+'\n'+fresh+'\n'+end+'\n后文');
 assert.ok(refreshMcpInstructions('旧会话\n'+old,{root}).includes(fresh));
 assert.equal(refreshMcpInstructions('没有这个片段的用户原话。',{root}),'没有这个片段的用户原话。');
});

test('native compaction reads the edited prompt after cache reuse and restart without rewriting ordinary messages',async t=>{
 const {refreshSessionSummaryPrompt}=await import('../../adapters/project-prompts.mjs');
 const root=fixture(t),old='合成摘要：保留当前话题。',fresh='合成摘要：保留偏好的条件与原话。';
 projectPrompt('summary/session',old,{root});fs.mkdirSync(path.join(root,'summary'),{recursive:true});
 fs.writeFileSync(path.join(root,'summary/session.json'),JSON.stringify({name:'summary/session',version:2,edited:true,text:fresh}));
 assert.equal(refreshSessionSummaryPrompt(old,{root}),fresh);
 assert.equal(refreshSessionSummaryPrompt('她引用过：'+old,{root}),'她引用过：'+old);
 projectPrompt('summary/session','更新后的代码默认摘要。',{root});
 assert.equal(refreshSessionSummaryPrompt(old,{root}),fresh,'an old native session still gets the KIN edit after a default refresh');
 assert.equal(currentProjectPrompt('summary/session','cached old launch',{root}),fresh);
});


test('persona refresh recognizes current, historical and collaboration-wrapped text without rewriting neighbors',async t=>{
 const {refreshPersonaPrompt}=await import('../../adapters/project-prompts.mjs');
 const root=fixture(t),old='Synthetic old persona.',fresh='Synthetic new persona.';
 projectPrompt('persona',old,{root});projectPrompt('persona','Synthetic new code default.',{root});
 fs.mkdirSync(path.join(root,'.history/persona'),{recursive:true});
 fs.writeFileSync(path.join(root,'.history/persona/2.json'),JSON.stringify({text:'Synthetic prior edit.'}));
 fs.writeFileSync(path.join(root,'persona.json'),JSON.stringify({version:3,edited:true,text:fresh}));
 for(const cached of [old,'Synthetic prior edit.',fresh])assert.equal(refreshPersonaPrompt(cached,{root}),fresh);
 assert.equal(refreshPersonaPrompt('<collaboration_mode>'+old+'</collaboration_mode>',{root}),'<collaboration_mode>'+fresh+'</collaboration_mode>');
 for(const text of ['Synthetic base.', 'Quoted '+old,old+'\nSynthetic permissions.'])assert.equal(refreshPersonaPrompt(text,{root}),text);
});

test('an edit stays in force when its code default changes, and is reported stale until it is saved again or follows the default',async t=>{
 const {stalePrompts}=await import('../../adapters/project-prompts.mjs');
 const root=fixture(t),put=(name,value)=>{fs.mkdirSync(path.dirname(path.join(root,name)),{recursive:true});fs.writeFileSync(path.join(root,name+'.json'),JSON.stringify({name,...value}));};
 for(const name of ['system','assessment/review'])put('.defaults/'+name,{version:1,source:'default',text:'合成默认一。',updated_at:'2026-01-01T00:00:00.000Z'});
 put('system',{version:2,edited:true,text:'Kin 改过的系统提示。',updated_at:'2026-01-02T00:00:00.000Z'});
 put('assessment/review',{version:2,edited:true,text:'冻结的评估。',updated_at:'2026-01-02T00:00:00.000Z'});
 assert.deepEqual(stalePrompts(root),[]);
 assert.equal(projectPrompt('system','合成默认二。',{root}),'Kin 改过的系统提示。','never overwritten');
 projectPrompt('assessment/review','合成评估二，带本轮上下文。',{root});
 assert.deepEqual(stalePrompts(root).map(item=>[item.name,item.version]),[['system',2]],'assembled texts are frozen, not stale');
 put('system',{version:2,edited:true,text:'Kin 改过的系统提示。',updated_at:'2026-01-02T00:00:00.000Z',default_seen_at:JSON.parse(fs.readFileSync(path.join(root,'.defaults/system.json'))).updated_at});
 assert.deepEqual(stalePrompts(root),[],'kept over the newer default');
 put('system',{version:3,edited:false,text:'合成默认二。',updated_at:new Date().toISOString()});
 assert.equal(projectPrompt('system','合成默认三。',{root}),'合成默认三。','a restored default follows the code again');
 assert.equal(currentProjectPrompt('system','旧缓存',{root}),'合成默认三。');
 assert.deepEqual(stalePrompts(root),[]);
});

test('registered defaults keep a bounded list of earlier texts and an unchanged file is not read again',t=>{
 const root=fixture(t);
 for(let i=0;i<20;i++)projectPrompt('reply-interface','合成默认 '+(i%18),{root});
 const registered=JSON.parse(fs.readFileSync(path.join(root,'.defaults/reply-interface.json')));
 assert.equal(registered.previous_texts.length,16);assert.equal(registered.previous_texts.at(-1),'合成默认 0');
 assert.ok(!registered.previous_texts.includes(registered.text));
 projectPrompt('reply-interface',registered.text,{root});
 const original=fs.readFileSync;let reads=0;fs.readFileSync=(...args)=>{reads++;return original(...args);};
 try{projectPrompt('reply-interface',registered.text,{root});projectPrompt('reply-interface',registered.text,{root});}finally{fs.readFileSync=original;}
 assert.equal(reads,0);
 fs.writeFileSync(path.join(root,'reply-interface.json'),JSON.stringify({version:2,edited:true,text:'刚改的'}));
 assert.equal(projectPrompt('reply-interface',registered.text,{root}),'刚改的');
});

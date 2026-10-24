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

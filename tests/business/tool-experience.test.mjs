import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ToolActivityObserver,ToolArtifactObserver,observeToolUpdates,toolExperience,redactToolText} from '../../adapters/memory-events.mjs';

function observer(t,{owner=true}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-tool-experience-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const journal={directory:path.join(root,'journal'),append:()=>({state:'queued'})};
  const scope={project:'personal',persona:'Kin',collection:'default',world:'real'};
  const activity=new ToolActivityObserver({root,scope,session:()=>'main-session',owner:()=>owner});
  // The host hands one update stream to both observers.
  const o=Object.assign(observeToolUpdates(new ToolArtifactObserver({journal}),activity),{calls:activity.calls});
  const spooled=()=>{try{return fs.readdirSync(path.join(root,'host-spool')).map(name=>JSON.parse(fs.readFileSync(path.join(root,'host-spool',name),'utf8')));}catch{return [];}};
  return {o,spooled,scope};
}

test('a tool call Kin finishes in an owner turn becomes her tool activity, in the retired hook\'s shape',t=>{
  const {o,spooled,scope}=observer(t);
  o.update({toolCallId:'call_1',sessionUpdate:'tool_call',kind:'execute',title:'Run git status',status:'in_progress',rawInput:{command:['git','status'],cwd:'/work'}});
  assert.deepEqual(spooled(),[],'nothing is kept before the call ends');
  o.update({toolCallId:'call_1',sessionUpdate:'tool_call_update',status:'completed',rawOutput:{exit_code:0,stdout:'nothing to commit'}});
  const [{event,payload}]=spooled();
  assert.equal(event,'tool');
  assert.deepEqual({...payload,command_id:undefined},{session_id:'main-session',tool_name:'shell',tool_use_id:'call_1',tool_input:{command:['git','status'],cwd:'/work'},
    tool_response:{exit_code:0,stdout:'nothing to commit'},isError:false,host:'codex',scenario:'companion',hook_event_name:'PostToolUse',scope,
    memory_context_managed:true,extract:false,command_id:undefined});
  o.update({toolCallId:'call_1',status:'completed',rawOutput:{exit_code:0}});
  assert.equal(spooled().length,1,'one call is one record');
});

test('credentials never become memory, and every field is bounded',()=>{
  const record=toolExperience({id:'c',kind:'execute',status:'failed',rawInput:{command:['curl','-H','Authorization: Bearer abcdefghijklmnopqrstuvwxyz','https://x.test/?token=s3cr3t'],env:{OPENAI_API_KEY:'sk-live-abcdefghijklmnop'},password:'hunter2'},
    rawOutput:{exit_code:22,stderr:'x'.repeat(20000)}},{session:'s'});
  const text=JSON.stringify(record.payload);
  for(const secret of ['abcdefghijklmnopqrstuvwxyz','s3cr3t','sk-live-abcdefghijklmnop','hunter2'])assert.equal(text.includes(secret),false,secret);
  assert.equal(record.payload.isError,true);
  assert.ok(text.length<20000);
  assert.match(redactToolText('ghp_'+'a'.repeat(36)),/\[redacted\]/);
});

test('recall from the memory store, internal turns and unfinished calls leave nothing behind',t=>{
  const owner=observer(t);
  owner.o.update({toolCallId:'mcp_1',status:'completed',rawInput:{server:'memorypalace',tool:'recall',arguments:{query:'猫'}},rawOutput:{content:[]}});
  assert.deepEqual(owner.spooled(),[],'what the store returned is not a new observation');
  owner.o.update({toolCallId:'mcp_2',status:'completed',rawInput:{server:'kin-host',tool:'send_file_to_owner',arguments:{path:'/tmp/a.png'}},rawOutput:{state:'accepted'}});
  assert.equal(owner.spooled()[0].payload.tool_name,'mcp__kin-host__send_file_to_owner');
  const internal=observer(t,{owner:false});
  internal.o.update({toolCallId:'call_2',kind:'read',status:'in_progress',rawInput:{path:'/etc/hosts'}});
  internal.o.update({toolCallId:'call_2',status:'completed',rawOutput:'127.0.0.1 localhost'});
  assert.deepEqual(internal.spooled(),[]);
  for(let i=0;i<300;i++)owner.o.update({toolCallId:'open_'+i,kind:'execute',status:'in_progress',rawInput:{command:['sleep','1']}});
  assert.ok(owner.o.calls.size<=200,'calls that never finish are forgotten (AD2-12)');
});

test('one observer that throws never stops the other',()=>{
  const seen=[];
  const both=observeToolUpdates({update(){throw Error('broken');}},{update:u=>seen.push(u.toolCallId)});
  both.update({toolCallId:'x'});
  assert.deepEqual(seen,['x']);
});

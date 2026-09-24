import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {readNativeToolDeliveries} from '../../adapters/native-tool-deliveries.mjs';

// A reply Kin sent with its own file tool counts as delivered only when three records agree: the
// task's completed tool printed an accepted receipt in the task's own rollout, the host outbox
// accepted that message while the tool ran, and the file on disk is still the one it sent.
const sha=value=>createHash('sha256').update(value).digest('hex');
const T0=Date.parse('2026-09-20T08:00:00Z');
const at=seconds=>new Date(T0+seconds*1000).toISOString();
const TASK={id:'task-1',tools:{'call-1':{status:'completed'}}};

function world(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-native-tool-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const outbox=path.join(root,'outbox'),artifact=path.join(root,'artifacts','report.pdf'),file=path.join(root,'rollout.jsonl');
  fs.mkdirSync(outbox);fs.mkdirSync(path.dirname(artifact));
  const body=Buffer.from('合成附件：一页报告。');fs.writeFileSync(artifact,body);
  const lines=[];
  const w={root,outbox,artifact,body,file,lines,
    meta(id='sess-1'){lines.push({type:'session_meta',payload:{id}});return w;},
    tool(id,{receipt={state:'accepted',id:'out-1',messageId:'msg-1'},exit=0,start=0,end=10,type='CommandExecution'}={}){
      lines.push({type:'event_msg',payload:{type:'item_completed',thread_id:'sess-1',turn_id:'turn-1',started_at_ms:T0+start*1000,completed_at_ms:T0+end*1000,
        item:{type,id,status:'completed',exit_code:exit,command:'send-file --private-arg',stdout:JSON.stringify(receipt)+'\n'}}});
      return w;},
    said(text){lines.push({type:'event_msg',payload:{type:'item_completed',thread_id:'sess-1',turn_id:'turn-1',item:{type:'AgentMessage',id:'call-1',text}}});return w;},
    sent(change={}){
      fs.writeFileSync(path.join(outbox,'out-1.json'),JSON.stringify({id:'out-1',state:'accepted',stage:'platform-accepted',submissionStarted:true,kind:'reply',
        messageId:'msg-1',attemptedAt:at(1),uploadedAt:at(2),submittedAt:at(3),acceptedAt:at(4),
        artifact:{path:artifact,sha256:sha(body),bytes:body.length,name:'report.pdf'},media:{type:'file',name:'report.pdf',bytes:body.length},...change}));
      return w;},
    write(tail='\n'){fs.writeFileSync(file,lines.map(line=>JSON.stringify(line)).join('\n')+tail);return w;},
    read(task=TASK,sessionId='sess-1'){return readNativeToolDeliveries({file,sessionId,task,outboxDirectory:outbox});},
  };
  return w;
}

test('a completed tool receipt joins the accepted media it sent, and the proof names no path, command or output',async t=>{
  const w=world(t).meta().tool('call-1').sent().write();
  const {proofs,diagnostics}=await w.read();
  assert.deepEqual(diagnostics,[]);
  assert.equal(proofs.length,1);
  const {sourceHash,...proof}=proofs[0];
  assert.deepEqual(proof,{id:'out-1',outboxId:'out-1',toolId:'call-1',sessionId:'sess-1',taskId:'task-1',messageId:'msg-1',state:'accepted',
    source:'native-tool-outbox',artifact:{sha256:sha(w.body),bytes:w.body.length,type:'file',name:'report.pdf'},
    toolStartedAt:at(0),toolCompletedAt:at(10),acceptedAt:at(4)});
  assert.match(sourceHash,/^[a-f0-9]{64}$/);
  const text=JSON.stringify(proofs);
  for(const hidden of [path.basename(w.root),'send-file','--private-arg','"state":"accepted","id"','合成附件'])assert.ok(!text.includes(hidden),hidden);
});

test('only a receipt printed by one of the task\'s own completed tools counts',async t=>{
  const cases={
    'the task still runs the tool':w=>[w.meta().tool('call-1').sent().write(),{id:'task-1',tools:{'call-1':{status:'running'}}}],
    'the tool failed':w=>[w.meta().tool('call-1',{exit:1}).sent().write()],
    'another task\'s tool printed it':w=>[w.meta().tool('call-9').sent().write()],
    'Kin only quoted a receipt in its reply':w=>[w.meta().said(JSON.stringify({state:'accepted',id:'out-1',messageId:'msg-1'})).sent().write()],
  };
  for(const [name,make] of Object.entries(cases)){
    const [w,task]=make(world(t));
    assert.deepEqual(await w.read(task),{proofs:[],diagnostics:[]},name);
  }
});

test('media sent outside the tool run, not accepted, or changed since is no proof',async t=>{
  const elsewhere=fs.mkdtempSync(path.join(os.tmpdir(),'kin-native-elsewhere-'));t.after(()=>fs.rmSync(elsewhere,{recursive:true,force:true}));
  const cases={
    'accepted after the tool finished':[w=>w.sent({acceptedAt:at(11)}),'native-tool-outbox-invalid'],
    'refused by the platform':[w=>w.sent({state:'failed'}),'native-tool-outbox-invalid'],
    'a notice, not a reply':[w=>w.sent({kind:'notice'}),'native-tool-outbox-invalid'],
    'edited after it was sent':[w=>{w.sent();fs.writeFileSync(w.artifact,Buffer.alloc(w.body.length,1));},'native-tool-artifact-invalid'],
    'a file outside the host directories':[w=>{const copy=path.join(elsewhere,'report.pdf');fs.writeFileSync(copy,w.body);
      w.sent({artifact:{path:copy,sha256:sha(w.body),bytes:w.body.length,name:'report.pdf'}});},'native-tool-artifact-invalid'],
    'a link to the sent file':[w=>{const link=path.join(path.dirname(w.artifact),'link.pdf');fs.symlinkSync(w.artifact,link);
      w.sent({artifact:{path:link,sha256:sha(w.body),bytes:w.body.length,name:'report.pdf'}});},'native-tool-artifact-invalid'],
  };
  for(const [name,[send,code]] of Object.entries(cases)){
    const w=world(t).meta().tool('call-1').write();send(w);
    assert.deepEqual(await w.read(),{proofs:[],diagnostics:[{toolId:'call-1',code}]},name);
  }
});

test('one delivery claimed twice proves nothing',async t=>{
  const both=world(t).meta().tool('call-1').tool('call-2').sent().write();
  assert.deepEqual(await both.read({id:'task-1',tools:{'call-1':{status:'completed'},'call-2':{status:'completed'}}}),
    {proofs:[],diagnostics:[{toolId:'call-1',code:'native-tool-proof-conflict'},{toolId:'call-2',code:'native-tool-proof-conflict'}]});
  const repeated=world(t).meta().tool('call-1').tool('call-1').sent().write();
  assert.deepEqual(await repeated.read(),{proofs:[],diagnostics:[{toolId:'call-1',code:'native-tool-receipt-ambiguous'}]});
});

test('the rollout must be the task\'s own session, and a line still being written waits for the next read',async t=>{
  const other=world(t).meta('sess-2').tool('call-1').sent().write();
  await assert.rejects(other.read(),{code:'native-rollout-session-mismatch'});
  const twice=world(t).meta().meta('sess-2').tool('call-1').sent().write();
  await assert.rejects(twice.read(),{code:'native-rollout-session-mismatch'});
  await assert.rejects(readNativeToolDeliveries({file:'rollout.jsonl',sessionId:'sess-1',task:TASK,outboxDirectory:twice.outbox}),
    {code:'native-tool-delivery-input-invalid'});

  const open=world(t).meta().tool('call-1').sent().write('');
  assert.deepEqual(await open.read(),{proofs:[],diagnostics:[]});
  fs.appendFileSync(open.file,'\n');
  assert.equal((await open.read()).proofs.length,1);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fork} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {TransportManifests,transportId,operatorMain,GROUP_STATES,TERMINAL_GROUP_STATES} from './transport-manifest.mjs';
import {createFakeTransport} from './testing/fake-transport.mjs';
import {readJsonFile,writeJsonAtomic} from './atomic-json.mjs';

const sha256=value=>createHash('sha256').update(value).digest('hex');
const SHORT={feishu:{text:{limit:20,measure:'utf16'}}};   // LONG becomes three fragments
const LONG='第一段内容写到这里，先停一下。第二段内容接着往下写，把话说完整。第三段是收尾的话，到这里为止。';

/** One temp world per test: manifests, a fake transport with its own receipts, a clock, recorded side effects. */
function world(t,{contracts,hooks,retry,review,flavour}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-manifest-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const state={now:1_000_000},events=[],canceled=[],outcomes=[];
  const clock=()=>state.now,directory=path.join(root,'reply-manifests'),outbox=path.join(root,'outbox');
  const transport=createFakeTransport({directory:outbox,clock,flavour});
  const options={directory,clock,contracts,hooks,retry,review,sleep:async()=>{},lease:{heartbeat:false},
    emit:async event=>{events.push(event);},cancelShare:async draftId=>{canceled.push(draftId);},onOutcome:detail=>outcomes.push(detail)};
  const open=(extra={})=>new TransportManifests({...options,...extra});
  return {root,directory,outbox,state,clock,transport,platform:transport.platform,events,canceled,outcomes,open,manifests:open()};
}
const group=(batch,texts,extra={})=>texts.map((text,i)=>({request:{draft_id:`${batch}-draft-${i}`,reply_id:extra.replyId??'input-1',text},
  delivery:{id:`${batch}-bubble-${i}`,text,kind:'reply',memoryBatchId:batch,expectedBubbles:texts.length,...(extra.taskId?{taskId:extra.taskId}:{})}}));
const fragments=manifest=>manifest.bubbles.flatMap(b=>b.fragments);
const filed=(w,id)=>{const done=path.join(w.directory,'done');return (fs.existsSync(done)?fs.readdirSync(done):[]).map(month=>path.join(done,month,id+'.json')).find(file=>fs.existsSync(file));};
const onDisk=(w,id)=>readJsonFile(path.join(w.directory,id+'.json')).value??readJsonFile(filed(w,id)??'').value;

test('a reviewed group is final on disk before the first send, and only whole bubbles reach memory',async t=>{
  const w=world(t,{contracts:SHORT}),seenAtFirstSend=[];
  const reviewed=[];
  const review=async(requests,context)=>{reviewed.push({...context,stateOnDisk:onDisk(w,'batch-a').state});return {state:'ready',review_id:'review-1',checked:requests.map(r=>({text:r.text,references:[{unit_id:'finding-1',version:1}]}))};};
  w.platform.onSubmit(()=>{if(!seenAtFirstSend.length)seenAtFirstSend.push(onDisk(w,'batch-a'));});
  const draft=w.manifests.createDraft({entries:group('batch-a',['短的一句。',LONG]),ownerEpoch:'epoch-1'});
  assert.equal(draft.state,'draft');assert.equal(draft.group_id,'batch-a');assert.deepEqual(fragments(draft),[]);
  const {manifest}=await w.manifests.run('batch-a',{transport:w.transport,review});
  assert.deepEqual(reviewed.map(r=>[r.frozen,r.stateOnDisk]),[[false,'draft']],'the draft is on disk before the review starts');
  const frozen=seenAtFirstSend[0];
  assert.equal(frozen.state,'sending');assert.deepEqual(frozen.review,{id:'review-1',checked_hashes:[sha256('短的一句。'),sha256(LONG)]});
  assert.deepEqual(fragments(frozen).map(f=>f.transport_id),fragments(manifest).map(f=>f.transport_id),'every identity existed before the first byte left');
  assert.equal(manifest.state,'accepted');assert.equal(manifest.bubbles[1].fragments.length,3);
  for(const bubble of manifest.bubbles)for(const f of bubble.fragments) {
    assert.equal(f.transport_id,transportId(bubble.bubble_id,f.index,f.body_sha256));assert.match(f.transport_id,/^kin-frag-[0-9a-f]{48}$/);
  }
  assert.deepEqual(w.transport.sends.map(d=>d.text),['短的一句。',...manifest.bubbles[1].fragments.map(f=>LONG.slice(f.start,f.end))]);
  assert.equal(w.transport.sends.map(d=>d.text).slice(1).join(''),LONG);
  assert.ok(w.transport.sends.every(d=>d.memory===false&&d.memoryBatchId==='batch-a'&&d.expectedBubbles===2&&d.references[0].unit_id==='finding-1'));
  assert.deepEqual(w.transport.sends.map(d=>[d.bubbleId,d.fragment.index,d.fragment.count]),[['batch-a-bubble-0',0,1],['batch-a-bubble-1',0,3],['batch-a-bubble-1',1,3],['batch-a-bubble-1',2,3]]);
  assert.deepEqual(w.transport.events,[],'the transport itself reported nothing');
  assert.deepEqual(w.events.map(e=>[e.id,e.bubble_id,e.delivery_id,e.expected_bubbles,e.state,e.text]),[
    ['delivery:feishu:batch-a-bubble-0:accepted','batch-a-bubble-0','batch-a',2,'accepted','短的一句。'],
    ['delivery:feishu:batch-a-bubble-1:accepted','batch-a-bubble-1','batch-a',2,'accepted',LONG]]);
  assert.deepEqual(w.events[1].message_ids,['om_2','om_3','om_4']);assert.equal(w.events[1].message_id,'om_2');assert.equal(w.events[0].message_ids,undefined);
  assert.deepEqual(fs.readdirSync(w.directory).sort(),['done','leases'],'a settled group leaves the live set');
  assert.deepEqual(w.outcomes.map(o=>[o.groupId,o.groupState,o.state,o.inputId]),[['batch-a','reviewed','prepared','input-1'],['batch-a','sending','prepared','input-1'],['batch-a','accepted','accepted','input-1']]);
});

test('fragment partial success: one fragment accepted, the next rejected',async t=>{
  const w=world(t,{contracts:SHORT});
  w.platform.script('reject',{when:()=>w.platform.calls.length===2});
  w.manifests.createDraft({entries:group('batch-b',[LONG]),ownerEpoch:'e'});
  const {manifest}=await w.manifests.run('batch-b',{transport:w.transport});
  assert.equal(manifest.state,'partial');assert.equal(manifest.bubbles[0].state,'rejected');
  assert.deepEqual(manifest.bubbles[0].fragments.map(f=>f.state),['accepted','rejected','canceled']);
  assert.equal(w.transport.sends.length,2,'nothing is sent after the hole');
  assert.deepEqual(w.events.map(e=>e.state),['canceled'],'never an accepted event for a bubble the owner only got part of');
  assert.deepEqual(w.canceled,['batch-b-draft-0'],'its reservation is released so the content can be offered again');
  assert.ok(TERMINAL_GROUP_STATES.includes(manifest.state)&&GROUP_STATES.includes(manifest.state));
});

test('crash after the send, before the manifest is saved: a new instance reconciles by ID and never sends twice',async t=>{
  const w=world(t);let crashes=0;
  const crashing=w.open({hooks:{afterSend:()=>{if(!crashes++)throw Error('Simulated crash after send');}}});
  crashing.createDraft({entries:group('batch-c',['第一句。','第二句。']),ownerEpoch:'e'});
  await assert.rejects(crashing.run('batch-c',{transport:w.transport}),/Simulated crash/);
  const left=onDisk(w,'batch-c');
  assert.equal(left.bubbles[0].fragments[0].state,'submitting','the manifest only knows a send was about to begin');
  assert.equal((await w.transport.receipt(left.bubbles[0].fragments[0].transport_id)).state,'accepted','the transport wrote its receipt');
  const {manifest}=await w.open().run('batch-c',{transport:w.transport});
  assert.equal(manifest.state,'accepted');
  assert.deepEqual(w.transport.sends.map(d=>d.bubbleId),['batch-c-bubble-0','batch-c-bubble-1'],'the first fragment was sent exactly once');
  assert.equal(w.platform.calls.length,2);assert.deepEqual(w.events.map(e=>e.state),['accepted','accepted']);
});

test('a throw before submission leaves no receipt: the fragment is unsent again and retried',async t=>{
  const w=world(t);
  w.transport.fault('before-receipt');w.transport.fault('before-submit');
  w.manifests.createDraft({entries:group('batch-d',['只有一句。']),ownerEpoch:'e'});
  let result=await w.manifests.run('batch-d',{transport:w.transport});
  assert.deepEqual([result.manifest.state,result.manifest.bubbles[0].state,result.manifest.bubbles[0].fragments[0].state,result.manifest.failures],['sending','unsent','unsent',1]);
  assert.equal(result.manifest.retryAt,w.state.now+60000);assert.deepEqual(w.events,[],'nothing is called unconfirmed');
  assert.equal((await w.manifests.resumeDue({transport:w.transport})).state,'idle','not before its retry time');
  w.state.now+=60000;
  result=(await w.manifests.resumeDue({transport:w.transport}));
  assert.deepEqual(result.groups,[{group_id:'batch-d',state:'sending'}],'a receipt proving nothing was submitted is the same: unsent');
  assert.equal(onDisk(w,'batch-d').retryAt,w.state.now+120000);
  w.state.now+=120000;
  await w.manifests.resumeDue({transport:w.transport});
  assert.equal(onDisk(w,'batch-d').state,'accepted');assert.equal(w.platform.calls.length,1);
  assert.deepEqual(new Set(w.transport.sends.map(d=>d.id)).size,1,'every retry used the stored transport ID');
});

test('a transport that never becomes available ends the group; it is never left pending',async t=>{
  const w=world(t,{retry:{maxFailures:3}});
  w.transport.fault('before-receipt',{times:99});
  w.manifests.createDraft({entries:group('batch-e',['第一句。','第二句。']),ownerEpoch:'e'});
  await w.manifests.run('batch-e',{transport:w.transport});
  for(let pass=0;pass<5;pass++){w.state.now+=16*60000;await w.manifests.resumeDue({transport:w.transport});}
  const manifest=onDisk(w,'batch-e');
  assert.deepEqual([manifest.state,manifest.reason,...manifest.bubbles.map(b=>b.state)],['undeliverable','transport-unavailable','undeliverable','undeliverable']);
  assert.equal(w.transport.sends.length,3);assert.deepEqual(w.events.map(e=>e.state),['canceled','canceled']);
  assert.deepEqual(w.manifests.live(),[]);
});

test('a definitive platform rejection is rejected, never unconfirmed, and the rest of the group goes on',async t=>{
  const w=world(t);
  w.platform.script('reject');
  w.manifests.createDraft({entries:group('batch-f',['被拒绝的一句。','后面的一句。']),ownerEpoch:'e'});
  const {manifest}=await w.manifests.run('batch-f',{transport:w.transport});
  assert.deepEqual(manifest.bubbles.map(b=>b.state),['rejected','accepted']);assert.equal(manifest.state,'partial');
  assert.deepEqual(w.events.map(e=>[e.bubble_id,e.state]),[['batch-f-bubble-0','canceled'],['batch-f-bubble-1','accepted']]);
  assert.ok(!JSON.stringify(manifest).includes('unconfirmed')&&!JSON.stringify(manifest).includes('"unknown"'));
  assert.deepEqual(w.manifests.status().undelivered,['batch-f']);
});

test('an unknown outcome blocks only its own group; a newer group sends normally; a receipt or an operator unblocks it',async t=>{
  const w=world(t);
  w.platform.script('lost');
  w.manifests.createDraft({entries:group('batch-old',['旧的第一句。','旧的第二句。'],{replyId:'input-old'}),ownerEpoch:'e'});
  const old=(await w.manifests.run('batch-old',{transport:w.transport})).manifest;
  assert.deepEqual([old.state,old.bubbles[0].state,old.bubbles[0].fragments[0].state,old.bubbles[1].state],['blocked-unknown','unconfirmed','unknown','unsent']);
  assert.deepEqual(w.events.map(e=>[e.bubble_id,e.state]),[['batch-old-bubble-0','unconfirmed']]);
  w.manifests.createDraft({entries:group('batch-new',['新的回复。'],{replyId:'input-new'}),ownerEpoch:'e'});
  assert.equal((await w.manifests.run('batch-new',{transport:w.transport})).manifest.state,'accepted');
  w.state.now+=600000;
  const before=fs.readFileSync(path.join(w.directory,'batch-old.json'),'utf8');
  assert.deepEqual(await w.manifests.resumeDue({transport:w.transport}),{state:'idle',checked:0,groups:[]},'lease expiry and time change nothing about an unknown fragment');
  assert.equal(fs.readFileSync(path.join(w.directory,'batch-old.json'),'utf8'),before);assert.equal(w.transport.sends.length,2,'and it is never resent');
  const status=w.manifests.status();
  assert.deepEqual(status.blocked,['batch-old']);assert.deepEqual(Object.keys(status.groups).sort(),['batch-new','batch-old'],'status is keyed by group; the older group stays visible');
  assert.ok(!JSON.stringify(status).includes('旧的'),'status carries no message text');
  // The operator states what happened; the service's next pass goes on from the manifest.
  const printed=[];
  await operatorMain(['resolve','--dir',w.directory,'--group','batch-old','--fragment',old.bubbles[0].fragments[0].transport_id,'--outcome','accepted','--message-id','om_operator'],{clock:w.clock,print:line=>printed.push(line)});
  assert.equal(JSON.parse(printed[0]).state,'sending');assert.ok(!printed[0].includes('旧的'));
  await w.manifests.resumeDue({transport:w.transport});
  const done=onDisk(w,'batch-old');
  assert.equal(done.state,'accepted');assert.equal(done.bubbles[0].fragments[0].receipt.source,'operator');
  assert.deepEqual(w.events.filter(e=>e.delivery_id==='batch-old').map(e=>[e.bubble_id,e.state,e.message_id]),
    [['batch-old-bubble-0','unconfirmed',undefined],['batch-old-bubble-0','accepted','om_operator'],['batch-old-bubble-1','accepted','om_2']]);
  await assert.rejects(w.manifests.resolve('batch-x','kin-frag-x',{outcome:'accepted'}),/platform message ID/);
});

test('an unknown fragment is settled by the receipt the transport wrote later, without any send',async t=>{
  const w=world(t);
  w.transport.fault('crash-mid-submit');
  w.manifests.createDraft({entries:group('batch-g',['一句话。']),ownerEpoch:'e'});
  const blocked=(await w.manifests.run('batch-g',{transport:w.transport})).manifest,id=blocked.bubbles[0].fragments[0].transport_id;
  assert.equal(blocked.state,'blocked-unknown');assert.equal((await w.transport.receipt(id)).submissionStarted,true);
  writeJsonAtomic(path.join(w.outbox,id+'.json'),{...await w.transport.receipt(id),state:'accepted',messageId:'om_late',acceptedAt:new Date(w.state.now).toISOString()},{previous:false});
  const printed=[];
  await operatorMain(['reconcile','--dir',w.directory,'--receipts',w.outbox],{clock:w.clock,print:line=>printed.push(line)});
  assert.equal(JSON.parse(printed[0])['batch-g'].state,'accepted');assert.equal(w.transport.sends.length,1);
});

test('unknown inside the idempotency window with resend enabled: the same transport ID is resent once',async t=>{
  const w=world(t,{contracts:{feishu:{resendWithinWindow:true}}});
  w.platform.script('timeout');
  w.manifests.createDraft({entries:group('batch-h',['第一句。','第二句。']),ownerEpoch:'e'});
  const first=(await w.manifests.run('batch-h',{transport:w.transport})).manifest,id=first.bubbles[0].fragments[0].transport_id;
  assert.equal(first.state,'blocked-unknown');assert.equal(first.bubbles[0].fragments[0].firstSubmitAt,w.state.now);
  w.state.now+=30*60000;
  await w.manifests.resumeDue({transport:w.transport});
  assert.equal(onDisk(w,'batch-h').state,'accepted');
  assert.deepEqual(w.transport.sends.map(d=>[d.id===id,Boolean(d.resend)]),[[true,false],[true,true],[false,false]]);
  assert.equal(w.platform.delivered.filter(d=>d.id===id).length,1,'the platform deduplicated the resend: one message on the phone');
  // A second bubble whose resend is also lost stays unknown: never a second automatic resend.
  const again=world(t,{contracts:{feishu:{resendWithinWindow:true}}});
  again.platform.script('lost');again.platform.script('lost');
  again.manifests.createDraft({entries:group('batch-i',['一句。']),ownerEpoch:'e'});
  await again.manifests.run('batch-i',{transport:again.transport});
  for(let pass=0;pass<3;pass++){again.state.now+=60000;await again.manifests.resumeDue({transport:again.transport});}
  assert.equal(onDisk(again,'batch-i').state,'blocked-unknown');assert.equal(again.transport.sends.length,2);
});

test('unknown outside the window, or with resend not enabled: blocked-unknown and never resent',async t=>{
  for(const [contracts,wait] of [[{feishu:{resendWithinWindow:true}},59*60000+1],[undefined,60000]]) {
    const w=world(t,{contracts});
    w.platform.script('timeout');
    w.manifests.createDraft({entries:group('batch-j',['一句。']),ownerEpoch:'e'});
    await w.manifests.run('batch-j',{transport:w.transport});
    w.state.now+=wait;
    await w.manifests.resumeDue({transport:w.transport});
    assert.equal(onDisk(w,'batch-j').state,'blocked-unknown');assert.equal(w.transport.sends.length,1);
  }
});

test('an oversize code block at the 4000 limit goes as one file with no words of the host\'s own',async t=>{
  const w=world(t,{flavour:'wechat'}),block='```\n'+'value = value + 1\n'.repeat(260)+'```';
  assert.ok(block.length>4000);
  w.manifests.createDraft({entries:group('batch-k',['先说一句。',block,'最后一句。']),ownerEpoch:'e',channel:'wechat'});
  const {manifest}=await w.manifests.run('batch-k',{transport:w.transport});
  assert.equal(manifest.state,'accepted');
  const file=w.transport.sends[1],fragment=manifest.bubbles[1].fragments[0];
  assert.deepEqual(manifest.bubbles[1].fragments.length,1);assert.equal(fragment.kind,'file');
  assert.equal(fragment.name,'kin-reply-'+sha256(block).slice(0,16)+'.md');assert.equal(file.media.name,fragment.name);
  assert.equal(file.text,undefined);assert.equal(file.media.type,'file');assert.equal(file.media.data.toString('utf8'),block,'the complete bubble, byte for byte');
  assert.deepEqual(w.events.map(e=>e.state),['accepted','accepted','accepted']);assert.equal(w.events[1].text,block);
});

test('when the file path fails the bubble is undeliverable, visible, and the remaining bubbles are sent',async t=>{
  const w=world(t,{flavour:'wechat'}),block='```\n'+'value = value + 1\n'.repeat(260)+'```';
  w.transport.fault('upload-failed',{when:delivery=>Boolean(delivery.media)});
  w.manifests.createDraft({entries:group('batch-l',['先说一句。',block,'最后一句。']),ownerEpoch:'e',channel:'wechat'});
  const {manifest}=await w.manifests.run('batch-l',{transport:w.transport});
  assert.deepEqual(manifest.bubbles.map(b=>[b.state,b.reason??null]),[['accepted',null],['undeliverable','file-channel-failed'],['accepted',null]]);
  assert.equal(manifest.state,'partial');assert.deepEqual(w.transport.sends.map(d=>d.bubbleId),['batch-l-bubble-0','batch-l-bubble-1','batch-l-bubble-2']);
  assert.deepEqual(w.events.map(e=>e.state),['accepted','canceled','accepted']);assert.deepEqual(w.canceled,['batch-l-draft-1']);
  assert.deepEqual(w.manifests.live(),[],'terminal: nothing stays pending');
  assert.deepEqual(w.manifests.status().undelivered,['batch-l']);
  assert.equal(w.manifests.read('batch-l').bubbles[1].text,block,'the undelivered body stays readable for the next turn');
});

test('re-entry while a group is being sent: the second entrant gets busy and marks nothing',async t=>{
  const w=world(t);let release;const gate=new Promise(resolve=>{release=resolve;});
  w.platform.onSubmit(()=>gate);
  w.manifests.createDraft({entries:group('batch-m',['第一句。','第二句。']),ownerEpoch:'e'});
  const first=w.manifests.run('batch-m',{transport:w.transport});
  while(!w.transport.sends.length)await new Promise(resolve=>setImmediate(resolve));
  const before=fs.readFileSync(path.join(w.directory,'batch-m.json'),'utf8');
  const other=createFakeTransport({directory:w.outbox,clock:w.clock});
  assert.deepEqual([(await w.manifests.run('batch-m',{transport:other})).busy,(await w.open().run('batch-m',{transport:other})).busy],[true,true]);
  assert.deepEqual((await w.open().resumeDue({transport:other})).groups,[{group_id:'batch-m',state:'busy'}]);
  assert.equal(fs.readFileSync(path.join(w.directory,'batch-m.json'),'utf8'),before,'the second entrant wrote nothing');
  assert.ok(!before.includes('unconfirmed')&&!before.includes('"unknown"'));assert.equal(other.sends.length,0);assert.deepEqual(w.events,[]);
  release();
  assert.equal((await first).manifest.state,'accepted');assert.equal(w.platform.calls.length,2);
});

test('lease loss: the old holder\'s save is refused, .prev is restored, and no fragment state regresses',async t=>{
  const w=world(t),second=w.open();let taken=null;
  w.platform.onSubmit(async()=>{
    if(w.platform.calls.length!==1)return;
    // The first holder stalls inside its second send; its lease runs out and another instance takes the group over.
    w.state.now+=90000+30000;
    taken=await second.run('batch-n',{transport:createFakeTransport({directory:w.outbox,clock:w.clock})});
  });
  w.manifests.createDraft({entries:group('batch-n',['第一句。','第二句。']),ownerEpoch:'e'});
  const stalled=await w.manifests.run('batch-n',{transport:w.transport});
  assert.equal(stalled.lost,true,'the old holder noticed it may no longer write');
  assert.equal(taken.manifest.state,'blocked-unknown','the new holder cannot tell what the stalled send did, and does not resend it');
  const kept=onDisk(w,'batch-n');
  assert.deepEqual([kept.leaseGeneration,kept.state,...fragments(kept).map(f=>f.state)],[2,'blocked-unknown','accepted','unknown']);
  // A write of the old generation that slipped past the fence lands over the newer manifest.
  const stale=structuredClone(kept);
  Object.assign(stale,{leaseGeneration:1,state:'sending',revision:kept.revision-1});stale.bubbles[0].fragments[0].state='submitting';stale.bubbles[1].fragments[0].state='submitting';
  writeJsonAtomic(path.join(w.directory,'batch-n.json'),stale);
  assert.equal(w.manifests.read('batch-n').leaseGeneration,2,'readers already see the higher generation');
  w.state.now+=90000+30000;
  const {manifest}=await w.open().run('batch-n',{transport:w.transport});
  assert.equal(manifest.state,'accepted');assert.equal(manifest.leaseGeneration,3);
  assert.equal(w.platform.calls.length,2,'both fragments reached the platform exactly once');
  assert.ok(fs.readdirSync(path.join(w.directory,'quarantine')).some(name=>name.startsWith('batch-n.json.stale.')),'the overwritten file is kept as evidence');
  assert.deepEqual(w.events.map(e=>[e.bubble_id,e.state]),[['batch-n-bubble-0','accepted'],['batch-n-bubble-1','unconfirmed'],['batch-n-bubble-1','accepted']]);
});

test('two instances and a forked process recovering one group: one owner per generation, losers write nothing',async t=>{
  const w=world(t),script=fileURLToPath(new URL('./testing/manifest-contender.mjs',import.meta.url));
  const spawn=()=>new Promise((resolve,reject)=>{const child=fork(script,[w.directory,w.outbox,'batch-o']);child.once('error',reject);child.once('message',()=>resolve(child));});
  w.manifests.createDraft({entries:group('batch-o',['第一句。','第二句。','第三句。']),ownerEpoch:'e'});
  // A real process dies right after its first send: receipt on disk, manifest not updated, lease still held.
  const crashing=await spawn();
  const exit=new Promise(resolve=>crashing.once('exit',resolve));crashing.send({now:w.state.now,crashAfterSends:1});
  assert.equal(await exit,86);
  const left=onDisk(w,'batch-o'),firstId=left.bubbles[0].fragments[0].transport_id;
  assert.deepEqual([left.leaseGeneration,left.bubbles[0].fragments[0].state,(await w.transport.receipt(firstId)).state],[1,'submitting','accepted']);
  const forked=await spawn();t.after(()=>forked.kill());
  const contend=async now=>{
    w.state.now=now;
    const local=[w.open(),w.open()].map(manifests=>{const writes=[],transport=createFakeTransport({directory:w.outbox,clock:w.clock});
      manifests.hooks={beforeSave:manifest=>{writes.push(manifest.leaseGeneration);}};
      transport.platform.onSubmit(()=>new Promise(resolve=>setTimeout(resolve,20)));
      return manifests.run('batch-o',{transport}).then(result=>({busy:Boolean(result.busy),state:result.manifest?.state??null,writes,sent:transport.sends.map(d=>d.id)}));});
    const remote=new Promise(resolve=>{forked.once('message',resolve);forked.send({now,slowMs:20});});
    return Promise.all([...local,remote]);
  };
  const early=await contend(left.updated_at+60000);
  assert.ok(early.every(r=>r.busy&&!r.writes.length&&!r.sent.length),'the dead holder\'s lease has not run out: nobody touches the group');
  const late=await contend(left.updated_at+90000+30000);
  const owners=late.filter(r=>r.writes.length);
  assert.equal(owners.length,1,'exactly one entrant owned the group');assert.deepEqual([...new Set(owners[0].writes)],[2],'under one new generation');
  assert.ok(late.filter(r=>r!==owners[0]).every(r=>!r.writes.length&&!r.sent.length),'the losers wrote and sent nothing');
  assert.equal(onDisk(w,'batch-o').state,'accepted');
  assert.equal(late.flatMap(r=>r.sent).includes(firstId),false,'the fragment the dead process sent was reconciled, not resent');
  assert.equal(late.flatMap(r=>r.sent).length,2);
});

test('a corrupt manifest is quarantined while the other groups resume',async t=>{
  const w=world(t);
  for(const batch of ['batch-p','batch-q','batch-r'])w.manifests.createDraft({entries:group(batch,['一句。']),ownerEpoch:'e'});
  fs.writeFileSync(path.join(w.directory,'batch-p.json'),'{"schema":1,"group_id":"batch-p","bubbles":[');
  const tampered=onDisk(w,'batch-q');tampered.bubbles[0].text='被改动的正文';
  fs.writeFileSync(path.join(w.directory,'batch-q.json'),JSON.stringify(tampered));
  fs.writeFileSync(path.join(w.directory,'batch-s.json'),JSON.stringify({schema:2,group_id:'batch-s',state:'future'}));
  const result=await w.manifests.resumeDue({transport:w.transport,limit:5});
  assert.deepEqual(result.groups,[{group_id:'batch-r',state:'accepted'}]);
  assert.deepEqual(fs.readdirSync(path.join(w.directory,'quarantine')).map(name=>name.split('.json.')[0]).sort(),['batch-p','batch-q'],'unreadable, or a body that no longer matches its hash');
  assert.ok(fs.existsSync(path.join(w.directory,'batch-s.json')),'another version\'s manifest is left for the code that wrote it');
  assert.deepEqual(w.transport.sends.map(d=>d.bubbleId),['batch-r-bubble-0']);
});

test('the guard speaks at bubble boundaries: a started bubble is finished, the rest retired with every reservation released',async t=>{
  const w=world(t,{contracts:SHORT});let crashes=0,asked=0;
  const crashing=w.open({hooks:{afterSend:()=>{if(++crashes===2)throw Error('Simulated crash mid-bubble');}}});
  crashing.createDraft({entries:group('batch-t',[LONG,'第二个气泡。','第三个气泡。']),ownerEpoch:'e'});
  await assert.rejects(crashing.run('batch-t',{transport:w.transport}));
  const guard=async view=>{asked++;assert.equal(view.ownerEpoch,'e');assert.equal(view.request.reply_id,'input-1');return 'cancel';};
  const {manifest}=await w.open().run('batch-t',{transport:w.transport,guard});
  assert.deepEqual(manifest.bubbles.map(b=>b.state),['accepted','canceled','canceled']);assert.equal(asked,1);
  assert.deepEqual([manifest.state,manifest.reason],['retired','input-or-session-superseded']);
  assert.equal(w.transport.sends.length,3,'all three fragments of the started bubble, nothing of the others');
  assert.deepEqual(w.canceled,['batch-t-draft-1','batch-t-draft-2'],'share-cancel for every retired draft, not only the first');
  assert.deepEqual(w.events.map(e=>[e.bubble_id,e.state]),[['batch-t-bubble-0','accepted'],['batch-t-bubble-1','canceled'],['batch-t-bubble-2','canceled']]);
  // wait changes nothing; interrupt parks the group for a tail decision; a retired remainder is what B2 builds on.
  const v=world(t);
  v.manifests.createDraft({entries:group('batch-u',['第一句。','第二句。']),ownerEpoch:'e'});
  const waiting=await v.manifests.run('batch-u',{transport:v.transport,guard:async()=>'wait'});
  assert.deepEqual([waiting.manifest.state,waiting.manifest.revision,v.transport.sends.length],['draft',0,0]);
  let calls=0;
  const interrupted=await v.manifests.run('batch-u',{transport:v.transport,guard:async()=>++calls<3?'send':{action:'interrupt',reason:'new-owner-input'}});
  assert.deepEqual([interrupted.manifest.state,interrupted.manifest.reason,...interrupted.manifest.bubbles.map(b=>b.state)],['interrupted','new-owner-input','accepted','unsent']);
  assert.equal((await v.manifests.resumeDue({transport:v.transport})).state,'idle','an interrupted group waits for its tail decision');
  await v.manifests.mutate('batch-u',manifest=>{manifest.tail_intent={id:'intent-1',decision:'supersede',state:'recorded'};});
  const retired=await v.manifests.retireRemainder('batch-u',{reason:'owner-stop',superseded_by:'intent-1'});
  assert.deepEqual([retired.manifest.state,retired.manifest.reason,retired.manifest.bubbles[1].superseded_by],['retired','owner-stop','intent-1']);
  assert.deepEqual(v.manifests.live(),['batch-u'],'a group with an unsettled tail intent stays in the live set');
  const w2=world(t);
  w2.manifests.createDraft({entries:group('batch-v',['第一句。','第二句。']),ownerEpoch:'e'});let turn=0;
  await w2.manifests.run('batch-v',{transport:w2.transport,guard:async()=>++turn<3?'send':'interrupt'});
  await w2.manifests.continueGroup('batch-v');
  await w2.manifests.resumeDue({transport:w2.transport});
  assert.equal(onDisk(w2,'batch-v').state,'accepted');
});

test('a review that is not ready holds the group; silence retires it; a frozen group is only validated, never rewritten',async t=>{
  const w=world(t,{contracts:SHORT});const calls=[];let verdict={state:'pending',reason:'prior-delivery-needs-review'};
  const review=async(requests,context)=>{calls.push(context.frozen);return verdict.state==='ready'?{state:'ready',checked:requests.map(r=>({text:r.text.replace('原文','改写')}))}:verdict;};
  w.manifests.createDraft({entries:group('batch-w',['原文第一句。','原文第二句。']),ownerEpoch:'e'});
  const held=(await w.manifests.run('batch-w',{transport:w.transport,review})).manifest;
  assert.deepEqual([held.state,held.reason,held.retryAt],['held','prior-delivery-needs-review',w.state.now+60000]);assert.deepEqual(fragments(held),[]);
  verdict={state:'ready'};w.state.now+=60000;w.transport.fault('before-receipt',{when:d=>d.bubbleId==='batch-w-bubble-1'});
  await w.manifests.resumeDue({transport:w.transport,review});
  const partway=onDisk(w,'batch-w');
  assert.deepEqual(partway.bubbles.map(b=>[b.text,b.state]),[['改写第一句。','accepted'],['改写第二句。','unsent']],'the reviewed text is what was frozen');
  verdict={state:'pending',reason:'frozen-reply-evidence-changed'};w.state.now+=60000;
  await w.manifests.resumeDue({transport:w.transport,review});
  assert.deepEqual([onDisk(w,'batch-w').state,onDisk(w,'batch-w').reason],['held','frozen-reply-evidence-changed']);
  verdict={state:'ready'};w.state.now+=60000;
  await w.manifests.resumeDue({transport:w.transport,review});
  assert.deepEqual(calls,[false,false,true,true]);assert.equal(onDisk(w,'batch-w').state,'accepted');
  assert.deepEqual(w.transport.sends.map(d=>d.text),['改写第一句。','改写第二句。','改写第二句。']);
  const s=world(t);
  s.manifests.createDraft({entries:group('batch-x',['一句。','两句。']),ownerEpoch:'e'});
  const silent=(await s.manifests.run('batch-x',{transport:s.transport,review:async()=>({state:'silent',choice:{action:'silent'}})})).manifest;
  assert.deepEqual([silent.state,silent.reason,s.transport.sends.length],['retired','silent',0]);assert.deepEqual(s.canceled,['batch-x-draft-0','batch-x-draft-1']);
});

test('a retry reads the manifest and never cuts again, even when the limit has changed',async t=>{
  const w=world(t,{contracts:SHORT});let crashes=0;
  const crashing=w.open({hooks:{afterSend:()=>{if(!crashes++)throw Error('Simulated crash');}}});
  crashing.createDraft({entries:group('batch-z',[LONG]),ownerEpoch:'e'});
  await assert.rejects(crashing.run('batch-z',{transport:w.transport}));
  const before=fragments(onDisk(w,'batch-z')).map(f=>[f.transport_id,f.start,f.end]);
  const {manifest}=await w.open({contracts:{feishu:{text:{limit:9,measure:'utf16'}}}}).run('batch-z',{transport:w.transport});
  assert.deepEqual(fragments(manifest).map(f=>[f.transport_id,f.start,f.end]),before);assert.equal(manifest.state,'accepted');
  assert.equal(w.transport.sends.length,3);
});

test('memory side effects are delivered at least once, identically, and a failing emitter never blocks delivery',async t=>{
  const w=world(t);const seen=[];let failures=2;
  const flaky=w.open({emit:async event=>{if(failures-->0)throw Error('Memory host unavailable');seen.push(event);}});
  flaky.createDraft({entries:group('batch-aa',['一句。']),ownerEpoch:'e'});
  const first=(await flaky.run('batch-aa',{transport:w.transport})).manifest;
  assert.equal(first.state,'accepted');assert.deepEqual(flaky.live(),['batch-aa'],'kept live while an event is owed');
  await flaky.resumeDue({transport:w.transport});await flaky.resumeDue({transport:w.transport});
  assert.equal(seen.length,1);assert.deepEqual(flaky.live(),[]);
  const replay=[];
  const again=w.open({emit:async event=>{replay.push(event);}});
  const manifest=onDisk(w,'batch-aa');manifest.bubbles[0].emitted={};
  assert.match(path.relative(w.directory,filed(w,'batch-aa')),/^done[\\/]\d{4}-\d{2}[\\/]batch-aa\.json$/,'settled groups are filed by month');
  fs.renameSync(filed(w,'batch-aa'),path.join(w.directory,'batch-aa.json'));fs.writeFileSync(path.join(w.directory,'batch-aa.json'),JSON.stringify(manifest));
  await again.resumeDue({transport:w.transport});
  assert.deepEqual(replay,seen,'a repeated event is byte-for-byte the first one, so the journal treats it as the same');
  assert.equal(w.transport.sends.length,1);
});

test('legacy .pending.json: unfinished groups are imported once and marked migrated; finished ones are never imported',async t=>{
  const w=world(t),legacy=path.join(w.root,'share-checks');fs.mkdirSync(legacy);
  const entry=(id,text,state,extra={})=>({request:{draft_id:'draft-'+id,reply_id:'input-legacy',text},delivery:{id,text,kind:'reply',draftId:'draft-'+id,memoryBatchId:extra.batch,expectedBubbles:extra.expected},state,...(extra.receipt?{receipt:extra.receipt}:{})});
  const file=(name,value)=>fs.writeFileSync(path.join(legacy,name+'.pending.json'),JSON.stringify(value));
  const whole=[entry('kin-chat-a0','已经送达的一句。','accepted',{batch:'reply-legacy-a',expected:3,receipt:{state:'accepted',messageId:'om_old'}}),
    entry('kin-chat-a1','卡住的一句。','unconfirmed',{batch:'reply-legacy-a',expected:3}),entry('kin-chat-a2','还没发的一句。','unsent',{batch:'reply-legacy-a',expected:3})];
  file('stuck',{state:'unconfirmed',wholeReply:true,review:{state:'ready',review_id:'review-old',checked:whole.map(e=>({text:e.request.text}))},request:whole[0].request,delivery:whole[0].delivery,ownerEpoch:'epoch-old',at:500,retryAt:0,entries:whole});
  file('single',{...entry('kin-chat-b0','单独排队的一句。','unsent',{batch:'reply-legacy-b',expected:2}),state:'pending',ownerEpoch:'epoch-old',at:600,retryAt:0});
  const finished={};
  for(const state of ['accepted','canceled','silent','merged','migrated','duplicate']) {
    const done=[entry('kin-chat-'+state,'历史回复，不得重放。','accepted',{batch:'reply-done-'+state})];
    file('done-'+state,{state,request:done[0].request,delivery:done[0].delivery,ownerEpoch:'epoch-old',at:100,entries:done});
    finished[state]=fs.readFileSync(path.join(legacy,'done-'+state+'.pending.json'),'utf8');
  }
  fs.writeFileSync(path.join(legacy,'torn.pending.json'),'{"state":"pend');
  const first=w.manifests.importLegacy(legacy);
  assert.deepEqual([first.imported.sort(),first.skipped],[['reply-legacy-a','reply-legacy-b'],['torn.pending.json']]);
  for(const [state,body] of Object.entries(finished))assert.equal(fs.readFileSync(path.join(legacy,'done-'+state+'.pending.json'),'utf8'),body,state+' is untouched');
  assert.deepEqual(w.manifests.live().sort(),['reply-legacy-a','reply-legacy-b']);
  assert.deepEqual(['stuck','single'].map(name=>{const v=JSON.parse(fs.readFileSync(path.join(legacy,name+'.pending.json'),'utf8'));return [v.state,v.migratedTo];}),[['migrated','reply-legacy-a'],['migrated','reply-legacy-b']]);
  assert.deepEqual(w.manifests.importLegacy(legacy).imported,[],'the old resume filter and this importer both ignore migrated files');
  const imported=onDisk(w,'reply-legacy-a');
  assert.deepEqual(imported.bubbles.map(b=>[b.fragments[0].transport_id,b.fragments[0].state,b.state]),[['kin-chat-a0','accepted','accepted'],['kin-chat-a1','submitting','sending'],['kin-chat-a2','unsent','unsent']],'imported bubbles keep their bubble ID as transport ID');
  assert.deepEqual([imported.state,imported.review.id,imported.ownerEpoch,imported.origin],['reviewed','review-old','epoch-old','legacy-import']);
  // The group the old guard left blocked forever: no receipt means its send never began, so it goes out now, once, under its old ID.
  await w.manifests.resumeDue({transport:w.transport,limit:5});
  assert.deepEqual(w.transport.sends.map(d=>d.id),['kin-chat-a1','kin-chat-a2','kin-chat-b0']);
  assert.deepEqual([onDisk(w,'reply-legacy-a').state,onDisk(w,'reply-legacy-b').state],['accepted','accepted']);
  assert.deepEqual(w.events.map(e=>e.bubble_id),['kin-chat-a1','kin-chat-a2','kin-chat-b0'],'the bubble the old sender already reported is not reported again');
  assert.ok(!w.transport.sends.some(d=>d.text.includes('历史回复')),'no historical replay');
});

test('a receipt reader that looks in the wrong place never causes a second send or a lasting false "unsent"',async t=>{
  const w=world(t);let crashes=0;const blind={send:delivery=>w.transport.send(delivery),receipt:async()=>null};
  const crashing=w.open({hooks:{afterSend:()=>{if(!crashes++)throw Error('Simulated crash after send');}}});
  crashing.createDraft({entries:group('batch-ab',['第一句。','第二句。']),ownerEpoch:'e'});
  w.platform.script('timeout',{when:()=>w.platform.calls.length===2});
  await assert.rejects(crashing.run('batch-ab',{transport:blind}));
  // The blind reader calls the crashed fragment unsent; the transport answers the retry from its own receipt, without the network.
  const first=(await w.open().run('batch-ab',{transport:blind})).manifest,ids=first.bubbles.map(b=>b.fragments[0].transport_id);
  assert.deepEqual(first.bubbles.map(b=>b.state),['accepted','unsent'],'the timed-out send looks unsent to a reader that sees no receipt');
  // On the retry the transport refuses to resubmit what it cannot resolve, and says so: unknown, never resent, never "undeliverable".
  w.state.now+=60000;
  await w.open().resumeDue({transport:blind});
  assert.deepEqual([onDisk(w,'batch-ab').state,...fragments(onDisk(w,'batch-ab')).map(f=>f.state)],['blocked-unknown','accepted','unknown']);
  assert.deepEqual(ids.map(id=>w.platform.calls.filter(call=>call.id===id).length),[1,1],'each fragment reached the platform exactly once');
});

test('one batch ID never mixes different bubbles, and the guard sees what the old journal showed it',async t=>{
  const w=world(t),legacy=path.join(w.root,'share-checks');fs.mkdirSync(legacy);
  const work=group('batch-ad',['工作回复。'],{taskId:'task-7'});work[0].request.work=true;work[0].delivery.sessionFence={generation:3};
  const first=w.manifests.createDraft({entries:work,ownerEpoch:'epoch-9',hold:{reason:'share-review-pending',retryAt:5}});
  assert.deepEqual([first.group_id,first.state,first.retryAt],['batch-ad','held',5]);
  assert.equal(w.manifests.createDraft({entries:work,ownerEpoch:'epoch-9'}).state,'held','the same entries are the same group');
  const other=group('batch-ad',['同一批次的另一个气泡。']);other[0].delivery.id='batch-ad-bubble-late';
  const second=w.manifests.createDraft({entries:other,ownerEpoch:'epoch-9'});
  assert.match(second.group_id,/^batch-ad-b[0-9a-f]{12}$/);assert.equal(second.delivery_id,'batch-ad','memory still hears the batch ID');
  const views=[];
  await w.manifests.run('batch-ad',{transport:w.transport,guard:async view=>{views.push(view);return 'send';}});
  assert.deepEqual([views[0].ownerEpoch,views[0].request.work,views[0].delivery.taskId,views[0].delivery.id,views[0].state,views[0].groupState],['epoch-9',true,'task-7','batch-ad-bubble-0','pending','held']);
  assert.deepEqual(w.transport.sends[0].sessionFence,{generation:3},'the session fence travels with every fragment for the host to assert');
  assert.equal(w.transport.sends[0].taskId,'task-7');
  // Two old single-bubble files of one batch become two groups; neither hides the other.
  for(const [name,id] of [['one','kin-chat-c0'],['two','kin-chat-c1']])fs.writeFileSync(path.join(legacy,name+'.pending.json'),JSON.stringify({state:'pending',ownerEpoch:'e',at:1,retryAt:0,
    request:{draft_id:'draft-'+id,reply_id:'input-1',text:'排队的一句。'},delivery:{id,text:'排队的一句。',kind:'reply',memoryBatchId:'reply-legacy-c',expectedBubbles:2}}));
  assert.equal(w.manifests.importLegacy(legacy).imported.length,2);
  assert.deepEqual(w.manifests.live().filter(id=>id.startsWith('reply-legacy-c')).map(id=>w.manifests.read(id).bubbles.map(b=>b.bubble_id)).flat().sort(),['kin-chat-c0','kin-chat-c1']);
  assert.deepEqual(w.manifests.findBubble('kin-chat-c1').bubble.draft_id,'draft-kin-chat-c1');
});

test('a held group is not reviewed once a minute for ever: the wait doubles, then the group is parked until there is a new reason',async t=>{
  const w=world(t,{retry:{maxHolds:4}}),asked=[];let verdict={state:'pending',reason:'prior-delivery-needs-review'};
  const review=async()=>{asked.push(w.state.now);return verdict.state==='ready'?{state:'ready',checked:[{},{}]}:verdict;};
  w.manifests.createDraft({entries:group('batch-hold',['第一句。','第二句。']),ownerEpoch:'e'});
  await w.manifests.run('batch-hold',{transport:w.transport,review});
  for(let tick=0;tick<600;tick++){w.state.now+=60000;await w.manifests.resumeDue({transport:w.transport,review});}
  const start=asked[0];
  assert.deepEqual(asked.map(at=>(at-start)/60000),[0,1,3,7],'four reviews in ten hours, each after twice the wait');
  const parked=onDisk(w,'batch-hold');
  assert.deepEqual([parked.state,parked.reason,parked.holds,parked.parked.reason,parked.parked.holds],['held','prior-delivery-needs-review',4,'review-hold-limit',4]);
  assert.deepEqual(w.manifests.status().groups['batch-hold'].parked,parked.parked,'a parked group is visible in status');
  assert.equal(w.transport.sends.length,0);
  // The timer may still stop a parked group (a guard that says so), which costs no review at all.
  const other=world(t,{retry:{maxHolds:1}});let reviews=0;
  other.manifests.createDraft({entries:group('batch-stop',['一句。']),ownerEpoch:'e'});
  await other.manifests.run('batch-stop',{transport:other.transport,review:async()=>{reviews++;return {state:'pending',reason:'held'};}});
  other.state.now+=3600000;
  await other.manifests.resumeDue({transport:other.transport,review:async()=>{reviews++;return {state:'pending'};},guard:async()=>({action:'interrupt',reason:'new-owner-input',inputId:'input-2'})});
  assert.deepEqual([onDisk(other,'batch-stop').state,onDisk(other,'batch-stop').interrupted.by,reviews],['interrupted','input-2',1]);
  // The operator is a new reason: a fresh, bounded set of tries, and this time the review passes.
  const printed=[];
  await operatorMain(['retry','--dir',w.directory,'--group','batch-hold'],{clock:w.clock,print:line=>printed.push(line)});
  assert.deepEqual([JSON.parse(printed[0]).state,JSON.parse(printed[0]).parked,onDisk(w,'batch-hold').holds],['held',undefined,undefined]);
  verdict={state:'ready'};
  await w.manifests.resumeDue({transport:w.transport,review});
  assert.deepEqual([asked.length,onDisk(w,'batch-hold').state],[5,'accepted']);
  // Asking for a parked group by name is a new reason too.
  const named=world(t,{retry:{maxHolds:1}});let ready=false;
  const flip=async requests=>ready?{state:'ready',checked:requests.map(()=>({}))}:{state:'pending',reason:'held'};
  named.manifests.createDraft({entries:group('batch-named',['一句。']),ownerEpoch:'e'});
  assert.ok((await named.manifests.run('batch-named',{transport:named.transport,review:flip})).manifest.parked);
  ready=true;
  assert.equal((await named.manifests.run('batch-named',{transport:named.transport,review:flip})).manifest.state,'accepted');
});

test('review chunks that are already paid for are progress, not a refusal',async t=>{
  const w=world(t,{retry:{maxHolds:2}});let reviewed=0;
  const review=async requests=>++reviewed<5?{state:'pending',reason:'reply-review-chunks-incomplete',chunks:{reviewed,total:5}}:{state:'ready',checked:requests.map(()=>({}))};
  w.manifests.createDraft({entries:group('batch-chunks',['一句。']),ownerEpoch:'e'});
  await w.manifests.run('batch-chunks',{transport:w.transport,review});
  assert.deepEqual([onDisk(w,'batch-chunks').holds,onDisk(w,'batch-chunks').parked],[0,undefined]);
  for(let tick=0;tick<5;tick++){w.state.now+=60000;await w.manifests.resumeDue({transport:w.transport,review});}
  assert.deepEqual([reviewed,onDisk(w,'batch-chunks').state,onDisk(w,'batch-chunks').review_progress],[5,'accepted',undefined]);
});

test('bubbles can be retired by name; a group whose remainder is still undecided stays in the live set',async t=>{
  const w=world(t);let turn=0;
  w.manifests.createDraft({entries:group('batch-part',['第一句。','第二句。','第三句。']),ownerEpoch:'e'});
  await w.manifests.run('batch-part',{transport:w.transport,guard:async()=>++turn<3?'send':{action:'interrupt',reason:'new-owner-input',inputId:'input-2'}});
  const part=await w.manifests.retireRemainder('batch-part',{reason:'tail-rewrite_remainder',superseded_by:'intent-1',only:['batch-part-draft-1']});
  assert.deepEqual(part.manifest.bubbles.map(b=>[b.state,b.superseded_by??null]),[['accepted',null],['canceled','intent-1'],['unsent',null]]);
  assert.deepEqual([part.manifest.state,w.canceled],['interrupted',['batch-part-draft-1']],'what was not named stays exactly as it was');
  const resumed=await w.manifests.continueGroup('batch-part',{ownerEpoch:'e2',by:'input-2'});
  assert.deepEqual([resumed.manifest.state,resumed.manifest.ownerEpoch,resumed.manifest.continued.by],['sending','e2','input-2']);
  await w.manifests.mutate('batch-part',manifest=>{manifest.tail_owed={items:['batch-part-draft-1'],resurfaced:1,since:w.state.now};});
  await w.manifests.resumeDue({transport:w.transport});
  assert.deepEqual([onDisk(w,'batch-part').state,w.manifests.live()],['retired',['batch-part']],'an obligation that came back keeps its group live');
  assert.deepEqual(w.manifests.status().groups['batch-part'].tail_owed,{items:1,resurfaced:1,forced:false});
  await w.manifests.mutate('batch-part',manifest=>{delete manifest.tail_owed;});
  assert.deepEqual(w.manifests.live(),[]);
  // A draft carries, from its very first write, the older groups it answers for.
  const next=w.manifests.createDraft({entries:group('batch-next',['新的回复。']),ownerEpoch:'e2',continues:[{group_id:'batch-part',intent_id:'intent-1'}]});
  assert.deepEqual([next.continues_reply_id,next.continues],['batch-part',[{group_id:'batch-part',intent_id:'intent-1'}]]);
  assert.equal(w.manifests.status().groups['batch-next'].continues_reply_id,'batch-part');
});

test('the operator can end the wait of an interrupted group; what the tool settles is reported and filed by the service, never by the tool',async t=>{
  const w=world(t),printed=[],print=line=>printed.push(JSON.parse(line));
  for(const batch of ['batch-go','batch-drop']) {
    let turn=0;
    w.manifests.createDraft({entries:group(batch,['第一句。','第二句。']),ownerEpoch:'e'});
    await w.manifests.run(batch,{transport:w.transport,guard:async()=>++turn<3?'send':'interrupt'});
  }
  await operatorMain(['continue','--dir',w.directory,'--group','batch-go'],{clock:w.clock,print});
  await operatorMain(['retire','--dir',w.directory,'--group','batch-drop','--reason','operator-retired'],{clock:w.clock,print});
  assert.deepEqual(printed.map(p=>[p.group_id,p.state,p.reason]),[['batch-go','sending',null],['batch-drop','retired','operator-retired']]);
  assert.deepEqual([w.canceled,w.manifests.live().sort()],[[],['batch-drop','batch-go']],'the tool has no memory host: it released nothing and filed nothing');
  await w.manifests.resumeDue({transport:w.transport});
  assert.deepEqual([w.canceled,w.manifests.live(),onDisk(w,'batch-go').state],[['batch-drop-draft-1'],[],'accepted']);
  assert.deepEqual(w.events.filter(e=>e.delivery_id==='batch-drop').map(e=>e.state),['accepted','canceled']);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {parsePrivateReply,publicReplyText} from '../../adapters/private-notes.mjs';
import {parseContactDraft} from '../../adapters/contact-draft.mjs';
import {TransportManifests} from '../../adapters/transport-manifest.mjs';
import {createContactBatch} from '../../adapters/contact-batch.mjs';
import {speechDelivery} from '../../adapters/local-speech.mjs';
import {bubblePieces} from '../../adapters/reply-tail.mjs';
import {replyOutcomeFacts} from '../../adapters/reply-outcomes.mjs';

function world(t,mode,{fail=false,pause=false}={}) {
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-speech-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 const sent=[],receipts=new Map(),synthesized=[];
 const parsed=parsePrivateReply(JSON.stringify({private_note:'这段只留在私密札记。',action:'send',bubbles:['嗯，我在。'],presentation:{mode,style:'温柔自然'}}));
 const m=new TransportManifests({directory,receipt:id=>receipts.get(id),sleep:async()=>{}});
 const draft=m.createDraft({entries:[{request:{draft_id:'draft',reply_id:'input',text:parsed.bubbles[0]},delivery:{id:'bubble',text:parsed.bubbles[0],presentation:parsed.presentation}}],ownerEpoch:1});
 let paused=pause;
 const transport={receipt:id=>receipts.get(id),send:async delivery=>{
   if(delivery.speech) {
     if(paused){paused=false;return {state:'not-submitted',submissionStarted:false,reason:'frozen'};}
     if(fail)return {state:'not-submitted',submissionStarted:false,reason:'tts-failed'};
     delivery=await speechDelivery(delivery,'feishu',async request=>{synthesized.push(request);return {data:Buffer.from('opus'),name:'sample.ogg',durationMs:1000};});
   }
   sent.push(delivery);const receipt={state:'accepted',messageId:'m'+sent.length,acceptedAt:new Date().toISOString()};receipts.set(delivery.id,receipt);return receipt;
 }};
 return {m,draft,sent,synthesized,transport};
}
for(const mode of ['text','voice','both'])test('public '+mode+' uses one logical bubble and only public text reaches speech',async t=>{
 const w=world(t,mode),r=await w.m.run(w.draft.group_id,{transport:w.transport});
 assert.equal(r.manifest.state,'accepted');assert.equal(r.manifest.bubbles.length,1);
 assert.equal(w.sent.length,mode==='both'?2:1);assert.equal(w.synthesized.length,mode==='text'?0:1);
 assert.ok(!JSON.stringify(w.sent).includes('私密札记'));assert.ok(!JSON.stringify(w.synthesized).includes('私密札记'));
 if(mode!=='text')assert.deepEqual(w.synthesized[0].text,'嗯，我在。');
 const before=w.sent.length;await w.m.run(w.draft.group_id,{transport:w.transport});assert.equal(w.sent.length,before);
});
test('both resumes only speech after text was accepted',async t=>{
 const w=world(t,'both',{pause:true});await w.m.run(w.draft.group_id,{transport:w.transport});
 assert.equal(w.sent.length,1);assert.equal(w.sent[0].text,'嗯，我在。');
 const r=await w.m.run(w.draft.group_id,{transport:w.transport});
 assert.equal(r.manifest.state,'accepted');assert.equal(w.sent.length,2);assert.equal(w.sent[1].media.type,'audio');
});
for(const mode of ['voice','both'])test('failed synthesis is a visible '+mode+' failure, never an accepted reply',async t=>{
 const w=world(t,mode,{fail:true});const r=await w.m.run(w.draft.group_id,{transport:w.transport});
 assert.equal(r.manifest.state,mode==='voice'?'undeliverable':'partial');assert.equal(r.manifest.reason,'tts-failed');
 const facts=replyOutcomeFacts({}, {[r.manifest.group_id]:r.manifest});assert.equal(facts[0].reason,'tts-failed');
 assert.equal(facts[0].sentTextFragments,mode==='both'?1:0);
});
test('contact retains selected form after removing its private note',async()=>{
 const d=parseContactDraft(JSON.stringify({private_note:'秘密',action:'send',bubbles:['晚安。'],presentation:{mode:'voice',style:'轻声'}}));
 delete d.private_note;
 const store=new Map(),sent=[];
 const batch=createContactBatch({read:id=>store.get(id),write:(id,v)=>store.set(id,structuredClone(v)),send:async x=>{sent.push(x);return {state:'accepted',messageId:x.id};}});
 const r=await batch({id:'contact',...d,channel:'feishu'});assert.equal(r.state,'accepted');assert.equal(sent.length,1);assert.equal(sent[0].speech.style,'轻声');
 assert.ok(!JSON.stringify(sent).includes('秘密'));
});
test('contact both keeps accepted text when speech fails and exposes the actual failure',async()=>{
 const store=new Map(),sent=[];
 const batch=createContactBatch({read:id=>store.get(id),write:(id,v)=>store.set(id,structuredClone(v)),send:async x=>{sent.push(x);return x.speech?{state:'not-submitted',reason:'tts-failed'}:{state:'accepted',messageId:x.id};}});
 const r=await batch({id:'contact',bubbles:['晚安。'],presentation:{mode:'both'},channel:'feishu'});
 assert.equal(r.state,'accepted');assert.equal(r.acceptedBubbles,1);assert.equal(r.failure.code,'tts-failed');
 assert.equal(r.partial,true);assert.equal(sent.length,2);
});
test('Wechat speech delivery is explicitly a file attachment',async()=>{
 const d=await speechDelivery({id:'a',text:'你好。',speech:{style:''}},'wechat',async()=>({data:Buffer.from('opus'),name:'Kin.ogg'}));
 assert.equal(d.media.type,'file');assert.equal(d.media.transcript,'你好。');assert.equal(d.text,undefined);
 assert.equal(publicReplyText('{"private_note":"秘密","action":"send","bubbles":["公开"],"presentation":{"mode":"voice"}}'),'公开');
});

test('a failed audio copy does not claim the accepted text was unsent',()=>{const p=bubblePieces({text:'你好。',fragments:[{kind:'text',state:'accepted',start:0,end:3},{kind:'speech',state:'failed',start:0,end:3}]});assert.deepEqual(p.sent,['你好。']);assert.match(p.unsent[0],/语音部分/);});

const mixedReply=JSON.stringify({private_note:'私密札记',action:'send',presentation:{mode:'text',style:''},bubbles:[
 {text:'https://example.com/help',references:[],presentation:null},
 {text:'晚安。',references:[],presentation:{mode:'voice',style:''}},
]});
test('different public bubbles retain their own forms without repeating their content',async t=>{
 const p=parsePrivateReply(mixedReply);assert.equal(p.state,'send');
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-mixed-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const m=new TransportManifests({directory:dir,receipt:()=>null,sleep:async()=>{}}),sent=[];
 const entries=p.bubbles.map((text,i)=>({request:{draft_id:'d'+i,reply_id:'input',text},delivery:{id:'b'+i,text,presentation:p.presentations[i]??p.presentation}}));
 const draft=m.createDraft({entries,ownerEpoch:1});
 const result=await m.run(draft.group_id,{transport:{receipt:()=>null,send:async d=>{sent.push(d);return {state:'accepted',messageId:d.id};}}});
 assert.equal(result.manifest.state,'accepted');assert.equal(sent.length,2);
 assert.equal(sent[0].text,'https://example.com/help');assert.equal(sent[0].speech,undefined);
 assert.equal(sent[1].text,'晚安。');assert.ok(sent[1].speech);
 assert.ok(!JSON.stringify(sent).includes('私密札记'));
});
test('proactive mixed forms survive parsing, freezing, and journal resume',async()=>{
 const d=parseContactDraft(mixedReply),store=new Map(),sent=[];
 const batch=createContactBatch({read:id=>store.get(id),write:(id,v)=>store.set(id,structuredClone(v)),send:async x=>{sent.push(x);return {state:'accepted',messageId:x.id};}});
 const r=await batch({id:'mixed-contact',...d,channel:'feishu'});assert.equal(r.state,'accepted');
 assert.equal(sent.length,2);assert.equal(sent[0].speech,undefined);assert.ok(sent[1].speech);
 await batch({id:'mixed-contact'});assert.equal(sent.length,2);
});

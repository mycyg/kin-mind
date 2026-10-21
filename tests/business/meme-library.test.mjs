import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MemeLibrary,memeHash,inspectMemeImage} from '../../adapters/meme-library.mjs';
import {MemeSession,frozenMemeMedia,verifiedMemeVision} from '../../adapters/meme-session.mjs';
import {chatEnvelope} from '../../adapters/chat-bubbles.mjs';
import {parseContactDraft} from '../../adapters/contact-draft.mjs';
import {TransportManifests} from '../../adapters/transport-manifest.mjs';
import {createContactBatch} from '../../adapters/contact-batch.mjs';
// Synthetic pixels only. Not a starter pack or production fixture.
import {png} from './helpers/meme-fixture.mjs';
function fixture(t){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-meme-test-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return {dir,lib:new MemeLibrary(path.join(dir,'library')),data:png()};}
const metadata={description:'猫咪扶额，无奈的反讽，不适合安慰难过的人',ocr:'好极了',tags:['无奈'],contexts:['自嘲'],aliases:['扶额'],emotions:['无奈']};
const collect=(lib,data,extra={})=>lib.collect({data,source:{channel:'feishu',id:'owner-image-1'},metadata,intent:{kind:'explicit'},...extra});
const context={sourceId:'input-1',sessionId:'session-1',model:'test-vision',provider:'test',bundle:'test-bundle',lane:'chat'};
const proof={...context,path:'mcp-image-content/v1',state:'verified',mode:'pixels',nativeTurnId:'synthetic-native-proof',fixtureSha256:'a'.repeat(64)};

test('recognition is not collection: default/negative choice writes no index; chosen and explicit are distinct',t=>{
  const {lib,data}=fixture(t);assert.equal(collect(lib,data,{intent:null}).state,'skipped');assert.equal(fs.existsSync(lib.file),false);
  assert.equal(collect(lib,data,{intent:{kind:'interesting',visual:true,isMeme:true,worthKeeping:false}}).state,'saved');
  const ordinary=collect(lib,png(256,100),{intent:{kind:'explicit'},source:{channel:'feishu',id:'explicit-photo'}});assert.equal(ordinary.state,'saved');
});
test('restart, precise dedup, sources, pack search, edits and retirement are consistent',t=>{
  const {lib,data}=fixture(t);const a=collect(lib,data).item;
  lib.group({id:'cats',name:'猫咪',aliases:['猫猫']});
  const b=collect(lib,data,{source:{channel:'wechat',id:'owner-image-2'},group:'cats'});assert.equal(b.duplicate,true);assert.equal(a.id,b.item.id);assert.equal(b.item.sources.length,2);
  const restarted=new MemeLibrary(lib.directory);assert.equal(restarted.search({query:'扶额',group:'cats'})[0].id,a.id);
  assert.equal(restarted.search({query:'无奈'})[0].id,a.id);assert.deepEqual(restarted.search({query:'不相干xyz'}),[]);
  restarted.edit(a.id,{metadata:{description:'新描述',contexts:['庆祝'],aliases:['太棒了']}});assert.equal(restarted.search({query:'太棒了'}).length,1);
  restarted.edit(a.id,{remove:true});assert.equal(restarted.search({query:'太棒了'}).length,0);assert.throws(()=>restarted.readImage(a.id),/unavailable/);assert.equal(fs.existsSync(path.join(lib.directory,'originals',a.hash+'.png')),false);
  assert.equal(collect(restarted,data).item.id,a.id);assert.equal(restarted.search({query:'无奈'}).length,1);
});
test('missing/corrupt index and modified bytes fail closed',t=>{const {lib,data}=fixture(t);const a=collect(lib,data).item;fs.writeFileSync(path.join(lib.directory,'originals',a.hash+'.png'),png(256,4));assert.throws(()=>lib.readImage(a.id),/changed/);fs.writeFileSync(lib.file,'{');assert.throws(()=>lib.search({query:'猫'}),/unavailable/);});
test('PNG CRC, dimensions, corruption and non-PNG rejected',()=>{assert.equal(inspectMemeImage(png()).width,256);const corrupt=png();corrupt[40]^=1;assert.throws(()=>inspectMemeImage(corrupt),/corrupt/);assert.throws(()=>inspectMemeImage(Buffer.from('GIF89a')),/image-required/);});
test('pack validation is all-or-nothing and no placeholder/missing/escape/hash mismatch imports',t=>{
  const {lib,data,dir}=fixture(t);fs.writeFileSync(path.join(dir,'one.png'),data);
  const pack={schema:'kin-meme-pack/v1',id:'cats',name:'猫咪',original:true,source:'synthetic test, not deployable art',items:[{id:'one',file:'one.png',sha256:memeHash(data),...metadata}]};
  assert.equal(lib.importPack(pack,dir,{dryRun:true}).state,'valid');assert.equal(fs.existsSync(lib.file),false);
  assert.equal(lib.importPack(pack,dir).count,1);assert.equal(lib.importPack(pack,dir).count,1);assert.equal(lib.search({query:'无奈'}).length,1);
  const bad={...pack,id:'bad',items:[...pack.items,{...pack.items[0],id:'two',file:'missing.png'}]};assert.throws(()=>lib.importPack(bad,dir));assert.equal(lib.groups().some(p=>p.id==='bad'),false);
  assert.throws(()=>lib.importPack({...pack,items:[{...pack.items[0],file:'../one.png'}]},dir),/path/);
  assert.throws(()=>lib.importPack({...pack,items:[{...pack.items[0],sha256:'0'.repeat(64)}]},dir),/image/);
});
test('scope, view path/lane/model/source/hash, zero vision fallback and expiry',t=>{
  const {lib,data}=fixture(t),id=collect(lib,data).item.id;let time=1;const session=new MemeSession({library:lib,proofs:()=>[proof,{...proof,path:'owner-image-input/v1'}],clock:()=>time});
  assert.equal(verifiedMemeVision({...context,lane:'proactive'},[proof]),false);
  assert.equal(session.inspect([id],{...context,model:'text-only'}).state,'skipped');
  const view=session.inspect([id],context),ref={type:'image',meme_id:id,inspection_id:view.inspection_id};
  assert.equal(view.state,'pixels');
  assert.ok(session.resolve(ref,{...context,bundle:'unrelated-new-release',personaRevision:'new'}));
  assert.equal(verifiedMemeVision({...context,visionPath:'changed-pixel-pipeline/v2'},[proof]),false);
  assert.equal(verifiedMemeVision({...context,visionPath:'owner-image-input/v1'},[proof]),false);assert.equal(session.resolve(ref,{...context,sourceId:'different'}),null);
  const payload=session.resolve(ref,context);assert.ok(frozenMemeMedia(payload).data.equals(data));
  const forged={...payload,hash:'0'.repeat(64)};assert.throws(()=>frozenMemeMedia(forged),/changed/);
  time=700000;assert.equal(session.resolve(ref,context),null);
});
test('ordinary incoming pixels remain transient until chosen; unknown vision cannot mark interesting',t=>{
  const {lib,data}=fixture(t),session=new MemeSession({library:lib,proofs:()=>[proof,{...proof,path:'owner-image-input/v1'}]});
  const images=session.receive({sourceId:context.sourceId,channel:'feishu',images:[{data:data.toString('base64')}]});assert.equal(fs.existsSync(lib.file),false);
  const args={attachment_id:images[0].attachment_id,choice:'skip',is_meme:true,worth_keeping:true};assert.equal(session.collect(args,context).state,'skipped');
  assert.equal(session.collect({...args,choice:'collect'},context).state,'saved');
});
test('receipt unknown is not use; accepted is idempotent and drives recent repetition penalty',t=>{const {lib,data}=fixture(t),id=collect(lib,data).item.id;
  assert.equal(lib.recordUse(id,{id:'send-1',state:'unconfirmed'}).state,'not-recorded');
  assert.equal(lib.recordUse(id,{id:'send-1',state:'accepted',messageId:'platform-1'}).state,'recorded');
  lib.recordUse(id,{id:'send-1',state:'accepted',messageId:'platform-1'});assert.equal(lib.readImage(id).item.useCount,1);
  assert.deepEqual(lib.search({query:'猫',exclude:[id]}),[]);
});
test('strict optional image envelope, pure image, mixed voice; no arbitrary path or protocol leakage',t=>{
  const ref={type:'image',meme_id:'meme-a',inspection_id:'view-00000000-0000-0000-0000-000000000000'};
  const result=chatEnvelope(JSON.stringify({action:'send',bubbles:[{text:'好呀',references:[],presentation:{mode:'voice'}},ref]}));
  assert.equal(result.state,'send');assert.equal(result.meme_refs[1].meme_id,'meme-a');
  assert.equal(chatEnvelope(JSON.stringify({action:'send',bubbles:[{...ref,path:'/private/file'}]})).state,'invalid');
  assert.equal(parseContactDraft(JSON.stringify({action:'send',bubbles:[ref]})).meme_refs[0].meme_id,'meme-a');
});
test('manifest freezes exact image bytes, never sends marker text or TTS for image',t=>{
  const {lib,data,dir}=fixture(t),id=collect(lib,data).item.id,session=new MemeSession({library:lib,proofs:()=>[proof,{...proof,path:'owner-image-input/v1'}]});const view=session.inspect([id],context),meme=session.resolve({meme_id:id,inspection_id:view.inspection_id},context);
  const store=new TransportManifests({directory:path.join(dir,'manifests')}),manifest=store.createDraft({entries:[{request:{draft_id:'draft',text:'[表情图片]'},delivery:{id:'bubble',text:'[表情图片]',presentation:{mode:'both'},meme}}],ownerEpoch:1});
  store.finalize(manifest,{checked:[{text:'[表情图片]'}]});assert.equal(manifest.bubbles[0].fragments.length,1);
  const d=store.delivery(manifest,manifest.bubbles[0],manifest.bubbles[0].fragments[0]);assert.equal(d.text,undefined);assert.equal(d.speech,undefined);assert.ok(d.media.data.equals(data));
});
test('proactive batch reuses receipt IDs and drops an unavailable optional image without failing text',async()=>{
  const states=new Map(),sent=[];const batch=createContactBatch({read:id=>states.get(id),write:(id,b)=>states.set(id,structuredClone(b)),verifyMeme:()=>false,
    send:async item=>{sent.push(item);return {state:'accepted',messageId:'p-'+item.id};}});
  const result=await batch({id:'batch',bubbles:['正常正文','[表情图片]'],memes:[null,{meme_id:'missing'}],channel:'feishu'});
  assert.equal(sent.length,1);assert.equal(sent[0].text,'正常正文');assert.notEqual(result.state,'unconfirmed');
});

test('proactive image unknown receipt blocks replay; accepted reconciliation counts usage once across restart',async t=>{
  const {lib,data}=fixture(t),image=collect(lib,data).item,states=new Map(),receipts=new Map();let calls=0;
  const options={read:id=>states.get(id),write:(id,b)=>states.set(id,structuredClone(b)),verifyMeme:()=>true,
    receipt:id=>receipts.get(id),onMemeAccepted:(meme,receipt)=>lib.recordUse(meme.meme_id,receipt),
    send:async item=>{calls++;return {state:'unconfirmed'};}};
  const batch=createContactBatch(options);const a=await batch({id:'unknown-image',bubbles:['[表情图片]'],memes:[{meme_id:image.id}],channel:'feishu'});
  assert.equal(a.state,'unconfirmed');assert.equal(calls,1);assert.equal(lib.readImage(image.id).item.useCount,0);
  await createContactBatch(options)({id:'unknown-image'});assert.equal(calls,1);
  const item=states.get('unknown-image').items[0];receipts.set(item.id,{state:'accepted',messageId:'accepted-platform-image'});
  const b=await createContactBatch(options)({id:'unknown-image'});assert.equal(b.state,'accepted');assert.equal(calls,1);assert.equal(lib.readImage(image.id).item.useCount,1);
  await createContactBatch(options)({id:'unknown-image'});assert.equal(lib.readImage(image.id).item.useCount,1);
});
test('image removed immediately before proactive submit does not cancel following text',async()=>{
  const states=new Map(),sent=[];const batch=createContactBatch({read:id=>states.get(id),write:(id,b)=>states.set(id,structuredClone(b)),verifyMeme:()=>true,
    send:async item=>{sent.push(item);return item.meme?{state:'not-submitted',submissionStarted:false,reason:'meme-unavailable'}:{state:'accepted',messageId:'text'};}});
  const result=await batch({id:'race',bubbles:['[表情图片]','仍然说话'],memes:[{meme_id:'deleted'},null],channel:'feishu'});
  assert.equal(result.state,'accepted');assert.equal(sent.length,2);assert.equal(states.get('race').items[0].state,'canceled');
});

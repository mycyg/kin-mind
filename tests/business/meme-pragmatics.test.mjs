import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {MemeLibrary,memeMetadata,memeHash} from '../../adapters/meme-library.mjs';
import {png} from './helpers/meme-fixture.mjs';
function fixture(t){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'meme-pragmatics-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return {dir,lib:new MemeLibrary(path.join(dir,'library'))};}
const save=(lib,metadata,seed=0)=>lib.collect({data:png(256,seed),source:{channel:'feishu',id:'synthetic-'+seed},metadata,intent:{kind:'explicit'}}).item;
const uses=[{when:'对方夸耀又翻车',reading:'反讽：你可真厉害'},{when:'被问是谁干的',reading:'装乖：我什么都不知道'},{when:'大家开始乱接话',reading:'抽象无厘头接梗，不按字面回应'}];
test('same pixels support different contextual readings; smile alone is not sarcasm',t=>{
  const {lib}=fixture(t),item=save(lib,{description:'圆形角色轻微笑容，直视前方',uses});
  const literal=save(lib,{description:'另一角色轻微笑容',contexts:['微笑']},19);
  const before=fs.readFileSync(lib.file);
  const a=lib.search({query:'圆形',context:'对方夸耀又翻车'})[0];
  const b=lib.search({query:'圆形',context:'被问是谁干的'})[0];
  assert.equal(a.id,item.id);assert.equal(b.id,item.id);
  assert.equal(a.matched_uses[0].reading,uses[0].reading);assert.equal(b.matched_uses[0].reading,uses[1].reading);
  assert.equal(lib.search({query:'反讽'}).some(x=>x.id===literal.id),false);
  assert.equal(lib.search({context:'抽象无厘头'})[0].id,item.id);
  assert.equal(lib.readImage(item.id).item.description,item.description);
  assert.ok(fs.readFileSync(lib.file).equals(before),'Search context must not persist');
});
test('unknown meaning stays unknown, interesting alone admits only chosen MEMEs',t=>{
  const {lib}=fixture(t),data=png();
  const args={data,source:{channel:'feishu',id:'unknown'},metadata:{description:'不规则彩色团块',ambiguity:'意义不明，不确定如何使用'}};
  assert.equal(lib.collect(args).state,'skipped');assert.equal(fs.existsSync(lib.file),false);
  assert.equal(lib.collect({...args,intent:{kind:'interesting',visual:true,isMeme:false}}).state,'skipped');
  assert.equal(lib.collect({...args,intent:{kind:'interesting',visual:false,isMeme:true}}).state,'skipped');
  const saved=lib.collect({...args,intent:{kind:'interesting',visual:true,isMeme:true,worthKeeping:false}});
  assert.equal(saved.state,'saved');assert.deepEqual(saved.item.uses,[]);assert.deepEqual(saved.item.emotions,[]);
  assert.equal(lib.search({query:'意义不明'})[0].id,saved.item.id);assert.deepEqual(lib.search({query:'反讽'}),[]);
  assert.equal(lib.collect({...args,intent:{kind:'explicit'}}).duplicate,true);
});
test('single-image correction patches metadata, removes stale readings and survives restart/deletion',t=>{
  const {lib}=fixture(t),a=save(lib,{description:'可见笑容',uses,ocr:'文字',aliases:['圆团']});
  const untouched=save(lib,{description:'别的图片',uses},33);
  lib.edit(a.id,{metadata:{uses:[{when:'用户说明这是冷场时',reading:'尴尬凝视'}],ambiguity:'也可能只是随手发图'}});
  const reboot=new MemeLibrary(lib.directory),updated=reboot.readImage(a.id).item;
  assert.equal(updated.hash,a.hash);assert.equal(updated.description,a.description);assert.equal(updated.ocr,'文字');assert.deepEqual(updated.aliases,['圆团']);
  assert.equal(reboot.search({query:'反讽'}).some(i=>i.id===a.id),false);
  assert.equal(reboot.search({query:'尴尬凝视'})[0].id,a.id);assert.deepEqual(reboot.readImage(untouched.id).item,untouched);
  reboot.edit(a.id,{metadata:{uses:[],ambiguity:''}});assert.equal(reboot.search({query:'尴尬凝视'}).length,0);
  reboot.edit(a.id,{remove:true});assert.deepEqual(reboot.read().items[a.id].uses,[]);assert.equal(reboot.read().items[a.id].ambiguity,'');assert.equal(reboot.read().items[a.id].state,'trashed');
});
test('legacy manifests and unknown-use manifests share v1; malformed uses fail before indexing',t=>{
  const {dir,lib}=fixture(t),data=png();fs.writeFileSync(path.join(dir,'test.png'),data);
  const pack={schema:'kin-meme-pack/v1',id:'abstract',name:'抽象',original:true,source:'synthetic test',items:[{id:'one',file:'test.png',sha256:memeHash(data),description:'随机色块',ambiguity:'未知'}]};
  assert.equal(lib.importPack(pack,dir).count,1);assert.equal(lib.importPack(pack,dir).count,1);
  assert.equal(lib.search({query:'未知',group:'abstract'}).length,1);
  assert.throws(()=>memeMetadata({uses:[{when:'',reading:'凭空解释'}]}),/invalid-use/);
  assert.throws(()=>memeMetadata({uses:Array(9).fill(uses[0])}),/invalid-uses/);
  assert.throws(()=>lib.search({context:'x'.repeat(1201)}),/invalid-text/);
});

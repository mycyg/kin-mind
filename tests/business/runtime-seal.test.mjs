import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {activateMobileRuntimeBundle,caretRangeSatisfied,compareReleases,planMobileRuntimeRollback,prepareMobileRuntimeBundle,resolveActiveMobileRuntime,rollbackMobileRuntimeBundle,REQUIRED_OWNED_ACP_MARKERS} from '../../adapters/mobile-runtime-bundle.mjs';
import {withPidLock} from '../../adapters/atomic-json.mjs';
const hash=value=>createHash('sha256').update(value).digest('hex');

// A bundle whose Codex binary is large enough to be sealed, activated with a
// verified receipt, as the host activates one.
async function activeBundle(t,{root=null,bundleId='seal-fixture',version='0.156.1',activate=true}={}){
  if(!root){root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'kin-seal-')));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));}
  const bin=path.join(root,'vendor',bundleId,'bin'),modules=path.join(root,'node_modules'),acp=path.join(modules,'@agentclientprotocol/codex-acp');
  fs.mkdirSync(bin,{recursive:true});fs.mkdirSync(acp,{recursive:true});
  const binary=`#!/bin/sh\necho "codex-cli ${version}"\nexit 0\n#${'x'.repeat(1536*1024)}\n`;
  fs.writeFileSync(path.join(bin,'codex'),binary,{mode:0o700});fs.writeFileSync(path.join(bin,'codex-code-mode-host'),'#!/bin/sh\n',{mode:0o700});
  fs.writeFileSync(path.join(acp,'package.json'),JSON.stringify({name:'@agentclientprotocol/codex-acp',version:'1.11.0',main:'index.js'}));
  const body=REQUIRED_OWNED_ACP_MARKERS.join('\n')+'\n';fs.writeFileSync(path.join(acp,'index.js'),body);
  const owned=path.join(root,'owned.mjs');fs.writeFileSync(owned,body);
  const host=path.join(root,'host');
  const bundle=prepareMobileRuntimeBundle({rootDir:host,bundleId,codexBinary:path.join(bin,'codex'),acpPackageRoot:acp,nodeModulesRoot:modules,
    allowedPackages:['@agentclientprotocol/codex-acp'],ownedAcpEntry:{path:owned,realpath:owned,allowed_root:root,sha256:hash(body),source_vendor_sha256:hash(body),bytes:Buffer.byteLength(body),required_markers:REQUIRED_OWNED_ACP_MARKERS}});
  const runtime=path.join(host,'state/mobile-runtime'),privateRoot=path.join(runtime,'candidates',bundleId+'-candidate');fs.mkdirSync(privateRoot,{recursive:true});
  const artifacts={};
  for(const name of ['base','developer','launcher','config','schema','catalog']){
    const file=path.join(privateRoot,name+'.txt'),text=name+'\n';fs.writeFileSync(file,text);
    artifacts[name]={path:file,relative_path:name+'.txt',sha256:hash(text),bytes:Buffer.byteLength(text)};
  }
  const receipt=JSON.stringify({state:'verified',compatible:true,stages:{declared:true,prepared:true,native_loaded:true,request_verified:true},
    candidate_kind:'mobile-main-maintenance',bundle_id:bundleId,bundle:{manifest_sha256:bundle.manifestSha256},private_root:privateRoot,artifacts});
  const receiptPath=path.join(runtime,'receipts',bundleId,hash(receipt)+'.json');fs.mkdirSync(path.dirname(receiptPath),{recursive:true});fs.writeFileSync(receiptPath,receipt);
  if(activate)assert.equal((await activateMobileRuntimeBundle({rootDir:host,bundleId,receiptPath})).state,'activated');
  return {root,host,runtime,bundle,codex:path.join(bundle.bundleDir,'bin/codex'),binary,receiptPath};
}

function readsOf(file,work){
  const original=fs.readFileSync;let reads=0;
  fs.readFileSync=function(target,...rest){if(String(target)===file)reads++;return original.call(this,target,...rest);};
  try{work();}finally{fs.readFileSync=original;}
  return reads;
}

test('activation seals the large files; resolving the active runtime then reads none of them',async t=>{
  const f=await activeBundle(t);
  const seal=JSON.parse(fs.readFileSync(path.join(f.runtime,'seals/seal-fixture.json'),'utf8'));
  assert.equal(seal.manifest_sha256,f.bundle.manifestSha256);
  assert.deepEqual(Object.keys(seal.files),['bin/codex'],'only files of 1 MB or more are sealed');
  let active;
  assert.equal(readsOf(f.codex,()=>{active=resolveActiveMobileRuntime({rootDir:f.host});}),0);
  assert.equal(active.codexPath,f.codex);
});

test('a sealed file written in place is hashed again and refused; a seal is never written by a resolve',async t=>{
  const f=await activeBundle(t);
  const sealPath=path.join(f.runtime,'seals/seal-fixture.json'),sealed=fs.readFileSync(sealPath,'utf8');
  const stat=fs.statSync(f.codex);
  fs.writeFileSync(f.codex,f.binary.replace('0.156.1','0.999.9'));
  fs.utimesSync(f.codex,stat.atime,stat.mtime); // same size and mtime: only the change time moved
  assert.throws(()=>resolveActiveMobileRuntime({rootDir:f.host}),/bytes differ from manifest/);
  fs.writeFileSync(f.codex,f.binary);fs.utimesSync(f.codex,stat.atime,stat.mtime);
  assert.ok(readsOf(f.codex,()=>resolveActiveMobileRuntime({rootDir:f.host}))>0,'no longer matching its seal, the file is hashed');
  assert.equal(fs.readFileSync(sealPath,'utf8'),sealed);
  fs.rmSync(sealPath);
  assert.ok(readsOf(f.codex,()=>resolveActiveMobileRuntime({rootDir:f.host}))>0,'without a seal every file is hashed');
});

test('a rollback to an older Codex is refused unless allowed; the same release rolls back',async t=>{
  const first=await activeBundle(t,{bundleId:'codex-old',version:'0.156.1'});
  await activeBundle(t,{root:first.root,bundleId:'codex-new',version:'0.157.0'});
  await assert.rejects(rollbackMobileRuntimeBundle({rootDir:first.host}),/Rollback would run codex-cli 0\.156\.1 on a thread codex-cli 0\.157\.0 has written/);
  assert.equal(resolveActiveMobileRuntime({rootDir:first.host}).index.current.bundle_id,'codex-new');
  assert.equal((await rollbackMobileRuntimeBundle({rootDir:first.host,allowDowngrade:true})).state,'activated');
  assert.equal(resolveActiveMobileRuntime({rootDir:first.host}).index.current.bundle_id,'codex-old');
  await activeBundle(t,{root:first.root,bundleId:'codex-same',version:'0.156.1'});
  assert.equal((await rollbackMobileRuntimeBundle({rootDir:first.host})).state,'activated','no downgrade, no flag needed');
});

test('release comparison and the ACP package\'s declared Codex range',()=>{
  assert.equal(compareReleases('codex-cli 0.156.1','0.153.4'),1);assert.equal(compareReleases('0.155.0','codex-cli 0.155.0'),0);
  assert.equal(compareReleases('0.155.0-alpha.16','0.156.1'),-1);assert.equal(compareReleases('dev','0.1.0'),null);
  assert.equal(caretRangeSatisfied('^0.153.4','codex-cli 0.153.9'),true);
  assert.equal(caretRangeSatisfied('^0.153.4','codex-cli 0.156.1'),false,'0.x caret ranges stop at the next minor');
  assert.equal(caretRangeSatisfied('^1.2.0','1.9.0'),true);assert.equal(caretRangeSatisfied('^1.2.0','2.0.0'),false);
  assert.equal(caretRangeSatisfied('>=0.150.0','0.156.1'),null);assert.equal(caretRangeSatisfied(null,'0.156.1'),null);
});

test('a seal that cannot be written leaves the switch in place and says so (CR-OPS-10)',async t=>{
  const first=await activeBundle(t,{bundleId:'codex-a'});
  const second=await activeBundle(t,{root:first.root,bundleId:'codex-b',activate:false});
  const result=await activateMobileRuntimeBundle({rootDir:first.host,bundleId:'codex-b',receiptPath:second.receiptPath,writeSeal:()=>{throw Error('no space left');}});
  assert.deepEqual([result.state,result.seal,result.sealError,result.index.current.bundle_id],['activated','failed','no space left','codex-b']);
  assert.equal(resolveActiveMobileRuntime({rootDir:first.host}).index.current.bundle_id,'codex-b','resolving without a seal hashes every file');
  assert.equal(fs.existsSync(path.join(first.runtime,'seals/codex-b.json')),false);
  assert.equal(result.rollback.state,'rollback','the answer carries the way back');
});

test('nothing is activated while another holder has the activation lock (CR-RT-04)',async t=>{
  const first=await activeBundle(t,{bundleId:'codex-a'});
  const second=await activeBundle(t,{root:first.root,bundleId:'codex-b',activate:false});
  let release;const held=new Promise(resolve=>{release=resolve;});
  const holder=withPidLock(path.join(first.runtime,'activation.lock'),{work:()=>held});
  await new Promise(resolve=>setTimeout(resolve,50));
  assert.equal((await activateMobileRuntimeBundle({rootDir:first.host,bundleId:'codex-b',receiptPath:second.receiptPath})).state,'busy');
  release();await holder;
  assert.equal(resolveActiveMobileRuntime({rootDir:first.host}).index.current.bundle_id,'codex-a');
  fs.rmSync(path.join(first.runtime,'versions/codex-b'),{recursive:true});
  await assert.rejects(activateMobileRuntimeBundle({rootDir:first.host,bundleId:'codex-b',receiptPath:second.receiptPath}),/ENOENT|no such file/,
    'a bundle removed before the lock is found missing under it');
  assert.equal(resolveActiveMobileRuntime({rootDir:first.host}).index.current.bundle_id,'codex-a');
});

test('the way back is decided from the index before anything moves (CR-RT-03)',async t=>{
  const first=await activeBundle(t,{bundleId:'codex-old',version:'0.156.1'});
  assert.deepEqual(planMobileRuntimeRollback({rootDir:first.host}),{current:'codex-old',revision:1,previous:null,state:'forward-fix',reason:'no-previous-runtime'});
  await activeBundle(t,{root:first.root,bundleId:'codex-new',version:'0.157.0'});
  const plan=planMobileRuntimeRollback({rootDir:first.host});
  assert.deepEqual(plan,{current:'codex-new',revision:2,previous:'codex-old',from:'codex-cli 0.157.0',to:'codex-cli 0.156.1',state:'forward-fix',reason:'downgrade-unproven'});
  assert.deepEqual(planMobileRuntimeRollback({rootDir:first.host,allowDowngrade:true}).state,'rollback');
  assert.equal(planMobileRuntimeRollback({rootDir:first.host,allowDowngrade:true}).allowDowngrade,true);
  assert.equal(planMobileRuntimeRollback({rootDir:first.host,expectedPrevious:'codex-other'}).reason,'previous-runtime-is-not-the-saved-one');
  await activeBundle(t,{root:first.root,bundleId:'codex-same',version:'0.157.0'});
  assert.deepEqual(planMobileRuntimeRollback({rootDir:first.host,expectedPrevious:'codex-new'}),
    {current:'codex-same',revision:3,previous:'codex-new',from:'codex-cli 0.157.0',to:'codex-cli 0.157.0',state:'rollback',allowDowngrade:false});
  fs.rmSync(path.join(first.runtime,'versions/codex-new/manifest.json'));
  assert.equal(planMobileRuntimeRollback({rootDir:first.host}).reason,'previous-runtime-unreadable');
});

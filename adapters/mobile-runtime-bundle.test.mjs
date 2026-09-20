import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {
  MOBILE_RUNTIME_DESCRIPTOR_SCHEMA,
  MOBILE_RUNTIME_PROOF_SCHEMA,
  activateMobileRuntimeBundle,
  prepareMobileRuntimeBundle,
  readMobileRuntimeBundleState,
  resolveActiveMobileRuntime,
  validateMobileRuntimeBundle,
  verifyMobileRuntimeBundle,
} from './mobile-runtime-bundle.mjs';

const sha256=value=>createHash('sha256').update(value).digest('hex');
const temporary=()=>fs.mkdtempSync(path.join(os.tmpdir(),'kin-mobile-runtime-test-'));
const json=(file,value)=>{fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,JSON.stringify(value,null,2));return file;};
const write=(file,value,mode=0o600)=>{fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,value,{mode});return file;};
const record=file=>{const realpath=fs.realpathSync(file),bytes=fs.readFileSync(realpath);return {path:file,realpath,sha256:sha256(bytes),bytes:bytes.length};};

function makeSources(root,{escape=false}={}){
  const nodeModules=path.join(root,'source','node_modules'),acp=path.join(nodeModules,'@agentclientprotocol','codex-acp'),codexPackage=path.join(nodeModules,'@openai','codex'),dependency=path.join(nodeModules,'synthetic-dep');
  const codex=write(path.join(root,'source','codex'),'#!/usr/bin/env node\nif(process.argv.includes("--version")) console.log("codex-cli 0.155.0");\n',0o755);
  json(path.join(acp,'package.json'),{name:'@agentclientprotocol/codex-acp',version:'1.11.0',type:'module',main:'dist/index.js',bin:{'codex-acp':'dist/index.js'},dependencies:{'@openai/codex':'^0.153.4','synthetic-dep':'1.0.0'}});
  json(path.join(codexPackage,'package.json'),{name:'@openai/codex',version:'0.153.4'});write(path.join(codexPackage,'index.js'),'export {};\n');
  json(path.join(dependency,'package.json'),{name:'synthetic-dep',version:'1.0.0'});write(path.join(dependency,'index.js'),'export const dependency=true;\n');
  json(path.join(nodeModules,'private-sibling','package.json'),{name:'private-sibling',version:'1.0.0'});write(path.join(nodeModules,'private-sibling','credential.txt'),'must never be copied');
  const vendorText='#!/usr/bin/env node\n// KIN_SESSION_CONTINUITY_V1\n// KIN_MEMORY_COMPACTION_V1\n// KIN_MODEL_ROUTING_V2\n// fastMode: state.fastModeEnabled === true ? "on" : state.fastModeEnabled === false ? "off" : undefined\nif(process.argv.includes("--version")) console.log("@agentclientprotocol/codex-acp 1.11.0");\n';
  const vendor=write(path.join(acp,'dist','index.js'),vendorText,0o755);write(path.join(acp,'dist','index.js.kin-routing-backup-deadbeef'),'old bytes');
  const ownedRoot=path.join(root,'owned'),owned=write(path.join(ownedRoot,'codex-acp.mjs'),vendorText+'// KIN_PRIVATE_SYNTHETIC_V1\n',0o755);
  if(escape){const outside=write(path.join(root,'outside-secret'),'outside');fs.symlinkSync(outside,path.join(dependency,'escape'));}
  return {nodeModules,acp,codex,vendor,owned,ownedRoot,allowedPackages:['@agentclientprotocol/codex-acp','@openai/codex','synthetic-dep'],ownedAcpEntry:{...record(owned),allowed_root:ownedRoot,source_vendor_sha256:sha256(fs.readFileSync(vendor)),required_markers:['// KIN_PRIVATE_SYNTHETIC_V1']}};
}

function prepare(root,bundleId='runtime-a',options={}){
  const sources=options.sources??makeSources(root,options);const host=path.join(root,'host');fs.mkdirSync(host,{recursive:true});
  const bundle=prepareMobileRuntimeBundle({rootDir:host,bundleId,candidateKind:'mobile-main-maintenance',codexBinary:sources.codex,acpPackageRoot:sources.acp,nodeModulesRoot:sources.nodeModules,
    allowedPackages:sources.allowedPackages,ownedAcpEntry:sources.ownedAcpEntry,createdAt:'2026-09-20T00:00:00.000Z'});
  return {root,host,sources,bundle};
}

const requestScenarios=['new_session','old_base_resume','maintenance_promotion_resume','post_promotion_request','isolated_compaction_resume'];
function expectedProfile(scenario,index){const fast=index%2===1;return {scenario_id:scenario,checkpoint:'final',model:fast?'gpt-5.6-sol':'deepseek-flash',provider:fast?'openai':'custom-gateway',reasoning_effort:fast?'max':'high',fast_mode:fast?'on':'off',canonical_id:fast?'gpt-5.6-sol':'deepseek-flash',canonical_match:true,
  service_tier_expectation:{preference:fast?'fast':'default',actual:fast?'priority':'default'}};}

function candidateFiles(prepared,candidateId='candidate-a'){
  const runtimeRoot=path.join(prepared.host,'state','mobile-runtime'),privateRoot=path.join(runtimeRoot,'candidates',candidateId);fs.mkdirSync(privateRoot,{recursive:true});
  const base=write(path.join(privateRoot,'base.md'),'Synthetic companion base.\n'),developer=write(path.join(privateRoot,'developer.md'),'Synthetic developer layer.\n'),launcher=write(path.join(privateRoot,'launcher.sh'),'#!/bin/sh\nexit 0\n',0o700);
  const catalog=json(path.join(privateRoot,'catalog.json'),{models:[{id:'deepseek-flash'},{id:'gpt-5.6-sol'}]}),schema=json(path.join(privateRoot,'schema.json'),{thread_start:true,thread_resume:true});
  const config=json(path.join(privateRoot,'config.json'),{model_instructions_file:fs.realpathSync(base),developer_instructions:fs.readFileSync(developer,'utf8'),model_catalog_json:fs.realpathSync(catalog),'features.fast_mode':true});
  const file=relative=>prepared.bundle.manifest.files.find(item=>item.path===relative);
  const descriptor={schema_version:MOBILE_RUNTIME_DESCRIPTOR_SCHEMA,candidate_id:candidateId,candidate_kind:'mobile-main-maintenance',created_at:'2026-09-20T00:01:00.000Z',private_root:fs.realpathSync(runtimeRoot),
    bundle:{bundle_id:prepared.bundle.manifest.bundle_id,manifest_sha256:prepared.bundle.manifestSha256},base:{...record(base),wire_transformation:'native-trim-v1',wire_sha256:sha256(fs.readFileSync(base,'utf8').trim()),wire_bytes:Buffer.byteLength(fs.readFileSync(base,'utf8').trim()),contract_version:'synthetic-v1',persona_version:'synthetic-v1',generator_version:'synthetic-v1',regular_file:true,symlink:false},developer:record(developer),
    runtime:{codex_bin:{relative_path:prepared.bundle.manifest.runtime.codex.path,version:prepared.bundle.manifest.runtime.codex.version,sha256:file(prepared.bundle.manifest.runtime.codex.path).sha256},
      acp:{version:prepared.bundle.manifest.runtime.acp.version,entry_sha256:prepared.bundle.manifest.runtime.acp.entry_sha256,package_json_sha256:prepared.bundle.manifest.runtime.acp.package_json_sha256},launcher:record(launcher),config:record(config),schema:record(schema),catalog:record(catalog)},
    expected_profiles:requestScenarios.map(expectedProfile),unknown_future_field:{ignored:true}};
  const descriptorPath=json(path.join(privateRoot,'descriptor.json'),descriptor);return {privateRoot,descriptor,descriptorPath,base,developer,launcher,catalog,schema,config};
}

// These fixtures test only the trusted local store/pointer mechanics. They do
// not represent a native compatibility result. Real native execution is tested
// separately; caller-authored observations are always rejected by the verifier.
function activationFixture(prepared,files){
  const artifacts=Object.fromEntries(['base','developer','launcher','catalog','schema','config'].map(name=>{const file=files[name];return [name,{...record(file),path:fs.realpathSync(file),relative_path:path.relative(fs.realpathSync(files.privateRoot),fs.realpathSync(file))}];}));
  const receipt={schema_version:'kin.mobile-runtime.compatibility/v1',bundle_id:prepared.bundle.manifest.bundle_id,candidate_id:files.descriptor.candidate_id,
    candidate_kind:'mobile-main-maintenance',state:'verified',compatible:true,stages:{declared:true,prepared:true,native_loaded:true,request_verified:true},
    bundle:{manifest_sha256:prepared.bundle.manifestSha256},private_root:fs.realpathSync(files.privateRoot),artifacts};
  const text=JSON.stringify(receipt,null,2),receiptPath=path.join(prepared.host,'state/mobile-runtime/receipts',receipt.bundle_id,sha256(text)+'.json');
  write(receiptPath,text);return {...receipt,receiptPath};
}
function runnerInput(prepared,files,{mutate}={}){
  const value={schema_version:'kin.mobile-runtime.runner-input/v2',candidate_id:files.descriptor.candidate_id,bundle_manifest_sha256:prepared.bundle.manifestSha256,
    descriptor_sha256:sha256(fs.readFileSync(files.descriptorPath))};mutate?.(value);
  return json(path.join(files.privateRoot,'runner-input-'+Math.random().toString(16).slice(2)+'.json'),value);
}

test('prepare snapshots only the allow-listed closure and keeps vendor and owned ACP bytes separate',()=>{
  const root=temporary();try{const prepared=prepare(root),{bundle, sources}=prepared;
    assert.equal(bundle.state,'prepared');assert.equal(bundle.manifest.runtime.acp.vendor_entry_sha256,sha256(fs.readFileSync(sources.vendor)));
    assert.equal(bundle.manifest.runtime.acp.entry_sha256,sha256(fs.readFileSync(sources.owned)));assert.notEqual(bundle.manifest.runtime.acp.entry_path,bundle.manifest.runtime.acp.vendor_entry_path);
    assert.equal(fs.readFileSync(path.join(bundle.bundleDir,bundle.manifest.runtime.acp.vendor_entry_path),'utf8'),fs.readFileSync(sources.vendor,'utf8'));
    assert.equal(fs.readFileSync(path.join(bundle.bundleDir,bundle.manifest.runtime.acp.entry_path),'utf8'),fs.readFileSync(sources.owned,'utf8'));
    assert.equal(bundle.manifest.files.some(item=>item.path.includes('private-sibling')||item.path.includes('.kin-routing-backup-')),false);
    assert.deepEqual(bundle.manifest.packages.map(item=>item.install_name).sort(),['@agentclientprotocol/codex-acp','@openai/codex','synthetic-dep']);
    assert.equal(validateMobileRuntimeBundle(bundle.bundleDir,{executeBinary:true}).manifestSha256,bundle.manifestSha256);
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('trusted-store activation is atomic and repeated activation is idempotent',async()=>{
  const root=temporary();try{
    const first=prepare(root,'runtime-a'),firstFiles=candidateFiles(first,'candidate-a'),firstInput=runnerInput(first,firstFiles),firstReceipt=activationFixture(first,firstFiles);
    assert.equal(firstReceipt.state,'verified');assert.equal(firstReceipt.compatible,true);assert.deepEqual(firstReceipt.stages,{declared:true,prepared:true,native_loaded:true,request_verified:true});
    const activated=await activateMobileRuntimeBundle({rootDir:first.host,bundleId:'runtime-a',receiptPath:firstReceipt.receiptPath,activatedAt:'2026-09-20T00:03:00.000Z'});assert.equal(activated.state,'activated');assert.equal(activated.index.previous,null);
    const repeated=await activateMobileRuntimeBundle({rootDir:first.host,bundleId:'runtime-a',receiptPath:firstReceipt.receiptPath,activatedAt:'2026-09-20T00:04:00.000Z'});assert.equal(repeated.state,'already-active');assert.equal(repeated.index.revision,activated.index.revision);assert.equal(repeated.index.previous,null);
    const second=prepare(root,'runtime-b',{sources:first.sources}),secondFiles=candidateFiles(second,'candidate-b'),secondInput=runnerInput(second,secondFiles),secondReceipt=activationFixture(second,secondFiles);
    await activateMobileRuntimeBundle({rootDir:second.host,bundleId:'runtime-b',receiptPath:secondReceipt.receiptPath,expectedRevision:1,activatedAt:'2026-09-20T00:06:00.000Z'});
    const resolved=resolveActiveMobileRuntime({rootDir:first.host});assert.equal(resolved.index.current.bundle_id,'runtime-b');assert.equal(resolved.index.previous.bundle_id,'runtime-a');assert.equal(resolved.indexPath,path.join(fs.realpathSync(first.host),'state','mobile-runtime','activation.json'));
    assert.equal(resolved.codexRelativePath,'bin/codex');assert.equal(resolved.acpEntryRelativePath,'lib/owned/codex-acp.mjs');assert.equal(resolved.catalogPath,fs.realpathSync(secondFiles.catalog));assert.equal(readMobileRuntimeBundleState({rootDir:first.host}).state,'verified');
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('prepared or caller-authored proof cannot promote and a failed candidate keeps the verified previous pointer',async()=>{
  const root=temporary();try{
    const prepared=prepare(root),files=candidateFiles(prepared),valid=runnerInput(prepared,files),receipt=activationFixture(prepared,files);
    await activateMobileRuntimeBundle({rootDir:prepared.host,bundleId:'runtime-a',receiptPath:receipt.receiptPath});const before=fs.readFileSync(path.join(prepared.host,'state','mobile-runtime','activation.json'));
    const invalid=runnerInput(prepared,files,{mutate:value=>{value.schema_version=MOBILE_RUNTIME_PROOF_SCHEMA;value.observations=[{scenario_id:'native_config_load',outcome:'accepted',native:{exit_code:0,acp_initialized:true}}];}}),rejected=verifyMobileRuntimeBundle({rootDir:prepared.host,bundleId:'runtime-a',descriptorPath:files.descriptorPath,runnerInputPath:invalid});
    assert.equal(rejected.state,'rejected');assert.equal(rejected.compatible,false);assert.equal(rejected.stages.request_verified,false);await assert.rejects(activateMobileRuntimeBundle({rootDir:prepared.host,bundleId:'runtime-a',receiptPath:rejected.receiptPath}));
    assert.deepEqual(fs.readFileSync(path.join(prepared.host,'state','mobile-runtime','activation.json')),before);
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('corrupt activation state is never overwritten as a fresh install and status does not create missing roots',async()=>{
  const root=temporary();try{
    const missing=path.join(root,'missing-owner');assert.deepEqual(readMobileRuntimeBundleState({rootDir:missing}),{state:'inactive'});assert.equal(fs.existsSync(missing),false);
    const prepared=prepare(root),files=candidateFiles(prepared),input=runnerInput(prepared,files),receipt=activationFixture(prepared,files);
    const index=path.join(prepared.host,'state','mobile-runtime','activation.json');fs.writeFileSync(index,'{broken');const before=fs.readFileSync(index);await assert.rejects(activateMobileRuntimeBundle({rootDir:prepared.host,bundleId:'runtime-a',receiptPath:receipt.receiptPath}),/activation index is invalid/);assert.deepEqual(fs.readFileSync(index),before);
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('symlink escapes, missing package allow-list entries and owned-byte mismatches fail closed',()=>{
  const root=temporary();try{
    const escaped=makeSources(root,{escape:true});assert.throws(()=>prepare(root,'escape',{sources:escaped}),/symlink|escaped/);assert.equal(fs.existsSync(path.join(root,'host','state','mobile-runtime','versions','escape')),false);
    const clean=makeSources(path.join(root,'clean'));assert.throws(()=>prepare(path.join(root,'clean'),'allowlist',{sources:{...clean,allowedPackages:['@agentclientprotocol/codex-acp','@openai/codex']}}),/allow-listed/);
    const bad={...clean.ownedAcpEntry,sha256:'0'.repeat(64)};assert.throws(()=>prepare(path.join(root,'clean'),'owned-mismatch',{sources:{...clean,ownedAcpEntry:bad}}),/does not match/);
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('a version-only candidate cannot manufacture native compatibility from valid input or supplied evidence',()=>{
  const root=temporary();try{
    const prepared=prepare(root),files=candidateFiles(prepared);
    for(const mutate of [null,value=>{value.observations=[{native:{acp_initialized:true},passed:true}];}]){
      const input=runnerInput(prepared,files,{mutate});
      const result=verifyMobileRuntimeBundle({rootDir:prepared.host,bundleId:'runtime-a',descriptorPath:files.descriptorPath,runnerInputPath:input});
      assert.equal(result.state,'rejected');assert.equal(result.stages.request_verified,false);
      assert.equal(readMobileRuntimeBundleState({rootDir:prepared.host}).state,'inactive');
    }
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

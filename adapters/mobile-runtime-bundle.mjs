import fs from 'node:fs';
import path from 'node:path';
import {createHash,randomBytes} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {TextDecoder} from 'node:util';
import {loadJson,readJsonFile,withPidLock} from './atomic-json.mjs';

export const MOBILE_RUNTIME_BUNDLE_SCHEMA='kin.mobile-runtime.bundle/v1';
export const MOBILE_RUNTIME_DESCRIPTOR_SCHEMA='kin.mobile-runtime.candidate/v1';
export const MOBILE_RUNTIME_PROOF_SCHEMA='kin.mobile-runtime.proof/v1';
export const MOBILE_RUNTIME_RECEIPT_SCHEMA='kin.mobile-runtime.compatibility/v1';
export const MOBILE_RUNTIME_ACTIVATION_SCHEMA='kin.mobile-runtime.activation/v1';
export const MOBILE_RUNTIME_CANDIDATE_KINDS=Object.freeze(['mobile-main-maintenance','isolated-exploration','isolated-creation']);
export const REQUIRED_OWNED_ACP_MARKERS=Object.freeze(['// KIN_MODEL_ROUTING_V3','// KIN_MEMORY_COMPACTION_V1','// KIN_SESSION_CONTINUITY_V1']);
const TRUSTED_PROOF_RUNNER=fileURLToPath(new URL('./mobile-runtime-proof-runner.mjs',import.meta.url));

const sha256=value=>createHash('sha256').update(value).digest('hex');
const hex64=value=>typeof value==='string'&&/^[0-9a-f]{64}$/.test(value);
const candidateId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value);
const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
export const canonicalJson=value=>JSON.stringify(canonical(value));
export const mobileRuntimeDigest=value=>sha256(Buffer.isBuffer(value)||typeof value==='string'?value:canonicalJson(value));

function syncDirectory(directory){let fd;try{fd=fs.openSync(directory,'r');fs.fsyncSync(fd);}catch(error){if(!['EINVAL','ENOTSUP','EISDIR','EBADF'].includes(error.code))throw error;}finally{if(fd!==undefined)fs.closeSync(fd);}}
function syncTree(directory){
  for(const name of fs.readdirSync(directory)){
    const file=path.join(directory,name),stat=fs.lstatSync(file);
    if(stat.isDirectory())syncTree(file);
    else{const fd=fs.openSync(file,'r');try{fs.fsyncSync(fd);}finally{fs.closeSync(fd);}}
  }
  syncDirectory(directory);
}
function inside(root,target){const relative=path.relative(root,target);return relative===''||(!relative.startsWith('..'+path.sep)&&relative!=='..'&&!path.isAbsolute(relative));}
function ensureInside(root,target,label='Path'){if(!inside(root,target))throw Error(label+' escaped its allowed root');return target;}
function readJsonStrict(file,label='JSON'){let value;try{value=JSON.parse(fs.readFileSync(file,'utf8'));}catch{throw Error(label+' is not valid JSON');}return value;}
function safeRelative(value,label='Path'){if(typeof value!=='string'||!value||path.isAbsolute(value)||value.split(/[\\/]/).includes('..'))throw Error(label+' must be relative');return value.split(path.sep).join('/');}
function safeRoot(directory){fs.mkdirSync(directory,{recursive:true,mode:0o700});return fs.realpathSync(directory);}
function mobileRuntimeRoot(rootDir,{create=false}={}){
  const owner=create?safeRoot(rootDir):fs.realpathSync(rootDir),runtime=path.join(owner,'state','mobile-runtime');
  return create?safeRoot(runtime):fs.realpathSync(runtime);
}
function activationFile(root){return path.join(root,'activation.json');}
function count(source,needle){return source.split(needle).length-1;}
function modeOf(stat){return stat.mode&0o777;}
function unique(){return process.pid+'.'+randomBytes(8).toString('hex');}
function writeFileStrict(file,bytes,{mode=0o600,exclusive=false}={}){
  const directory=path.dirname(file);fs.mkdirSync(directory,{recursive:true,mode:0o700});
  if(exclusive&&fs.existsSync(file)){if(fs.readFileSync(file).equals(Buffer.from(bytes)))return false;throw Error('Content-addressed file exists with different bytes');}
  const temporary=file+'.'+unique()+'.tmp',fd=fs.openSync(temporary,'wx',mode);
  try{fs.writeFileSync(fd,bytes);fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
  try{
    if(exclusive){try{fs.linkSync(temporary,file);}catch(error){if(error.code==='EEXIST'&&fs.readFileSync(file).equals(Buffer.from(bytes)))return false;throw error;}}
    else fs.renameSync(temporary,file);
    syncDirectory(directory);return true;
  }finally{fs.rmSync(temporary,{force:true});}
}
function writeJsonStrict(file,value,{pretty=false,mode=0o600}={}){const body=JSON.stringify(value,null,pretty?2:undefined);writeFileStrict(file,body,{mode});return body;}
function writeActivationIndexStrict(file,value,{pretty=true,mode=0o600}={}){
  const current=readJsonFile(file);if(current.state!=='missing'&&current.state!=='ok')throw Error('Existing activation index is unreadable');
  if(current.state==='ok')writeFileStrict(file+'.prev',current.body,{mode});
  return writeJsonStrict(file,value,{pretty,mode});
}

function packageRootFromEntry(entry,nodeModulesRoot){
  let current=fs.realpathSync(path.dirname(entry));
  while(inside(nodeModulesRoot,current)){
    if(fs.existsSync(path.join(current,'package.json')))return current;
    const parent=path.dirname(current);if(parent===current)break;current=parent;
  }
  throw Error('Resolved dependency has no package root');
}

function resolvePackageRoot(requireFrom,name,nodeModulesRoot,optional){
  try{return fs.realpathSync(path.dirname(requireFrom.resolve(name+'/package.json')));}
  catch{
    try{return packageRootFromEntry(requireFrom.resolve(name),nodeModulesRoot);}
    catch(error){if(optional)return null;throw Error('Required ACP dependency is missing: '+name,{cause:error});}
  }
}

/** Resolve only the installed dependency graph rooted at the ACP package. The
 * allow-list names packages that may enter a phone bundle; siblings in the
 * private runtime tree are never traversed or copied. */
export function resolveAcpDependencyClosure({acpPackageRoot,nodeModulesRoot,allowedPackages}){
  if(!Array.isArray(allowedPackages)||!allowedPackages.length||allowedPackages.some(name=>typeof name!=='string'||!name))throw Error('An explicit ACP package allow-list is required');
  const allowed=new Set(allowedPackages),modules=fs.realpathSync(nodeModulesRoot),root=fs.realpathSync(acpPackageRoot);
  ensureInside(modules,root,'ACP package');
  const queue=[root],seen=new Set(),packages=[];
  while(queue.length){
    const packageRoot=queue.shift();if(seen.has(packageRoot))continue;seen.add(packageRoot);
    const installPath=safeRelative(path.relative(modules,packageRoot),'ACP install path');
    const parts=installPath.split('/'),nodeModulesAt=parts.lastIndexOf('node_modules');
    const tail=parts.slice(nodeModulesAt+1),installName=tail[0]?.startsWith('@')?tail.slice(0,2).join('/'):tail[0];
    if(!allowed.has(installName))throw Error('ACP dependency is not allow-listed: '+installName);
    const packageFile=path.join(packageRoot,'package.json'),packageJson=readJsonStrict(packageFile,'ACP package metadata');
    const required=Object.keys(packageJson.dependencies??{}).sort(),optional=Object.keys(packageJson.optionalDependencies??{}).sort();
    const requireFrom=createRequire(packageFile),resolved=[];
    for(const name of [...required,...optional.filter(name=>!required.includes(name))]){
      const child=resolvePackageRoot(requireFrom,name,modules,optional.includes(name));
      if(!child)continue;ensureInside(modules,child,'ACP dependency');queue.push(child);
      resolved.push({name,install_path:safeRelative(path.relative(modules,child),'ACP dependency install path'),optional:optional.includes(name)});
    }
    packages.push({install_name:installName,declared_name:packageJson.name,version:packageJson.version,install_path:installPath,
      package_json_sha256:sha256(fs.readFileSync(packageFile)),dependencies:resolved.sort((a,b)=>a.name.localeCompare(b.name))});
  }
  return packages.sort((a,b)=>a.install_path.localeCompare(b.install_path));
}

function copyPackageTree(sourceRoot,targetRoot){
  const source=fs.realpathSync(sourceRoot);
  const walk=(actual,relative,ancestors=new Set())=>{
    const real=fs.realpathSync(actual);ensureInside(source,real,'Package symlink');
    const stat=fs.statSync(real);
    if(stat.isDirectory()){
      if(relative&&path.basename(relative)==='node_modules')return;
      if(ancestors.has(real))throw Error('Package symlink cycle');const next=new Set(ancestors);next.add(real);
      const destination=path.join(targetRoot,relative);fs.mkdirSync(destination,{recursive:true,mode:0o755});
      for(const name of fs.readdirSync(real).sort()){
        if(name.includes('.kin-'))continue;
        walk(path.join(real,name),path.join(relative,name),next);
      }
      return;
    }
    if(!stat.isFile())throw Error('Package contains a non-file runtime entry');
    const destination=path.join(targetRoot,relative);fs.mkdirSync(path.dirname(destination),{recursive:true,mode:0o755});
    fs.copyFileSync(real,destination);fs.chmodSync(destination,modeOf(stat));
  };
  for(const name of fs.readdirSync(source).sort())walk(path.join(source,name),name);
}

function copyRegularFile(source,target,{executable=false}={}){
  const real=fs.realpathSync(source),stat=fs.statSync(real);if(!stat.isFile())throw Error('Runtime source is not a regular file');
  fs.mkdirSync(path.dirname(target),{recursive:true,mode:0o755});fs.copyFileSync(real,target);fs.chmodSync(target,executable?(modeOf(stat)|0o500):modeOf(stat));return real;
}

function copyOwnedAcpEntry(descriptor,vendorEntry,ownedEntry){
  if(!descriptor||typeof descriptor.path!=='string'||typeof descriptor.realpath!=='string'||typeof descriptor.allowed_root!=='string'||!hex64(descriptor.sha256)||!hex64(descriptor.source_vendor_sha256)||!Number.isSafeInteger(descriptor.bytes)||!Array.isArray(descriptor.required_markers))throw Error('Owned ACP entry descriptor is incomplete');
  const allowed=fs.realpathSync(descriptor.allowed_root),lstat=fs.lstatSync(descriptor.path),real=fs.realpathSync(descriptor.path);if(lstat.isSymbolicLink()||!lstat.isFile()||!inside(allowed,real)||real!==descriptor.realpath)throw Error('Owned ACP entry escaped its allowed root');
  const vendor=fs.readFileSync(vendorEntry),owned=fs.readFileSync(real);if(descriptor.source_vendor_sha256!==sha256(vendor)||descriptor.sha256!==sha256(owned)||descriptor.bytes!==owned.length)throw Error('Owned ACP entry descriptor does not match its bytes');
  const markers=[...new Set([...REQUIRED_OWNED_ACP_MARKERS,...descriptor.required_markers])].sort(),source=owned.toString('utf8');for(const marker of markers)if(typeof marker!=='string'||!marker||count(source,marker)!==1)throw Error('Owned ACP patch marker is missing or duplicated');
  fs.mkdirSync(path.dirname(ownedEntry),{recursive:true,mode:0o755});fs.copyFileSync(real,ownedEntry);fs.chmodSync(ownedEntry,modeOf(lstat));
  return {source_vendor_sha256:sha256(vendor),generated_sha256:sha256(owned),descriptor_sha256:sha256(Buffer.from(canonicalJson({sha256:descriptor.sha256,bytes:descriptor.bytes,source_vendor_sha256:descriptor.source_vendor_sha256,required_markers:descriptor.required_markers}))),markers};
}

function listRegularFiles(root,{exclude=[]}={}){
  const excluded=new Set(exclude),files=[];
  const walk=relative=>{
    const absolute=path.join(root,relative),stat=fs.lstatSync(absolute);
    if(stat.isSymbolicLink())throw Error('Runtime bundle contains a symlink');
    if(stat.isDirectory()){for(const name of fs.readdirSync(absolute).sort())walk(path.join(relative,name));return;}
    if(!stat.isFile())throw Error('Runtime bundle contains a special file');
    const portable=relative.split(path.sep).join('/');if(excluded.has(portable))return;
    const bytes=fs.readFileSync(absolute);files.push({path:portable,bytes:bytes.length,mode:modeOf(stat),sha256:sha256(bytes)});
  };
  for(const name of fs.readdirSync(root).sort())walk(name);
  return files.sort((a,b)=>a.path.localeCompare(b.path));
}

function exactFiles(left,right){return canonicalJson(left)===canonicalJson(right);}
function codexVersion(binary){return execFileSync(binary,['--version'],{encoding:'utf8',timeout:15000,stdio:['ignore','pipe','ignore']}).trim();}

/** Create an immutable phone-only runtime bundle. No absolute source path is
 * written to its public manifest. */
export function prepareMobileRuntimeBundle({rootDir,bundleId,candidateKind='mobile-main-maintenance',codexBinary,acpPackageRoot,nodeModulesRoot,allowedPackages,ownedAcpEntry,createdAt=new Date().toISOString()}){
  if(!candidateId(bundleId))throw Error('Invalid mobile runtime bundle id');
  if(!MOBILE_RUNTIME_CANDIDATE_KINDS.includes(candidateKind))throw Error('Unknown mobile runtime candidate kind');
  const root=mobileRuntimeRoot(rootDir,{create:true}),versions=path.join(root,'versions');fs.mkdirSync(versions,{recursive:true,mode:0o700});
  const destination=path.join(versions,bundleId);if(fs.existsSync(destination))throw Error('Mobile runtime bundle already exists');
  const staging=path.join(versions,'.'+bundleId+'.'+unique()+'.staging');fs.mkdirSync(staging,{mode:0o700});
  try{
    const closure=resolveAcpDependencyClosure({acpPackageRoot,nodeModulesRoot,allowedPackages});
    const modules=fs.realpathSync(nodeModulesRoot);
    const binary=path.join(staging,'bin','codex');copyRegularFile(codexBinary,binary,{executable:true});
    for(const packageInfo of closure)copyPackageTree(path.join(modules,packageInfo.install_path),path.join(staging,'lib','node_modules',packageInfo.install_path));
    const acp=closure.find(item=>item.install_name==='@agentclientprotocol/codex-acp');if(!acp)throw Error('ACP root is absent from dependency closure');
    const acpRoot=path.join(staging,'lib','node_modules',acp.install_path),acpJson=readJsonStrict(path.join(acpRoot,'package.json'),'Bundled ACP package metadata');
    const vendorEntryRelative=safeRelative(path.join('lib','node_modules',acp.install_path,acpJson.bin?.['codex-acp']??acpJson.main),'ACP vendor entry');
    const vendorEntry=path.join(staging,vendorEntryRelative),entryRelative='lib/owned/codex-acp.mjs',entry=path.join(staging,entryRelative),ownedEntry=copyOwnedAcpEntry(ownedAcpEntry,vendorEntry,entry);
    const manifest={schema_version:MOBILE_RUNTIME_BUNDLE_SCHEMA,bundle_id:bundleId,candidate_kind:candidateKind,created_at:createdAt,
      runtime:{codex:{path:'bin/codex',version:codexVersion(binary)},acp:{package_name:acpJson.name,version:acpJson.version,
        declared_codex_range:acpJson.dependencies?.['@openai/codex']??null,package_path:'lib/node_modules/'+acp.install_path,
        vendor_entry_path:vendorEntryRelative,vendor_entry_sha256:sha256(fs.readFileSync(vendorEntry)),entry_path:entryRelative,
        package_json_sha256:sha256(fs.readFileSync(path.join(acpRoot,'package.json'))),entry_sha256:sha256(fs.readFileSync(entry)),owned_entry:ownedEntry}},
      packages:closure,files:listRegularFiles(staging)};
    const manifestFile=path.join(staging,'manifest.json');writeJsonStrict(manifestFile,manifest,{pretty:true,mode:0o600});
    syncTree(staging);fs.renameSync(staging,destination);syncDirectory(versions);
    return validateMobileRuntimeBundle(destination,{executeBinary:true});
  }catch(error){fs.rmSync(staging,{recursive:true,force:true});throw error;}
}

function validateManifestShape(manifest){
  if(manifest?.schema_version!==MOBILE_RUNTIME_BUNDLE_SCHEMA||!candidateId(manifest.bundle_id)||!MOBILE_RUNTIME_CANDIDATE_KINDS.includes(manifest.candidate_kind))throw Error('Unsupported mobile runtime manifest');
  if(!Array.isArray(manifest.files)||!Array.isArray(manifest.packages)||!manifest.runtime?.codex?.path||!manifest.runtime?.acp?.entry_path)throw Error('Incomplete mobile runtime manifest');
  for(const file of manifest.files){safeRelative(file.path,'Manifest file');if(!hex64(file.sha256)||!Number.isSafeInteger(file.bytes)||file.bytes<0||!Number.isInteger(file.mode))throw Error('Invalid manifest file record');}
  if(new Set(manifest.files.map(file=>file.path)).size!==manifest.files.length)throw Error('Duplicate manifest file path');
  return true;
}

function validateBundledClosure(bundle,manifest){
  const modules=path.join(bundle,'lib','node_modules'),allowed=[...new Set(manifest.packages.map(item=>item.install_name))];
  const acpPackage=manifest.packages.find(item=>item.install_name==='@agentclientprotocol/codex-acp');if(!acpPackage)throw Error('Manifest has no ACP package');
  const observed=resolveAcpDependencyClosure({acpPackageRoot:path.join(modules,acpPackage.install_path),nodeModulesRoot:modules,allowedPackages:allowed});
  const compact=list=>list.map(item=>({install_name:item.install_name,declared_name:item.declared_name,version:item.version,install_path:item.install_path,package_json_sha256:item.package_json_sha256,dependencies:item.dependencies}));
  if(canonicalJson(compact(observed))!==canonicalJson(compact(manifest.packages)))throw Error('Bundled ACP dependency closure changed');
}

export function validateMobileRuntimeBundle(bundleDir,{executeBinary=false}={}){
  const lstat=fs.lstatSync(bundleDir);if(lstat.isSymbolicLink()||!lstat.isDirectory())throw Error('Runtime bundle must be a real directory');
  const bundle=fs.realpathSync(bundleDir),manifestFile=path.join(bundle,'manifest.json'),manifestBytes=fs.readFileSync(manifestFile),manifest=JSON.parse(manifestBytes);
  validateManifestShape(manifest);if(path.basename(bundle)!==manifest.bundle_id)throw Error('Runtime bundle directory does not match its manifest');
  const observed=listRegularFiles(bundle,{exclude:['manifest.json']});if(!exactFiles(observed,manifest.files))throw Error('Runtime bundle bytes differ from manifest');
  const entry=path.join(bundle,safeRelative(manifest.runtime.acp.entry_path,'ACP entry'));ensureInside(bundle,entry,'ACP entry');
  const vendorEntry=path.join(bundle,safeRelative(manifest.runtime.acp.vendor_entry_path,'ACP vendor entry'));ensureInside(bundle,vendorEntry,'ACP vendor entry');
  const source=fs.readFileSync(entry,'utf8');for(const marker of REQUIRED_OWNED_ACP_MARKERS)if(count(source,marker)!==1)throw Error('Bundled ACP owned patch is missing or duplicated');
  if(sha256(fs.readFileSync(entry))!==manifest.runtime.acp.entry_sha256)throw Error('Bundled ACP entry digest changed');
  if(sha256(fs.readFileSync(vendorEntry))!==manifest.runtime.acp.vendor_entry_sha256||manifest.runtime.acp.owned_entry?.source_vendor_sha256!==manifest.runtime.acp.vendor_entry_sha256||manifest.runtime.acp.owned_entry?.generated_sha256!==manifest.runtime.acp.entry_sha256)throw Error('Vendor and owned ACP bytes are not independently bound');
  validateBundledClosure(bundle,manifest);
  const binary=path.join(bundle,safeRelative(manifest.runtime.codex.path,'Codex binary'));ensureInside(bundle,binary,'Codex binary');
  if(executeBinary&&codexVersion(binary)!==manifest.runtime.codex.version)throw Error('Bundled Codex version changed');
  return {bundleDir:bundle,manifestPath:manifestFile,manifest,manifestSha256:sha256(manifestBytes),state:'prepared'};
}

function validateUtf8(bytes,label,{maxBytes=1024*1024,lf=false}={}){
  if(!bytes.length||bytes.length>maxBytes||bytes.includes(0))throw Error(label+' bytes are invalid');
  let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch{throw Error(label+' is not UTF-8');}
  if(lf&&text.includes('\r'))throw Error(label+' is not LF-normalized');return text;
}

function validateLocalArtifact(record,privateRoot,label,{json=false,text=false,maxBytes=4*1024*1024}={}){
  if(!record||typeof record.path!=='string'||typeof record.realpath!=='string'||!hex64(record.sha256)||!Number.isSafeInteger(record.bytes))throw Error(label+' descriptor is incomplete');
  const lstat=fs.lstatSync(record.path);if(lstat.isSymbolicLink()||!lstat.isFile())throw Error(label+' must be a non-symlink regular file');
  const real=fs.realpathSync(record.path);ensureInside(privateRoot,real,label);if(record.realpath&&record.realpath!==real)throw Error(label+' realpath changed');
  const bytes=fs.readFileSync(real);if(bytes.length!==record.bytes||sha256(bytes)!==record.sha256)throw Error(label+' digest changed');
  let value=null;if(text||json){const decoded=validateUtf8(bytes,label,{maxBytes,lf:text});if(json)try{value=JSON.parse(decoded);}catch{throw Error(label+' is not JSON');}}
  return {real,bytes,value};
}

function requiredScenarioRequirements(descriptor){
  const baseline=[
    {scenario_id:'native_config_load',phase:'native_loaded',outcome:'accepted'},
    {scenario_id:'new_session',phase:'request_verified',outcome:'accepted',rpc_methods:['thread/start']},
    {scenario_id:'old_base_resume',phase:'request_verified',outcome:'accepted',rpc_methods:['thread/resume']},
    {scenario_id:'maintenance_promotion_resume',phase:'request_verified',outcome:'accepted',rpc_methods:['thread/resume']},
    {scenario_id:'post_promotion_request',phase:'request_verified',outcome:'accepted',rpc_methods:['turn/start']},
    {scenario_id:'isolated_compaction_resume',phase:'request_verified',outcome:'accepted',rpc_methods:['thread/resume','turn/start']},
  ];
  const supplied=Array.isArray(descriptor.scenario_requirements)?descriptor.scenario_requirements:[];
  const merged=new Map(baseline.map(item=>[item.scenario_id,item]));for(const item of supplied)if(item?.scenario_id)merged.set(item.scenario_id,item);
  for(const item of baseline){const value=merged.get(item.scenario_id);if(value?.phase!==item.phase||value?.outcome!=='accepted'||(item.rpc_methods&&!item.rpc_methods.every(method=>value.rpc_methods?.includes(method))))throw Error('A required mobile runtime scenario was weakened');}
  return [...merged.values()];
}

/** Validate the private candidate descriptor without copying its paths or text
 * into a public manifest or compatibility receipt. Unknown optional fields are
 * deliberately ignored; every prompt/runtime-critical field below is read back. */
export function validateMobileRuntimeCandidateDescriptor(descriptor,{bundle,manifestSha256}){
  if(descriptor?.schema_version!==MOBILE_RUNTIME_DESCRIPTOR_SCHEMA||!candidateId(descriptor.candidate_id)||!MOBILE_RUNTIME_CANDIDATE_KINDS.includes(descriptor.candidate_kind))throw Error('Unsupported mobile runtime candidate descriptor');
  if(descriptor.bundle?.bundle_id!==bundle.manifest.bundle_id||descriptor.bundle?.manifest_sha256!==manifestSha256||descriptor.candidate_kind!==bundle.manifest.candidate_kind)throw Error('Candidate descriptor is bound to different runtime bytes');
  const privateRoot=fs.realpathSync(descriptor.private_root),base=validateLocalArtifact(descriptor.base,privateRoot,'Base instructions',{text:true,maxBytes:64*1024});
  const nativeBase=fs.readFileSync(base.real,'utf8').trim();
  if(descriptor.base.wire_transformation!=='native-trim-v1'||descriptor.base.wire_sha256!==sha256(nativeBase)||descriptor.base.wire_bytes!==Buffer.byteLength(nativeBase))throw Error('Base wire representation is not bound to file bytes');
  const developer=validateLocalArtifact(descriptor.developer,privateRoot,'Developer instructions',{text:true,maxBytes:1024*1024});
  const launcher=validateLocalArtifact(descriptor.runtime?.launcher,privateRoot,'Launcher',{text:true,maxBytes:1024*1024});
  const config=validateLocalArtifact(descriptor.runtime?.config,privateRoot,'Runtime config',{json:true,maxBytes:1024*1024});
  const schema=validateLocalArtifact(descriptor.runtime?.schema,privateRoot,'Native schema',{json:true,maxBytes:16*1024*1024});
  const catalog=validateLocalArtifact(descriptor.runtime?.catalog,privateRoot,'Model catalog',{json:true,maxBytes:16*1024*1024});
  if(descriptor.runtime?.codex_bin?.sha256!==bundle.manifest.files.find(file=>file.path===bundle.manifest.runtime.codex.path)?.sha256||descriptor.runtime?.codex_bin?.version!==bundle.manifest.runtime.codex.version)throw Error('Candidate Codex identity changed');
  if(descriptor.runtime?.acp?.entry_sha256!==bundle.manifest.runtime.acp.entry_sha256||descriptor.runtime?.acp?.version!==bundle.manifest.runtime.acp.version||descriptor.runtime?.acp?.package_json_sha256!==bundle.manifest.runtime.acp.package_json_sha256)throw Error('Candidate ACP identity changed');
  if(descriptor.candidate_kind==='mobile-main-maintenance'){
    if(config.value.model_instructions_file!==base.real)throw Error('Runtime config did not retain model_instructions_file');
    if(typeof config.value.developer_instructions!=='string'||sha256(Buffer.from(config.value.developer_instructions))!==descriptor.developer.sha256)throw Error('Runtime config did not retain developer instructions');
    if(config.value.model_catalog_json!==catalog.real)throw Error('Runtime config did not retain model catalog');
  }
  const requirements=requiredScenarioRequirements(descriptor),profiles=Array.isArray(descriptor.expected_profiles)?descriptor.expected_profiles:[];
  for(const requirement of requirements){if(!candidateId(requirement.scenario_id)||!['native_loaded','request_verified'].includes(requirement.phase)||!['accepted','rejected'].includes(requirement.outcome))throw Error('Invalid runtime scenario requirement');}
  for(const profile of profiles){if(!candidateId(profile.scenario_id)||typeof profile.checkpoint!=='string'||!profile.checkpoint||typeof profile.model!=='string'||typeof profile.provider!=='string'||typeof profile.reasoning_effort!=='string'||!['on','off','unknown'].includes(profile.fast_mode)||typeof profile.canonical_id!=='string'||typeof profile.canonical_match!=='boolean'||!profile.service_tier_expectation||!Object.hasOwn(profile.service_tier_expectation,'preference')||!Object.hasOwn(profile.service_tier_expectation,'actual'))throw Error('Invalid expected runtime profile');}
  if(descriptor.candidate_kind==='mobile-main-maintenance')for(const requirement of requirements.filter(item=>item.phase==='request_verified'&&item.outcome==='accepted'))if(!profiles.some(profile=>profile.scenario_id===requirement.scenario_id))throw Error('A main-session request scenario lacks an expected runtime profile');
  return {privateRoot,base,developer,launcher,config,schema,catalog,requirements,profiles};
}

function observedRuntimeDigests(observation,descriptor,bundle){
  const native=observation?.native;
  if(!native||native.exit_code!==0||native.acp_initialized!==true||!hex64(native.receipt_sha256))throw Error('Native runtime load was not observed');
  const expected={codex_sha256:descriptor.runtime.codex_bin.sha256,acp_entry_sha256:descriptor.runtime.acp.entry_sha256,
    acp_package_json_sha256:descriptor.runtime.acp.package_json_sha256,base_sha256:descriptor.base.sha256,developer_sha256:descriptor.developer.sha256,
    config_sha256:descriptor.runtime.config.sha256,schema_sha256:descriptor.runtime.schema.sha256,catalog_sha256:descriptor.runtime.catalog.sha256,
    bundle_manifest_sha256:bundle.manifestSha256};
  for(const [key,value] of Object.entries(expected))if(native[key]!==value)throw Error('Native runtime loaded different '+key);
}

function validateProfile(expected,observed){
  if(!observed||observed.checkpoint!==expected.checkpoint)throw Error('Runtime profile checkpoint is missing');
  for(const [expectedKey,observedKey] of [['model','model'],['provider','provider'],['reasoning_effort','reasoning_effort'],['fast_mode','fast_mode'],['canonical_id','canonical_id']])if(expected[expectedKey]!==undefined&&observed[observedKey]!==expected[expectedKey])throw Error('Runtime profile '+expectedKey+' changed');
  const tier=expected.service_tier_expectation;
  if(tier&&observed.service_tier_preference!==tier.preference)throw Error('Configured service-tier preference changed');
  if(tier&&observed.actual_service_tier!==tier.actual)throw Error('Actual service tier changed');
  if(expected.canonical_match!==undefined&&observed.canonical_match!==expected.canonical_match)throw Error('Canonical runtime identity changed');
}

function validateObservedRequest(request,requirement,descriptor){
  if(!Array.isArray(requirement.rpc_methods)||!requirement.rpc_methods.includes(request?.rpc_method)||request.turn_status!=='completed'||!hex64(request.capture?.raw_request_sha256)||!hex64(request.capture?.extraction_receipt_sha256)||!hex64(request.turn_sha256)||!hex64(request.rpc_receipt_sha256))throw Error('Native request proof is incomplete');
  const base=request.capture?.base,developer=request.capture?.developer;
  if(base?.sha256!==descriptor.base.wire_sha256||base?.bytes!==descriptor.base.wire_bytes||!['request.instructions','request.input.developer-base'].includes(base?.basis))throw Error('Native request used different base instruction bytes');
  if(developer?.sha256!==descriptor.developer.sha256||developer?.bytes!==descriptor.developer.bytes||!['request.instructions-segment','request.input.developer','thread-config.developerInstructions','thread-config.developer_instructions'].includes(developer?.basis))throw Error('Native request used different developer instruction bytes');
  if(request.config_sha256!==descriptor.runtime.config.sha256||request.catalog_sha256!==descriptor.runtime.catalog.sha256)throw Error('Native request used different config bytes');
  if(typeof request.thread_id!=='string'||!request.thread_id||typeof request.session_id!=='string'||!request.session_id)throw Error('Native request lacks thread identity');
  if(['thread/start','thread/resume','thread/fork'].includes(request.rpc_method)&&(!Array.isArray(request.instruction_sources)||request.instruction_sources_provenance!=='app-server-native'))throw Error('Native instructionSources observation is missing');
  if(!request.tools||!Number.isSafeInteger(request.tools.definitions)||request.tools.definitions<0||request.tools.calls!==0||request.tools.executions!==0)throw Error('Native proof observed an unexpected tool execution');
}

function scenarioReceipt(observation,requirement,descriptor,validated,bundle){
  if(observation?.scenario_id!==requirement.scenario_id||observation.outcome!==requirement.outcome)throw Error('Required scenario outcome is missing');
  if(requirement.outcome==='rejected'){
    if(typeof observation.error_code!=='string'||!observation.error_code||!hex64(observation.activation_index_before_sha256)||observation.activation_index_before_sha256!==observation.activation_index_after_sha256)throw Error('Rejected scenario did not preserve activation state');
    return {scenario_id:requirement.scenario_id,state:'verified-rejection',error_code:observation.error_code};
  }
  observedRuntimeDigests(observation,descriptor,bundle);
  const requests=Array.isArray(observation.requests)?observation.requests:[];
  if(requirement.phase==='request_verified'){
    if(!requests.length||!requirement.rpc_methods.every(method=>requests.some(request=>request.rpc_method===method)))throw Error('Required request proof is missing');for(const request of requests)validateObservedRequest(request,requirement,descriptor);
  }
  for(const expected of validated.profiles.filter(item=>item.scenario_id===requirement.scenario_id))validateProfile(expected,(observation.profiles??[]).find(item=>item.checkpoint===expected.checkpoint));
  return {scenario_id:requirement.scenario_id,state:requirement.phase==='request_verified'?'request-verified':'native-loaded',
    native_receipt_sha256:observation.native.receipt_sha256,requests:requests.map(request=>({rpc_method:request.rpc_method,raw_request_sha256:request.capture.raw_request_sha256,extraction_receipt_sha256:request.capture.extraction_receipt_sha256,
      base_observed_sha256:request.capture.base.sha256,developer_observed_sha256:request.capture.developer.sha256,base_extraction_basis:request.capture.base.basis,developer_extraction_basis:request.capture.developer.basis,
      turn_sha256:request.turn_sha256,rpc_receipt_sha256:request.rpc_receipt_sha256,thread_id_sha256:sha256(request.thread_id),session_id_sha256:sha256(request.session_id),
      instruction_sources_sha256:sha256(canonicalJson(request.instruction_sources??[])),tool_definitions:request.tools?.definitions??null,tool_calls:request.tools?.calls??null,tool_executions:request.tools?.executions??null})),
    profiles:(observation.profiles??[]).map(profile=>({checkpoint:profile.checkpoint,model:profile.model,provider:profile.provider,reasoning_effort:profile.reasoning_effort,
      fast_mode:profile.fast_mode,service_tier_preference:profile.service_tier_preference,actual_service_tier:profile.actual_service_tier,canonical_id:profile.canonical_id,canonical_match:profile.canonical_match}))};
}

function safeFailureCode(error){const message=String(error?.message??'verification failed');
  if(message.includes('descriptor'))return 'descriptor-invalid';if(message.includes('manifest')||message.includes('bundle'))return 'bundle-mismatch';
  if(message.includes('profile')||message.includes('tier')||message.includes('Canonical'))return 'profile-mismatch';if(message.includes('scenario'))return 'scenario-missing';
  if(message.includes('request')||message.includes('instruction'))return 'request-proof-invalid';if(message.includes('Native')||message.includes('ACP'))return 'native-load-invalid';return 'compatibility-rejected';}

function writeReceipt(root,receipt){
  const body=JSON.stringify(receipt,null,2),digest=sha256(body),directory=path.join(root,'receipts',receipt.bundle_id),file=path.join(directory,digest+'.json');
  fs.mkdirSync(directory,{recursive:true,mode:0o700});writeFileStrict(file,body,{mode:0o600,exclusive:true});return {receiptPath:file,receiptSha256:digest};
}

/** Consume the shared native-runner proof. The input has observations, never a
 * caller-supplied `passed` flag; this function derives all four phases and
 * binds the receipt to the manifest, descriptor, proof, and prompt bytes. */
export function verifyMobileRuntimeBundle({rootDir,bundleId,descriptorPath,runnerInputPath,verifiedAt=new Date().toISOString()}){
  const root=mobileRuntimeRoot(rootDir,{create:true}),bundleDir=path.join(root,'versions',bundleId);let bundle,descriptor,proof,descriptorSha256=null,proofSha256=null,runnerInputSha256=null,validated,scenarios=[];
  try{
    bundle=validateMobileRuntimeBundle(bundleDir,{executeBinary:true});
    const descriptorBytes=fs.readFileSync(descriptorPath),runnerInputBytes=fs.readFileSync(runnerInputPath);descriptorSha256=sha256(descriptorBytes);runnerInputSha256=sha256(runnerInputBytes);
    descriptor=JSON.parse(descriptorBytes);validated=validateMobileRuntimeCandidateDescriptor(descriptor,{bundle,manifestSha256:bundle.manifestSha256});
    const proofText=execFileSync(process.execPath,[TRUSTED_PROOF_RUNNER,'--bundle',bundleDir,'--descriptor',descriptorPath,'--input',runnerInputPath],{
      encoding:'utf8',timeout:300000,maxBuffer:16*1024*1024,env:Object.fromEntries(['PATH','TMPDIR','LANG','LC_ALL'].filter(key=>process.env[key]).map(key=>[key,process.env[key]])),stdio:['ignore','pipe','pipe']});
    const proofBytes=Buffer.from(proofText.trim());proofSha256=sha256(proofBytes);proof=JSON.parse(proofBytes);
    if(proof?.schema_version!==MOBILE_RUNTIME_PROOF_SCHEMA||proof.candidate_id!==descriptor.candidate_id||proof.bundle_manifest_sha256!==bundle.manifestSha256||proof.descriptor_sha256!==descriptorSha256||!Array.isArray(proof.observations))throw Error('Native proof is bound to different candidate bytes');
    const runnerSha256=sha256(fs.readFileSync(TRUSTED_PROOF_RUNNER)),codexSha256=bundle.manifest.files.find(file=>file.path===bundle.manifest.runtime.codex.path)?.sha256;
    if(proof.runner_input_sha256!==runnerInputSha256||proof.producer?.runner_sha256!==runnerSha256||proof.producer?.codex_sha256!==codexSha256||proof.producer?.codex_version!==bundle.manifest.runtime.codex.version||proof.producer?.acp_entry_sha256!==bundle.manifest.runtime.acp.entry_sha256||!String(proof.producer?.acp_version??'').includes(bundle.manifest.runtime.acp.version)||!hex64(proof.producer?.actual_exec_receipt_sha256))throw Error('Trusted proof runner did not execute these runtime bytes');
    for(const requirement of validated.requirements){const observation=proof.observations.find(item=>item?.scenario_id===requirement.scenario_id);scenarios.push(scenarioReceipt(observation,requirement,descriptor,validated,bundle));}
    const runtimeSignature={bundle_manifest_sha256:bundle.manifestSha256,codex_sha256:descriptor.runtime.codex_bin.sha256,acp_entry_sha256:descriptor.runtime.acp.entry_sha256,
      acp_package_json_sha256:descriptor.runtime.acp.package_json_sha256,base_sha256:descriptor.base.sha256,developer_sha256:descriptor.developer.sha256,
      launcher_sha256:descriptor.runtime.launcher.sha256,config_sha256:descriptor.runtime.config.sha256,schema_sha256:descriptor.runtime.schema.sha256,catalog_sha256:descriptor.runtime.catalog.sha256};
    const artifacts=Object.fromEntries(Object.entries({base:descriptor.base,developer:descriptor.developer,launcher:descriptor.runtime.launcher,config:descriptor.runtime.config,schema:descriptor.runtime.schema,catalog:descriptor.runtime.catalog}).map(([name,value])=>[name,{path:value.realpath,relative_path:path.relative(validated.privateRoot,value.realpath),sha256:value.sha256,bytes:value.bytes}]));
    const receipt={schema_version:MOBILE_RUNTIME_RECEIPT_SCHEMA,candidate_id:descriptor.candidate_id,candidate_kind:descriptor.candidate_kind,bundle_id:bundleId,state:'verified',compatible:true,verified_at:verifiedAt,
      stages:{declared:true,prepared:true,native_loaded:true,request_verified:true},bundle:{manifest_sha256:bundle.manifestSha256},
      evidence:{descriptor_sha256:descriptorSha256,runner_input_sha256:runnerInputSha256,proof_sha256:proofSha256,runner_sha256:proof.producer.runner_sha256,actual_exec_receipt_sha256:proof.producer.actual_exec_receipt_sha256,runtime_signature_sha256:sha256(canonicalJson(runtimeSignature))},private_root:validated.privateRoot,artifacts,runtime_signature:runtimeSignature,scenarios,reasons:[]};
    return {...receipt,...writeReceipt(root,receipt)};
  }catch(error){
    const receipt={schema_version:MOBILE_RUNTIME_RECEIPT_SCHEMA,candidate_id:candidateId(descriptor?.candidate_id)?descriptor.candidate_id:(candidateId(bundleId)?bundleId:'unknown'),candidate_kind:descriptor?.candidate_kind??null,bundle_id:bundleId,state:'rejected',compatible:false,verified_at:verifiedAt,
      stages:{declared:Boolean(descriptor),prepared:Boolean(bundle),native_loaded:false,request_verified:false},bundle:{manifest_sha256:bundle?.manifestSha256??null},
      evidence:{descriptor_sha256:descriptorSha256,runner_input_sha256:runnerInputSha256,proof_sha256:proofSha256,runtime_signature_sha256:null},runtime_signature:null,scenarios,reasons:[safeFailureCode(error)]};
    return {...receipt,...writeReceipt(root,receipt)};
  }
}

const validPointer=value=>Boolean(value&&candidateId(value.bundle_id)&&hex64(value.manifest_sha256)&&hex64(value.receipt_sha256));
function validActivationIndex(value){return value?.schema_version===MOBILE_RUNTIME_ACTIVATION_SCHEMA&&Number.isSafeInteger(value.revision)&&value.revision>=1&&validPointer(value.current)&&(value.previous===null||validPointer(value.previous));}
function readReceiptBound(root,pointer){
  const file=path.join(root,'receipts',pointer.bundle_id,pointer.receipt_sha256+'.json'),bytes=fs.readFileSync(file),receipt=JSON.parse(bytes);
  if(sha256(bytes)!==pointer.receipt_sha256||receipt.state!=='verified'||receipt.compatible!==true||receipt.stages?.request_verified!==true||receipt.candidate_kind!=='mobile-main-maintenance'||receipt.bundle_id!==pointer.bundle_id||receipt.bundle?.manifest_sha256!==pointer.manifest_sha256)throw Error('Activation receipt is not verified for these runtime bytes');
  return {file,receipt};
}

function validateReceiptArtifacts(receipt){
  const root=fs.realpathSync(receipt.private_root);for(const name of ['base','developer','launcher','config','schema','catalog']){
    const record=receipt.artifacts?.[name];if(!record||typeof record.path!=='string'||!hex64(record.sha256)||!Number.isSafeInteger(record.bytes))throw Error('Active runtime artifact receipt is incomplete');
    const lstat=fs.lstatSync(record.path),real=fs.realpathSync(record.path);if(lstat.isSymbolicLink()||!lstat.isFile()||!inside(root,real))throw Error('Active runtime artifact changed type or root');
    const bytes=fs.readFileSync(real);if(bytes.length!==record.bytes||sha256(bytes)!==record.sha256||path.relative(root,real)!==record.relative_path)throw Error('Active runtime artifact bytes changed');
  }return root;
}

export function resolveActiveMobileRuntime({rootDir,allowPreviousIndex=false}={}){
  const root=mobileRuntimeRoot(rootDir),indexFile=activationFile(root),loaded=allowPreviousIndex?loadJson(indexFile,{validate:validActivationIndex}):{...readJsonFile(indexFile),source:'current'};
  const index=loaded.value??(loaded.state==='ok'?loaded.value:null);if(!validActivationIndex(index))throw Error('No verified mobile runtime is active');
  const bundle=validateMobileRuntimeBundle(path.join(root,'versions',index.current.bundle_id));if(bundle.manifestSha256!==index.current.manifest_sha256)throw Error('Active runtime manifest changed');
  const receipt=readReceiptBound(root,index.current),privateRoot=validateReceiptArtifacts(receipt.receipt),artifact=name=>receipt.receipt.artifacts[name].path;
  return {root,indexPath:indexFile,indexSource:loaded.source??'current',index,bundleDir:bundle.bundleDir,manifest:bundle.manifest,receipt:receipt.receipt,privateRoot,
    codexRelativePath:bundle.manifest.runtime.codex.path,acpEntryRelativePath:bundle.manifest.runtime.acp.entry_path,catalogRelativePath:receipt.receipt.artifacts.catalog.relative_path,
    codexPath:path.join(bundle.bundleDir,bundle.manifest.runtime.codex.path),acpEntryPath:path.join(bundle.bundleDir,bundle.manifest.runtime.acp.entry_path),catalogPath:artifact('catalog'),configPath:artifact('config'),
    baseInstructionsPath:artifact('base'),developerInstructionsPath:artifact('developer'),launcherPath:artifact('launcher'),schemaPath:artifact('schema')};
}

/** Promote only a fully verified main-session candidate. The single activation
 * index is authoritative for current and previous; convenience symlinks are
 * intentionally not part of the contract. */
export async function activateMobileRuntimeBundle({rootDir,bundleId,receiptPath,expectedRevision=null,activatedAt=new Date().toISOString(),writeIndex=writeActivationIndexStrict}){
  const root=mobileRuntimeRoot(rootDir),receiptBytes=fs.readFileSync(receiptPath),receiptSha256=sha256(receiptBytes),receipt=JSON.parse(receiptBytes),bundle=validateMobileRuntimeBundle(path.join(root,'versions',bundleId));
  if(receipt.state!=='verified'||receipt.compatible!==true||receipt.stages?.request_verified!==true||receipt.candidate_kind!=='mobile-main-maintenance'||receipt.bundle_id!==bundleId||receipt.bundle?.manifest_sha256!==bundle.manifestSha256)throw Error('Only a verified mobile main runtime can be activated');
  const canonicalReceipt=path.join(root,'receipts',bundleId,receiptSha256+'.json');if(fs.realpathSync(receiptPath)!==fs.realpathSync(canonicalReceipt))throw Error('Activation receipt is outside the runtime receipt store');
  validateReceiptArtifacts(receipt);const pointer={bundle_id:bundleId,manifest_sha256:bundle.manifestSha256,receipt_sha256:receiptSha256};
  const result=await withPidLock(path.join(root,'activation.lock'),{work:async()=>{
    const existing=readJsonFile(activationFile(root));if(existing.state!=='missing'&&(existing.state!=='ok'||!validActivationIndex(existing.value)))throw Error('Existing activation index is invalid');
    const current=existing.state==='ok'?existing.value:null;
    if(expectedRevision!==null&&(current?.revision??0)!==expectedRevision)throw Error('Activation revision changed');
    if(current&&canonicalJson(current.current)===canonicalJson(pointer))return {index:current,changed:false};
    const index={schema_version:MOBILE_RUNTIME_ACTIVATION_SCHEMA,revision:(current?.revision??0)+1,activated_at:activatedAt,current:pointer,previous:current?.current??null};
    writeIndex(activationFile(root),index,{previous:true,pretty:true,mode:0o600});return {index,changed:true};
  },reconcile:async()=>({state:'needs-retry',reason:'interrupted-activation-kept-existing-index'})});
  if(result.state!=='ran')return {state:result.state,...result.value};return {state:result.value.changed?'activated':'already-active',index:result.value.index};
}

export async function rollbackMobileRuntimeBundle({rootDir,expectedRevision=null,activatedAt=new Date().toISOString()}){
  const active=resolveActiveMobileRuntime({rootDir});if(!active.index.previous)throw Error('No previous verified mobile runtime is recorded');
  const previous=active.index.previous,receipt=readReceiptBound(active.root,previous);
  return activateMobileRuntimeBundle({rootDir,bundleId:previous.bundle_id,receiptPath:receipt.file,expectedRevision:expectedRevision??active.index.revision,activatedAt});
}

export function readMobileRuntimeBundleState({rootDir}){
  let root;try{root=mobileRuntimeRoot(rootDir);}catch(error){if(error.code==='ENOENT')return {state:'inactive'};throw error;}const result=readJsonFile(activationFile(root));
  if(result.state!=='ok'||!validActivationIndex(result.value))return {state:result.state==='missing'?'inactive':'invalid'};
  try{const active=resolveActiveMobileRuntime({rootDir});return {state:'verified',revision:active.index.revision,current:active.index.current,previous:active.index.previous,index_source:active.indexSource,candidate_kind:active.receipt.candidate_kind};}
  catch{return {state:'invalid',revision:result.value.revision,current:result.value.current,previous:result.value.previous};}
}

function cliArgs(argv){const result={_:[]};for(let i=0;i<argv.length;i++){const value=argv[i];if(!value.startsWith('--'))result._.push(value);else{const key=value.slice(2);if(i+1>=argv.length||argv[i+1].startsWith('--'))result[key]=true;else result[key]=argv[++i];}}return result;}
function requiredArg(args,name){if(typeof args[name]!=='string'||!args[name])throw Error('Missing --'+name);return args[name];}

export async function mobileRuntimeBundleCli(argv=process.argv.slice(2)){
  const args=cliArgs(argv),command=args._[0];let result;
  if(command==='prepare')result=prepareMobileRuntimeBundle(readJsonStrict(requiredArg(args,'spec'),'Bundle specification'));
  else if(command==='verify')result=verifyMobileRuntimeBundle({rootDir:requiredArg(args,'root'),bundleId:requiredArg(args,'bundle'),descriptorPath:requiredArg(args,'descriptor'),runnerInputPath:requiredArg(args,'runner-input')});
  else if(command==='activate')result=await activateMobileRuntimeBundle({rootDir:requiredArg(args,'root'),bundleId:requiredArg(args,'bundle'),receiptPath:requiredArg(args,'receipt'),expectedRevision:args['expected-revision']===undefined?null:Number(args['expected-revision'])});
  else if(command==='rollback')result=await rollbackMobileRuntimeBundle({rootDir:requiredArg(args,'root'),expectedRevision:args['expected-revision']===undefined?null:Number(args['expected-revision'])});
  else if(command==='status')result=readMobileRuntimeBundleState({rootDir:requiredArg(args,'root')});
  else if(command==='resolve'){const active=resolveActiveMobileRuntime({rootDir:requiredArg(args,'root')});result={state:'verified',bundle_dir:active.bundleDir,codex_path:active.codexPath,acp_entry_path:active.acpEntryPath,index:active.index};}
  else throw Error('Usage: mobile-runtime-bundle.mjs prepare|verify|activate|rollback|status|resolve');
  process.stdout.write(JSON.stringify(result,null,2)+'\n');if(result.state==='rejected'||result.state==='invalid')process.exitCode=2;return result;
}

const invoked=process.argv[1]&&pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url;
if(invoked)mobileRuntimeBundleCli().catch(error=>{process.stderr.write(String(error?.message??error)+'\n');process.exitCode=1;});

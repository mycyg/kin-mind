import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {prepareMobileRuntimeBundle,validateMobileRuntimeBundle,REQUIRED_OWNED_ACP_MARKERS} from '../../adapters/mobile-runtime-bundle.mjs';
const hash=s=>createHash('sha256').update(s).digest('hex');

test('runtime packaging keeps code-mode sibling and shell resources; missing support fails before activation',t=>{
  const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'kin-support-')));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const source=path.join(root,'vendor'),bin=path.join(source,'bin'),modules=path.join(root,'node_modules'),acp=path.join(modules,'@agentclientprotocol/codex-acp');
  fs.mkdirSync(bin,{recursive:true});fs.mkdirSync(acp,{recursive:true});
  const binary=path.join(bin,'codex'),helper=path.join(bin,'codex-code-mode-host');
  fs.writeFileSync(binary,'#!/bin/sh\necho "codex-cli 0.155.0"\n',{mode:0o700});
  fs.writeFileSync(helper,'#!/bin/sh\necho isolated-helper\n',{mode:0o700});
  const shell=path.join(source,'codex-resources/zsh/bin');fs.mkdirSync(shell,{recursive:true});fs.writeFileSync(path.join(shell,'zsh'),'fixture',{mode:0o700});
  fs.writeFileSync(path.join(acp,'package.json'),JSON.stringify({name:'@agentclientprotocol/codex-acp',version:'1.11.0',main:'index.js'}));
  const body=REQUIRED_OWNED_ACP_MARKERS.join('\n')+'\n';fs.writeFileSync(path.join(acp,'index.js'),body);
  const owned=path.join(root,'owned.mjs');fs.writeFileSync(owned,body);
  const args={rootDir:path.join(root,'host'),bundleId:'support-fixture',codexBinary:binary,acpPackageRoot:acp,nodeModulesRoot:modules,
    allowedPackages:['@agentclientprotocol/codex-acp'],ownedAcpEntry:{path:owned,realpath:owned,allowed_root:root,sha256:hash(body),source_vendor_sha256:hash(body),bytes:Buffer.byteLength(body),required_markers:REQUIRED_OWNED_ACP_MARKERS}};
  const bundle=prepareMobileRuntimeBundle(args);
  assert.equal(bundle.manifest.runtime.codex.code_mode_host,'bin/codex-code-mode-host');
  assert.ok(bundle.manifest.files.some(f=>f.path==='bin/codex-code-mode-host'));
  assert.ok(bundle.manifest.files.some(f=>f.path==='codex-resources/zsh/bin/zsh'));
  fs.unlinkSync(path.join(bundle.bundleDir,'bin/codex-code-mode-host'));
  assert.throws(()=>validateMobileRuntimeBundle(bundle.bundleDir),/bytes differ/);
  fs.unlinkSync(helper);
  assert.throws(()=>prepareMobileRuntimeBundle({...args,bundleId:'missing-support'}),/ENOENT/);
  assert.equal(fs.existsSync(path.join(root,'host/state/mobile-runtime/activation.json')),false);
});

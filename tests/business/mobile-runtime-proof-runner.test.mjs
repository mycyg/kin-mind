import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {produceMobileRuntimeProof,validateRunnerInput} from '../../adapters/mobile-runtime-proof-runner.mjs';

// A runtime candidate is proven by the runner running it, never by what the caller hands in: the
// input only names the exact manifest and descriptor bytes to run, and anything else is refused
// before a scratch home exists or a binary starts.
const RUNNER=fileURLToPath(new URL('../../adapters/mobile-runtime-proof-runner.mjs',import.meta.url));
const SCHEMA='kin.mobile-runtime.runner-input/v2';
const sha=value=>createHash('sha256').update(value).digest('hex');

function candidate(t) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'kin-proof-runner-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const bundle=path.join(root,'bundle'),launched=path.join(root,'launched'),scratch=path.join(root,'tmp');
  fs.mkdirSync(path.join(bundle,'bin'),{recursive:true});fs.mkdirSync(scratch);
  // Every executable the candidate names leaves a mark if it is ever started.
  const executable=file=>{fs.writeFileSync(file,`#!/bin/sh\necho "$0" >> '${launched}'\necho 0.0.0\n`,{mode:0o755});return file;};
  executable(path.join(bundle,'bin','codex'));
  const manifest=JSON.stringify({runtime:{codex:{path:'bin/codex'},acp:{entry_path:'bin/codex',package_path:'.',version:'0.0.0'}}});
  fs.writeFileSync(path.join(bundle,'manifest.json'),manifest);
  const descriptor=path.join(root,'runtime-descriptor.private.json'),input=path.join(root,'runner-input.private.json');
  const describe=launcher=>fs.writeFileSync(descriptor,JSON.stringify({candidate_id:'candidate-1',runtime:{launcher:{realpath:launcher}}}));
  describe(executable(path.join(root,'launcher')));
  const exact={schema_version:SCHEMA,candidate_id:'candidate-1',bundle_manifest_sha256:sha(manifest),descriptor_sha256:sha(fs.readFileSync(descriptor))};
  fs.writeFileSync(input,JSON.stringify(exact));
  return {root,bundle,descriptor,input,exact,manifest,launched,scratch,describe,executable,
    untouched(){assert.equal(fs.existsSync(launched),false,'a candidate binary was started');assert.deepEqual(fs.readdirSync(scratch),[],'a scratch home was made');}};
}

test('runner input names exactly the bytes to run and nothing else',t=>{
  const c=candidate(t),descriptor=JSON.parse(fs.readFileSync(c.descriptor));
  const check=input=>validateRunnerInput(input,descriptor,sha(c.manifest),sha(fs.readFileSync(c.descriptor)));
  check(c.exact);
  for(const input of [null,{...c.exact,schema_version:'kin.mobile-runtime.proof/v1'}])
    assert.throws(()=>check(input),/not executable runner input/);
  for(const extra of [{observations:[{kind:'fresh_thread',passed:true}]},{compatible:true},{codex_path:'/tmp/another-codex'}])
    assert.throws(()=>check({...c.exact,...extra}),/cannot contain observations, code or external capture paths/);
  for(const change of [{candidate_id:'candidate-2'},{bundle_manifest_sha256:sha('another manifest')},{descriptor_sha256:sha('another descriptor')}])
    assert.throws(()=>check({...c.exact,...change}),/targets different bytes/);
});

test('a descriptor changed after its input was written is refused before anything starts',async t=>{
  const c=candidate(t),saved=process.env.TMPDIR;
  process.env.TMPDIR=c.scratch;t.after(()=>{if(saved===undefined)delete process.env.TMPDIR;else process.env.TMPDIR=saved;});
  c.describe(c.executable(path.join(c.root,'another-launcher')));
  await assert.rejects(produceMobileRuntimeProof({bundleDir:c.bundle,descriptorPath:c.descriptor,inputPath:c.input}),/targets different bytes/);
  c.untouched();
});

test('the command line answers a forged input with a failure and no proof',t=>{
  const c=candidate(t);
  fs.writeFileSync(c.input,JSON.stringify({...c.exact,observations:[],compatible:true}));
  const run=spawnSync(process.execPath,[RUNNER,'--bundle',c.bundle,'--descriptor',c.descriptor,'--input',c.input],
    {encoding:'utf8',env:{...process.env,TMPDIR:c.scratch},timeout:30000});
  assert.equal(run.status,1);assert.equal(run.stdout,'');assert.match(run.stderr,/cannot contain observations/);
  c.untouched();
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {instructionTextEvidence,MAX_COMPANION_INSTRUCTION_BYTES,sha256,
  verifyCompanionInstructions,nativeInstructionRequestIdentity} from './instruction-evidence.mjs';

const fixture=t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-instructions-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const allowedRoot=path.join(dir,'private');fs.mkdirSync(allowedRoot);
  const modelInstructionsFile=path.join(allowedRoot,'base.md');
  const base='Synthetic companion base\n',developerInstructions='Synthetic developer persona\n';
  fs.writeFileSync(modelInstructionsFile,base);
  return {dir,allowedRoot,modelInstructionsFile,base,developerInstructions,
    descriptor:{enabled:true,allowedRoot,modelInstructionsFile,modelInstructionsSha256:sha256(base),
      developerInstructions,developerInstructionsSha256:sha256(developerInstructions)}};
};

test('native identity requires agreement across authenticated headers and request metadata',()=>{
  const headers={'session-id':'thread-1','thread-id':'thread-1','x-client-request-id':'thread-1'};
  const body={prompt_cache_key:'thread-1',client_metadata:{session_id:'thread-1',thread_id:'thread-1',turn_id:'turn-1',root_turn_id:'turn-1'}};
  assert.deepEqual(nativeInstructionRequestIdentity(headers,body),{basis:'native-http-metadata/v1',sessionId:'thread-1',threadId:'thread-1',turnId:'turn-1'});
  assert.equal(nativeInstructionRequestIdentity({},body),'unknown');
  assert.equal(nativeInstructionRequestIdentity({...headers,'thread-id':'other'},body),'unknown');
  assert.equal(nativeInstructionRequestIdentity(headers,{...body,client_metadata:{...body.client_metadata,root_turn_id:'old'}}),'unknown');
});

test('instruction evidence contains exact UTF-8 hashes and lengths but never prompt text',()=>{
  const value=instructionTextEvidence('你好\n');
  assert.deepEqual(value,{present:true,sha256:'4e0826721642ed8e3a27e7147538ac7b7013a08fe5ae343a8ef09749b7e5790f',utf8Bytes:7});
  assert.equal(JSON.stringify(value).includes('你好'),false);
  assert.deepEqual(instructionTextEvidence(undefined),{present:false,sha256:null,utf8Bytes:0});
});

test('companion instruction verification returns only the exact path, developer bytes and hash binding',t=>{
  const f=fixture(t),value=verifyCompanionInstructions(f.descriptor);
  assert.equal(value.enabled,true);assert.equal(value.modelInstructionsFile,fs.realpathSync(f.modelInstructionsFile));
  assert.equal(value.developerInstructions,f.developerInstructions);
  assert.deepEqual(value.evidence,{modelInstructionsSha256:sha256(f.base),modelInstructionsUtf8Bytes:25,
    developerInstructionsSha256:sha256(f.developerInstructions),developerInstructionsUtf8Bytes:28});
  assert.deepEqual(verifyCompanionInstructions(),{enabled:false});
});

test('enabled companion instructions fail closed on missing, mismatched or unsafe inputs',t=>{
  const f=fixture(t);
  assert.throws(()=>verifyCompanionInstructions({enabled:true}),/incomplete/);
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,modelInstructionsSha256:'0'.repeat(64)}),/hash mismatch/);
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,developerInstructionsSha256:'0'.repeat(64)}),/hash mismatch/);
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,developerInstructions:''}),/developer instructions/);

  const link=path.join(f.allowedRoot,'link.md');fs.symlinkSync(f.modelInstructionsFile,link);
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,modelInstructionsFile:link}),/unverified/);
  const outside=path.join(f.dir,'outside.md');fs.writeFileSync(outside,f.base);
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,modelInstructionsFile:outside}),/unverified/);

  fs.writeFileSync(f.modelInstructionsFile,'bad\r\n');
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,modelInstructionsSha256:sha256('bad\r\n')}),/normalization/);
  fs.writeFileSync(f.modelInstructionsFile,Buffer.from([0xc3,0x28]));
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,modelInstructionsSha256:sha256(Buffer.from([0xc3,0x28]))}),/UTF-8/);
  fs.writeFileSync(f.modelInstructionsFile,Buffer.alloc(MAX_COMPANION_INSTRUCTION_BYTES+1,0x61));
  assert.throws(()=>verifyCompanionInstructions({...f.descriptor,modelInstructionsSha256:sha256(fs.readFileSync(f.modelInstructionsFile))}),/size/);
});

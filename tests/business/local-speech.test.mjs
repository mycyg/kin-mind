import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {spawn,execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath,pathToFileURL} from 'node:url';

const moduleUrl=process.env.KIN_TEST_SPEECH_MODULE?pathToFileURL(process.env.KIN_TEST_SPEECH_MODULE):new URL('../../adapters/local-speech.mjs',import.meta.url);
const {LocalSpeech}=await import(moduleUrl);

test('configured sampling and the complete public bubble reach the worker; resume keeps its audio and settings',async t=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'kin-local-speech-'));
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  const generation={temperature:0.45,top_p:0.95,top_k:40,repetition_penalty:1.6,max_tokens:768};
  const script=`const fs=require('node:fs'),rl=require('node:readline').createInterface({input:process.stdin});
    rl.on('line',line=>{const r=JSON.parse(line);fs.writeFileSync(r.file,JSON.stringify(r));
    process.stdout.write(JSON.stringify({ok:true,durationMs:1000,synthesis:{generation:r.generation}})+'\\n');});`;
  const worker=new LocalSpeech({python:'unused',model:'unused',ffmpeg:'unused',directory,generation,
    spawnImpl:()=>spawn(process.execPath,['-e',script],{stdio:['pipe','pipe','ignore']})});
  t.after(async()=>{if(worker.child){const exited=once(worker.child,'exit');worker.close();await exited;}});
  const text='嗯，我在呢。\n你慢慢说，我听着。';
  const result=await worker.synthesize({id:'bubble',text,style:''});
  const request=JSON.parse(result.data.toString());
  assert.deepEqual(request.generation,generation);
  assert.equal(request.text,text);
  assert.deepEqual(result.synthesis.generation,generation);
  worker.generation={temperature:0.8};
  const resumed=await worker.synthesize({id:'bubble',text,style:''});
  assert.equal(resumed.cached,true);
  assert.deepEqual(resumed.data,result.data);
  assert.deepEqual(resumed.synthesis.generation,generation);
});

test('unsupported controls and invalid sampling fail before any model starts',()=>{
  for(const generation of [{speed:0.8},{instruct:'轻声'},{split_pattern:'。'},{top_p:2},{temperature:-1},{top_k:1.5},{repetition_penalty:0.5},{max_tokens:0},null])
    assert.throws(()=>new LocalSpeech({generation,spawnImpl:()=>{throw Error('must not spawn');}}),/Invalid TTS generation/);
});

test('Python worker preserves current defaults and records the Base ICL floor after overrides',()=>{
  const worker=fileURLToPath(new URL('../../scripts/local_speech.py',import.meta.url));
  const result=JSON.parse(execFileSync('python3',['-c',
    'import json,runpy,sys; f=runpy.run_path(sys.argv[1])["generation_options"]; print(json.dumps([f({},True),f({},False),f({"generation":{"temperature":0.45,"top_p":0.95,"top_k":40,"repetition_penalty":1.1,"max_tokens":768}},True)]))',worker],{encoding:'utf8'}));
  assert.deepEqual(result[0],{temperature:0.3,top_p:0.9,top_k:50,repetition_penalty:1.5,max_tokens:4096});
  assert.deepEqual(result[1],{temperature:0.9,top_p:1,top_k:50,repetition_penalty:1.05,max_tokens:4096});
  assert.deepEqual(result[2],{temperature:0.45,top_p:0.95,top_k:40,repetition_penalty:1.5,max_tokens:768});
});

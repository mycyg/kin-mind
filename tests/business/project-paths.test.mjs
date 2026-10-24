import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
import {projectRoot,projectCodeRoot,projectModule} from '../../adapters/project-paths.mjs';
test('isolated host data never becomes executable module resolution',async t=>{
 const scratch=fs.mkdtempSync(path.join(os.tmpdir(),'kin-module-roots-'));
 const data=path.join(scratch,'data'),host=path.join(scratch,'code');
 fs.mkdirSync(data);fs.mkdirSync(host);
 const prior=Object.fromEntries(['KIN_HOST_ROOT','KIN_HOST_CODE_ROOT'].map(key=>[key,process.env[key]]));
 process.env.KIN_HOST_ROOT=data;process.env.KIN_HOST_CODE_ROOT=host;
 t.after(()=>{for(const [key,value] of Object.entries(prior)){if(value===undefined)delete process.env[key];else process.env[key]=value;}fs.rmSync(scratch,{recursive:true,force:true});});
 assert.equal(projectRoot('host'),data);assert.equal(projectCodeRoot('host'),host);
 fs.writeFileSync(path.join(data,'message-items.mjs'),'throw Error("data directory was executed");');
 fs.writeFileSync(path.join(host,'message-items.mjs'),'export const source="configured-code-root";');
 const code=await projectModule('host','message-items.mjs');assert.equal(code.source,'configured-code-root');
});

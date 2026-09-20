import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ServiceRuntime} from '../../dsh-plugin/lib/service-runtime.js';
import {Config} from '../../dsh-plugin/lib/config.js';

test('plugin orders capture, recall, compact and end; offline messages remain durable',async t=>{
 const root=mkdtempSync(join(tmpdir(),'kin-plugin-'));
 const previous=process.env.EVENTMEM_HOME;process.env.EVENTMEM_HOME=root;
 t.after(()=>{if(previous===undefined)delete process.env.EVENTMEM_HOME;else process.env.EVENTMEM_HOME=previous;rmSync(root,{recursive:true,force:true});});
 writeFileSync(join(root,'local-token'),'fixture-token');
 const runtime=new ServiceRuntime(Config({})),sent=[],injected=[];
 t.mock.method(globalThis,'fetch',async(_url,init)=>{sent.push(JSON.parse(init.body));return new Response(JSON.stringify({text:'Scoped context'}),{status:200});});
 const inject=text=>injected.push(text);
 runtime.sessionStart('s','/synthetic-project',inject);
 runtime.message('s','/synthetic-project','user','My preference',1);
 await runtime.preAction('s','/synthetic-project','Read',{path:'app.py'},inject);
 runtime.sessionStart('s','/synthetic-project',inject,'compact');await runtime.flushAll();
 assert.deepEqual(sent.map(r=>r.event),['start','message','pre_action','compact','end']);
 assert.equal(readdirSync(join(root,'host-spool')).length,0);assert.ok(injected.length);
 globalThis.fetch=async()=>{throw Error('offline');};
 runtime.message('s','/synthetic-project','assistant','Unverified account',9);await runtime.flush('s');
 assert.equal(readdirSync(join(root,'host-spool')).filter(f=>f.endsWith('.json')).length,1);
});

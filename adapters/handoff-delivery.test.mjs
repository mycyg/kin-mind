import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {deliverHandoff,deliveryIds} from './handoff-delivery.mjs';

const artifact=Buffer.from('Example artifact');
const entry=()=>({id:'task:example:4',state:'pending',title:'Example task',text:'Complete',kind:'completed',artifacts:[{path:'/example/note.txt',name:'note.txt',sha256:createHash('sha256').update(artifact).digest('hex')}]});

test('completion requires text and every file receipt; IDs survive a restart',async()=>{
  const item=entry(),receipts=new Map(),states=[];
  const options={read:async()=>artifact,settle:async(id,r)=>{states.push(r);return r;},send:async p=>{const r={state:'accepted',messageId:'platform-'+p.id};receipts.set(p.id,r);return r;},lookup:async id=>receipts.get(id)};
  const result=await deliverHandoff(item,options);
  assert.equal(result.state,'accepted');assert.equal(result.message_ids.length,2);
  const reconciled=await deliverHandoff({...item,state:'sending'},{...options,send:()=>{throw Error('must not replay');}});
  assert.deepEqual(reconciled.message_ids,result.message_ids);
  assert.deepEqual(deliveryIds(item),[...receipts.keys()]);
});

test('changed artifacts stop all transmission before the first part',async()=>{
  let sends=0;
  const result=await deliverHandoff(entry(),{settle:async(id,r)=>r,read:async()=>Buffer.from('Changed'),send:async()=>{sends++;},lookup:async()=>null});
  assert.equal(result.state,'failed');assert.equal(sends,0);
});

test('uncertain partial delivery remains uncertain without replay or new IDs',async()=>{
  const item=entry(),receipts=new Map();let sends=0;
  const options={settle:async(id,r)=>r,read:async()=>artifact,lookup:async id=>receipts.get(id),send:async p=>{sends++;if(sends===2)throw Error('timeout');const r={state:'accepted',messageId:'one'};receipts.set(p.id,r);return r;}};
  assert.equal((await deliverHandoff(item,options)).state,'unconfirmed');
  assert.equal((await deliverHandoff({...item,state:'unconfirmed'},options)).state,'unconfirmed');
  assert.equal(sends,2);
});

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Client,acceptDelivery} from '../../sdk/typescript/dist/index.js';
test('contract path, query, bearer credentials and JSON',async()=>{
 const calls=[];const client=new Client('http://127.0.0.1:8319','fixture',async(url,init)=>{calls.push({url:String(url),...init});return new Response(JSON.stringify({id:'memory',status:'active',revision:1}),{headers:{'Content-Type':'application/json'}})});
 await client.call('read_memory',{path:{record_id:'id/with space'},query:{offset:10,length:20}});
 assert.equal(calls[0].url,'http://127.0.0.1:8319/v1/memories/id%2Fwith%20space?offset=10&length=20');assert.equal(calls[0].headers.Authorization,'Bearer fixture');
 await assert.rejects(client.call('read_memory'),/Missing path/);
});
test('HTTP failure is surfaced and attachments remain binary',async()=>{
 const client=new Client('http://127.0.0.1','fixture',async()=>new Response('missing',{status:404}));await assert.rejects(client.call('health'),/404/);
 const bytes=new Client('http://127.0.0.1','fixture',async()=>new Response(new Uint8Array([1,2,3]),{headers:{'Content-Type':'application/octet-stream'}}));assert.deepEqual([...new Uint8Array(await bytes.call('read_attachment',{path:{source_id:'src'}}))],[1,2,3]);
});
test('callback transaction deduplicates one logical effect',async()=>{
 const rows=new Map();let effects=0;const store={transaction:fn=>fn({has:async id=>rows.has(id),put:async(id,body)=>{rows.set(id,body)}})};
 assert.equal(await acceptDelivery(store,{id:'stable'},async()=>{effects++}),true);assert.equal(await acceptDelivery(store,{id:'stable'},async()=>{effects++}),false);assert.equal(effects,1);
});

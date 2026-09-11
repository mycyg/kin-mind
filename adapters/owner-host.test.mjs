import test from 'node:test';
import assert from 'node:assert/strict';
import {MindLoop} from './owner-host.mjs';
function fixture(overrides={}) {
 const events=[];let epoch='owner-1';let eligible=true;let busy=false;
 const loop=new MindLoop({call:async(action,req)=>{events.push([action,req]);if(action==='candidate')return{eligible:true};if(action==='claim')return{id:'stable-id',state:'drafting'};if(action==='check')return{eligible:true};return req;},eligibility:()=>({eligible}),ownerEpoch:()=>epoch,isBusy:()=>busy,draft:async()=> 'A sourced hello',send:async()=>({state:'accepted',messageId:'server-id'}),...overrides});
 return{loop,events,change:()=>{epoch='owner-2';},quiet:()=>{eligible=false;},busy:()=>{busy=true;}};
}
test('under four hours is allowed by threshold; accepted receipt settles',async()=>{const{loop,events}=fixture();await loop.tick();assert.equal(events.at(-1)[1].state,'accepted');assert.equal(events.filter(x=>x[0]==='claim').length,1);});
test('quiet hours and waiting owner never draft',async()=>{const{loop,events,quiet}=fixture();quiet();await loop.tick();assert.equal(events.length,0);});
test('new message invalidates a draft',async()=>{let f;f=fixture({draft:async()=>{f.change();return'outdated';},send:async()=>assert.fail('must not send')});await f.loop.tick();assert.equal(f.events.at(-1)[1].state,'canceled');});
test('missing message ID is unconfirmed',async()=>{const{loop,events}=fixture({send:async()=>({state:'accepted'})});await loop.tick();assert.equal(events.at(-1)[1].state,'unconfirmed');});
test('timeout never invents another send ID',async()=>{const{loop,events}=fixture({send:async()=>{throw Error('timeout');}});await loop.tick();assert.equal(events.at(-1)[1].state,'unconfirmed');assert.equal(events.filter(x=>x[0]==='claim').length,1);});
test('concurrent ticks share one draft',async()=>{let release;const gate=new Promise(r=>release=r);const{loop,events}=fixture({draft:async()=>{await gate;return'hello';}});const one=loop.tick();await new Promise(r=>setImmediate(r));await loop.tick();release();await one;assert.equal(events.filter(x=>x[0]==='claim').length,1);});

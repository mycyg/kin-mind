import test from 'node:test';
import assert from 'node:assert/strict';
import {NativeContextDelivery} from '../../adapters/context-delivery.mjs';
import {patchModelRetries} from '../../adapters/codex-runtime-patch.mjs';

test('a completed failed native turn defers background without pretending an append was sent',async()=>{
  const calls=[];
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,backgroundTasks:0,nativeStatus:'systemError'}),
    call:async(...args)=>calls.push(args),inject:async()=>calls.push('inject')});
  assert.equal((await delivery.deliver({id:'context',session:'main'})).state,'deferred');
  assert.deepEqual(calls,[]);
});

test('an uncertain previous append is kept without replay',async()=>{
  let injected=0;
  const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,nativeStatus:'idle'}),
    call:async()=>[{id:'old',session:'main',marker:'old'}],find:async()=>({found:false}),inject:async()=>injected++});
  assert.equal((await delivery.deliver({id:'old',session:'main'})).state,'unconfirmed');
  assert.equal(injected,0);
});

test('the native gateway owns at most three HTTP retries with no stream replay loop',()=>{
  const original='config = {\n        wire_api: wireApi\n}';
  const patched=patchModelRetries(original);
  assert.match(patched,/request_max_retries: 3/);assert.match(patched,/stream_max_retries: 0/);
  assert.equal(patchModelRetries(patched),patched);
});

test('one uncertain identity does not hold unrelated fresh background forever',async()=>{
 const calls=[],context={id:'new',session:'main',epoch:'now',marker:'new',text:'new background'};
 let appended=false;
 const delivery=new NativeContextDelivery({runtime:async()=>({known:true,threadId:'main',rolloutPath:'private',active:false,nativeStatus:'idle'}),
  call:async(action,args)=>{calls.push([action,args.id]);if(action==='context-delivery-pending')return [{id:'old',session:'main',epoch:'before',marker:'old'}];if(action==='context-delivery-begin')return {...context,state:'sending'};if(action==='context-delivery-ack')return {...context,state:'accepted'};assert.fail(action);},
  find:async(file,marker)=>({found:marker==='new'&&appended}),inject:async()=>{appended=true;}});
 assert.equal((await delivery.deliver(context)).state,'accepted');assert.equal(calls.filter(x=>x[1]==='old').length,0);
});

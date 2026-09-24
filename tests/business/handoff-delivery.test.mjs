import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {deliverHandoff,deliveryIds} from '../../adapters/handoff-delivery.mjs';

const artifact=Buffer.from('Example artifact');
const entry=()=>({id:'task:example:4',state:'pending',title:'Example task',text:'Complete',kind:'completed',artifacts:[{path:'/example/note.txt',name:'note.txt',sha256:createHash('sha256').update(artifact).digest('hex')}]});

/** A sender with the real Feishu sender's three answers (T1-04): its receipt is
 * written before the platform can see anything, `submissionStarted` before the
 * request leaves, and a failure says which of the three it was. */
function sender({fail=()=>null}={}) {
  const receipts=new Map(),sent=[];let n=0;
  const send=async part=>{
    const previous=receipts.get(part.id);
    if(previous&&!(previous.state==='not-submitted'&&previous.submissionStarted===false))throw Object.assign(Error('reconcile first'),{code:'KIN_SEND_NEEDS_RECONCILE'});
    sent.push(part);
    const failure=fail(part,++n);
    if(failure==='before-submit'){receipts.set(part.id,{state:'not-submitted',submissionStarted:false});throw Error('offline');}
    if(failure==='timeout'){receipts.set(part.id,{state:'unconfirmed',submissionStarted:true});throw Error('timeout');}
    if(failure==='rejected'){receipts.set(part.id,{state:'rejected',submissionStarted:true});throw Object.assign(Error('refused'),{code:'PLATFORM_REJECTED'});}
    const receipt={state:'accepted',messageId:'om_'+part.id.slice(-6),submissionStarted:true};receipts.set(part.id,receipt);return receipt;
  };
  return {receipts,sent,send,lookup:async id=>receipts.get(id)??null};
}
const store=()=>{const states=[];return {states,settle:async(id,r)=>{states.push(r);return r;}};};

test('completion requires text and every file receipt; IDs survive a restart',async()=>{
  const item=entry(),s=sender(),st=store();
  const result=await deliverHandoff(item,{read:async()=>artifact,...st,send:s.send,lookup:s.lookup});
  assert.equal(result.state,'accepted');assert.equal(result.message_ids.length,2);
  const reconciled=await deliverHandoff({...item,state:'sending'},{read:async()=>artifact,...st,send:()=>{throw Error('must not replay');},lookup:s.lookup});
  assert.deepEqual(reconciled.message_ids,result.message_ids);
  assert.deepEqual(deliveryIds(item),[...s.receipts.keys()]);
});

test('changed artifacts stop all transmission before the first part',async()=>{
  const s=sender(),st=store();
  const result=await deliverHandoff(entry(),{...st,read:async()=>Buffer.from('Changed'),send:s.send,lookup:s.lookup});
  assert.equal(result.state,'failed');assert.equal(s.sent.length,0);
});

test('an unknown part stays unknown: looked up, never replayed, never given a new ID',async()=>{
  const item=entry(),s=sender({fail:(_part,n)=>n===2?'timeout':null}),st=store();
  const options={...st,read:async()=>artifact,send:s.send,lookup:s.lookup};
  assert.equal((await deliverHandoff(item,options)).state,'unconfirmed');
  assert.equal((await deliverHandoff({...item,state:'unconfirmed'},options)).state,'unconfirmed');
  assert.equal(s.sent.length,2);
});

test('a part proven never submitted goes out again under its own ID and completes the handoff',async()=>{
  let offline=true;
  const item=entry(),s=sender({fail:part=>offline&&part.media?'before-submit':null}),st=store();
  const options={...st,read:async()=>artifact,send:s.send,lookup:s.lookup};
  assert.equal((await deliverHandoff(item,options)).state,'unconfirmed');
  offline=false;
  const done=await deliverHandoff({...item,state:'unconfirmed'},options);
  assert.equal(done.state,'accepted');assert.equal(done.message_ids.length,2);
  const [text,file]=deliveryIds(item);
  assert.deepEqual(s.sent.map(p=>p.id),[text,file,file],'the text is never sent twice; the file keeps its ID');
});

test('a part the platform refused ends the handoff as failed, visibly',async()=>{
  const item=entry(),s=sender({fail:part=>part.media?'rejected':null}),st=store();
  const result=await deliverHandoff(item,{...st,read:async()=>artifact,send:s.send,lookup:s.lookup});
  assert.equal(result.state,'failed');assert.equal(result.message_ids.length,1);
  const again=await deliverHandoff({...item,state:'unconfirmed'},{...st,read:async()=>artifact,send:()=>assert.fail('never resent'),lookup:s.lookup});
  assert.equal(again.state,'failed');
});

test('a text past the channel limit goes whole as one file under the same ID',async()=>{
  const item={...entry(),artifacts:[],text:'长'.repeat(50000)},s=sender(),st=store();
  const result=await deliverHandoff(item,{...st,send:s.send,lookup:s.lookup});
  assert.equal(result.state,'accepted');
  assert.equal(s.sent[0].id,deliveryIds(item)[0]);assert.equal(s.sent[0].media.type,'file');
  assert.equal(s.sent[0].media.data.toString('utf8'),item.title+'\n'+item.text);
});

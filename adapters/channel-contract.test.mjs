import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {CHANNEL_CONTRACTS,channelContract,measureText,classifyReceipt,normalizeReceipt,mayResend,fileReceipts} from './channel-contract.mjs';
import {createFakeTransport,createFakePlatform} from './testing/fake-transport.mjs';
import {transportContractCases} from './testing/transport-contract.mjs';

test('channel limits are counted in the unit each platform counts',()=>{
  assert.deepEqual(CHANNEL_CONTRACTS.wechat.text,{limit:4000,measure:'utf16'});
  assert.deepEqual(CHANNEL_CONTRACTS.feishu.text,{limit:120*1024,measure:'json2-utf8'});
  assert.equal(measureText('utf16')('好a😀'),4);assert.equal(measureText('utf8')('好a😀'),8);
  // Inside a Feishu request the text is JSON inside a JSON string.
  const size=measureText('json2-utf8');
  assert.equal(size(''),0);assert.equal(size('好a'),4);assert.equal(size('\n'),3);assert.equal(size('"'),4);assert.equal(size('\\'),4);
  const text='第一行 "quoted"\n{"k":"v\\\\"}';
  assert.equal(size(text),Buffer.byteLength(JSON.stringify({content:JSON.stringify({text})}))-Buffer.byteLength(JSON.stringify({content:JSON.stringify({text:''})})));
  assert.equal(size('ab'+'好'),size('ab')+size('好'),'additive, so fragments can be measured piece by piece');
  assert.throws(()=>measureText('words'),/Unknown text measure/);assert.throws(()=>channelContract('carrier-pigeon'),/Unknown delivery channel/);
});

test('the Feishu idempotency facts are recorded, and automatic resend stays off until a caller injects it',()=>{
  const feishu=channelContract('feishu');
  assert.deepEqual([feishu.idempotencyKey,feishu.idempotencyWindowMs,feishu.duplicateResponse,feishu.resendWithinWindow,feishu.maxSendsPerSecond],['uuid',3600000,'undocumented',false,5]);
  assert.equal(channelContract('wechat').resendWithinWindow,false);assert.equal(channelContract('wechat').idempotencyWindowMs,0);
  const enabled=channelContract('feishu',{resendWithinWindow:true,text:{limit:500}});
  assert.equal(enabled.text.limit,500);assert.equal(enabled.text.measure,'json2-utf8');
  const first=1000000,inside=first+3600000-60001,outside=first+3600000-60000;
  assert.equal(mayResend(enabled,{firstSubmitAt:first},inside),true);
  assert.equal(mayResend(enabled,{firstSubmitAt:first},outside),false,'too close to the end of the window to be sure');
  assert.equal(mayResend(enabled,{},inside),false,'an unknown first attempt time never allows a resend');
  assert.equal(mayResend(enabled,{firstSubmitAt:first,resentAt:first+5},inside),false,'once only');
  assert.equal(mayResend(feishu,{firstSubmitAt:first},inside),false);
  assert.equal(mayResend(channelContract('wechat',{resendWithinWindow:true}),{firstSubmitAt:first},first+1),false,'no window, no resend');
});

test('the reconcile rule reads both channels\' receipts the same way',()=>{
  const cases=[
    [null,'absent'],[{},'absent'],
    [{state:'pending',submissionStarted:false},'never-started'],
    [{state:'not-submitted',stage:'upload-failed',submissionStarted:false},'never-started'],[{state:'not-submitted'},'never-started'],
    [{state:'pending',submissionStarted:true},'unknown'],[{state:'pending'},'unknown'],[{state:'not-submitted',submissionStarted:true},'unknown'],
    [{state:'unconfirmed',submissionStarted:true},'unknown'],[{state:'unreadable'},'unknown'],[{state:'accepted'},'unknown'],
    [{state:'accepted',messageId:'om_1'},'accepted'],[{state:'accepted',message_id:'om_1'},'accepted'],
    [{state:'accepted',responseText:'{"ret":0,"message_id":7441234567890123456}'},'accepted'],
    [{state:'rejected',reason:'api-230099'},'rejected'],
  ];
  for(const [receipt,expected] of cases)assert.equal(classifyReceipt(receipt),expected,JSON.stringify(receipt));
  assert.equal(normalizeReceipt({state:'accepted',responseText:'{"message_id":7441234567890123456}'}).messageId,'7441234567890123456');
  assert.deepEqual(normalizeReceipt({state:'unconfirmed',text:'private body',reason:'free text with spaces',checkedAt:'t'}),{state:'unconfirmed',checkedAt:'t'},'no message text, no free-form error text');
});

test('receipts are read by transport ID from the directories both channels use',async t=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-receipts-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  fs.writeFileSync(path.join(directory,'kin-frag-one.json'),JSON.stringify({id:'kin-frag-one',state:'accepted',messageId:'om_9',text:'body'}));
  fs.writeFileSync(path.join(directory,'kin-frag-torn.json'),'{"state":');
  const read=fileReceipts([path.join(directory,'absent'),directory]);
  assert.deepEqual(await read('kin-frag-one'),{state:'accepted',messageId:'om_9'});
  assert.equal(await read('kin-frag-none'),null);assert.equal(await read('../kin-frag-one'),null);
  assert.equal(classifyReceipt(await read('kin-frag-torn')),'unknown','an unreadable receipt is never read as "nothing was sent"');
});

const harnessDirectories=[];
after(()=>harnessDirectories.forEach(directory=>fs.rmSync(directory,{recursive:true,force:true})));
for(const flavour of ['feishu','wechat']) {
  const create=()=>{
    const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-contract-')),platform=createFakePlatform(),transport=createFakeTransport({directory,platform,flavour});
    harnessDirectories.push(directory);
    return {send:transport.send,receipt:transport.receipt,submissions:()=>platform.calls.length,delivered:()=>platform.delivered.length,platform:outcome=>platform.script(outcome),
      fault:outcome=>transport.fault(outcome),onSubmit:fn=>platform.onSubmit(fn),events:()=>transport.events,resend:flavour==='feishu',directory};
  };
  for(const contractCase of transportContractCases(create))test(`transport contract (${flavour}-shaped receipts): ${contractCase.name}`,contractCase.run);
}

test('the fake reports to memory unless told not to, like the real senders before memory:false',async t=>{
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'kin-contract-'));t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
  const transport=createFakeTransport({directory});
  await transport.send({id:'kin-frag-loud',text:'A public sentence.'});
  assert.deepEqual(transport.events.map(e=>e.state),['pending','accepted']);
});

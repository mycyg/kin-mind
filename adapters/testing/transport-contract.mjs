/** The contract every phone transport has to keep for the transport manifest
 * to be safe, as runnable cases. The manifest's duplicate guard is not a lock
 * and not a configuration flag: it is the transport writing its receipt, keyed
 * by transport ID, before it submits. Run these cases against a fake here and
 * against each real sender (with a fake platform client) in the host.
 *
 * `create()` returns a fresh harness:
 *   send(delivery)      the transport under test
 *   receipt(id)         its durable receipt for one transport ID, or null
 *   submissions()       how many requests reached the platform
 *   platform(outcome)   script the platform's next answer: reject | timeout | lost
 *   fault(outcome)      script a failure before the network: before-receipt | before-submit
 *   onSubmit(fn)        called when the platform sees a request
 *   events()            delivery events the transport emitted on its own
 *   resend              whether the transport honours `delivery.resend` */
import assert from 'node:assert/strict';
import {classifyReceipt,RECONCILE_FIRST_CODES} from '../channel-contract.mjs';

const message=(id,text='A public sentence.')=>({id,text,kind:'reply',memory:false});

export function transportContractCases(create) {
  return [
    {name:'the receipt is on disk before the platform sees the request',async run() {
      const h=await create();let atSubmit='never-submitted';
      h.onSubmit(async()=>{atSubmit=classifyReceipt(await h.receipt('kin-frag-a'));});
      const receipt=await h.send(message('kin-frag-a'));
      assert.notEqual(atSubmit,'absent');assert.notEqual(atSubmit,'never-started');assert.notEqual(atSubmit,'never-submitted');
      assert.equal(receipt.state,'accepted');assert.equal(classifyReceipt(await h.receipt('kin-frag-a')),'accepted');
    }},
    {name:'an accepted ID is answered from its receipt and never submitted twice',async run() {
      const h=await create(),first=await h.send(message('kin-frag-b')),again=await h.send(message('kin-frag-b'));
      assert.equal(h.submissions(),1);assert.equal(again.messageId??again.message_id,first.messageId??first.message_id);
    }},
    {name:'one ID never carries two contents',async run() {
      const h=await create();await h.send(message('kin-frag-c'));
      await assert.rejects(h.send(message('kin-frag-c','Another sentence.')));assert.equal(h.submissions(),1);
    }},
    {name:'an unknown outcome is recorded as unknown and never resubmitted on its own',async run() {
      const h=await create();h.platform('timeout');
      await assert.rejects(h.send(message('kin-frag-d')));
      assert.equal(classifyReceipt(await h.receipt('kin-frag-d')),'unknown');
      // The refusal names itself, so even a caller that cannot see this receipt learns the outcome is unknown.
      await assert.rejects(h.send(message('kin-frag-d')),error=>RECONCILE_FIRST_CODES.includes(error.code));assert.equal(h.submissions(),1);
    }},
    {name:'a definitive platform rejection is recorded as rejected, not as unknown',async run() {
      const h=await create();h.platform('reject');
      await assert.rejects(h.send(message('kin-frag-e')));
      assert.equal(classifyReceipt(await h.receipt('kin-frag-e')),'rejected');
      await assert.rejects(h.send(message('kin-frag-e')));assert.equal(h.submissions(),1);
    }},
    {name:'a failure before submission proves nothing was sent and may start over under the same ID',async run() {
      const h=await create();
      for(const fault of ['before-receipt','before-submit']) {
        const id='kin-frag-f-'+fault;h.fault(fault);
        await assert.rejects(h.send(message(id)));
        assert.ok(['absent','never-started'].includes(classifyReceipt(await h.receipt(id))));
        const before=h.submissions();
        assert.equal((await h.send(message(id))).state,'accepted');assert.equal(h.submissions(),before+1);
      }
    }},
    {name:'memory:false keeps the transport from reporting the fragment to memory',async run() {
      const h=await create();await h.send(message('kin-frag-g'));
      assert.deepEqual(h.events(),[]);
    }},
    {name:'an explicit resend reuses the idempotency key, so the platform delivers once',async run() {
      const h=await create();
      if(!h.resend)return;
      h.platform('timeout');await assert.rejects(h.send(message('kin-frag-h')));
      const receipt=await h.send({...message('kin-frag-h'),resend:{firstSubmitAt:0}});
      assert.equal(receipt.state,'accepted');assert.equal(h.submissions(),2);assert.equal(h.delivered(),1);
    }},
  ];
}

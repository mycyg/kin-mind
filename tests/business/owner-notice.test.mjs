import test from 'node:test';
import assert from 'node:assert/strict';
import {ownerNotice,isSystemNoticeId,OWNER_NOTICE_KINDS,SYSTEM_NOTICE_LABEL} from '../../adapters/owner-notice.mjs';

test('a host notice has fixed system words and one identity per subject, kind and scope',()=>{
  assert.deepEqual(OWNER_NOTICE_KINDS,['stopped','unknown','partial']);
  const notice=ownerNotice('partial','inj_feishu_1');
  assert.match(notice.id,/^kin-input-notice-[a-f0-9]{32}$/);
  assert.equal(notice.text,SYSTEM_NOTICE_LABEL+'刚才只送达了一部分，我会核对剩余内容；已送达的不会重发。');
  assert.equal(ownerNotice('partial','inj_feishu_1').id,notice.id);
  assert.notEqual(ownerNotice('unknown','inj_feishu_1').id,notice.id);
  assert.match(ownerNotice('partial','group-1',{scope:'reply'}).id,/^kin-reply-notice-/);
  assert.ok(isSystemNoticeId(notice.id)&&!isSystemNoticeId('kin-chat-'+'a'.repeat(48)));
  assert.throws(()=>ownerNotice('apology','inj_feishu_1'),/Unsupported/);
  assert.throws(()=>ownerNotice('stopped',''),/names what it is about/);
});

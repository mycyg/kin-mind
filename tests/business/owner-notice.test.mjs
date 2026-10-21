import test from 'node:test';
import assert from 'node:assert/strict';
import {ownerNotice,isSystemNoticeId,OWNER_NOTICE_KINDS,SYSTEM_NOTICE_LABEL} from '../../adapters/owner-notice.mjs';

test('a host notice has fixed system words and one identity per subject, kind and scope',()=>{
  assert.deepEqual(OWNER_NOTICE_KINDS,['stopped','unknown','partial','pending-input','format-failed']);
  assert.equal(ownerNotice('pending-input','synthetic').text,SYSTEM_NOTICE_LABEL+'刚才那条暂时没处理成功，消息还留着，不用重复发。');
  const notice=ownerNotice('partial','inj_feishu_1');
  assert.match(notice.id,/^kin-input-notice-[a-f0-9]{32}$/);
  assert.equal(notice.text,SYSTEM_NOTICE_LABEL+'刚才只送达了一部分，已送达的不会重发。');
  assert.equal(ownerNotice('partial','inj_feishu_1').id,notice.id);
  assert.notEqual(ownerNotice('unknown','inj_feishu_1').id,notice.id);
  assert.match(ownerNotice('partial','group-1',{scope:'reply'}).id,/^kin-reply-notice-/);
  assert.ok(isSystemNoticeId(notice.id)&&!isSystemNoticeId('kin-chat-'+'a'.repeat(48)));
  assert.throws(()=>ownerNotice('apology','inj_feishu_1'),/Unsupported/);
  assert.throws(()=>ownerNotice('stopped',''),/names what it is about/);
});

test('format failure is an explicit system notice with no generated content',()=>{
 const notice=ownerNotice('format-failed','synthetic-format');
 assert.equal(notice.text,SYSTEM_NOTICE_LABEL+'刚才这条回复格式处理失败，未能发出。原消息还在，不用重复发。');
 assert.equal(ownerNotice('format-failed','synthetic-format').id,notice.id);
});

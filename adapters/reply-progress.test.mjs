import test from 'node:test';
import assert from 'node:assert/strict';
import {replyProgress} from './reply-progress.mjs';
test('only matching whole reply or deliberate input choice resolves waiting',()=>{
 const old={awaitingReplyInputId:'new',awaitingReplySince:'2026-01-01T00:00:00Z'};
 for(const event of ['reply-complete','reply-choice','reply-review'])assert.equal(replyProgress(old,event,{inputId:'old',state:'accepted'}),null);
 assert.equal(replyProgress(old,'reply',{inputId:'new'}),null);
 assert.equal(replyProgress(old,'control-reply',{replyForAt:Date.now(),messageId:'notice'}).awaitingReplySince,undefined);
 assert.equal(replyProgress(old,'share-held',{inputId:'new',state:'pending',reason:'review'}).awaitingReplySince,undefined);
 assert.equal(replyProgress(old,'reply-complete',{inputId:'new'}).awaitingReplySince,null);
 assert.equal(replyProgress(old,'reply-choice',{inputId:'new',state:'silent'}).replyDisposition,'silent');
 assert.equal(replyProgress(old,'reply-choice',{inputId:'old',state:'silent'}),null);
});

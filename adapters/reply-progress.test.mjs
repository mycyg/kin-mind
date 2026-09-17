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
test('reply groups are tracked by their own ID: an older group stays visible while a newer input is awaited',()=>{
 const waiting={awaitingReplyInputId:'new',awaitingReplySince:'2026-01-01T00:00:00Z'};
 const older=replyProgress(waiting,'reply-review',{inputId:'old',groupId:'group-old',state:'canceled',groupState:'retired',reason:'input-or-session-superseded'},'2026-01-01T00:00:01Z');
 assert.deepEqual(older,{replyGroups:{'group-old':{state:'retired',reason:'input-or-session-superseded',inputId:'old',event:'reply-review',at:'2026-01-01T00:00:01Z'}}});
 assert.equal(older.awaitingReplySince,undefined,'it says nothing about the input that is being awaited now');
 const current=replyProgress({...waiting,...older},'reply-review',{inputId:'new',groupId:'group-new',state:'accepted',groupState:'accepted'},'2026-01-01T00:00:02Z');
 assert.equal(current.awaitingReplySince,null);assert.equal(current.replyDisposition,'accepted');
 assert.deepEqual(Object.keys(current.replyGroups),['group-old','group-new']);
 let status={...waiting};
 for(let i=0;i<40;i++)status={...status,...replyProgress(status,'reply-review',{inputId:'old',groupId:'group-'+String(i).padStart(2,'0'),state:'prepared',groupState:'sending'},new Date(Date.UTC(2026,0,1,0,0,i)).toISOString())};
 assert.equal(Object.keys(status.replyGroups).length,32);assert.ok(!status.replyGroups['group-00']&&status.replyGroups['group-39']);
});

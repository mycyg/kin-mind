import test from 'node:test';
import assert from 'node:assert/strict';
import {parsePrivateReply} from '../../adapters/private-notes.mjs';
const image={meme_id:'meme-fixture',inspection_id:'view-00000000-0000-4000-8000-000000000000'};
const reply=bubbles=>JSON.stringify({private_note:'synthetic hidden note',presentation:{mode:'text',style:''},action:'send',bubbles});
test('native inline viewed MEME is separated from text without exposing the private note',()=>{
 const result=parsePrivateReply(reply([{text:'图在这儿。',references:[{source_id:'synthetic'}],bubble_id:'text-1',image}]));
 assert.equal(result.state,'send');assert.deepEqual(result.bubbles,['图在这儿。','[表情图片]']);
 assert.deepEqual(result.meme_refs,[null,{type:'image',...image}]);
 assert.deepEqual(result.references[0],[{source_id:'synthetic'}]);assert.equal(result.reference_bubble_ids[0],'text-1');
 assert.ok(result.bubbles.every(b=>!b.includes('synthetic hidden note')));
});
test('ambiguous nested image shapes remain invalid',()=>{
 for(const bubble of [{text:'x',references:[],image:{...image,url:'https://example.invalid/image'}},
  {text:'x',references:[],image:{...image,inspection_id:'invented'}},
  {text:'x',references:[],private_note:'leak',image},
  {text:'',references:[],image},{text:'x',image},
  {text:'x',references:[],image:[image]}])assert.equal(parsePrivateReply(reply([bubble])).state,'invalid');
});

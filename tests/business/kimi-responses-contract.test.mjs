import test from 'node:test';
import assert from 'node:assert/strict';
import {kimiRequest,deepseekRequest} from '../../adapters/deepseek-gateway.mjs';

// Independent documented Responses constraints, not Chat Completions options:
// https://platform.kimi.com/docs/api/responses (checked 2026-10-10).
const documented = body => {
  assert(body.tool_choice === undefined || body.tool_choice === 'auto');
  assert((body.include ?? []).every(x => ['web_search_call.results','web_search_call.action.sources'].includes(x)));
};
const native = () => ({model:'k3',reasoning:{effort:'high'},stream:true,
  include:['reasoning.encrypted_content'],client_metadata:{thread_id:'synthetic'},
  prompt_cache_key:'synthetic',tool_choice:'auto',tools:[{type:'function',name:'lookup',parameters:{type:'object'}}],
  input:[{type:'message',role:'user',content:[{type:'input_text',text:'Synthetic parcel BLUE-17.'}]}]});

test('native encrypted include violates documented contract; Kimi adapter removes only that extension',()=>{
  const input=native(),before=structuredClone(input);
  assert.throws(()=>documented(input));
  const result=kimiRequest(input,'chat');documented(result);
  assert.equal(result.include,undefined);assert.equal(result.client_metadata,undefined);
  assert.deepEqual(result.input,input.input);assert.deepEqual(result.tools,input.tools);
  assert.equal(result.prompt_cache_key,'synthetic');assert.equal(result.stream,true);
  assert.deepEqual(result.reasoning,{effort:'high'});assert.deepEqual(input,before);
});
test('no-tools choice retains its meaning without sending unsupported none',()=>{
  const input={...native(),tool_choice:'none'};const result=kimiRequest(input,'chat');
  documented(result);assert.deepEqual(result.tools,[]);assert.equal(result.tool_choice,undefined);
  assert.equal(input.tools.length,1);
});
test('documented search includes, developer roles, images and paired tool history survive',()=>{
  const input=native();input.include=['reasoning.encrypted_content','web_search_call.action.sources'];
  input.input.push({type:'message',role:'developer',content:[{type:'input_text',text:'Synthetic instructions.'}]},
    {type:'message',role:'user',content:[{type:'input_image',image_url:'data:image/png;base64,c3ludGhldGlj'}]},
    {type:'function_call',name:'lookup',call_id:'test',arguments:'{}'},
    {type:'function_call_output',call_id:'test',output:'Synthetic result.'});
  const result=kimiRequest(input,'chat');documented(result);
  assert.deepEqual(result.include,['web_search_call.action.sources']);assert.deepEqual(result.input,input.input);
});
test('DeepSeek serialization stays unchanged and incompatible Kimi compaction still refuses',()=>{
  const input={...native(),model:'deepseek-flash'};const result=deepseekRequest(input);
  assert.deepEqual(result.include,input.include);assert.deepEqual(result.client_metadata,input.client_metadata);
  const invalid=native();invalid.input.push({type:'compaction',encrypted_content:'synthetic'});
  assert.throws(()=>kimiRequest(invalid));
});

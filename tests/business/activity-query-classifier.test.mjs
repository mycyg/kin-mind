import test from 'node:test';
import assert from 'node:assert/strict';
import {createMobileReviewer} from '../../adapters/mobile-reviewer.mjs';

test('the existing classifier transports a typed activity query in its one call',async()=>{
  const activity_query={kind:'dream',query:'',period:'last_night'};
  let calls=0;
  const reviewer=createMobileReviewer({key:'synthetic',fetchImpl:async(_url,options)=>{
    calls++;
    const body=JSON.parse(options.body),schema=body.tools[0].input_schema;
    assert.deepEqual(schema.properties.recall.properties.activity_query.properties.kind.enum,['dream','exploration']);
    assert.match(body.system,/不混入同轮晚饭/);
    return {ok:true,json:async()=>({stop_reason:'tool_use',content:[{type:'tool_use',name:'route_message',input:{route:'chat',reason:'ask stored dream',recall:{mode:'light',query:'昨晚做了什么梦，晚饭呢',reason:'stored activity',activity_query}}}]})};
  }});
  const result=await reviewer.classify({text:'昨晚梦到什么？我吃过晚饭啦'});
  assert.equal(calls,1);assert.deepEqual(result.recall.activity_query,activity_query);
});

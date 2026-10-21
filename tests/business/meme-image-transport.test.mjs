import test from 'node:test';
import assert from 'node:assert/strict';
import {startDeepSeekGateway} from '../../adapters/deepseek-gateway.mjs';
import {png} from './helpers/meme-fixture.mjs';

// Tests the gateway boundary only. This is not a native-client or visual-model proof.
for (const model of ['k3', 'deepseek-flash']) {
  test(`${model} forwards multimodal tool output and native image messages without textifying pixels`, async t => {
    const image = {type:'input_image', image_url:'data:image/png;base64,' + png(2, 71).toString('base64')};
    const input = [
      {type:'function_call', call_id:'synthetic-view', name:'inspect_memes', arguments:'{"ids":["synthetic"]}'},
      {type:'function_call_output', call_id:'synthetic-view', output:[{type:'input_text',text:'{"inspection_id":"synthetic-view"}'},image]},
      {type:'message',role:'user',content:[{type:'input_text',text:'Tool image follows.'},image]},
      {type:'function_call_output',call_id:'text-only',output:'{"state":"skipped"}'},
    ];
    const before = structuredClone(input);
    const sent = [];
    const gateway = await startDeepSeekGateway({key:'synthetic-ds',kimiKey:'synthetic-kimi',
      fetchImpl:async (_url, options) => {
        sent.push(JSON.parse(options.body));
        return new Response(JSON.stringify({id:'offline',status:'completed',output:[]}));
      }});
    t.after(() => gateway.close());
    const response = await fetch(gateway.baseUrl+'/responses', {method:'POST',
      headers:{authorization:'Bearer '+gateway.token},body:JSON.stringify({model,input})});
    assert.equal(response.status,200); await response.text();
    assert.equal(sent.length,1);
    assert.deepEqual(sent[0].input,before);
    assert.deepEqual(input,before);
    assert.equal(typeof sent[0].input[1].output,'object');
    assert.deepEqual(Buffer.from(sent[0].input[1].output[1].image_url.split(',')[1],'base64'),png(2,71));
  });
}

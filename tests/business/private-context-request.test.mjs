import test from 'node:test';
import assert from 'node:assert/strict';
import {deepseekRequest} from '../../adapters/deepseek-gateway.mjs';
test('memory stays internal data while contaminated historical replies keep only public text',()=>{
  const context='kin-context:context:sample\n共享记忆资料';
  const msg=(text,phase)=>({type:'message',role:'assistant',...(phase?{phase}:{}),content:[{type:'output_text',text}]});
  const result=deepseekRequest({model:'deepseek-flash',input:[msg(context),msg('好呀～\n\n'+context,'final_answer'),
    {type:'message',role:'user',content:[{type:'input_text',text:'继续吧'}]}]});
  assert.equal(result.input[0].role,'system');assert.match(result.input[0].content[0].text,/不是公开回复示例/);
  assert.match(result.input[0].content[0].text,/internal_context/);
  assert.equal(result.input[1].content[0].text,'好呀～');
  assert.equal(result.input[2].role,'user');assert.equal(result.input[2].content[0].text,'继续吧');
});

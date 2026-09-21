import test from 'node:test';
import assert from 'node:assert/strict';
import {patchCodexRuntime} from '../../adapters/codex-runtime-patch.mjs';

test('existing adapter upgrade retains full final text and its original input identity',async()=>{
 const source=`// KIN_MODEL_ROUTING_V2
 class Adapter {
 async kinLastReply(sessionId) {
   const turn=await this.read(sessionId),item=turn.items.find(i=>i.type==='agentMessage');
   return {turnId:turn.id,text: item?.text?.slice(-4000) ?? ""};
 }
 runtime(state) {return {fastMode: state.fastModeEnabled === true ? "on" : state.fastModeEnabled === false ? "off" : undefined};}
 }`;
 const upgraded=patchCodexRuntime(source),Adapter=Function(upgraded+';return Adapter')(),instance=new Adapter();
 instance.read=async()=>({id:'actual',items:[{type:'userMessage',content:[{type:'text',text:'[internal-assessment:original]'}]},{type:'agentMessage',text:'完整内容'.repeat(3000)}]});
 const receipt=await instance.kinLastReply('same');
 assert.equal(receipt.text.length,12000);assert.equal(receipt.inputText,'[internal-assessment:original]');
 assert.equal(patchCodexRuntime(upgraded),upgraded);
});

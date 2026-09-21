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

import {ownerSessionWork} from '../../adapters/mobile-session-host.mjs';
import {developerInstructionEvidence,instructionTextEvidence} from '../../adapters/instruction-evidence.mjs';
test('assessment does not claim foreground capacity but owner input still does',()=>{
 const session={processing:true,activeMessage:{contextToken:'assessment-1'},queue:[]};
 assert.equal(ownerSessionWork(session,[],'assessment-1'),false);
 assert.equal(ownerSessionWork({...session,queue:[{contextToken:'owner'}]},[],'assessment-1'),true);
 assert.equal(ownerSessionWork(session,[{id:'owner-work'}],'assessment-1'),true);
 assert.equal(ownerSessionWork({...session,activeMessage:{contextToken:'owner'}},[],'assessment-1'),true);
 assert.equal(ownerSessionWork(session,[],undefined),true);
});
test('native collaboration envelope retains the exact approved developer body',()=>{
 const approved='当前人格和二十三组范本。\n';
 const evidence=text=>developerInstructionEvidence({input:[{role:'developer',content:[{type:'input_text',text}]}]})[0];
 assert.deepEqual(evidence('<collaboration_mode>'+approved+'</collaboration_mode>'),instructionTextEvidence(approved));
 assert.notDeepEqual(evidence('前缀<collaboration_mode>'+approved+'</collaboration_mode>'),instructionTextEvidence(approved));
});

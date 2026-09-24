import test from 'node:test';
import assert from 'node:assert/strict';

// The V2 to V3 upgrade of an in-place patched ACP is gone with in-place patching;
// the owned entry's full-text last reply is tested in the host's mobile-owned-acp test.
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

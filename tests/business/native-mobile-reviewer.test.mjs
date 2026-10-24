import test from 'node:test';import assert from 'node:assert/strict';
import {createMobileReviewer} from '../../adapters/mobile-reviewer.mjs';
test('classification summary and health use the same native callback with existing validation',async()=>{
 const seen=[],sourceInput={id:'original-y-input',recipient_person_id:'person:yiyi'};
 const reviewer=createMobileReviewer({key:null,fetchImpl:()=>assert.fail('independent model API'),nativeRequest:async request=>{
  seen.push(request);const result=request.name==='route_message'?{route:'chat',reason:'casual',recall:{mode:'light',query:''}}:
   request.name==='summarize_open_work'?{summary:'Pending',delivered:[],open:['pending'],unsent:[],evidenceIds:['i']}:{status:'healthy',findings:[]};
  return {state:'complete',result,receipt:{native_session_id:'same-main'}};}});
 assert.equal((await reviewer.classify({text:'hello',availableModels:[]},{sourceInput})).route,'chat');
 assert.equal(seen[0].sourceInput,sourceInput);
 assert.equal((await reviewer.summarizeWork({inputs:[{id:'i'}]})).receipt.native_session_id,'same-main');
 assert.equal((await reviewer.audit({})).status,'healthy');assert.equal(seen.length,3);
 const busy=createMobileReviewer({nativeRequest:async()=>({state:'waiting',reason:'owner-work'})});
 await assert.rejects(busy.classify({text:'later',availableModels:[]}),e=>e.leaseSkipped===true);
});

test('isolated route classification cannot add unsent dialogue to main history or wait for its active turn',async()=>{
 const history=[],requests=[];let busy=false;
 const nativeRequest=async()=>{if(busy)return {state:'waiting',reason:'owner-work'};history.push('synthetic internal draft: one, two');return {state:'complete',result:{text:'one, two'}};};
 const input={text:'synthetic new image and follow-up',availableModels:[]};
 const old=createMobileReviewer({nativeRequest});
 await assert.rejects(old.classify(input),/invalid-classification/);assert.equal(history.length,1,'old internal draft is already in native history despite failed classification');
 busy=true;await assert.rejects(old.classify(input),e=>e.leaseSkipped===true);
 const isolated=createMobileReviewer({key:'synthetic-key',nativeRequest,isolatedClassification:true,fetchImpl:async(url,opts)=>{
  requests.push(JSON.parse(opts.body));return {ok:true,json:async()=>({model:'synthetic',id:'test',stop_reason:'tool_use',usage:{input_tokens:1,output_tokens:1},content:[{type:'tool_use',name:'route_message',input:{route:'chat',reason:'supplement',recall:{mode:'light',query:''}}}]})};
 }});
 for(const state of [true,false]){busy=state;assert.equal((await isolated.classify(input)).route,'chat');}
 assert.equal(history.length,1,'new classifications never enter native chat history');assert.equal(requests.length,2,'one classification request per input');
});

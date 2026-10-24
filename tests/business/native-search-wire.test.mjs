import test from 'node:test';import assert from 'node:assert/strict';
import {flattenExplorationTools} from '../../adapters/deepseek-gateway.mjs';
const member={type:'function',name:'read_mobile_runtime',parameters:{type:'object',properties:{}}};
const namespace={type:'namespace',name:'mcp__memorypalace',tools:[member]};
test('Kimi keeps native tool search query and result as paired ordinary function history',()=>{
 const call={type:'tool_search_call',call_id:'search1',execution:'client',status:'completed',arguments:{query:'runtime'}};
 const output={type:'tool_search_output',call_id:'search1',execution:'client',status:'completed',tools:[namespace]};
 const body={tools:[],input:[call,output,{type:'message',role:'user',content:'The toy cat is Bean.'}]};
 flattenExplorationTools(body,{chat:true,functionSearchHistory:true});
 assert.deepEqual(body.input[0],{type:'function_call',call_id:'search1',name:'tool_search',arguments:JSON.stringify(call.arguments)});
 assert.deepEqual(body.input[1],{type:'function_call_output',call_id:'search1',output:JSON.stringify({tools:output.tools})});
 assert.equal(body.input[2].content,'The toy cat is Bean.');assert.equal(call.type,'tool_search_call','native history is not mutated');
});
test('tool discovery supplies the namespace reverse mapping even when current tool list is flat',()=>{
 const body={tools:[{...member,name:'mcp__memorypalace__read_mobile_runtime'}],input:[{type:'tool_search_output',call_id:'search1',tools:[namespace]}]};
 const reverse=flattenExplorationTools(body,{chat:true});
 assert.deepEqual(reverse.get('mcp__memorypalace__read_mobile_runtime'),{namespace:'mcp__memorypalace',name:'read_mobile_runtime'});
 assert.equal(body.tools.length,1);assert.equal(body.input[0].type,'tool_search_output');
});

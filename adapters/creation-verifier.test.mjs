import test from 'node:test';
import assert from 'node:assert/strict';
import {CreationVerifier} from './creation-verifier.mjs';
test('missing renderer is a check gap and preserves existing production bytes',async()=>{
 const v=new CreationVerifier({playwrightModule:'/missing-kin-renderer'});
 const result={state:'produced',receipt:{workspace:'/tmp/isolated'},artifacts:[{path:'/tmp/isolated/card.html',sha256:'source',bytes:100}]};
 const checked=await v.verify(result);
 assert.equal(checked.host_verification[0].state,'unavailable');
 assert.deepEqual(checked.artifacts,result.artifacts);assert.equal(checked.state,'produced');
 assert.equal(v.capabilities().network,false);assert.equal(v.capabilities().scripts,false);
});
test('an interrupted creator cannot start optional rendering',async()=>{
 const v=new CreationVerifier({playwrightModule:'/missing-kin-renderer',spawnImpl:()=>{throw Error('must not launch');}});
 assert.deepEqual(await v.verify({state:'interrupted'}),{state:'interrupted'});
 const result={state:'produced',receipt:{workspace:'/tmp/isolated'},artifacts:[{path:'/tmp/isolated/card.html'}]};
 assert.deepEqual((await v.verify(result,{signal:AbortSignal.abort()})).host_verification,[]);
});

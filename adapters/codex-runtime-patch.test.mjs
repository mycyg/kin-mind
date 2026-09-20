import test from 'node:test';
import assert from 'node:assert/strict';
import {patchCodexRuntime} from './codex-runtime-patch.mjs';

const V1='// KIN_MODEL_ROUTING_V1';
const V2='// KIN_MODEL_ROUTING_V2';
const OLD='fastMode: value("fast-mode")';
const CURRENT='fastMode: state.fastModeEnabled === true ? "on" : state.fastModeEnabled === false ? "off" : undefined';

const vendorFixture=`
class CodexAcpServer {
  constructor(state) {
    this.sessions = new Map([[state.sessionId, state]]);
    this.activePrompts = new Map();
    this.pendingTurnStarts = new Map();
    this.codexAcpClient = {
      appServerClient: {
        threadTurnsList: async () => ({data: []}),
        threadRead: async () => ({thread: {id: state.sessionId, sessionId: state.sessionId, status: {type: "idle"}}}),
        threadBackgroundTerminalsList: async () => ({data: [], nextCursor: null}),
      },
      listProviders: () => [{current: null}],
      getCurrentModelProvider: async () => "custom-gateway",
      setProvider() {},
      disableProvider() {},
      gatewayConfig: null,
    };
  }
  createSessionConfigOptions(state) {
    return [
      {id: "model", currentValue: state.currentModelId},
      {id: "reasoning_effort", currentValue: "high"},
    ];
  }
  async extMethod(method, params) {
    const methodRequest = { method, params };
    return methodRequest;
  }
  async setProvider(params) {
    this.codexAcpClient.setProvider(params);
  }
  async disableProvider(params) {
    this.codexAcpClient.disableProvider(params);
  }
}
const emptyExtensionParamsParser = {};
const external_exports = {object: value => value, string: () => ({})};
const getAgent = () => null;
const router = {onRequest() { return this; }};
router.onRequest("authentication/status", emptyExtensionParamsParser, () => null);
`;

const loadFixture=source=>new Function(source+'\nreturn CodexAcpServer;')();
const state=()=>({sessionId:'deepseek-thread',currentModelId:'deepseek-flash',fastModeEnabled:false,lastTokenUsage:null,totalTokenUsage:null});

test('runtime reads the durable session Fast preference when the current model has no Fast UI option',async()=>{
  const patched=patchCodexRuntime(vendorFixture),Session=loadFixture(patched),sessionState=state(),server=new Session(sessionState);
  assert.ok(patched.includes(V2));assert.ok(patched.includes(CURRENT));assert.ok(!patched.includes(OLD));
  const ordinary=await server.kinRuntime(sessionState.sessionId);
  assert.equal(ordinary.fastMode,'off');assert.equal(Object.hasOwn(ordinary,'serviceTier'),false,'configured preference is not billed-tier evidence');
  sessionState.fastModeEnabled=true;
  assert.equal((await server.kinRuntime(sessionState.sessionId)).fastMode,'on');
  delete sessionState.fastModeEnabled;
  assert.equal((await server.kinRuntime(sessionState.sessionId)).fastMode,undefined,'missing native state stays unknown');
});

test('an installed V1 patch upgrades once and the V2 result is idempotent',async()=>{
  const v2=patchCodexRuntime(vendorFixture),v1=v2.replace(V2,V1).replace(CURRENT,OLD),upgraded=patchCodexRuntime(v1);
  assert.ok(upgraded.includes(V2));assert.ok(!upgraded.includes(V1));assert.ok(upgraded.includes(CURRENT));assert.ok(!upgraded.includes(OLD));
  assert.equal(patchCodexRuntime(upgraded),upgraded);
  const Session=loadFixture(upgraded),sessionState=state();
  assert.equal((await new Session(sessionState).kinRuntime(sessionState.sessionId)).fastMode,'off');
});

test('a contradictory V2 marker fails closed instead of trusting a partial installation',()=>{
  const inconsistent=patchCodexRuntime(vendorFixture).replace(CURRENT,OLD);
  assert.throws(()=>patchCodexRuntime(inconsistent),/marker is inconsistent/);
});

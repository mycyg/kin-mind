import fs from 'node:fs';
import {createHash} from 'node:crypto';
const marker = '// KIN_MODEL_ROUTING_V1';

const runtimeMethods = `
  async kinLastReply(sessionId) {
    if (!this.sessions.has(sessionId)) return { known: false };
    const page = await this.codexAcpClient.appServerClient.threadTurnsList({threadId: sessionId, limit: 1, sortDirection: "desc", itemsView: "full"});
    const turn = page.data[0];
    const messages = (turn?.items ?? []).filter(item => item.type === "agentMessage");
    const item = messages.findLast(item => item.phase === "final_answer") ?? messages.at(-1);
    return {known: true, turnId: turn?.id, status: turn?.status, text: item?.text?.slice(-4000) ?? ""};
  }
  async kinRuntime(sessionId) {
    const state = this.sessions.get(sessionId);
    if (!state) return { known: false, reason: "session-not-loaded", sessionId };
    try {
      const api = this.codexAcpClient.appServerClient;
      const thread = (await api.threadRead({ threadId: sessionId, includeTurns: false })).thread;
      let backgroundTasks = 0, cursor = null;
      const seen = new Set();
      do {
        const page = await api.threadBackgroundTerminalsList({ threadId: sessionId, cursor });
        backgroundTasks += page.data.length;
        cursor = page.nextCursor;
        if (cursor && seen.has(cursor)) throw new Error("Repeated terminal cursor");
        seen.add(cursor);
      } while (cursor);
      const options = this.createSessionConfigOptions(state);
      const value = id => options.find(o => o.id === id)?.currentValue;
      const provider = this.codexAcpClient.listProviders()[0]?.current;
      return { known: true, sessionId, threadId: thread.id, nativeSessionId: thread.sessionId,
        active: this.activePrompts.has(sessionId) || this.pendingTurnStarts.has(sessionId) || thread.status?.type === "active",
        nativeStatus: thread.status?.type, backgroundTasks,
        model: value("model"), reasoningEffort: value("reasoning_effort"), fastMode: value("fast-mode"),
        modelProvider: await this.codexAcpClient.getCurrentModelProvider(sessionId),
        providerBaseUrl: provider?.baseUrl, providerOverride: this.codexAcpClient.gatewayConfig !== null,
        lastTokenUsage: state.lastTokenUsage, totalTokenUsage: state.totalTokenUsage,
        checkedAt: new Date().toISOString() };
    } catch { return { known: false, sessionId, reason: "native-runtime-unavailable" }; }
  }
  async kinAssertProviderIdle() {
    for (const sessionId of this.sessions.keys()) {
      const state = await this.kinRuntime(sessionId);
      if (!state.known || state.active || state.backgroundTasks || state.nativeStatus !== "idle") {
        throw new Error("KIN_PROVIDER_BUSY_OR_UNCONFIRMED");
      }
    }
  }
`;

export function patchCodexRuntime(source) {
  if (source.includes(marker)) return source;
  const replacements = [
    ['  async extMethod(method, params) {\n    const methodRequest = { method, params };',
      runtimeMethods + '\n  async extMethod(method, params) {\n    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);\n    if (method === "_kin/last-reply") return await this.kinLastReply(params.sessionId);\n    const methodRequest = { method, params };'],
    ['  async setProvider(params) {\n    this.codexAcpClient.setProvider(params);',
      '  async setProvider(params) {\n    await this.kinAssertProviderIdle();\n    this.codexAcpClient.setProvider(params);'],
    ['  async disableProvider(params) {\n    this.codexAcpClient.disableProvider(params);',
      '  async disableProvider(params) {\n    await this.kinAssertProviderIdle();\n    this.codexAcpClient.disableProvider(params);'],
    ['.onRequest("authentication/status", emptyExtensionParamsParser,',
      '.onRequest("_kin/runtime", external_exports.object({sessionId: external_exports.string()}), (ctx) => getAgent().extMethod("_kin/runtime", ctx.params)).onRequest("_kin/last-reply", external_exports.object({sessionId: external_exports.string()}), (ctx) => getAgent().extMethod("_kin/last-reply", ctx.params)).onRequest("authentication/status", emptyExtensionParamsParser,'],
  ];
  for (const [before, after] of replacements) {
    if (source.split(before).length !== 2) throw Error('Codex ACP runtime needs compatibility review');
    source = source.replace(before, after);
  }
  const offset = source.startsWith('#!') ? source.indexOf('\n') + 1 : 0;
  return source.slice(0,offset) + marker + '\n' + source.slice(offset);
}

export function installCodexRuntimePatch(file) {
  const original = fs.readFileSync(file,'utf8'), patched = patchMemoryRuntime(patchCodexRuntime(original));
  if (original === patched) return;
  const hash = createHash('sha256').update(original).digest('hex').slice(0,16);
  const backup = file + '.kin-routing-backup-' + hash;
  if (!fs.existsSync(backup)) fs.writeFileSync(backup, original, {mode:0o600,flag:'wx'});
  const temporary = file + '.kin-routing-' + process.pid;
  fs.writeFileSync(temporary, patched, {mode:fs.statSync(file).mode & 0o777});
  fs.renameSync(temporary,file);
}

/** Private ACP extension: run native compaction without a synthetic user turn. */
export function patchMemoryRuntime(source) {
  const marker='// KIN_MEMORY_COMPACTION_V1';
  if(source.includes(marker))return source;
  const method=`
  async kinCompact(sessionId, operationId, checkOnly = false) {
    const state = this.sessions.get(sessionId);
    if (state?.kinCompactionReceipt?.operationId === operationId) return state.kinCompactionReceipt;
    if (checkOnly) return {completed: false, operationId, actual_session: sessionId};
    if (state?.kinCompactionOperation === operationId) throw new Error("KIN_COMPACTION_UNCONFIRMED");
    const before = await this.kinRuntime(sessionId);
    if (!before.known || before.active || before.backgroundTasks || before.nativeStatus !== "idle") throw new Error("KIN_COMPACTION_BUSY");
    state.kinCompactionOperation = operationId;
    await this.codexAcpClient.runCompact(sessionId);
    const after = await this.kinRuntime(sessionId);
    if (!after.known || after.threadId !== sessionId || after.nativeSessionId !== sessionId || after.model !== before.model) throw new Error("KIN_COMPACTION_UNCONFIRMED");
    state.kinCompactionReceipt = {completed: true, operationId, actual_session: sessionId, model: after.model, at: new Date().toISOString()};
    return state.kinCompactionReceipt;
  }
`;
  const entry='  async kinLastReply(sessionId) {';
  const dispatch='    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);';
  const register='.onRequest("_kin/runtime",';
  for(const value of [entry,dispatch,register])if(source.split(value).length!==2)throw Error('ACP compaction extension needs compatibility review');
  source=source.replace(entry,method+'\n'+entry)
    .replace(dispatch,dispatch+'\n    if (method === "_kin/compact") return await this.kinCompact(params.sessionId, params.operationId, params.checkOnly);')
    .replace(register,'.onRequest("_kin/compact", external_exports.object({sessionId: external_exports.string(),operationId: external_exports.string(),checkOnly: external_exports.boolean().optional()}), (ctx) => getAgent().extMethod("_kin/compact", ctx.params))'+register);
  source=source.replace('text: item?.text?.slice(-4000) ?? ""','text: item?.text ?? ""');
  const offset=source.startsWith('#!')?source.indexOf('\n')+1:0;
  return source.slice(0,offset)+marker+'\n'+source.slice(offset);
}

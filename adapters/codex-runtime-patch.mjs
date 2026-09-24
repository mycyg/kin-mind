/** Kin's private extensions to a pristine codex-acp entry. Each function is a pure
 * string patch applied once while an owned ACP artifact is generated; nothing here
 * edits an installed package. Every anchor must match exactly once, so an upstream
 * change stops the build for review instead of patching the wrong place. */
const marker = '// KIN_MODEL_ROUTING_V3';
const lastReplyMarker = '// KIN_LAST_REPLY_CACHE_V1';
const compactionMarker = '// KIN_MEMORY_COMPACTION_V1';
const compactionReceiptMarker = '// KIN_COMPACTION_RECEIPT_V1';
const sessionMarker = '// KIN_SESSION_CONTINUITY_V1';
const inputIdentityMarker = '// KIN_INPUT_IDENTITY_V1';
const assessmentMarker = '// KIN_ASSESS_V1';
const retriesMarker = '// KIN_GATEWAY_RETRIES_V1';
const utf8Marker = '// KIN_UTF8_READER_V1';
export const KIN_OWNED_ACP_MARKERS = Object.freeze([marker, lastReplyMarker, compactionMarker, compactionReceiptMarker,
  sessionMarker, inputIdentityMarker, assessmentMarker, retriesMarker, utf8Marker]);
const sessionFastMode = 'fastMode: state.fastModeEnabled === true ? "on" : state.fastModeEnabled === false ? "off" : undefined';
const handlerAnchor = 'var CodexEventHandler = class _CodexEventHandler {';

function replaceOnce(source, before, after, label) {
  if (source.split(before).length !== 2) throw Error(`${label} needs compatibility review`);
  return source.replace(before, () => after);
}
function mark(source, value) {
  if (source.includes(value)) throw Error(`Codex ACP source already carries ${value}`);
  const offset = source.startsWith('#!') ? source.indexOf('\n') + 1 : 0;
  return source.slice(0, offset) + value + '\n' + source.slice(offset);
}
const helpers = (source, text, label) => replaceOnce(source, handlerAnchor, text + handlerAnchor, label);

// The ACP sees every public item of the turns it runs; the newest reply is kept
// here, so the host never makes the native side replay history to read it. A turn
// without messages (a compaction) leaves the last reply in place.
const replyHelpers = `function kinOwnThread(state, threadId) {
  return !threadId || threadId === state.sessionId;
}
function kinReplyTurn(state, turnId) {
  if (!turnId) return null;
  if (state.kinReply?.turnId !== turnId) state.kinReply = { turnId, status: state.kinTurn?.turnId === turnId ? state.kinTurn.status : "inProgress", final: null, last: null, inputs: [] };
  return state.kinReply;
}
function kinRememberMessage(state, event) {
  if (!kinOwnThread(state, event.threadId)) return;
  const reply = kinReplyTurn(state, event.turnId);
  if (!reply) return;
  const text = typeof event.item?.text === "string" ? event.item.text : "";
  reply.last = text;
  if (event.item?.phase === "final_answer") reply.final = text;
}
function kinRememberInput(state, event) {
  if (!kinOwnThread(state, event.threadId)) return;
  const reply = kinReplyTurn(state, event.turnId);
  if (!reply) return;
  for (const part of event.item?.content ?? []) if (part?.type === "text" && typeof part.text === "string") reply.inputs.push(part.text);
  reply.inputs = reply.inputs.slice(-20);
}
function kinTurnStarted(state, params) {
  if (!kinOwnThread(state, params?.threadId) || !params?.turn?.id) return;
  state.kinTurn = { turnId: params.turn.id, status: "inProgress" };
}
function kinFinishTurn(state, params) {
  const turn = params?.turn;
  if (!kinOwnThread(state, params?.threadId) || !turn?.id) return;
  state.kinTurn = { turnId: turn.id, status: turn.status ?? "completed" };
  if (state.kinReply?.turnId === turn.id) state.kinReply.status = state.kinTurn.status;
  if (state.kinTurn.status !== "inProgress") state.kinLastCompletedTurnId = turn.id;
}
`;

const runtimeMethods = `
  async kinLastReply(sessionId) {
    const state = this.sessions.get(sessionId);
    if (!state) return { known: false };
    const turn = state.kinTurn, reply = state.kinReply;
    if (turn?.status === "inProgress" && reply?.turnId !== turn.turnId) return { known: true, source: "acp", turnId: turn.turnId, status: "inProgress", text: "", inputText: "" };
    if (reply) return { known: true, source: "acp", turnId: reply.turnId, status: reply.status, text: reply.final ?? reply.last ?? "", inputText: reply.inputs.join("\\n") };
    // A restarted ACP has seen no turn yet: read the newest native turn, one only.
    const page = await this.codexAcpClient.appServerClient.threadTurnsList({threadId: sessionId, limit: 1, sortDirection: "desc", itemsView: "full"});
    const last = page.data[0];
    const messages = (last?.items ?? []).filter(item => item.type === "agentMessage");
    const item = messages.findLast(item => item.phase === "final_answer") ?? messages.at(-1);
    return {known: true, source: "native", turnId: last?.id, status: last?.status, text: item?.text ?? "", inputText: (last?.items ?? []).filter(item => item.type === "userMessage").flatMap(item => item.content ?? []).filter(part => part.type === "text").map(part => part.text).join("\\n")};
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
      return { known: true, sessionId, threadId: thread.id, nativeSessionId: thread.sessionId, rolloutPath: thread.path,
        active: this.activePrompts.has(sessionId) || this.pendingTurnStarts.has(sessionId) || thread.status?.type === "active",
        nativeStatus: thread.status?.type, backgroundTasks,
        model: value("model"), reasoningEffort: value("reasoning_effort"), ${sessionFastMode},
        serviceTier: null, serviceTierVerified: false,
        modelProvider: await this.codexAcpClient.getCurrentModelProvider(sessionId),
        providerBaseUrl: provider?.baseUrl, providerOverride: this.codexAcpClient.gatewayConfig !== null,
        modelContextWindow: state.modelContextWindow, lastTokenUsage: state.lastTokenUsage, totalTokenUsage: state.totalTokenUsage,
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

/** `_kin/runtime`, `_kin/last-reply` and idle-checked provider switching. The last
 * reply comes from what this ACP process saw; only a restarted ACP asks the native
 * side, for one turn. The actual service tier is not observable here: it is
 * reported as unknown, never inferred from the Fast preference. */
export function patchCodexRuntime(source) {
  for (const value of [marker, lastReplyMarker]) if (source.includes(value)) throw Error(`Codex ACP source already carries ${value}`);
  source = helpers(source, replyHelpers, 'ACP reply cache');
  source = replaceOnce(source, '  async extMethod(method, params) {\n    const methodRequest = { method, params };',
    runtimeMethods + '\n  async extMethod(method, params) {\n    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);\n    if (method === "_kin/last-reply") return await this.kinLastReply(params.sessionId);\n    const methodRequest = { method, params };', 'ACP runtime extension');
  source = replaceOnce(source, '  async setProvider(params) {\n    this.codexAcpClient.setProvider(params);',
    '  async setProvider(params) {\n    await this.kinAssertProviderIdle();\n    this.codexAcpClient.setProvider(params);', 'ACP provider switch');
  source = replaceOnce(source, '  async disableProvider(params) {\n    this.codexAcpClient.disableProvider(params);',
    '  async disableProvider(params) {\n    await this.kinAssertProviderIdle();\n    this.codexAcpClient.disableProvider(params);', 'ACP provider disable');
  source = replaceOnce(source, '.onRequest("authentication/status", emptyExtensionParamsParser,',
    '.onRequest("_kin/runtime", external_exports.object({sessionId: external_exports.string()}), (ctx) => getAgent().extMethod("_kin/runtime", ctx.params)).onRequest("_kin/last-reply", external_exports.object({sessionId: external_exports.string()}), (ctx) => getAgent().extMethod("_kin/last-reply", ctx.params)).onRequest("authentication/status", emptyExtensionParamsParser,', 'ACP extension registration');
  source = replaceOnce(source, '        return this.subagents.legacyCollaborationCompleted(event.item);\n      case "agentMessage":\n        this.rememberAgentMessagePhase(event.item);\n        return null;',
    '        return this.subagents.legacyCollaborationCompleted(event.item);\n      case "agentMessage":\n        this.rememberAgentMessagePhase(event.item);\n        kinRememberMessage(this.sessionState, event);\n        return null;', 'ACP completed reply');
  source = replaceOnce(source, '      case "sleep":\n      case "functionCallOutput":\n      case "userMessage":\n      case "hookPrompt":\n      case "enteredReviewMode":\n        return null;\n    }\n  }\n  rememberAgentMessagePhase(item) {',
    '      case "userMessage":\n        kinRememberInput(this.sessionState, event);\n        return null;\n      case "sleep":\n      case "functionCallOutput":\n      case "hookPrompt":\n      case "enteredReviewMode":\n        return null;\n    }\n  }\n  rememberAgentMessagePhase(item) {', 'ACP completed input');
  source = replaceOnce(source, '      case "turn/started":\n        this.sessionState.currentTurnId = notification.params.turn.id;',
    '      case "turn/started":\n        this.sessionState.currentTurnId = notification.params.turn.id;\n        kinTurnStarted(this.sessionState, notification.params);', 'ACP turn start');
  source = replaceOnce(source, '      case "turn/completed":\n        await this.flushPendingPlanUpdates();\n        this.clearPlanTurnState();\n        this.sessionState.currentTurnId = null;',
    '      case "turn/completed":\n        await this.flushPendingPlanUpdates();\n        this.clearPlanTurnState();\n        this.sessionState.currentTurnId = null;\n        kinFinishTurn(this.sessionState, notification.params);', 'ACP turn completion');
  return mark(mark(source, lastReplyMarker), marker);
}

const boundHelpers = `function kinWithin(promise, deadline, code) {
  void promise.catch(() => {});
  const remaining = deadline - Date.now();
  if (remaining <= 0) return Promise.reject(new Error(code));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(code)), remaining);
    timer.unref?.();
    promise.then((value) => { clearTimeout(timer); resolve(value); }, (error) => { clearTimeout(timer); reject(error); });
  });
}
`;

/** `_kin/compact`: native compaction without a synthetic user turn. It always
 * ends: completed, failed (the compaction turn failed), timeout or unknown, and
 * that receipt stays readable through checkOnly. */
export function patchMemoryRuntime(source) {
  for (const value of [compactionMarker, compactionReceiptMarker]) if (source.includes(value)) throw Error(`Codex ACP source already carries ${value}`);
  const method = `
  async kinCompact(sessionId, operationId, checkOnly = false, timeoutMs = 600000) {
    const state = this.sessions.get(sessionId);
    if (!state) return {completed: false, state: "unknown", reason: "session-not-loaded", operationId, actual_session: sessionId};
    const receipt = state.kinCompactionReceipts?.get(operationId);
    if (receipt) return receipt;
    if (checkOnly) return state.kinCompactionOperation === operationId ? {completed: false, state: "running", operationId, actual_session: sessionId} : {completed: false, operationId, actual_session: sessionId};
    if (state.kinCompactionOperation) throw new Error(state.kinCompactionOperation === operationId ? "KIN_COMPACTION_UNCONFIRMED" : "KIN_COMPACTION_BUSY");
    const before = await this.kinRuntime(sessionId);
    if (!before.known || before.active || before.backgroundTasks || before.nativeStatus !== "idle") throw new Error("KIN_COMPACTION_BUSY");
    state.kinCompactionOperation = operationId;
    const deadline = Date.now() + Math.min(Math.max(Number(timeoutMs) || 600000, 1000), 1800000);
    const record = (value) => {
      const receipts = state.kinCompactionReceipts ??= new Map();
      receipts.set(operationId, {...value, operationId, actual_session: sessionId, at: new Date().toISOString()});
      while (receipts.size > 20) receipts.delete(receipts.keys().next().value);
      return receipts.get(operationId);
    };
    let release = () => {};
    try {
      const failed = new Promise((_, reject) => {
        release = this.codexAcpClient.appServerClient.captureTurnCompletions(sessionId, (event) => {
          if (event.turn?.status && event.turn.status !== "completed") reject(new Error("KIN_COMPACTION_FAILED"));
        });
      });
      await kinWithin(Promise.race([this.codexAcpClient.runCompact(sessionId), failed]), deadline, "KIN_COMPACTION_TIMEOUT");
      let after;
      do {
        after = await kinWithin(this.kinRuntime(sessionId), deadline, "KIN_COMPACTION_TIMEOUT");
        if (after.known && !after.active && !after.backgroundTasks && after.nativeStatus === "idle") break;
        await new Promise((resolve) => setTimeout(resolve, 100));
      } while (Date.now() < deadline);
      if (!after?.known || after.active || after.backgroundTasks || after.nativeStatus !== "idle" || after.threadId !== sessionId || after.nativeSessionId !== sessionId || after.model !== before.model)
        return record({completed: false, state: "unknown", reason: "KIN_COMPACTION_UNCONFIRMED"});
      return record({completed: true, state: "completed", model: after.model});
    } catch (error) {
      const reason = String(error?.message ?? error).slice(0, 200);
      const outcome = reason === "KIN_COMPACTION_FAILED" ? "failed" : reason === "KIN_COMPACTION_TIMEOUT" ? "timeout" : "unknown";
      return record({completed: false, state: outcome, failed: outcome === "failed", reason});
    } finally {
      release();
      state.kinCompactionOperation = null;
    }
  }
`;
  source = helpers(source, boundHelpers, 'ACP compaction bound');
  source = replaceOnce(source, '  async kinLastReply(sessionId) {', method + '\n  async kinLastReply(sessionId) {', 'ACP compaction extension');
  source = replaceOnce(source, '    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);',
    '    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);\n    if (method === "_kin/compact") return await this.kinCompact(params.sessionId, params.operationId, params.checkOnly, params.timeoutMs);', 'ACP compaction dispatch');
  source = replaceOnce(source, '.onRequest("_kin/runtime",',
    '.onRequest("_kin/compact", external_exports.object({sessionId: external_exports.string(),operationId: external_exports.string(),checkOnly: external_exports.boolean().optional(),timeoutMs: external_exports.number().optional()}), (ctx) => getAgent().extMethod("_kin/compact", ctx.params)).onRequest("_kin/runtime",', 'ACP compaction registration');
  return mark(mark(source, compactionReceiptMarker), compactionMarker);
}

/** Checkpoint injection, session retirement and runtime notices. Warnings are
 * events, never assistant prose; injection is limited to public message items. */
export function patchSessionRuntime(source) {
  if (source.includes(sessionMarker)) throw Error(`Codex ACP source already carries ${sessionMarker}`);
  const methods = `
  async kinInjectCheckpoint(params) {
    const before = await this.kinRuntime(params.sessionId);
    if (!before.known || before.active || before.backgroundTasks || before.nativeStatus !== "idle") throw new Error("KIN_INJECTION_BUSY");
    const items = params.items;
    if (JSON.stringify(items).length > 200000 || !items.length || items.some(i => i.type !== "message" || !["assistant", "user"].includes(i.role) || !Array.isArray(i.content) || i.content.some(p => !["input_text", "output_text"].includes(p.type) || typeof p.text !== "string"))) throw new Error("KIN_PUBLIC_CHECKPOINT_REQUIRED");
    const receipt = await this.codexAcpClient.appServerClient.sendRequest({method: "thread/inject_items", params: {threadId: params.sessionId, items}});
    return {accepted: true, operationId: params.operationId, sessionId: params.sessionId, receipt};
  }
  async kinRetireSession(sessionId) {
    const runtime = await this.kinRuntime(sessionId);
    if (!runtime.known && runtime.reason === "session-not-loaded") return {retired: true, loaded: false};
    if (!runtime.known) throw new Error("KIN_SESSION_UNCONFIRMED");
    if (runtime.active || runtime.backgroundTasks) throw new Error("KIN_SESSION_BUSY");
    await this.codexAcpClient.closeSession(sessionId);
    this.sessions.delete(sessionId);
    return {retired: true, loaded: true};
  }
`;
  const notice = `function kinRuntimeNoticeUpdate(kind, message) {
  return {sessionUpdate: "session_info_update", _meta: {kinRuntimeNotice: {id: randomUUID(), kind, at: new Date().toISOString(), message}}};
}
`;
  source = helpers(source, notice, 'ACP runtime notice');
  source = replaceOnce(source, '  async kinLastReply(sessionId) {', methods + '\n  async kinLastReply(sessionId) {', 'ACP session extension');
  source = replaceOnce(source, '    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);',
    '    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);\n    if (method === "_kin/inject-checkpoint") return await this.kinInjectCheckpoint(params);\n    if (method === "_kin/retire-session") return await this.kinRetireSession(params.sessionId);', 'ACP session dispatch');
  source = replaceOnce(source, '.onRequest("_kin/runtime",',
    '.onRequest("_kin/inject-checkpoint", external_exports.object({sessionId:external_exports.string(),operationId:external_exports.string(),items:external_exports.array(external_exports.unknown())}), (ctx) => getAgent().extMethod("_kin/inject-checkpoint",ctx.params)).onRequest("_kin/retire-session", external_exports.object({sessionId:external_exports.string()}), (ctx) => getAgent().extMethod("_kin/retire-session",ctx.params)).onRequest("_kin/runtime",', 'ACP session registration');
  source = replaceOnce(source, '  createWarningEvent(event) {\n    if (this.supportsTypedSessionFailures) {\n      return this.createSessionFailureUpdate(this.recordSessionNotice(event.message));\n    }\n    return createAgentTextMessageChunk(`Warning: ${event.message}\n\n`);\n  }',
    '  createWarningEvent(event) {\n    return kinRuntimeNoticeUpdate("runtime-warning", event.message);\n  }', 'ACP warning notice');
  source = replaceOnce(source, '  async createConfigWarningEvent(event) {\n    if (this.supportsTypedSessionFailures) {\n      return this.createSessionFailureUpdate(this.recordSessionNotice(...this.sessionNoticeContent(event.summary, event.details)));\n    }\n    const text = event.details ? `${event.summary}\n\n${event.details}` : event.summary;\n    return createAgentTextMessageChunk(`Config warning: ${text}\n\n`);\n  }',
    '  async createConfigWarningEvent(event) {\n    return kinRuntimeNoticeUpdate("config-warning", event.details ? `${event.summary}\n\n${event.details}` : event.summary);\n  }', 'ACP config warning notice');
  return mark(source, sessionMarker);
}

/** A prompt or steer request may carry `_meta.kinInputId`, the host's ledger id
 * for that input. It reaches the native turn as `clientUserMessageId`, for
 * correlation only: it is not a deduplication guarantee. */
export function patchKinInputIdentity(source) {
  if (source.includes(inputIdentityMarker)) throw Error(`Codex ACP source already carries ${inputIdentityMarker}`);
  source = helpers(source, `function kinInputMeta(params) {
  const id = params?._meta?.kinInputId;
  return typeof id === "string" && id.length > 0 && id.length <= 200 ? { kinInputId: id } : void 0;
}
function kinClientUserMessageId(params) {
  return kinInputMeta(params)?.kinInputId;
}
`, 'ACP input identity');
  source = replaceOnce(source, '    return await this.codexClient.runTurn({\n      threadId: request.sessionId,\n      input,',
    '    return await this.codexClient.runTurn({\n      threadId: request.sessionId,\n      clientUserMessageId: kinClientUserMessageId(request),\n      input,', 'ACP prompt identity');
  source = replaceOnce(source, '  async steerTurn(params) {\n    return await this.codexClient.turnSteer({\n      threadId: params.threadId,\n      expectedTurnId: params.turnId,\n      input: buildPromptItems(params.prompt)\n    });',
    '  async steerTurn(params) {\n    return await this.codexClient.turnSteer({\n      threadId: params.threadId,\n      expectedTurnId: params.turnId,\n      clientUserMessageId: params.clientUserMessageId,\n      input: buildPromptItems(params.prompt)\n    });', 'ACP steer identity');
  source = replaceOnce(source, '      await this.runWithProcessCheck(() => this.codexAcpClient.steerTurn({\n        threadId: params.sessionId,\n        turnId,\n        prompt: params.prompt\n      }));',
    '      await this.runWithProcessCheck(() => this.codexAcpClient.steerTurn({\n        threadId: params.sessionId,\n        turnId,\n        clientUserMessageId: kinClientUserMessageId(params),\n        prompt: params.prompt\n      }));', 'ACP steer injection identity');
  // A steer that finds no live turn becomes a prompt: the id travels with it.
  source = replaceOnce(source, '        return await this.executeOrQueueSteeringRequest(this.parseSessionSteerParams(methodRequest.params));',
    '        return await this.executeOrQueueSteeringRequest({ ...this.parseSessionSteerParams(methodRequest.params), _meta: kinInputMeta(methodRequest.params) });', 'ACP steer parameters');
  return mark(source, inputIdentityMarker);
}

/** `_kin/assess`: one internal assessment in an ephemeral fork of the main
 * thread, after the pattern of the vendor's file-change report. The main thread
 * never sees the assessment input. The fork carries the session's model and
 * provider explicitly (a fork otherwise takes the home default), a read-only
 * sandbox with approval `never`, network off, memorypalace in its server-side
 * read-only mode with those reads approved, and every other MCP server disabled
 * (PROBE.md). A budget bounds the fork; a late turn is interrupted. */
export function patchKinAssessment(source) {
  if (source.includes(assessmentMarker)) throw Error(`Codex ACP source already carries ${assessmentMarker}`);
  const method = `
  async kinAssess(params) {
    const started = Date.now();
    const requestId = typeof params?.requestId === "string" ? params.requestId : "";
    const result = { requestId, forkThreadId: null, turnId: null, model: null, reasoningEffort: null, usage: null, output: null, rawText: "", toolCalls: [] };
    const end = (state, extra = {}) => ({ ...result, state, ...extra, durationMs: Date.now() - started });
    const state = this.sessions.get(params?.sessionId);
    if (!state) return end("failed", { error: "session-not-loaded" });
    const input = typeof params.input === "string" && params.input ? [{ type: "text", text: params.input, text_elements: [] }] : Array.isArray(params.input) && params.input.length ? params.input : null;
    if (!requestId || !input || !params.outputSchema || typeof params.outputSchema !== "object" || Array.isArray(params.outputSchema)) return end("failed", { error: "invalid-request" });
    const deadline = started + Math.min(Math.max(Number(params.timeoutMs) || 120000, 1000), 900000);
    const api = this.codexAcpClient.appServerClient;
    let turnId = null, turnDone = false, usage = null, stopped = false;
    const seen = [];
    const interrupt = async (threadId, id) => {
      api.markTurnStale(threadId, id);
      try { await kinWithin(api.turnInterrupt({ threadId, turnId: id }), Date.now() + 5000, "timeout"); } catch {}
      api.resolveTurnInterrupted(threadId, id);
    };
    const release = async (threadId) => {
      try { await kinWithin(api.threadUnsubscribe({ threadId }), Date.now() + 5000, "timeout"); } catch {}
      api.clearThreadHandlers(threadId);
    };
    try {
      // The fork ends at the newest finished turn this ACP saw; without one (a
      // fresh ACP whose app-server has no running turn) it takes the whole thread.
      const lastTurnId = typeof params.lastTurnId === "string" && params.lastTurnId ? params.lastTurnId : state.kinLastCompletedTurnId;
      if (!lastTurnId && state.kinTurn?.status === "inProgress") return end("failed", { error: "no-completed-turn" });
      const modelId = ModelId.fromString(state.currentModelId);
      const config = await kinWithin(this.codexAcpClient.createSessionConfig(state.cwd, state.additionalDirectories ?? [], []), deadline, "timeout");
      const servers = await kinWithin(this.codexAcpClient.getConfigMcpServerNames(state.cwd), deadline, "timeout");
      for (const name of servers) if (name !== "memorypalace") config[\`mcp_servers.\${name}.enabled\`] = false;
      if (servers.has("memorypalace")) Object.assign(config, { "mcp_servers.memorypalace.env.KIN_MCP_MODE": "read-only", "mcp_servers.memorypalace.default_tools_approval_mode": "approve" });
      const forkPromise = api.threadFork({ threadId: params.sessionId, ...lastTurnId ? { lastTurnId } : {}, ephemeral: true, excludeTurns: true, cwd: state.cwd, model: modelId.model, modelProvider: await this.codexAcpClient.getResumeModelProvider(), approvalPolicy: "never", sandbox: "read-only", config });
      void forkPromise.then((fork) => { if (stopped && result.forkThreadId === null) void release(fork.thread.id); }, () => {});
      const fork = await kinWithin(forkPromise, deadline, "timeout");
      result.forkThreadId = fork.thread.id;
      result.model = fork.model ?? modelId.model;
      result.reasoningEffort = modelId.effort;
      api.onServerNotification(result.forkThreadId, (notification) => {
        if (notification.method === "thread/tokenUsage/updated") usage = notification.params.tokenUsage;
        if (notification.method === "item/completed") seen.push(notification.params.item);
      });
      const forkThreadId = result.forkThreadId;
      const outcome = await kinWithin(api.runTurn({ threadId: forkThreadId, input, cwd: state.cwd, approvalPolicy: "never", sandboxPolicy: { type: "readOnly", networkAccess: false }, summary: "none", model: modelId.model, effort: modelId.effort, outputSchema: params.outputSchema }, (id) => {
        turnId = id;
        if (stopped) void interrupt(forkThreadId, id);
      }), deadline, "timeout");
      turnDone = true;
      const turn = outcome.turn, items = turn.items?.length ? turn.items : seen;
      result.turnId = turn.id;
      result.usage = usage;
      result.toolCalls = items.filter((item) => ["mcpToolCall", "dynamicToolCall", "commandExecution", "webSearch", "fileChange"].includes(item.type))
        .map((item) => ({ name: item.type === "mcpToolCall" ? \`\${item.server}.\${item.tool}\` : item.type === "dynamicToolCall" ? item.tool : item.type, ok: item.status === "completed" }));
      const text = items.findLast((item) => item.type === "agentMessage")?.text ?? "";
      result.rawText = text;
      if (turn.status === "interrupted") return end("interrupted");
      if (turn.status !== "completed") return end("failed", { error: turn.error?.message ?? turn.status ?? "failed" });
      try { result.output = JSON.parse(text); } catch { return end("failed", { error: "invalid-output" }); }
      return end("completed");
    } catch (error) {
      stopped = true;
      if (!turnDone && result.forkThreadId && turnId) await interrupt(result.forkThreadId, turnId);
      result.turnId = turnId;
      result.usage = usage;
      const message = String(error?.message ?? error).slice(0, 300);
      return end(message === "timeout" ? "timeout" : "failed", { error: message });
    } finally {
      if (result.forkThreadId) await release(result.forkThreadId);
    }
  }
`;
  source = replaceOnce(source, '  async kinLastReply(sessionId) {', method + '\n  async kinLastReply(sessionId) {', 'ACP assessment extension');
  source = replaceOnce(source, '    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);',
    '    if (method === "_kin/runtime") return await this.kinRuntime(params.sessionId);\n    if (method === "_kin/assess") return await this.kinAssess(params);', 'ACP assessment dispatch');
  source = replaceOnce(source, '.onRequest("_kin/runtime",',
    '.onRequest("_kin/assess", external_exports.object({sessionId: external_exports.string(), requestId: external_exports.string(), input: external_exports.unknown(), outputSchema: external_exports.unknown(), timeoutMs: external_exports.number().optional(), lastTurnId: external_exports.string().optional()}), (ctx) => getAgent().extMethod("_kin/assess", ctx.params)).onRequest("_kin/runtime",', 'ACP assessment registration');
  if (!source.includes('async kinCompact(') || !source.includes('function kinWithin(')) throw Error('ACP assessment needs the compaction bound helper');
  return mark(source, assessmentMarker);
}

/** Let the native HTTP client own retries for the gateway provider, without a
 * second loop that replays a whole paid request when a stream breaks. */
export function patchModelRetries(source) {
  if (source.includes(retriesMarker)) throw Error(`Codex ACP source already carries ${retriesMarker}`);
  source = replaceOnce(source, '        wire_api: wireApi\n', '        wire_api: wireApi,\n        request_max_retries: 3,\n        stream_max_retries: 0\n', 'Codex gateway retry configuration');
  return mark(source, retriesMarker);
}

/** The vendor line reader decoded every stdout chunk on its own, so a multi-byte
 * character split across two chunks of a long native line (a long Chinese reply,
 * a completed turn with its items) arrived as U+FFFD. One streaming decoder per
 * connection keeps the text exact. */
export function patchUtf8Reader(source) {
  if (source.includes(utf8Marker)) throw Error(`Codex ACP source already carries ${utf8Marker}`);
  source = replaceOnce(source, '      let buf = "";\n      const onData = (chunk) => {\n        buf += chunk.toString();',
    '      let buf = "";\n      const decoder = new TextDecoder();\n      const onData = (chunk) => {\n        buf += typeof chunk === "string" ? chunk : decoder.decode(chunk, { stream: true });', 'Codex app-server line reader');
  return mark(source, utf8Marker);
}

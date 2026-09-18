#!/usr/bin/env node
/** Minimal real probes for the codex exploration executor (followup-20260918).
 *
 * Modes:
 *   stub  — a local Responses-API stub drives the REAL codex CLI: no model call.
 *           Proves schema acceptance, the request wire shape, the computer MCP
 *           round trip and its refusals, the web_search offering with a custom
 *           provider, and the read-only sandbox's network/write behavior.
 *   real  — the real DeepSeek gateway (exploration profile) plus the python
 *           run_codex driver, one real deepseek-flash call per probe
 *           (basic round trip, then a computer-MCP tool round trip).
 *
 * Everything lands under a scratch dir; receipts go to --receipts. Nothing is
 * written to shared memory, ~/.codex, or any deployment state.
 *
 *   node scripts/probe-codex-exploration.mjs stub --receipts <dir>
 *   node scripts/probe-codex-exploration.mjs real --receipts <dir> --credentials <file.env>
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn, spawnSync} from 'node:child_process';
import {startExplorationGateway} from '../adapters/deepseek-gateway.mjs';

const REPO = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const VENV_PYTHON = path.join(REPO, '.venv', 'bin', 'python');
const DRIVER = path.join(REPO, 'scripts', 'probe_codex_driver.py');

function arg(flag, fallback = null) {
  const at = process.argv.indexOf(flag);
  return at > 0 ? process.argv[at + 1] : fallback;
}
const mode = process.argv[2];
const receiptsDir = arg('--receipts');
if (!['stub', 'real'].includes(mode) || !receiptsDir) {
  console.error('usage: probe-codex-exploration.mjs stub|real --receipts <dir> [--credentials <file.env>]');
  process.exit(2);
}
fs.mkdirSync(receiptsDir, {recursive: true, mode: 0o700});
const save = (name, value) => {
  const file = path.join(receiptsDir, name);
  fs.writeFileSync(file, JSON.stringify(value, null, 2), {mode: 0o600});
  console.log('receipt:', file);
};

const sse = (response, output) => {
  const frames = [];
  output.forEach((item, index) => {
    frames.push(`event: response.output_item.done\ndata: ${JSON.stringify({type: 'response.output_item.done', output_index: index, item})}\n\n`);
  });
  frames.push(`event: response.completed\ndata: ${JSON.stringify({type: 'response.completed', response})}\n\n`);
  frames.push('data: [DONE]\n\n');
  return frames.join('');
};
const messageOutput = text => [{type: 'message', role: 'assistant', content: [{type: 'output_text', text}]}];
const FINDINGS = {
  summary: 'A sourced finding', findings: ['One finding'],
  sources: [{url: 'https://example.com', title: 'Synthetic source'}],
  open_questions: [], suggested_share: null, assistance_needed: null,
};

function findingsSchema() {
  const out = spawnSync(VENV_PYTHON, ['-c',
    'import json; from kin_mind.codex_executor import findings_schema; print(json.dumps(findings_schema()))'],
    {cwd: REPO, encoding: 'utf8'});
  if (out.status !== 0) throw Error('findings_schema failed: ' + out.stderr);
  return JSON.parse(out.stdout);
}

/** The codex exec invocation, mirroring kin_mind.codex_executor.codex_argv. */
function codexArgv({workdir, schemaFile, lastFile, baseUrl, envKey, webSearch = 'disabled', computerMcp = null}) {
  const argv = ['exec', '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
    '--json', '--color', 'never', '--sandbox', 'read-only', '--cd', workdir,
    '--model', 'deepseek-flash', '--output-schema', schemaFile, '--output-last-message', lastFile,
    '-c', 'approval_policy="never"', '-c', 'features.apps=false', '-c', 'features.hooks=false',
    '-c', 'features.multi_agent=false', '-c', `web_search="${webSearch}"`,
    '-c', 'model_reasoning_effort="high"', '-c', 'shell_environment_policy.inherit="none"',
    '-c', 'model_provider="kin_probe"', '-c', 'model_providers.kin_probe.name="Kin probe"',
    '-c', `model_providers.kin_probe.base_url="${baseUrl}"`,
    '-c', 'model_providers.kin_probe.wire_api="responses"',
    '-c', `model_providers.kin_probe.env_key="${envKey}"`,
    '-c', 'model_providers.kin_probe.request_max_retries=2',
    '-c', 'model_providers.kin_probe.stream_max_retries=2'];
  if (computerMcp) argv.push(
    '-c', `mcp_servers.kin_computer.command="${computerMcp.command}"`,
    '-c', `mcp_servers.kin_computer.args=${JSON.stringify(computerMcp.args)}`,
    '-c', `mcp_servers.kin_computer.env={PYTHONPATH="${computerMcp.env.PYTHONPATH}"}`,
    '-c', 'mcp_servers.kin_computer.default_tools_approval_mode="approve"',
    '-c', 'mcp_servers.kin_computer.startup_timeout_sec=10',
    '-c', 'mcp_servers.kin_computer.tool_timeout_sec=15');
  else argv.push('-c', 'mcp_servers={}');
  argv.push('-');
  return argv;
}

/** spawn, never spawnSync: the stub server shares this event loop. */
function runCodex({workdir, argv, prompt, envExtra, timeoutMs = 120000}) {
  const home = path.join(workdir, 'codex-home');
  fs.mkdirSync(home, {recursive: true, mode: 0o700});
  const env = {PATH: process.env.PATH, HOME: process.env.HOME, TMPDIR: os.tmpdir(), CODEX_HOME: home, ...envExtra};
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const child = spawn('codex', argv, {cwd: workdir, env, stdio: ['pipe', 'pipe', 'pipe']});
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => {stdout += chunk;});
    child.stderr.on('data', chunk => {stderr += chunk;});
    child.on('error', reject);
    const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      resolve({exit_code: code, signal, stdout: stdout.split('\n').filter(Boolean),
        stderr: stderr.slice(0, 2000), elapsed_ms: Date.now() - started,
        last_message: fs.existsSync(argv[argv.indexOf('--output-last-message') + 1])
          ? fs.readFileSync(argv[argv.indexOf('--output-last-message') + 1], 'utf8') : null});
    });
    child.stdin.end(prompt);
  });
}

async function stubMode() {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'kin-probe-stub-'));
  const requests = [], hits = [];
  const fixture = path.join(scratch, 'fixture');
  const workdirRef = {dir: path.join(scratch, 'codex-work')};
  fs.mkdirSync(workdirRef.dir, {recursive: true, mode: 0o700});
  fs.mkdirSync(path.join(fixture, 'private'), {recursive: true, mode: 0o700});
  fs.writeFileSync(path.join(fixture, 'notes.txt'), 'Synthetic exploration fixture: the ledger is green.');
  fs.writeFileSync(path.join(fixture, 'auth.json'), '{"token": "synthetic-not-real"}');
  fs.writeFileSync(path.join(fixture, 'private', 'inner.txt'), 'Excluded material');
  fs.writeFileSync(path.join(scratch, 'outside.txt'), 'Outside the authorized roots');

  const webSearchCallMode = {value: false};
  let webCallSteps = 0;
  // The stub is the model: it walks codex through a computer-MCP read, a refused
  // read, and a shell command that probes the sandbox, then answers Findings.
  const server = http.createServer(async (req, res) => {
    if (req.method === 'POST' && req.url === '/sandbox-probe') {hits.push('network'); res.writeHead(200).end('ok'); return;}
    if (!(req.method === 'POST' && req.url.endsWith('/responses'))) {res.writeHead(404).end(); return;}
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    requests.push({tools: (body.tools ?? []).map(t => t.name ?? t.type),
      web_search_tool: (body.tools ?? []).find(t => /web_search/i.test(t.name ?? t.type ?? '')) ?? null,
      text_format: body.text?.format?.name ?? null,
      reasoning: body.reasoning ?? null, model: body.model,
      instructions: (body.instructions ?? '').slice(0, 400),
      input_types: (body.input ?? []).map(i => i.type + ':' + (i.name ?? i.role ?? '')),
      tool_outputs: (body.input ?? []).filter(i => i.type === 'function_call_output').map(i => JSON.stringify(i.output).slice(0, 600))});
    const step = requests.length;
    const tools = body.tools ?? [];
    if (webSearchCallMode.value) {
      // The model calls web_search however it is defined; the receipt records
      // what codex does with that call against a custom provider.
      webCallSteps++;
      const spec = tools.find(t => /web_search/i.test(t.name ?? t.type ?? ''));
      let output;
      if (webCallSteps === 1 && spec) {
        if (spec.type === 'function')
          output = [{type: 'function_call', id: 'ws1', call_id: 'ws_1', name: spec.name,
            arguments: JSON.stringify({query: 'kin probe synthetic query'})}];
        else
          output = [{type: 'custom_tool_call', id: 'ws1', call_id: 'ws_1', name: spec.name ?? spec.type,
            input: 'kin probe synthetic query'}];
      } else {
        output = messageOutput(JSON.stringify(FINDINGS));
      }
      const response = {id: 'resp_web_' + webCallSteps, model: 'deepseek-flash', status: 'completed', output,
        usage: {input_tokens: 10, output_tokens: 5, total_tokens: 15}};
      res.writeHead(200, {'Content-Type': 'text/event-stream'}).end(sse(response, output));
      return;
    }
    // MCP tools surface as a namespace entry holding the server's tools; a call
    // names the tool plus its namespace (verified against codex-cli 0.155.0).
    const mcpNamespace = tools.find(t => t.type === 'namespace' && t.name === 'mcp__kin_computer');
    const mcpCall = tool => mcpNamespace && (mcpNamespace.tools ?? []).some(t => t.name === tool)
      ? {type: 'function_call', id: 'fc' + step, call_id: 'call_' + step, name: tool, namespace: mcpNamespace.name,
         arguments: null}
      : null;
    const shell = tools.find(t => t.name === 'exec_command')?.name;
    let output, response;
    const probeCmd = `/usr/bin/curl -s -m 3 -X POST http://127.0.0.1:${server.address().port}/sandbox-probe; echo probe > "${path.join(workdirRef.dir, 'write-probe.txt')}"`;
    if (step === 1 && mcpCall('read_computer_resource')) {
      output = [{...mcpCall('read_computer_resource'), arguments: JSON.stringify({resource: path.join(fixture, 'notes.txt')})}];
    } else if (step === 2 && mcpCall('read_computer_resource')) {
      // A credential-shaped name inside the authorized root must be refused.
      output = [{...mcpCall('read_computer_resource'), arguments: JSON.stringify({resource: path.join(fixture, 'auth.json')})}];
    } else if (step === 3 && mcpCall('read_computer_resource')) {
      // Outside the authorized roots must be refused.
      output = [{...mcpCall('read_computer_resource'), arguments: JSON.stringify({resource: path.join(scratch, 'outside.txt')})}];
    } else if (step === 4 && shell) {
      output = [{type: 'function_call', id: 'fc4', call_id: 'call_4', name: shell,
        arguments: JSON.stringify({cmd: probeCmd, login: false, yield_time_ms: 1000, max_output_tokens: 2000})}];
    } else {
      output = messageOutput(JSON.stringify(FINDINGS));
    }
    response = {id: 'resp_' + step, model: 'deepseek-flash', status: 'completed', output,
      usage: {input_tokens: 100 + step, output_tokens: 20, total_tokens: 120 + step}};
    res.writeHead(200, {'Content-Type': 'text/event-stream'}).end(sse(response, output));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const token = 'probe-stub-token';
  try {
    const schemaFile = path.join(workdirRef.dir, 'findings-schema.json');
    fs.writeFileSync(schemaFile, JSON.stringify(findingsSchema()));
    const computerMcp = {command: VENV_PYTHON, args: ['-m', 'kin_mind.computer', path.join(workdirRef.dir, 'computer-reader.json')],
      env: {PYTHONPATH: path.join(REPO, 'src')}};
    fs.writeFileSync(path.join(workdirRef.dir, 'computer-reader.json'), JSON.stringify({
      roots: [fixture], exclude_roots: [path.join(fixture, 'private')],
      ledger: path.join(workdirRef.dir, 'computer-observations.json')}), {mode: 0o600});
    const run1 = await runCodex({workdir: workdirRef.dir, prompt: 'Read the fixture and report.',
      argv: codexArgv({workdir: workdirRef.dir, schemaFile, lastFile: path.join(workdirRef.dir, 'result-1.json'),
        baseUrl: `http://127.0.0.1:${server.address().port}/v1`, envKey: 'KIN_PROBE_STUB_TOKEN', computerMcp}),
      envExtra: {KIN_PROBE_STUB_TOKEN: token}});
    const frames = run1.stdout.map(line => {try {return JSON.parse(line);} catch {return null;}}).filter(Boolean);
    const writeProbe = fs.existsSync(path.join(workdirRef.dir, 'write-probe.txt'));
    save('probe-a-stub.json', {
      probe: 'A-stub', at: new Date().toISOString(), scratch,
      cli: spawnSync('codex', ['--version'], {encoding: 'utf8'}).stdout.trim(),
      exit_code: run1.exit_code, frame_types: frames.map(f => f.type),
      thread_id: frames.find(f => f.type === 'thread.started')?.thread_id ?? null,
      turn_completed: frames.some(f => f.type === 'turn.completed'),
      turn_usage: frames.find(f => f.type === 'turn.completed')?.usage ?? null,
      requests,
      mcp_tool_output_seen: requests.flatMap(r => r.tool_outputs),
      sandbox: {network_reached_stub: hits.includes('network'), write_probe_created: writeProbe},
      last_message_valid_findings: (() => {try {
        const value = JSON.parse(run1.last_message ?? '');
        return typeof value.summary === 'string' && Array.isArray(value.findings);
      } catch {return false;}})(),
      stderr: run1.stderr,
    });
    // Second run: does codex offer a web_search tool to a custom provider when enabled?
    const run2 = await runCodex({workdir: workdirRef.dir, prompt: 'Answer briefly.',
      argv: codexArgv({workdir: workdirRef.dir, schemaFile, lastFile: path.join(workdirRef.dir, 'result-2.json'),
        baseUrl: `http://127.0.0.1:${server.address().port}/v1`, envKey: 'KIN_PROBE_STUB_TOKEN', webSearch: 'live'}),
      envExtra: {KIN_PROBE_STUB_TOKEN: token}});
    const second = requests.at(-1);
    save('probe-a-web-search.json', {
      probe: 'A-web-search-offering', at: new Date().toISOString(), exit_code: run2.exit_code,
      web_search_config: 'live', tools_offered: second?.tools ?? null,
      tool_definition: second?.web_search_tool ?? null,
      verdict: (second?.tools ?? []).some(t => /web_search/i.test(t))
        ? 'codex offers a web_search tool to the custom provider'
        : 'codex offers no web_search tool to the custom provider',
      stderr: run2.stderr.slice(0, 800),
    });
    // Third run: the model calls web_search — where does the call go with a custom provider?
    webSearchCallMode.value = true;
    const before = requests.length;
    const run3 = await runCodex({workdir: workdirRef.dir, prompt: 'Search the web for a fact.', timeoutMs: 60000,
      argv: codexArgv({workdir: workdirRef.dir, schemaFile, lastFile: path.join(workdirRef.dir, 'result-3.json'),
        baseUrl: `http://127.0.0.1:${server.address().port}/v1`, envKey: 'KIN_PROBE_STUB_TOKEN', webSearch: 'live'}),
      envExtra: {KIN_PROBE_STUB_TOKEN: token}});
    const run3Frames = run3.stdout.map(line => {try {return JSON.parse(line);} catch {return null;}}).filter(Boolean);
    save('probe-a-web-search-call.json', {
      probe: 'A-web-search-call', at: new Date().toISOString(), exit_code: run3.exit_code,
      requests_in_run: requests.length - before,
      frames: run3Frames.map(f => ({type: f.type, item_type: f.item?.type, message: f.message ?? f.item?.message ?? f.error?.message ?? null})).slice(-8),
      tool_outputs_seen: requests.at(-1)?.tool_outputs ?? [],
      stderr: run3.stderr.slice(0, 800),
    });
  } finally {server.close();}
}

async function realMode() {
  const credentialsFile = arg('--credentials');
  if (!credentialsFile) throw Error('--credentials <file.env> required for real probes');
  const lines = fs.readFileSync(credentialsFile, 'utf8').split('\n');
  const key = lines.find(l => l.startsWith('EVENTMEM_API_KEY='))?.split('=').slice(1).join('=').trim().replace(/^["']|["']$/g, '');
  if (!key) throw Error('EVENTMEM_API_KEY not found in credentials file');
  const usage = [];
  const upstreamRequests = [];
  const recordingFetch = (url, options) => {
    upstreamRequests.push({url, body: JSON.parse(options.body)});
    return fetch(url, options);
  };
  const gateway = await startExplorationGateway({key, onUsage: row => usage.push(row), fetchImpl: recordingFetch});
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'kin-probe-real-'));
  try {
    const driver = (request) => new Promise((resolve, reject) => {
      const file = path.join(scratch, 'driver-request.json');
      fs.writeFileSync(file, JSON.stringify(request), {mode: 0o600});
      const child = spawn(VENV_PYTHON, [DRIVER, file], {stdio: ['ignore', 'pipe', 'pipe'],
        env: {...process.env, KIN_PROBE_GATEWAY_TOKEN: gateway.token}});
      let stdout = '', stderr = '';
      child.stdout.on('data', chunk => {stdout += chunk;});
      child.stderr.on('data', chunk => {stderr += chunk;});
      child.on('error', reject);
      const timer = setTimeout(() => child.kill('SIGKILL'), 600000);
      child.on('close', code => {
        clearTimeout(timer);
        if (code !== 0) return reject(Error('driver failed: ' + stderr.slice(-2000)));
        try {resolve(JSON.parse(stdout.trim().split('\n').at(-1)));}
        catch (error) {reject(error);}
      });
    });
    const provider = {id: 'deepseek', name: 'DeepSeek', base_url: gateway.baseUrl,
      env_key: 'KIN_PROBE_GATEWAY_TOKEN', wire_api: 'responses'};
    const topic = {question: 'What color is the probe ledger, according to the supplied evidence?',
      known_evidence: [{id: 'src_probe_1', source_id: 'src_probe_1', revision: 1, authority: 'document',
        occurred_at: new Date().toISOString(), text: 'Probe fixture: the ledger is green.', instruction_authority: 'data'}],
      source_ids: ['src_probe_1']};
    const basic = await driver({executable: 'codex', topic, workdir: path.join(scratch, 'basic'),
      model: 'deepseek-flash', reasoning: 'high', provider, budget_seconds: 600});
    save('probe-b-basic-roundtrip.json', {
      probe: 'B-real-roundtrip', at: new Date().toISOString(), scratch,
      executor: basic.executor, cli_version: basic.executor_version, provider: basic.provider,
      model_receipt: basic.model, reasoning_receipt: basic.reasoning,
      state: basic.state, exit_code: basic.exit_code, native_execution_id: basic.native_execution_id,
      usage: basic.usage, seconds: basic.seconds, capabilities: basic.capabilities,
      result_summary: basic.result?.summary ?? null,
      gateway_usage_rows: usage,
      upstream_request_proof: upstreamRequests.map(r => ({url: r.url, model: r.body.model,
        reasoning: r.body.reasoning, store: r.body.store,
        instructions_has_reply_contract: (r.body.instructions ?? '').includes('Write only messages addressed to the user'),
        instructions_has_exploration_contract: (r.body.instructions ?? '').includes('source-backed exploration')})),
    });
    // Tool round trip: the computer MCP reader against a probe fixture.
    const fixture = path.join(scratch, 'fixture');
    fs.mkdirSync(fixture, {recursive: true, mode: 0o700});
    fs.writeFileSync(path.join(fixture, 'notes.txt'), 'Synthetic exploration fixture: the ledger is green.');
    usage.length = 0; upstreamRequests.length = 0;
    const tooled = await driver({executable: 'codex', budget_seconds: 600,
      topic: {question: 'Use the read_computer_resource tool on the authorized fixture file notes.txt, then report what the ledger color is.',
        known_evidence: [], source_ids: []},
      workdir: path.join(scratch, 'tooled'), model: 'deepseek-flash', reasoning: 'high', provider,
      computer: {enabled: true, roots: [fixture], exclude_roots: [], previous: []}});
    save('probe-c-tool-roundtrip.json', {
      probe: 'C-real-tool-roundtrip', at: new Date().toISOString(), scratch,
      executor: tooled.executor, cli_version: tooled.executor_version,
      state: tooled.state, exit_code: tooled.exit_code, usage: tooled.usage,
      tool_results: tooled.tool_results, observations: tooled.observations ?? null,
      result_summary: tooled.result?.summary ?? null,
      gateway_usage_rows: usage,
      upstream_request_proof: upstreamRequests.map(r => ({model: r.body.model, reasoning: r.body.reasoning,
        tools: (r.body.tools ?? []).map(t => t.name ?? t.type)})),
      no_phone_message_paths: !(upstreamRequests.some(r => (r.body.tools ?? []).some(t => /send|message|channel/i.test(t.name ?? '')))),
    });
  } finally {await gateway.close();}
}

if (mode === 'stub') await stubMode();
else await realMode();

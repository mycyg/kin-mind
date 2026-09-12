import http from 'node:http';
import {randomBytes, timingSafeEqual} from 'node:crypto';

export const replyContract = 'Write only messages addressed to the user: the answer or a useful progress update. Do not narrate your interpretation of the user, response planning, private analysis, or internal tool-result commentary. Earlier assistant messages may contain that narration; do not imitate it. Keep tool calls separate from user-facing text. Runtime metadata is evidence to check, not a preface to repeat. Follow the conversation language and persona.';
const privateChannels = new Set(['analysis', 'reasoning', 'summary']);
export const isPrivateOutput = item => item?.type === 'reasoning' ||
  privateChannels.has(item?.channel) || privateChannels.has(item?.phase);

// DeepSeek treats developer messages as user input. Map trusted developer
// instructions to its supported system role; leave user data and receipts alone.
export function deepseekRequest(body, reasoningEffort = 'max') {
  if (body.model !== 'deepseek-flash' || !Array.isArray(body.input)) throw Error('unsupported-request');
  if (!['none','low','high','max'].includes(reasoningEffort)) throw Error('unsupported-reasoning-effort');
  const result = {...body, reasoning: {effort: reasoningEffort}, store: false};
  result.input = body.input.filter(item => !isPrivateOutput(item)).map(item =>
    item.role === 'developer' ? {...item, role: 'system'} : item);
  result.instructions = [body.instructions, replyContract].filter(Boolean).join('\n\n');
  delete result.service_tier;
  delete result.previous_response_id;
  delete result.conversation;
  return result;
}

export function responseNormalizer() {
  const indexes = new Map();
  const suppressed = new Set(), suppressedIds = new Set();
  let next = 0;
  return event => {
    if (event.type?.startsWith('response.reasoning')) return null;
    if (isPrivateOutput(event.item) || isPrivateOutput(event)) {
      if (Number.isInteger(event.output_index)) suppressed.add(event.output_index);
      if (event.item?.id) suppressedIds.add(event.item.id);
      return null;
    }
    if (suppressed.has(event.output_index) || suppressedIds.has(event.item_id)) return null;
    const value = structuredClone(event);
    if (Number.isInteger(value.output_index)) {
      if (!indexes.has(value.output_index)) indexes.set(value.output_index, next++);
      value.output_index = indexes.get(value.output_index);
    }
    if (value.response?.output) value.response.output = value.response.output.filter(item => !isPrivateOutput(item));
    return value;
  };
}

export async function startDeepSeekGateway({key, fetchImpl = fetch, onUsage = () => {}, timeoutMs = 120000, reasoningEffort = 'max'}) {
  if (!key) throw Error('deepseek-key-unavailable');
  const token = randomBytes(32).toString('hex');
  const controllers = new Set();
  const server = http.createServer(async (req, res) => {
    const supplied = Buffer.from(req.headers.authorization ?? '');
    const expected = Buffer.from('Bearer ' + token);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
      res.writeHead(401).end(); return;
    }
    if (req.method !== 'POST' || !['/responses', '/v1/responses'].includes(req.url)) {
      res.writeHead(404).end(); return;
    }
    const abort = new AbortController(); controllers.add(abort);
    const timer = setTimeout(() => abort.abort(), timeoutMs);
    res.on('close', () => { if (!res.writableEnded) abort.abort(); });
    try {
      let raw = '';
      for await (const chunk of req) {
        raw += chunk;
        if (Buffer.byteLength(raw) > 64 * 1024 * 1024) throw Error('request-too-large');
      }
      const body = deepseekRequest(JSON.parse(raw), reasoningEffort);
      const upstream = await fetchImpl('https://api.deepseek.com/responses', {
        method: 'POST', redirect: 'error', signal: abort.signal,
        headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + key},
        body: JSON.stringify(body),
      });
      if (!upstream.ok) {
        res.writeHead(upstream.status, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: {message: 'DeepSeek HTTP ' + upstream.status, type: 'provider_error'}})); return;
      }
      const normalize = responseNormalizer();
      if (!(upstream.headers.get('content-type') ?? '').includes('text/event-stream')) {
        const value = await upstream.json();
        value.output = (value.output ?? []).filter(item => !isPrivateOutput(item));
        onUsage({model: value.model, usage: value.usage, requestId: value.id});
        res.writeHead(200, {'Content-Type': 'application/json'}).end(JSON.stringify(value)); return;
      }
      res.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'});
      const decoder = new TextDecoder(); let pending = '',terminal = false;
      const emit = frame => {
        const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!data) return;
        if (data === '[DONE]') { if(!terminal)throw Error('missing-terminal-event');res.write('data: [DONE]\n\n'); return; }
        const event = normalize(JSON.parse(data));
        if (!event) return;
        if (['response.completed','response.failed','response.incomplete','error'].includes(event.type)) terminal = true;
        if (event.type === 'response.completed') onUsage({model: event.response.model, usage: event.response.usage, requestId: event.response.id});
        res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      };
      for await (const chunk of upstream.body) {
        pending += decoder.decode(chunk, {stream: true}); pending = pending.replaceAll('\r\n', '\n');
        if (pending.length > 8 * 1024 * 1024) throw Error('stream-frame-too-large');
        let end;
        while ((end = pending.indexOf('\n\n')) !== -1) { emit(pending.slice(0, end)); pending = pending.slice(end + 2); }
      }
      pending += decoder.decode(); if (pending.trim()) emit(pending);
      if(!terminal)throw Error('missing-terminal-event');
      res.end();
    } catch {
      // Never return provider bodies, credentials, or raw conversation data in errors.
      if (!res.headersSent) {
        res.writeHead(502, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: {message: 'DeepSeek transport incomplete', type: 'provider_error'}}));
      } else res.end('event: error\ndata: '+JSON.stringify({type:'error',code:'incomplete_stream',message:'DeepSeek transport incomplete'})+'\n\n');
    } finally {clearTimeout(timer); controllers.delete(abort);}
  });
  await new Promise((resolve, reject) => {server.once('error', reject);server.listen(0, '127.0.0.1', resolve);});
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}/v1`, token, reasoningEffort,
    active: () => controllers.size,
    close: () => {for (const controller of controllers) controller.abort(); server.closeAllConnections(); return new Promise(resolve => server.close(resolve));},
  };
}

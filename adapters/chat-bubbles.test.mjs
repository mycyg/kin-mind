import test from 'node:test';
import assert from 'node:assert/strict';
import {splitChatText,chatEnvelope,chatVoice} from './chat-bubbles.mjs';

test('voice prefers one complete everyday bubble without making it a transport limit',()=>{
  assert.match(chatVoice,/优先用一个简短完整的气泡/);
  assert.match(chatVoice,/深度讨论、工作、分析与交付按内容展开/);
  assert.doesNotMatch(chatVoice,/20字|1[—-]4|1-4/);
  // Multiple intentional paragraphs remain supported; this preference is not
  // a lossy splitter or a hard one-bubble protocol constraint.
  assert.deepEqual(splitChatText('一个完整念头。\n\n内容需要时再分开。'),[
    '一个完整念头。','内容需要时再分开。'
  ]);
});

test('paragraph splitting keeps fences, words and URLs whole',()=>{
  const code='```js\nconst x = 1;\n\nprint(x);\n```';
  assert.deepEqual(splitChatText(`说明\n\n${code}\n\n结论`),['说明',code,'结论']);
  assert.deepEqual(splitChatText('一句。\n\n两句。'),['一句。','两句。']);
});

test('a complete send envelope is recognised and its bubbles are the structure',()=>{
  const body=JSON.stringify({action:'send',bubbles:['你躲哪儿去了？','我有个馊主意。 🫧']});
  const hit=chatEnvelope(body);
  assert.equal(hit.state,'send');
  assert.deepEqual(hit.bubbles,['你躲哪儿去了？','我有个馊主意。 🫧']);
  // Key order and pretty-printing are not part of the structure.
  assert.equal(chatEnvelope('{\n  "bubbles": ["甲",\n  "乙"],\n  "action": "send"\n}').state,'send');
  assert.deepEqual(chatEnvelope('  {"action":"send","bubbles":[" 带边的 "]}  ').bubbles,['带边的']);
});

test('envelope-shaped but not deliverable: incomplete, never a text fallback',()=>{
  for(const [body,reason] of [
    [JSON.stringify({action:'send',bubbles:'不是数组'}),'chat-envelope-incomplete'],
    [JSON.stringify({action:'send',bubbles:[]}),'chat-envelope-incomplete'],
    [JSON.stringify({action:'send',bubbles:['  ']}),'chat-envelope-incomplete'],
    [JSON.stringify({action:'send',bubbles:[{text:'带引用的气泡',references:[]}]}),'chat-envelope-incomplete'],
    [JSON.stringify({action:'send',bubbles:['甲'],note:'多出来的字段'}),'chat-envelope-incomplete'],
    [JSON.stringify({action:'send',text:'旧草稿形态的纯文本'}),'chat-envelope-incomplete'],
    [JSON.stringify({action:'wait',condition:'time',reason:'稍后'}),'chat-envelope-incomplete'],
    [JSON.stringify({action:'abandon',reason:'失效'}),'chat-envelope-incomplete'],
    [JSON.stringify({bubbles:['缺 action']}),'chat-envelope-incomplete'],
  ])assert.deepEqual(chatEnvelope(body),{state:'invalid',reason});
});

test('a truncated or malformed envelope is recognised as one and refused',()=>{
  for(const body of [
    '{"action":"send","bubbles":["第一句。"',
    '{"action":"send","bubbles":["第一句。","第二',
    '{"bubbles":["只有开头"',
    '{"action":"send","bubbles":["甲"]} 后面又长出了别的话',
  ])assert.deepEqual(chatEnvelope(body),{state:'invalid',reason:'chat-envelope-truncated'});
});

test('everything else is body text: fences, prose, arbitrary JSON, arrays',()=>{
  for(const body of [
    '```json\n{"action":"send","bubbles":["代码块里的字段"]}\n```',
    '看这个例子：\n\n{"action":"send","bubbles":["不是裸信封"]}',
    '{"steps":["第一步","第二步"],"note":"小光要的 JSON"}',
    '{"action":"run","steps":["不是协议动作"]}',
    '{"action_name":"send","bubble_list":["形近的字段名不算"]}',
    '[{"action":"send","bubbles":["数组包着不是裸对象"]}]',
    '42',
  ])assert.equal(chatEnvelope(body),null,body);
  assert.equal(chatEnvelope(null),null);
  assert.equal(chatEnvelope(''),null);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {deepseekRequest,gatewayContracts,NEUTRAL_OWNER_NAME,startDeepSeekGateway} from '../../adapters/deepseek-gateway.mjs';

const source = fs.readFileSync(new URL('../../adapters/deepseek-gateway.mjs', import.meta.url), 'utf8');
const user = text => ({type: 'message', role: 'user', content: [{type: 'input_text', text}]});

test('the public gateway names no particular owner; the host supplies the name', () => {
  assert.doesNotMatch(source, /小光/);
  const neutral = gatewayContracts();
  assert.match(neutral.reply, new RegExp(`只输出给${NEUTRAL_OWNER_NAME}的回复`));
  const named = gatewayContracts('阿澄');
  for (const [profile, text] of Object.entries(named))
    if (profile !== 'continuity-check') assert.match(text, /阿澄/, profile);
  const body = {model: 'deepseek-flash', instructions: 'base', input: [user('在吗')]};
  assert.match(deepseekRequest(body, 'high', null, named).instructions, /^base\n\n只输出给阿澄的回复/);
  assert.match(deepseekRequest(body, 'high', 'exploration', named).instructions, /不是与阿澄聊天/);
  assert.match(deepseekRequest(body).instructions, /只输出给主人的回复/);
});

test('a name that could break the contract text is refused at start', async () => {
  for (const bad of ['', 'two words', '`x`', '${x}', 'x'.repeat(21)])
    assert.throws(() => gatewayContracts(bad), /invalid-owner-name/, bad);
  await assert.rejects(startDeepSeekGateway({key: 'k', ownerName: 'two words', fetchImpl: async () => { throw Error('no call'); }}),
    /invalid-owner-name/);
});

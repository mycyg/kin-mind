// Synthetic boundary regressions; generated modules and databases stay in this worktree's .dev.
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'
import { build } from 'esbuild'
const kin = resolve(import.meta.dirname, '..'), repo = resolve(kin, '..')
const scratch = join(kin, '.dev/review-health')
mkdirSync(scratch, { recursive: true })
await build({ stdin: { contents: `export { Mind } from './src/mind.ts'; export { Heartbeat } from './src/heartbeat.ts'; export { Reaching } from './src/contact/proactive.ts'; export { selfCheck, daily } from './src/health/checks.ts';`, resolveDir: kin }, outfile: join(scratch, 'modules.mjs'), bundle: true, platform: 'node', format: 'esm', packages: 'external' })
const { Mind, Heartbeat, Reaching, selfCheck, daily } = await import(pathToFileURL(join(scratch, 'modules.mjs')))
const OWNER = 'person:person_a', FRIEND = 'person:friend'
const directory = { people: [
  { id: OWNER, accounts: {} }, { id: FRIEND, accounts: { wechat: 'test-friend' } },
  { id: 'person:unbound', accounts: {} }, { id: 'person:missing', accounts: { wechat: 'test-missing' } },
  { id: 'person:wrong', accounts: { wechat: 'test-wrong' } },
], groups: [{ id: 'group:bound', chats: { wechat: 'test-group' } }, { id: 'group:stale', chats: { wechat: 'new-group' } }] }
const conversation = (recipient, chat = 'test-chat', kind = 'direct') => ({ recipient, channel: 'wechat', address: { chat, kind } })
const conversations = { friend: conversation(FRIEND), wrong: conversation(FRIEND), unbound: conversation('person:unbound'),
  group: conversation('group:bound', 'test-group', 'group'), stale: conversation('group:stale', 'old-group', 'group') }
const activities = { [FRIEND]: { via: 'friend' }, 'person:wrong': { via: 'wrong' }, 'person:unbound': { via: 'unbound' },
  'person:missing': { via: 'missing' }, 'group:bound': { via: 'group' }, 'group:stale': { via: 'stale' } }
// Run agenda expansion through the actual TypeScript Mind -> Python host boundary.
// Only due/review are scripted: a wrong resident call must fail exactly as it does in DSH.
const realMind = new Mind({ python: join(repo, 'core/.venv/bin/python'), coreSource: join(repo, 'core/src'),
  dataDir: mkdtempSync(join(scratch, 'host-')), tokenizerCache: join(repo, '.dev/tiktoken'), pace: 1, log: () => {} })
await realMind.run('ensure-initialized', { text: 'Synthetic integration fixture initialized.' })
let reviewArgs, reviewDue = true
const mind = {
  async call(action, args) {
    if (action === 'review-due') return { action: reviewDue, enrichment: false }
    return realMind.call(action, args)
  },
  async run(action, args) { if (action === 'companion-targets') return realMind.run(action, args); if (action === 'review') { reviewArgs = args; reviewDue = false; return { state: 'waiting' } } return {} },
  keepWorker() {},
}
const reaching = new Reaching({ mind, conversations: () => conversations }, () => directory, () => ({}), (id) => activities[id] ?? {}, () => {}, {}, {})
const beat = new Heartbeat(mind, () => false, () => {}, 1, () => reaching.reviewTargets())
try {
  await assert.rejects(realMind.call('companion-targets', { routes: {} }), /not-a-resident-action/)
  await beat.start()
  for (let i = 0; i < 1000 && !reviewArgs && beat.beats.review?.state !== 'failed'; i++) await new Promise((r) => setTimeout(r, 10))
  assert.notEqual(beat.beats.review?.state, 'failed', beat.beats.review?.reason)
  assert(reviewArgs, 'main review reached after real host agenda expansion')
} finally {
  beat.close()
  realMind.close()
}
assert.deepEqual(Object.keys(reviewArgs.unified_targets).sort(), [OWNER, FRIEND, 'group:bound'].sort())
assert.equal(reviewArgs.unified_targets[FRIEND].route.conversation_key, 'friend')
assert.equal(reviewArgs.recipient_person_id, undefined)
writeFileSync(join(scratch, 'targets.json'), JSON.stringify(reviewArgs.unified_targets))
console.log('✓ main heartbeat expands bound routes through real Mind/Python host into unified review')
let ownReview
const own = new Reaching({ mind: { call: async () => ({ action: true }), run: async (_, args) => { ownReview = args; return {} } } },
  () => ({ people: directory.people.slice(0, 2), groups: [] }), () => ({}), (id) => activities[id] ?? {}, () => {}, {}, {})
await own.othersReview()
assert.deepEqual(ownReview, { recipient_person_id: FRIEND, conversation_key: 'friend' })
console.log('✓ other-person review retains its conversation scope')
const health = await selfCheck({ mind: { call: async () => ({}), run: async () => ({ digests: { failed: 10 }, digest_failures: { missing_evidence: 7, retryable: 2, exhausted: 1 } }) } })
assert.equal(health.length, 3)
assert.match(health.find((p) => p.key === 'digests-evidence').title, /^7 /)
assert.match(health.find((p) => p.key === 'digests-evidence').todo, /不能补回依据/)
assert.match(health.find((p) => p.key === 'digests').title, /^2 /)
assert.match(health.find((p) => p.key === 'digests-exhausted').title, /^1 /)
console.log('✓ digest health separates missing evidence, temporary errors and exhausted retries')
const finished_at = new Date().toISOString()
const attempts = Array.from({ length: 50 }, () => ({ outcome: 'failed', finished_at, waiting_reason: 'deepseek-foreground-priority' }))
const dailyHost = { config: { dataDir: scratch }, mind: { run: async () => ({ attempts }) } }
let summary = await daily(dailyHost)
assert.deepEqual(summary.reviews, { waiting: 50 })
assert(!summary.found.some((p) => p.key === 'reviews'))
attempts.push({ outcome: 'failed', finished_at, error: 'deepseek-model-unavailable' })
summary = await daily(dailyHost)
assert.equal(summary.reviews.failed, 1)
assert(summary.found.some((p) => p.key === 'reviews'))
attempts.push({ outcome: 'committed', finished_at })
assert(!(await daily(dailyHost)).found.some((p) => p.key === 'reviews'))
console.log('✓ foreground yields count as waiting without hiding real failures')
const pytest = join(scratch, 'test_core_boundaries.py')
writeFileSync(pytest, `import json
from pathlib import Path
from test_recipient_wishes import setup, view
from test_kin_mind import FakeReviewer
from kin_mind.appraisal import Appraisal, Appraisals, Wish
from kin_mind.lifecycle import EventLifecycle, DIGEST_RETRY_LIMIT
from kin_mind.memory import MemoryContinuity
from kin_mind.companion_host import dispatch_companion

def test_generated_targets_allow_cross_person_wishes(setup):
    mind, source, clock = setup
    mind.unified_targets = json.loads(Path(${JSON.stringify(join(scratch, 'targets.json'))}).read_text())
    routes = {person: target['route'] for person, target in mind.unified_targets.items()}
    mind.unified_targets = dispatch_companion(mind.engine, {'scope': mind.scope.model_dump()}, 'companion-targets', {'routes': routes})
    Appraisals(mind).enqueue([source('cross-person-regression')], 'synthetic-v1')
    proposal = Appraisal(reason='Synthetic cross-person intent', wishes=[Wish(kind='contact', content='Share a synthetic finding', topic='test', strength=70, ttl_hours=24, completion='accepted', recipient_person_id='person:friend', evidence_ids=[])])
    result = Appraisals(mind).run_one(FakeReviewer(proposal))
    assert result['state'] == 'complete', result
    with view(mind, 'person:friend'):
        assert len(mind.read()['desires']) == 1
    assert not mind.read()['desires']
    assert 'person:unbound' not in mind.unified_targets
    Appraisals(mind).enqueue([source('unbound-regression')], 'synthetic-v1')
    proposal.wishes[0].recipient_person_id = 'person:unbound'
    Appraisals(mind).run_one(FakeReviewer(proposal))
    with view(mind, 'person:unbound'):
        assert not mind.read()['desires']

def test_lifecycle_reports_failure_reasons(setup):
    mind, source, clock = setup
    MemoryContinuity(mind).configure({'event_lifecycle': True})
    with mind.engine.db.connect(write=True) as conn:
        for i, data in enumerate([{'last_error': 'Event has no currently valid evidence'}, {'last_error': 'temporary network failure'}, {'last_error': 'temporary network failure', 'failed_retries': DIGEST_RETRY_LIMIT}]):
            conn.execute("INSERT INTO mind_event_digests(scope,event_id,state,generation,dirty_at,due_at,data) VALUES(?,?,'failed',1,?,0,?)", (mind.scope.key(), 'synthetic-' + str(i), mind.clock(), json.dumps(data)))
    status = EventLifecycle(mind).status()
    assert status['digests']['failed'] == 3
    assert status['digest_failures'] == {'missing_evidence': 1, 'retryable': 1, 'exhausted': 1}
`)
const result = spawnSync(join(repo, 'core/.venv/bin/python'), ['-m', 'pytest', '-q', pytest, '--basetemp', join(scratch, 'pytest-tmp')],
  { cwd: repo, encoding: 'utf8', env: { ...process.env, PYTHONPATH: [join(repo, 'core/src'), join(repo, 'core/tests/business')].join(':') } })
process.stdout.write(result.stdout ?? '')
process.stderr.write(result.stderr ?? '')
assert.equal(result.status, 0, 'Core synthetic regression tests pass')
console.log('✓ real Core commits cross-person intent and reports correct digest health')

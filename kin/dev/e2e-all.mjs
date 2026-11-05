// Every offline suite at once — each dev/e2e*.mjs but this one, in a process of its own — then a table of what each
// proved and how long it took, and a non-zero exit when anything failed. A suite's own output is kept in
// .dev/e2e-all/<suite>.log. Usage: npm run e2e:all (builds first); KIN_PACE passes through to every suite.
import { spawn } from 'node:child_process'
import { mkdirSync, readdirSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { kinDir, repo } from './env.mjs'

const logs = join(repo, '.dev', 'e2e-all')
mkdirSync(logs, { recursive: true })
const suites = readdirSync(join(kinDir, 'dev')).filter((file) => /^e2e.*\.mjs$/.test(file) && file !== basename(import.meta.filename)).sort()

const began = Date.now()
const results = await Promise.all(suites.map((file) => new Promise((resolve) => {
  const name = file.replace(/\.mjs$/, '')
  const start = Date.now()
  let output = ''
  const child = spawn(process.execPath, [join(kinDir, 'dev', file)], { cwd: kinDir, stdio: ['ignore', 'pipe', 'pipe'] })
  child.stdout.on('data', (chunk) => { output += chunk })
  child.stderr.on('data', (chunk) => { output += chunk })
  child.on('close', (code) => {
    writeFileSync(join(logs, name + '.log'), output)
    const lines = output.split('\n')
    resolve({
      name, code, output, seconds: Math.round((Date.now() - start) / 1000),
      passed: lines.filter((line) => line.startsWith('✓ ')).length, failed: lines.filter((line) => line.startsWith('✗ ')).length,
    })
  })
})))

const ok = (result) => result.code === 0 && result.failed === 0 && result.passed > 0
console.log('| suite | passed | failed | time |\n|---|---:|---:|---:|')
for (const result of results) console.log(`| ${result.name}${ok(result) ? '' : ' ✗'} | ${result.passed} | ${result.failed} | ${result.seconds} s |`)
const sum = (key) => results.reduce((total, result) => total + result[key], 0)
console.log(`| all, in parallel | ${sum('passed')} | ${sum('failed')} | ${Math.round((Date.now() - began) / 1000)} s |`)

for (const result of results.filter((result) => !ok(result))) {
  console.log(`\n${result.name} (exit ${result.code}) — ${join(logs, result.name + '.log')}`)
  console.log(result.output.split('\n').filter(Boolean).slice(-25).join('\n'))
}
process.exit(results.every(ok) ? 0 : 1)

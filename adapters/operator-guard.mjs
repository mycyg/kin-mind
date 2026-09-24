import {execFileSync} from 'node:child_process';

/** Command-line entries whose effect reaches past the host (a message or a file for
 * the owner, a stopped service) are for a person at a terminal. They need
 * `--operator`, and they refuse to run from the phone session's own tools.
 *
 * The phone's app-server is started with KIN_PHONE_SESSION=1. Codex hands its
 * environment to every shell command of the session but not to MCP servers, so the
 * session's tools (the memory server and what it starts) are unaffected. The
 * process tree is checked as well, for a command started with a cleaned
 * environment: an ancestor that is an app-server running from a host's pinned
 * mobile runtime (`…/state/mobile-runtime/…`). A desktop Codex app-server is not the
 * phone session; the operator may work through it.
 *
 * This is not a security boundary (the session has full machine access). It keeps
 * the model from improvising around a missing tool with a host script. */
export const PHONE_SESSION_ENV = 'KIN_PHONE_SESSION';
export const OPERATOR_FLAG = '--operator';
const PHONE_APP_SERVER = /\/state\/mobile-runtime\/\S*\bcodex\b.*\sapp-server(?:\s|$)/;

/** Every process as {pid, ppid, command}, from one `ps` call. */
export function processTable(run = () => execFileSync('/bin/ps', ['-A', '-o', 'pid=,ppid=,command='], {encoding: 'utf8', timeout: 5000})) {
  const table = new Map();
  for (const line of run().split('\n')) {
    const match = /^\s*(\d+)\s+(\d+)\s(.*)$/.exec(line);
    if (match) table.set(Number(match[1]), {pid: Number(match[1]), ppid: Number(match[2]), command: match[3]});
  }
  return table;
}

/** The nearest ancestor of `pid` (itself excluded) that is the phone's app-server. */
export function phoneAppServerAncestor({pid = process.pid, table = processTable()} = {}) {
  let current = table.get(pid)?.ppid;
  for (let depth = 0; current && current > 1 && depth < 64; depth++) {
    const entry = table.get(current);
    if (!entry) return null;
    if (PHONE_APP_SERVER.test(entry.command)) return entry;
    current = entry.ppid;
  }
  return null;
}

/** Why this entry may not run, or null. `allowParent` names the one legitimate
 * in-session caller of an entry (the memory server for owner-files); `flag: false`
 * keeps only the phone-session refusal, for entries automation starts too. */
export function operatorRefusal({name, argv = process.argv, env = process.env, pid = process.pid, table,
  flag = true, allowParent = null} = {}) {
  let rows = table;
  const lookup = () => rows ??= processTable();
  if (allowParent) {
    try {
      const self = lookup().get(pid), parent = self && lookup().get(self.ppid);
      if (parent && allowParent.test(parent.command) && env[PHONE_SESSION_ENV] !== '1') return null;
    } catch {}
  }
  if (env[PHONE_SESSION_ENV] === '1')
    return `${name} does not run from the phone session's tools; use the session's own tool, or ask the owner`;
  let ancestor = null;
  try { ancestor = phoneAppServerAncestor({pid, table: lookup()}); } catch {}
  if (ancestor) return `${name} does not run inside the phone session's app-server (pid ${ancestor.pid})`;
  if (flag && !argv.includes(OPERATOR_FLAG)) return `${name} has an effect outside the host; run it by hand with ${OPERATOR_FLAG}`;
  return null;
}

/** For a command-line entry: print the refusal and exit 2, or return. */
export function requireOperator(options) {
  const refusal = operatorRefusal(options);
  if (!refusal) return;
  console.error(refusal);
  process.exit(2);
}

import fs from 'node:fs';
import {createHash} from 'node:crypto';

export const personaDigest = text => createHash('sha256').update(text).digest('hex');

/** What the owner's approval record names: the version and the hash of every part. */
export const APPROVAL_FIELDS = ['version', 'core_sha256', 'voice_sha256', 'maintenance_sha256'];
const HEX64 = /^[0-9a-f]{64}$/;

/** The gaps in an approval record, in the rehearsal's codes (kin_mind.deploy_checks):
 * `approval-record-missing`, or `approval-record-incomplete:<field>` for each field that is
 * absent or malformed. A record without a part's hash would let that part change unseen,
 * so an incomplete record approves nothing, not even the text it does name. */
export function approvalRecordProblems(approved) {
  if (!approved || typeof approved !== 'object' || Array.isArray(approved)) return ['approval-record-missing'];
  return APPROVAL_FIELDS.filter(field => field === 'version'
    ? typeof approved.version !== 'string' || !approved.version.trim()
    : typeof approved[field] !== 'string' || !HEX64.test(approved[field]))
    .map(field => 'approval-record-incomplete:' + field);
}

/** Read and self-check the persona file. Its `*_sha256` fields and
 * `requires_owner_confirmation` sit in the same file as the text, so they only
 * catch accidental damage: whoever rewrites the text can rewrite them too. The
 * owner's approval is the record the host keeps outside this file (mind-config
 * `persona_contract`: all of APPROVAL_FIELDS); pass it as `approved` and the file is
 * read only as that record names it. An incomplete record is refused whatever the
 * file holds. Without `approved` only the file itself is checked, which proves no
 * approval: a caller that uses the persona passes the record. */
export function readPersonaContract(file, approved = null) {
  if (approved !== null) {
    const problems = approvalRecordProblems(approved);
    if (problems.length) throw Object.assign(Error(`Persona approval record is ${problems[0] === 'approval-record-missing' ? 'missing' : 'incomplete'} (${problems.join(', ')}): needs host review`),
      {code: problems[0].split(':')[0], problems});
  }
  let policy;
  try {
    policy = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (policy.schema !== 1 || policy.requires_owner_confirmation !== true || !policy.version || !policy.approved_source) throw Error();
    for (const name of ['core', 'voice', 'maintenance']) {
      if (typeof policy[name] !== 'string' || !policy[name] || policy[name].length > 16000 || personaDigest(policy[name]) !== policy[name + '_sha256']) throw Error();
    }
    if (!policy.core.startsWith('【MY_PERSONA_LOAD】') || !policy.core.trimEnd().endsWith('【/MY_PERSONA_LOAD】')) throw Error();
  } catch {
    throw Error('Persona contract needs host review');
  }
  if (approved !== null && APPROVAL_FIELDS.some(field => approved[field] !== policy[field]))
    throw Error('Persona contract differs from the approved record');
  return policy;
}

export function assertPersonaPrefix(instructions, policy) {
  const end = instructions.indexOf('【/MY_PERSONA_LOAD】');
  if (end < 0 || instructions.slice(0, end + '【/MY_PERSONA_LOAD】'.length) !== policy.core.trimEnd()
      || instructions.indexOf('# SOUL') < end) {
    throw Error('Core persona changes require explicit owner approval');
  }
}

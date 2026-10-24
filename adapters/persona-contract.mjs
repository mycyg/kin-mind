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

/** The policy file seeds the editable persona. Historical approval fields are metadata. */
export function readPersonaContract(file) {
  const policy=JSON.parse(fs.readFileSync(file,'utf8'));
  for(const name of ['core','voice','maintenance']) {
    if(typeof policy[name]!=='string')throw Error(`Persona ${name} must be text`);
    policy[name+'_sha256']=personaDigest(policy[name]);
  }
  return {...policy,version:policy.version||'initial'};
}

// Kept for callers reading old candidate metadata; there is no frozen persona prefix.
export function assertPersonaPrefix(instructions) {
  if(typeof instructions!=='string')throw Error('Persona instructions must be text');
}

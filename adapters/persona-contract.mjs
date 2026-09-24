import fs from 'node:fs';
import {createHash} from 'node:crypto';

export const personaDigest = text => createHash('sha256').update(text).digest('hex');

/** Read and self-check the persona file. Its `*_sha256` fields and
 * `requires_owner_confirmation` sit in the same file as the text, so they only
 * catch accidental damage: whoever rewrites the text can rewrite them too. The
 * owner's approval is the record the host keeps outside this file (its
 * `{version, core_sha256, voice_sha256?, maintenance_sha256?}`); pass it as
 * `approved` and a file that differs from it is refused. */
export function readPersonaContract(file, approved = null) {
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
  if (approved !== null && (approved?.version !== policy.version || approved.core_sha256 !== policy.core_sha256 ||
      ['voice', 'maintenance'].some(name => approved[name + '_sha256'] !== undefined && approved[name + '_sha256'] !== policy[name + '_sha256'])))
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

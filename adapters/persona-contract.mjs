import fs from 'node:fs';
import {createHash} from 'node:crypto';

export const personaDigest = text => createHash('sha256').update(text).digest('hex');

export function readPersonaContract(file) {
  try {
    const policy = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (policy.schema !== 1 || policy.requires_owner_confirmation !== true || !policy.version || !policy.approved_source) throw Error();
    for (const name of ['core', 'voice', 'maintenance']) {
      if (typeof policy[name] !== 'string' || !policy[name] || policy[name].length > 16000 || personaDigest(policy[name]) !== policy[name + '_sha256']) throw Error();
    }
    if (!policy.core.startsWith('【MY_PERSONA_LOAD】') || !policy.core.trimEnd().endsWith('【/MY_PERSONA_LOAD】')) throw Error();
    return policy;
  } catch {
    throw Error('Persona contract needs host review');
  }
}

export function assertPersonaPrefix(instructions, policy) {
  const end = instructions.indexOf('【/MY_PERSONA_LOAD】');
  if (end < 0 || instructions.slice(0, end + '【/MY_PERSONA_LOAD】'.length) !== policy.core.trimEnd()
      || instructions.indexOf('# SOUL') < end) {
    throw Error('Core persona changes require explicit owner approval');
  }
}

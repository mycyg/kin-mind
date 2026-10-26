import fs from 'node:fs';
import {createHash} from 'node:crypto';

export const personaDigest = text => createHash('sha256').update(text).digest('hex');

/** The policy file seeds the editable persona; its hashes are metadata, not an approval. */
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

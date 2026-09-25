/** What a copy kept outside the store holds once it has lost its words (CL6-MM-07).
 *
 * The host keeps a few copies of what the mind wrote, next to the store rather than in it: the
 * status file, a creation's final answer in its working directory. A delete reaches the store and
 * not them, so they keep only what names or classifies something -- identifiers, states and codes,
 * model and executor names, paths and locators, times, hashes, numbers -- and never words. The
 * same rule as kin_mind/workdirs.py, which applies it to an exploration's working directory. */
export const ERASED='[已删除]';
// No space, nothing outside ASCII, no query string (`?`, `&` may carry the words of a search), not long.
const KEPT=/^[A-Za-z0-9_.:\/@#%+=,~-]{0,256}$/;

/** `value` with every string that does not name or classify something replaced by ERASED; its
 * keys, numbers, booleans and nulls as they were. */
export function withoutWords(value) {
  if(Array.isArray(value))return value.map(withoutWords);
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,withoutWords(item)]));
  if(typeof value==='string')return KEPT.test(value)?value:ERASED;
  return value;
}

/** `value` without the named keys, then without words. */
export function withoutWordsOf(value,dropped=[]) {
  if(!value||typeof value!=='object'||Array.isArray(value))return withoutWords(value);
  return withoutWords(Object.fromEntries(Object.entries(value).filter(([key])=>!dropped.includes(key))));
}

/** What a copy kept outside the store holds once it has lost its words (CL6-MM-07, CL7B-MM-05).
 *
 * The host keeps a few copies of what the mind wrote, next to the store rather than in it: the
 * status file, a creation's final answer in its working directory. A delete reaches the store and
 * not them, so they keep only what names or classifies something -- identifiers, states and codes,
 * model and executor names, paths and locators, times, hashes, numbers -- and never words. The
 * same rule, word for word, as kin_mind/workdirs.py, which applies it to an exploration's working
 * directory; tests/business/helpers/without-words-cases.json holds the cases both answer the same. */
export const ERASED='[已删除]';
// A string that may stay: no space, nothing outside ASCII, no query string (`?`, `&`) and no
// percent-encoding (`%`) -- either carries words of its own -- and not long.
const KEPT=/^[A-Za-z0-9_.:\/@#+=,~-]{0,256}$/;
// One word, letters alone, whatever separators stand at its ends.
const WORD=/^[_.:\/@#+=,~-]*[A-Za-z]+[_.:\/@#+=,~-]*$/;
// An email address, wherever it stands in the string, names a person.
const EMAIL=/[A-Za-z0-9_.+=~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/;
// The fields whose value is a state, a code or a name that the host, the mind, an executor or its
// tools give -- never a model's or a person's word. A single word stays in one of these, or where it
// is the name of a key of the same document; anywhere else it is a word. A code field missing here
// loses a one-word value to ERASED; `state` is here because the host's health and audit read it, and
// a receipt's `truncated_by` and `usage_status` because an operator reads there why a fork's reads
// were cut short and whether a call's usage was reported (CL8-MM-04).
export const CODE_KEYS=Object.freeze([
  'state','status','result_state','stage','outcome',
  'channel','provider','model','reasoning','executor','backend','adapter','server','tool',
  'kind','type','item_type','content_type','class','code','category','error','error_tags',
  'retry_condition','authority','basis','actor','origin',
  'tier','lane','stimulus','waiting_reason','repair_reason','reason_withheld',
  'truncated_by','usage_status',
]);
const codeKeys=new Set(CODE_KEYS);
// The fields that hold a code where the host writes them and a model's own word elsewhere: only their
// codes stay. `role` is whose turn a message is, and in a graph relation what the model called someone
// (erasure.py, CL6-MM-09) (CL8-MM-06).
export const CODE_VALUES=Object.freeze({role:Object.freeze(['user','assistant','system','tool','developer'])});
const codeValues=new Map(Object.entries(CODE_VALUES).map(([key,values])=>[key,new Set(values)]));

/** Whether the string `value` names or classifies something, as it stands under `key` in a
 * document whose keys are `keys`. */
export function kept(value,key=null,keys=new Set()) {
  if(!KEPT.test(value)||EMAIL.test(value))return false;
  if(WORD.test(value))return codeKeys.has(key)||keys.has(value)||(codeValues.get(key)?.has(value)??false);
  return true;
}
const isObject=value=>Boolean(value)&&typeof value==='object'&&!Array.isArray(value);
/** Every key of the document `value`, at any depth. */
export function keysOf(value,found=new Set()) {
  if(Array.isArray(value))for(const item of value)keysOf(item,found);
  else if(isObject(value))for(const [key,item] of Object.entries(value)){found.add(key);keysOf(item,found);}
  return found;
}

/** `value` with every string that does not name or classify something replaced by ERASED; its
 * keys, numbers, booleans and nulls as they were. */
export function withoutWords(value) {
  const keys=keysOf(value);
  const walk=(item,key)=>{
    if(Array.isArray(item))return item.map(inner=>walk(inner,key));
    if(isObject(item))return Object.fromEntries(Object.entries(item).map(([name,inner])=>[name,walk(inner,name)]));
    if(typeof item==='string')return kept(item,key,keys)?item:ERASED;
    return item;
  };
  return walk(value,null);
}

/** `value` without the named keys, then without words. */
export function withoutWordsOf(value,dropped=[]) {
  if(!isObject(value))return withoutWords(value);
  return withoutWords(Object.fromEntries(Object.entries(value).filter(([key])=>!dropped.includes(key))));
}

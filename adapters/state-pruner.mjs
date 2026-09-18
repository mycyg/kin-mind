/** Settled state files move out of the directories the hosts walk, and never out
 * of reach.
 *
 * The directories a host keeps its evidence in only grow. Every preflight parses
 * every file in the outbox; every start-up re-reads every receipt. None of that
 * evidence may be thrown away — it is what stops the same thing being said to the
 * owner twice — so the only thing on offer is to move what is finished somewhere
 * the hot paths do not walk, and to teach the readers that a miss means "look in
 * the archive", not "it never happened".
 *
 * Three rules shape everything below, and each is structural rather than a
 * warning next to the code:
 *
 * - **Nothing is deleted.** There is no unlink in this module that is not the
 *   second half of a move whose first half is already on the disk and verified.
 *   An archive slot that is already taken is left alone and reported; it is never
 *   overwritten, because overwriting is deleting with extra steps.
 * - **Age comes from inside the record.** `mtime` lies here: reconciliation
 *   rewrites finished records, so the youngest-looking file in the outbox can be
 *   the oldest thing in it. A record that carries no usable time of its own has no
 *   age, and something with no age is never old enough to move.
 * - **Planning and moving are different functions.** `plan()` reads and returns;
 *   `apply()` is the only thing that touches a directory. The plan names every
 *   source and every destination, so the report is the thing that happens — and it
 *   can be read, disputed and thrown away before any of it does.
 */
import fs from 'node:fs';
import path from 'node:path';
import {createHash,randomBytes} from 'node:crypto';
import {readJsonFile} from './atomic-json.mjs';

/** States that are never archived, whatever a caller configures. Each one names
 * a record whose story is not over: something was begun and its outcome is not
 * yet known, or was prepared and not yet begun. Moving one of those is how a
 * message gets sent twice, so this list is a floor under the configuration
 * rather than a default for it. */
export const NEVER_ARCHIVED=Object.freeze(['pending','unconfirmed','prepared','not-submitted']);

/** Anything that says work is still in flight, at any depth of a record. A group
 * whose own state is settled but which still holds one unsent bubble is not
 * settled; the cost of keeping such a file is a few bytes, and the cost of moving
 * it is the owner hearing the same thing again. */
export const LIVE_STATES=Object.freeze([...NEVER_ARCHIVED,'unsent','submitting','sending','selected','preparing',
  'switching','running','submitted','queued','draft','held','waiting','in_progress','deferred','busy']);

/** The default answer to "is this record finished". Deliberately short: a family
 * that settles under some other word says so itself, and a state this module has
 * never heard of keeps its file. */
export const SETTLED_STATES=Object.freeze(['accepted','canceled','cancelled','silent','merged','retired',
  'superseded','undeliverable','recorded',
  // A routing task is open until it is one of these two, by the router's own
  // definition; a status the router would still call open must not settle here.
  'completed']);

/** Where a record keeps the time something last happened to it. The newest of
 * them wins: a record that was accepted after it was attempted is as old as its
 * acceptance, not as old as the first try. `retryAt` and friends are deliberately
 * absent — they name a future, and a future is not an age. */
export const TIME_FIELDS=Object.freeze(['acceptedAt','checkedAt','attemptedAt','settledAt','decidedAt','canceledAt',
  'receivedAt','createdAt','observedAt','completedAt','at']);

/** Identifiers a record may carry that another live thing could still be holding.
 * Session and conversation ids are deliberately not here: they are the same on
 * every file, so matching them would keep the whole directory for ever. */
export const IDENTIFIER_KEYS=Object.freeze(['id','draftId','draft_id','messageId','message_id','taskId','task_id',
  'inputId','input_id','replyId','reply_id','sourceInputId','commandId','command_id','groupId','group_id',
  'bubbleId','bubble_id','transportId','transport_id','batchId','batch_id','memoryBatchId','grantId','grant_id',
  'outboxId','attemptId','delivery_id','workReviewId','reviewId',
  // A fragment names the bubble it was cut from as `part.of`. The key is a plain
  // word rather than an id-shaped one, and a fragment that outlives its parent is
  // exactly the kind of thing that must not be moved out from under it.
  'of']);
const IDENTIFIER_LISTS=Object.freeze(['inputIds','contextInputIds','sourceInputIds','replacementIds','fulfilledBy','messageIds']);

/** Files that carry the identity of a running process, or the evidence of an
 * interrupted one. They look settled and they are not: a sidecar is what lets a
 * recycled pid be told apart from its previous holder, and a quarantined lock is
 * the only surviving record that an attempt was cut short. */
const PROTECTED_PATTERNS=Object.freeze([/\.pid$/,/\.pid\.owner\.json$/,/^status\.json$/,/^[^.]*\.lock$/]);
/** Directories that are themselves an archive of a kind. Nothing under one of
 * them is ever a candidate, and a family rooted in one is refused outright. */
export const PROTECTED_SEGMENTS=Object.freeze(['broken','quarantine','archive','receipts-archive','.git']);

const DAY=24*60*60*1000;
export const DEFAULT_AGE_MS=30*DAY;
/** A timestamp before this is not a timestamp. It catches the one arithmetic
 * mistake that would empty a directory in a single pass: seconds read as
 * milliseconds, which dates every record to 1970 and makes all of them ancient. */
const EARLIEST_PLAUSIBLE=Date.parse('2001-01-01T00:00:00Z');

const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
const isRecord=value=>Boolean(value)&&typeof value==='object'&&!Array.isArray(value);
const segments=file=>path.resolve(file).split(path.sep);
// A path that climbs out of its parent starts with `..` as a whole segment. A
// sibling merely named `..something` does not, and must not be mistaken for one.
const escapes=rel=>rel===''||rel==='..'||rel.startsWith('..'+path.sep)||path.isAbsolute(rel);
const within=(child,parent)=>{const rel=path.relative(parent,child);return rel===''||!escapes(rel);};

function syncDirectory(directory) {
  let fd;
  try{fd=fs.openSync(directory,'r');fs.fsyncSync(fd);}
  catch{/* Some filesystems refuse a directory fsync; the rename stays atomic. */}
  finally{if(fd!==undefined)fs.closeSync(fd);}
}

/** True for a name this module will not consider under any configuration. */
export function protectedName(name) {
  return PROTECTED_PATTERNS.some(pattern=>pattern.test(name));
}

/** One point in time from inside the record, in milliseconds, or null.
 *
 * Null is the answer for a record with no time, an unparseable one, or one so
 * early that it is more likely a unit mistake than a date — and null means the
 * record is never old enough to move. */
export function recordTime(record,{fields=TIME_FIELDS}={}) {
  if(!isRecord(record))return null;
  let newest=null;
  for(const field of fields) {
    const raw=record[field];
    let value=null;
    if(typeof raw==='number'&&Number.isFinite(raw))value=raw;
    else if(typeof raw==='string'&&raw.trim()){const parsed=Date.parse(raw);if(Number.isFinite(parsed))value=parsed;}
    if(value===null||value<EARLIEST_PLAUSIBLE)continue;
    if(newest===null||value>newest)newest=value;
  }
  return newest;
}

/** Every identifier the record carries that something still running could be
 * holding. Over-collection is the safe direction: an identifier collected in
 * error keeps a file, and one missed moves a file somebody is still using. */
export function recordIdentifiers(record,{keys=IDENTIFIER_KEYS,lists=IDENTIFIER_LISTS,depth=5}={}) {
  const found=new Set(),wanted=new Set(keys),listed=new Set(lists);
  const visit=(value,level)=>{
    if(level>depth||!value||typeof value!=='object')return;
    if(Array.isArray(value)){for(const item of value)visit(item,level+1);return;}
    for(const [key,child] of Object.entries(value)) {
      if(typeof child==='string'){if(child&&wanted.has(key))found.add(child);continue;}
      if(Array.isArray(child)&&listed.has(key))for(const item of child)if(typeof item==='string'&&item)found.add(item);
      visit(child,level+1);
    }
  };
  visit(record,0);
  return found;
}

/** The first still-in-flight state found anywhere in the record, or null. */
export function liveState(record,{live=LIVE_STATES,depth=6}={}) {
  const flagged=new Set(live);
  const visit=(value,level)=>{
    if(level>depth||!value||typeof value!=='object')return null;
    if(Array.isArray(value)){for(const item of value){const seen=visit(item,level+1);if(seen)return seen;}return null;}
    for(const [key,child] of Object.entries(value)) {
      if(key==='state'&&typeof child==='string'&&flagged.has(child))return child;
      const seen=visit(child,level+1);
      if(seen)return seen;
    }
    return null;
  };
  return visit(record,0);
}

// ---------------------------------------------------------------------------
// Reading: the archive is the second place every point lookup looks
// ---------------------------------------------------------------------------

/** Where `file` would live once archived, or null when it is not under `root`.
 *
 * The archive mirrors the live tree exactly. That is what lets a reader that
 * missed find the record with one more `stat` and no search: the archived path is
 * a function of the live path, not a thing that has to be looked up. */
export function archivePathFor(file,{root,archive}={}) {
  if(!root||!archive)return null;
  const rel=path.relative(path.resolve(root),path.resolve(file));
  if(escapes(rel))return null;
  return path.join(path.resolve(archive),rel);
}

/** The path that holds this record now: the live one, else the archived one,
 * else null. */
export function existsThroughArchive(file,{root,archive}={}) {
  if(fs.existsSync(file))return file;
  const archived=archivePathFor(file,{root,archive});
  return archived&&fs.existsSync(archived)?archived:null;
}

/** `readJsonFile` that looks in the archive when the live file is gone, and says
 * which one answered.
 *
 * A live file that is present but corrupt is reported as corrupt and the archive
 * is not consulted: a damaged current revision is a different fact from a moved
 * one, and quietly answering from the archive would hide it. */
export function readThroughArchive(file,{root,archive}={}) {
  const live=readJsonFile(file);
  if(live.state!=='missing')return {...live,source:'live',file};
  const archived=archivePathFor(file,{root,archive});
  if(!archived)return {...live,source:'none',file};
  const kept=readJsonFile(archived);
  return kept.state==='missing'?{...live,source:'none',file}:{...kept,source:'archive',file:archived};
}

// ---------------------------------------------------------------------------
// Surveying: the only reading of directories, and it writes nothing
// ---------------------------------------------------------------------------

/** Read one family's directory into plain records. Files directly in it only:
 * a family's subdirectories hold other families, quarantined revisions or broken
 * locks, and none of those is this family's to reason about.
 *
 * Nothing here writes, renames or removes anything, so a survey of a production
 * directory is as safe to run as `ls`. */
export function surveyFamily(family,{root,archive}={}) {
  const directory=family.directory,extension=family.extension??'.json';
  const settings={...family,root:family.root??root,archive:family.archive??archive};
  const entries=[],unreadable=[],skipped=[];
  let names=[];
  try{names=fs.readdirSync(directory).sort();}
  catch(error){return {...settings,entries,unreadable,skipped,missing:error.code==='ENOENT',error:error.code??'unreadable'};}
  for(const name of names) {
    if(!name.endsWith(extension)){skipped.push({name,reason:'other-extension'});continue;}
    if(protectedName(name)){skipped.push({name,reason:'protected-name'});continue;}
    const file=path.join(directory,name);
    let bytes;
    try{const stat=fs.statSync(file);if(!stat.isFile()){skipped.push({name,reason:'not-a-file'});continue;}bytes=fs.readFileSync(file);}
    catch(error){unreadable.push({name,file,reason:error.code??'unreadable'});continue;}
    let record;
    try{record=JSON.parse(bytes.toString('utf8'));}
    catch{unreadable.push({name,file,reason:'invalid-json'});continue;}
    entries.push({name,file,record,bytes:bytes.length,sha256:digest(bytes)});
  }
  return {...settings,entries,unreadable,skipped,missing:false};
}

/** Survey every family. The families come back in the same order, each with the
 * records it holds. */
export function survey(families,{root,archive}={}) {
  return families.map(family=>surveyFamily(family,{root,archive}));
}

// ---------------------------------------------------------------------------
// Planning: pure, and the thing the report is made of
// ---------------------------------------------------------------------------

/** Order families so that anything a family depends on is planned before it.
 * A cycle is not resolvable and is reported rather than guessed at. */
function orderFamilies(families) {
  const byName=new Map(families.map(family=>[family.name,family]));
  const ordered=[],state=new Map(),cycles=[];
  const visit=(family,trail)=>{
    const seen=state.get(family.name);
    if(seen==='done')return;
    if(seen==='open'){cycles.push([...trail,family.name].join(' → '));return;}
    state.set(family.name,'open');
    const required=family.requires?.family?byName.get(family.requires.family):null;
    if(required)visit(required,[...trail,family.name]);
    state.set(family.name,'done');ordered.push(family);
  };
  for(const family of families)visit(family,[]);
  return {ordered,cycles};
}

function familyKey(family,entry) {
  if(typeof family.key==='function')return family.key(entry.record,entry.name)??null;
  const id=entry.record?.id;
  return typeof id==='string'&&id?id:entry.name.replace(/\.json$/,'');
}

/** Why one entry stays where it is, or null when nothing keeps it. The order of
 * the tests is the order of the argument: what it is, then what state it is in,
 * then how old, then who still wants it. */
function keepReason(entry,family,context) {
  if(protectedName(entry.name))return {reason:'protected-name'};
  if(!isRecord(entry.record))return {reason:'not-a-record'};
  const flagged=liveState(entry.record,{live:context.live});
  if(flagged)return {reason:'still-in-flight',detail:flagged};
  const state=entry.record.state;
  if(typeof state==='string') {
    if(!context.settledFor(family).has(state))return {reason:'not-settled',detail:state};
  } else if(!family.settledByDirectory)return {reason:'no-state',detail:typeof state};
  const at=recordTime(entry.record,{fields:family.timeFields??context.timeFields});
  // A receipt is written as an identity and a digest and nothing else — it has no
  // time of its own, and inventing one from the file would be reading `mtime`
  // again. What it does have is the record it vouches for, which is dated, and
  // which it can only ever move with. So a family may say that its age is its
  // partner's, and then the partner's age is the one that has to pass.
  if(at===null&&!(family.requires&&family.timeFromPartner))return {reason:'no-usable-timestamp'};
  if(at!==null&&at>context.cutoff)return {reason:'too-recent',detail:new Date(at).toISOString()};
  const handles=recordIdentifiers(entry.record,{keys:context.identifierKeys});
  // The name is a handle in its own right, and often the only one. Every point
  // lookup in the hosts opens `<id>.json` or `<digest of id>.json` without ever
  // reading what is inside first, so a file whose name something still resolves
  // stays whether or not the record agrees about its own id.
  for(const handle of [familyKey(family,entry),...fileHandles(entry.name)])if(handle)handles.add(handle);
  const held=[...handles].filter(id=>context.references.has(id));
  if(held.length)return {reason:'referenced',detail:held[0]};
  return null;
}

/** What a reader could have called this file: the name without its last suffix,
 * and the name up to its first dot, which is what a compound one like
 * `<digest>.pending.json` is addressed by. */
function fileHandles(name) {
  return [...new Set([name,name.replace(/\.[^.]*$/,''),name.split('.')[0]])].filter(Boolean);
}

/** What would move, where it would move to, and in what order — and, for
 * everything else, why not.
 *
 * Pure: it takes surveyed families and returns a description. It opens no file,
 * creates no directory and moves nothing, so the same call can be made a hundred
 * times before anybody decides whether to act on it.
 *
 * `references` is the set of identifiers something live still holds — routing
 * tasks that are open, reply groups still waiting, contact batches that have not
 * settled, grants that have not been spent. It is assembled by the host, which is
 * the only place that knows what is running; a record naming any of them stays. */
export function plan({families=[],references=[],now=Date.now(),olderThanMs=DEFAULT_AGE_MS,
  settled=SETTLED_STATES,live=LIVE_STATES,timeFields=TIME_FIELDS,identifierKeys=IDENTIFIER_KEYS,limit=Infinity}={}) {
  const cutoff=now-olderThanMs,warnings=[],held=new Set(references);
  // Said in every report rather than left to the caller to remember: a plan made
  // without the set of things still in use is a plan that names more than may
  // really move, and the difference is not visible by looking at it.
  if(!held.size)warnings.push('the reference set was empty, so nothing was held back for being in use; if a host is running, this over-states what may move');
  const settledCache=new Map();
  const context={cutoff,live,timeFields,identifierKeys,references:held,
    settledFor(family) {
      if(settledCache.has(family.name))return settledCache.get(family.name);
      const wanted=new Set(family.settled??settled),refused=[];
      for(const state of NEVER_ARCHIVED)if(wanted.delete(state))refused.push(state);
      if(refused.length)warnings.push(`${family.name}: ${refused.join(', ')} cannot be archived and was dropped from its settled list`);
      settledCache.set(family.name,wanted);return wanted;
    }};

  const usable=[];
  for(const family of families) {
    if(!family.name){warnings.push('a family with no name was skipped');continue;}
    if(!family.directory){warnings.push(`${family.name}: no directory, skipped`);continue;}
    if(!family.root||!family.archive){warnings.push(`${family.name}: no root or archive, skipped`);continue;}
    if(segments(family.directory).some(part=>PROTECTED_SEGMENTS.includes(part))){warnings.push(`${family.name}: its directory is itself an archive or a quarantine, skipped`);continue;}
    if(!within(family.directory,family.root)){warnings.push(`${family.name}: its directory is not under its root, so no archived path can mirror it, skipped`);continue;}
    if(within(family.directory,family.archive)||within(family.archive,family.directory)){warnings.push(`${family.name}: its archive and its directory contain one another, skipped`);continue;}
    if(family.missing)warnings.push(`${family.name}: ${family.directory} does not exist yet`);
    if(family.error&&!family.missing)warnings.push(`${family.name}: could not be read (${family.error})`);
    usable.push(family);
  }
  const {ordered,cycles}=orderFamilies(usable);
  for(const cycle of cycles)warnings.push(`families depend on one another in a circle and were left alone: ${cycle}`);
  const planned=ordered.filter(family=>!cycles.some(cycle=>cycle.includes(family.name)));

  // First pass: every family judged on its own. Pairing needs to know what the
  // other family decided, so nothing is committed until all of them have spoken.
  const verdicts=new Map(),byKey=new Map(),summaries=[];
  for(const family of planned) {
    const keyed=new Map();
    // Asked for once per family whether or not an entry reaches it, so that a
    // configuration naming a state that can never be archived is reported even
    // when no record happens to be in it.
    context.settledFor(family);
    for(const entry of family.entries??[]) {
      const key=familyKey(family,entry);
      if(key!==null&&!keyed.has(key))keyed.set(key,entry);
      verdicts.set(entry,{family,key,keep:keepReason(entry,family,context)});
    }
    byKey.set(family.name,keyed);
  }

  // Second pass: a record whose settlement is another record's evidence may only
  // move once that other record has, and after it. The outbox record is what a
  // reconciliation walks; its receipt is what stops the reconciliation from
  // queueing the same delivery again. Move the receipt first and an interruption
  // leaves the record with nothing to vouch for it, and the owner hears it twice.
  // Move the record first and every interruption leaves a receipt with nothing
  // left to vouch for, which costs a few bytes and nothing else.
  for(const family of planned) {
    if(!family.requires)continue;
    const required=planned.find(other=>other.name===family.requires.family);
    if(!required){warnings.push(`${family.name}: it depends on ${family.requires.family}, which is not in this plan, so none of it moves`);
      for(const entry of family.entries??[])verdicts.get(entry).keep??={reason:'paired-family-absent'};continue;}
    const keyed=byKey.get(required.name);
    for(const entry of family.entries??[]) {
      const verdict=verdicts.get(entry);
      if(verdict.keep)continue;
      let key=null;
      try{key=family.requires.key?.(entry.record,entry.name)??null;}catch{key=null;}
      if(typeof key!=='string'||!key){verdict.keep={reason:'unpaired'};continue;}
      const partner=keyed.get(key);
      // No live partner: either it was archived in an earlier pass or it never
      // existed. Either way nothing walks it any more, so this record is free —
      // unless it was relying on that partner to say how old it is.
      if(!partner) {
        if(family.timeFromPartner&&recordTime(entry.record,{fields:family.timeFields??timeFields})===null)verdict.keep={reason:'no-usable-timestamp'};
        else verdict.pairedWith=null;
        continue;
      }
      const partnerVerdict=verdicts.get(partner);
      if(partnerVerdict.keep){verdict.keep={reason:'paired-record-stays',detail:partnerVerdict.keep.reason};continue;}
      verdict.pairedWith={family:required.name,name:partner.name};
    }
  }

  const moves=[],kept=[];
  for(const family of planned) {
    const counts={},archive=path.resolve(family.archive);
    let bytes=0,movingBytes=0,moving=0;
    for(const entry of family.entries??[]) {
      bytes+=entry.bytes;
      const verdict=verdicts.get(entry);
      if(verdict.keep){counts[verdict.keep.reason]=(counts[verdict.keep.reason]??0)+1;
        kept.push({family:family.name,name:entry.name,file:entry.file,...verdict.keep});continue;}
      const to=archivePathFor(entry.file,{root:family.root,archive});
      if(!to){counts['no-archived-path']=(counts['no-archived-path']??0)+1;
        kept.push({family:family.name,name:entry.name,file:entry.file,reason:'no-archived-path'});continue;}
      moving++;movingBytes+=entry.bytes;
      moves.push({family:family.name,name:entry.name,from:entry.file,to,key:verdict.key,
        sha256:entry.sha256,bytes:entry.bytes,at:recordTime(entry.record,{fields:family.timeFields??timeFields}),
        state:typeof entry.record.state==='string'?entry.record.state:null,
        ...(verdict.pairedWith?{pairedWith:verdict.pairedWith}:{})});
    }
    summaries.push({name:family.name,directory:family.directory,archive,files:(family.entries??[]).length,bytes,
      archivedDirectory:archivePathFor(family.directory,{root:family.root,archive}),
      moving,movingBytes,unreadable:(family.unreadable??[]).length,kept:counts,missing:Boolean(family.missing)});
    for(const bad of family.unreadable??[])kept.push({family:family.name,name:bad.name,file:bad.file,reason:'unreadable',detail:bad.reason});
  }

  // The cap trims from the end, and dependents are ordered after what they
  // depend on, so a cap can only ever leave a receipt behind — never strand one.
  const deferred=moves.length>limit?moves.splice(limit):[];
  if(deferred.length) {
    warnings.push(`${deferred.length} more could move; this pass is capped at ${limit}`);
    // The per-family figures were counted before the cap. What the report says
    // would move has to be what the plan actually holds, or the two disagree.
    for(const summary of summaries) {
      const left=moves.filter(move=>move.family===summary.name);
      summary.moving=left.length;summary.movingBytes=left.reduce((total,move)=>total+move.bytes,0);
    }
  }

  return {at:new Date(now).toISOString(),cutoff:new Date(cutoff).toISOString(),olderThanMs,
    order:planned.map(family=>family.name),families:summaries,moves,kept,deferred:deferred.length,warnings,
    counts:{files:summaries.reduce((total,f)=>total+f.files,0),bytes:summaries.reduce((total,f)=>total+f.bytes,0),
      moving:moves.length,movingBytes:moves.reduce((total,move)=>total+move.bytes,0),
      kept:kept.length,unreadable:summaries.reduce((total,f)=>total+f.unreadable,0)}};
}

// ---------------------------------------------------------------------------
// Documents: counted, never rewritten
// ---------------------------------------------------------------------------

/** How much of one big state document is settled history.
 *
 * A file-mover cannot help a document that holds hundreds of finished records
 * inside it and is rewritten whole on every event. Pruning those records is a
 * different mechanism with different risks — the dedupe record that refuses a
 * replayed input lives in there — so this counts them and says how much they
 * weigh, and changes nothing. It exists so that a dry-run report does not go
 * quiet about the largest file it can see. */
export function documentPressure(document,{collections=[],references=[],now=Date.now(),olderThanMs=DEFAULT_AGE_MS,
  settled=SETTLED_STATES,live=LIVE_STATES,timeFields=TIME_FIELDS,identifierKeys=IDENTIFIER_KEYS}={}) {
  const cutoff=now-olderThanMs,held=new Set(references),allowed=new Set(settled);
  for(const state of NEVER_ARCHIVED)allowed.delete(state);
  const report=[];
  for(const name of collections) {
    const collection=document?.[name];
    if(!isRecord(collection)){report.push({name,present:false});continue;}
    const rows=Object.entries(collection);
    let stale=0,staleBytes=0,inFlight=0,referenced=0,undatable=0;
    for(const [key,row] of rows) {
      const bytes=Buffer.byteLength(JSON.stringify(row)??'null');
      if(liveState(row,{live})){inFlight++;continue;}
      if(typeof row?.state==='string'&&!allowed.has(row.state)){inFlight++;continue;}
      const at=recordTime(row,{fields:timeFields});
      if(at===null){undatable++;continue;}
      if(at>cutoff)continue;
      const ids=recordIdentifiers(row,{keys:identifierKeys});ids.add(key);
      if([...ids].some(id=>held.has(id))){referenced++;continue;}
      stale++;staleBytes+=bytes;
    }
    report.push({name,present:true,rows:rows.length,bytes:Buffer.byteLength(JSON.stringify(collection)??'null'),
      stale,staleBytes,inFlight,referenced,undatable});
  }
  return {at:new Date(now).toISOString(),cutoff:new Date(cutoff).toISOString(),collections:report,
    bytes:Buffer.byteLength(JSON.stringify(document)??'null')};
}

/** Every identifier held by a record in this document that is not finished.
 *
 * One of the four sources a reference set is made of — the routing tasks and
 * inputs a host has open. Reply groups still waiting, contact batches that have
 * not settled and grants that have not been spent live in their own files and
 * have to be added to this; a set built from the document alone is a partial one
 * and a plan made from it will name records that something else still holds. */
export function liveReferences(document,{collections=[],settled=SETTLED_STATES,live=LIVE_STATES,identifierKeys=IDENTIFIER_KEYS}={}) {
  const allowed=new Set(settled),found=new Set();
  for(const state of NEVER_ARCHIVED)allowed.delete(state);
  const finished=row=>{
    if(liveState(row,{live}))return false;
    for(const field of ['state','status'])if(typeof row?.[field]==='string'&&!allowed.has(row[field]))return false;
    return typeof row?.state==='string'||typeof row?.status==='string';
  };
  for(const name of collections) {
    const collection=document?.[name];
    if(!isRecord(collection))continue;
    for(const [key,row] of Object.entries(collection)) {
      if(finished(row))continue;
      found.add(key);
      for(const id of recordIdentifiers(row,{keys:identifierKeys}))found.add(id);
    }
  }
  return found;
}

// ---------------------------------------------------------------------------
// The report
// ---------------------------------------------------------------------------

const size=bytes=>bytes>=1048576?(bytes/1048576).toFixed(1)+' MB':bytes>=1024?(bytes/1024).toFixed(1)+' KB':bytes+' B';

/** The plan as something a person reads before deciding. Every move is named,
 * source and destination, in the order it would happen. */
export function formatPlan(plan,{documents=null,limit=Infinity,applied=null}={}) {
  const lines=[];
  lines.push(applied?`State pruning — ${applied.moved.length} moved, ${applied.skipped.length} left alone`
    :'State pruning plan — a dry run. Nothing below has moved.');
  lines.push(`Generated ${plan.at}; a record is old enough at ${plan.cutoff} (${Math.round(plan.olderThanMs/DAY)} days).`);
  lines.push('');
  lines.push('Family                       files      bytes    would move');
  for(const family of plan.families) {
    lines.push(`  ${family.name.padEnd(26)}${String(family.files).padStart(5)}${size(family.bytes).padStart(11)}`+
      `${(family.moving+' ('+size(family.movingBytes)+')').padStart(15)}${family.missing?'   [no such directory]':''}`);
    const reasons=Object.entries(family.kept).sort((a,b)=>b[1]-a[1]);
    if(reasons.length)lines.push('      kept: '+reasons.map(([reason,count])=>`${count} ${reason}`).join(', '));
    if(family.unreadable)lines.push(`      ${family.unreadable} could not be parsed and are never candidates`);
  }
  lines.push('');
  lines.push(`Total ${plan.counts.files} files, ${size(plan.counts.bytes)}; ${plan.counts.moving} would move (${size(plan.counts.movingBytes)}).`);
  if(plan.deferred)lines.push(`${plan.deferred} further candidates are held back by this pass's cap.`);
  if(documents) {
    lines.push('');
    lines.push('Documents — counted only. A file-mover never rewrites these.');
    lines.push(`  ${size(documents.bytes)} in all`);
    for(const collection of documents.collections) {
      if(!collection.present){lines.push(`  ${collection.name}: absent`);continue;}
      lines.push(`  ${collection.name}: ${collection.rows} record${collection.rows===1?'':'s'}, ${size(collection.bytes)} — `+
        `${collection.stale} settled and old enough (${size(collection.staleBytes)}), ${collection.inFlight} still in flight, `+
        `${collection.referenced} still referenced, ${collection.undatable} with no usable timestamp`);
    }
  }
  if(plan.moves.length) {
    lines.push('');
    lines.push(`Moves, in the order they would be made (${plan.moves.length}):`);
    for(const [index,move] of plan.moves.slice(0,limit).entries())
      lines.push(`  ${String(index+1).padStart(4)}. ${move.family}/${move.name}  ${move.state??'—'}  ${move.at?new Date(move.at).toISOString():'—'}`+
        `${move.pairedWith?`  after ${move.pairedWith.family}/${move.pairedWith.name}`:''}`);
    if(plan.moves.length>limit)lines.push(`  … and ${plan.moves.length-limit} more`);
    lines.push('');
    lines.push('Each keeps its own name, one directory over:');
    for(const family of plan.families)if(family.moving)lines.push(`  ${family.directory}\n    →  ${family.archivedDirectory}`);
  }
  if(applied?.skipped.length) {
    lines.push('');
    lines.push('Left alone while applying:');
    const counts={};
    for(const skip of applied.skipped)counts[skip.reason]=(counts[skip.reason]??0)+1;
    for(const [reason,count] of Object.entries(counts).sort((a,b)=>b[1]-a[1]))lines.push(`  ${count} ${reason}`);
  }
  if(plan.warnings.length) {
    lines.push('');
    lines.push('Warnings:');
    for(const warning of plan.warnings)lines.push(`  - ${warning}`);
  }
  lines.push('');
  lines.push(applied?'Nothing was deleted; every file above is at its archived path.'
    :'Nothing has been moved. Only apply(plan,{dryRun:false}) moves anything, and it moves only what is listed above.');
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Applying: the only code here that changes a directory
// ---------------------------------------------------------------------------

/** Move one file to its archived path, or explain why it stayed.
 *
 * The source is read again and compared against what was planned, so a record
 * that changed between the report and the decision is left where it is: the
 * argument was had about bytes that no longer exist. An occupied archive slot is
 * never overwritten — that would be the one deletion this module does not do. */
function moveOne(move) {
  if(fs.existsSync(move.to))return {state:'skipped',reason:'archive-occupied'};
  let bytes;
  try{bytes=fs.readFileSync(move.from);}
  catch(error){return {state:'skipped',reason:error.code==='ENOENT'?'source-gone':'source-unreadable'};}
  if(digest(bytes)!==move.sha256)return {state:'skipped',reason:'changed-since-plan'};
  fs.mkdirSync(path.dirname(move.to),{recursive:true,mode:0o700});
  try{fs.renameSync(move.from,move.to);}
  catch(error) {
    if(error.code!=='EXDEV')throw error;
    // A different filesystem cannot be renamed into. The bytes land whole under a
    // name nothing reads, are checked against what was planned, and only then is
    // the source let go: at no moment do fewer than one copy exist.
    const staging=move.to+'.'+process.pid+'.'+randomBytes(8).toString('hex')+'.tmp';
    const fd=fs.openSync(staging,'wx',0o600);
    try{fs.writeFileSync(fd,bytes);fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
    if(digest(fs.readFileSync(staging))!==move.sha256){fs.rmSync(staging,{force:true});return {state:'skipped',reason:'copy-mismatch'};}
    fs.renameSync(staging,move.to);
    syncDirectory(path.dirname(move.to));
    fs.rmSync(move.from,{force:true});
  }
  syncDirectory(path.dirname(move.to));syncDirectory(path.dirname(move.from));
  return {state:'moved'};
}

/** Carry out a plan. `dryRun` defaults to true, so a caller that has not decided
 * yet has decided nothing: the report comes back and the directories are as they
 * were. Only `{dryRun:false}` moves a file.
 *
 * The plan's own order is obeyed exactly, which is what makes "the record before
 * its receipt" hold at every interruption point rather than only at the end. */
export function apply(plan,{dryRun=true}={}) {
  const moved=[],skipped=[];
  for(const move of plan.moves) {
    if(dryRun){skipped.push({...move,reason:'dry-run'});continue;}
    // A pair is only ever as good as its first half: if the record stayed, its
    // receipt stays too, whatever the plan said a moment ago.
    if(move.pairedWith&&!moved.some(done=>done.family===move.pairedWith.family&&done.name===move.pairedWith.name))
      {skipped.push({...move,reason:'paired-record-did-not-move'});continue;}
    // One file that cannot be moved is one file left where it is, never a pass
    // that stops half way. The pairing guard above still holds either way: a
    // receipt only ever follows a record that is already in `moved`.
    let result;
    try{result=moveOne(move);}
    catch(error){result={state:'skipped',reason:error.code??'move-failed'};}
    if(result.state==='moved')moved.push(move);else skipped.push({...move,reason:result.reason});
  }
  return {dryRun,moved,skipped,bytes:moved.reduce((total,move)=>total+move.bytes,0)};
}

/** Survey, plan, and report. The tick a host runs on a timer: with `dryRun` left
 * alone it reads the directories, writes the report and moves nothing, which is
 * how this ships and how it stays until somebody has read one. */
export function prunePass({families=[],root,archive,references=[],now=Date.now(),olderThanMs=DEFAULT_AGE_MS,
  limit=Infinity,dryRun=true,document=null,collections=[]}={}) {
  const surveyed=survey(families,{root,archive});
  const made=plan({families:surveyed,references,now,olderThanMs,limit});
  const documents=document?documentPressure(document,{collections,references,now,olderThanMs}):null;
  const applied=apply(made,{dryRun});
  return {plan:made,documents,applied,report:formatPlan(made,{documents,applied:dryRun?null:applied})};
}

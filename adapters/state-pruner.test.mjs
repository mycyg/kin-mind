import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {survey,plan,apply,prunePass,formatPlan,documentPressure,recordTime,recordIdentifiers,liveState,
  archivePathFor,readThroughArchive,existsThroughArchive,protectedName,liveReferences,
  NEVER_ARCHIVED,SETTLED_STATES,DEFAULT_AGE_MS} from './state-pruner.mjs';
import {ReplyGuard} from './reply-guard.mjs';
import {MemoryEventJournal} from './memory-events.mjs';
import {workEvidence} from './work-review-evidence.mjs';

// Every directory, record and clock below is built here. Nothing asks this
// machine what it happens to be holding: a pruner tested against a real state
// directory would pass or fail by accident, and the cases that matter most —
// a record older than its own file, a receipt whose partner has to stay — are
// not arrangeable there at all.
const NOW=Date.parse('2026-09-18T12:00:00Z');
const ago=days=>new Date(NOW-days*24*60*60*1000).toISOString();
const temp=t=>{const dir=fs.mkdtempSync(path.join(os.tmpdir(),'kin-pruner-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));return dir;};
const sha=value=>createHash('sha256').update(value).digest('hex');

/** A state root with an archive beside it, and a `put` that writes one record. */
function state(t) {
  const base=temp(t),root=path.join(base,'state'),archive=path.join(base,'archive');
  const put=(relative,record,{mtime=null}={})=>{
    const file=path.join(root,relative);
    fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});
    fs.writeFileSync(file,JSON.stringify(record),{mode:0o600});
    if(mtime!==null)fs.utimesSync(file,new Date(mtime),new Date(mtime));
    return file;
  };
  const family=(name,directory,extra={})=>({name,directory:path.join(root,directory),root,archive,...extra});
  const names=directory=>{try{return fs.readdirSync(path.join(root,directory)).sort();}catch{return [];}};
  const archived=directory=>{try{return fs.readdirSync(path.join(archive,directory)).sort();}catch{return [];}};
  return {base,root,archive,put,family,names,archived};
}
/** Everything the plan would move, counted the only way that matters afterwards. */
const filesUnder=dir=>{
  const found=[];
  const walk=at=>{for(const entry of fs.existsSync(at)?fs.readdirSync(at,{withFileTypes:true}):[]){
    const file=path.join(at,entry.name);if(entry.isDirectory())walk(file);else found.push(file);}};
  walk(dir);return found.sort();
};
const settledDelivery=(id,days,extra={})=>({id,state:'accepted',messageId:'m-'+id,text:'…',acceptedAt:ago(days),attemptedAt:ago(days+1),...extra});
const run=(st,families,options={})=>plan({families:survey(families,{root:st.root,archive:st.archive}),now:NOW,...options});
const moving=made=>made.moves.map(move=>move.family+'/'+move.name).sort();

// ---------------------------------------------------------------------------
// Age, and where it is allowed to come from
// ---------------------------------------------------------------------------

test('age comes from inside the record, never from the file the record is in',t=>{
  const st=state(t);
  // Reconciliation rewrites finished records, so the file's own timestamps say
  // the opposite of the truth here. The pruner has to believe the record.
  st.put('outbox/old.json',settledDelivery('old',400),{mtime:NOW});
  st.put('outbox/fresh.json',settledDelivery('fresh',1),{mtime:NOW-500*24*60*60*1000});
  const made=run(st,[st.family('outbox','outbox')]);
  assert.deepEqual(moving(made),['outbox/old.json']);
  assert.equal(made.kept.find(k=>k.name==='fresh.json').reason,'too-recent');
});

test('a record with no time this module can trust is never old enough to move',t=>{
  const st=state(t);
  st.put('outbox/none.json',{id:'none',state:'accepted'});
  st.put('outbox/unparseable.json',{id:'unparseable',state:'accepted',acceptedAt:'last Tuesday'});
  // Seconds where milliseconds were meant dates everything to 1970 and would
  // empty the directory in one pass. It is refused as a date, not believed.
  st.put('outbox/seconds.json',{id:'seconds',state:'accepted',acceptedAt:Math.floor((NOW-400*86400000)/1000)});
  st.put('outbox/good.json',settledDelivery('good',400));
  const made=run(st,[st.family('outbox','outbox')]);
  assert.deepEqual(moving(made),['outbox/good.json']);
  assert.deepEqual(made.kept.filter(k=>k.reason==='no-usable-timestamp').map(k=>k.name).sort(),
    ['none.json','seconds.json','unparseable.json']);
  assert.equal(recordTime({acceptedAt:Math.floor(NOW/1000)}),null);
  assert.equal(recordTime({at:NOW}),NOW);
  // The newest time in the record wins: a record accepted after it was attempted
  // is as old as its acceptance, not as old as the first try at it.
  assert.equal(recordTime({attemptedAt:ago(400),acceptedAt:ago(2)}),Date.parse(ago(2)));
});

// ---------------------------------------------------------------------------
// The states that are never archived, whatever anyone configures
// ---------------------------------------------------------------------------

test('pending, unconfirmed, prepared and not-submitted are never archived, even when a caller calls them settled',t=>{
  const st=state(t);
  for(const value of NEVER_ARCHIVED)st.put(`checks/${value}.json`,{id:value,state:value,at:ago(400)});
  st.put('checks/settled.json',{id:'settled',state:'silent',at:ago(400)});
  // The caller is wrong on purpose. The floor holds and says so in the report.
  const made=run(st,[st.family('checks','checks',{settled:[...NEVER_ARCHIVED,'silent']})]);
  assert.deepEqual(moving(made),['checks/settled.json']);
  for(const value of NEVER_ARCHIVED)assert.equal(made.kept.find(k=>k.name===value+'.json').reason,'still-in-flight');
  assert.match(made.warnings.join('\n'),/cannot be archived and was dropped/);
});

test('a state still in flight anywhere inside the record keeps the whole file',t=>{
  const st=state(t);
  // The group says it is finished; one bubble of it has not been sent. Moving
  // this is how the rest of a reply is lost, or said twice.
  st.put('groups/half.json',{id:'half',state:'accepted',at:ago(400),
    entries:[{state:'accepted'},{state:'unsent'}]});
  st.put('groups/whole.json',{id:'whole',state:'accepted',at:ago(400),
    entries:[{state:'accepted'},{state:'accepted'}]});
  const made=run(st,[st.family('groups','groups')]);
  assert.deepEqual(moving(made),['groups/whole.json']);
  assert.equal(made.kept.find(k=>k.name==='half.json').detail,'unsent');
  assert.equal(liveState({a:{b:{state:'unconfirmed'}}}),'unconfirmed');
  assert.equal(liveState({state:'accepted',entries:[{state:'accepted'}]}),null);
});

test('a state this module has never met keeps its file, and one with no state at all needs saying so',t=>{
  const st=state(t);
  st.put('checks/strange.json',{id:'strange',state:'half-way-to-somewhere',at:ago(400)});
  st.put('checks/stateless.json',{id:'stateless',at:ago(400)});
  const shy=run(st,[st.family('checks','checks')]);
  assert.deepEqual(moving(shy),[]);
  assert.deepEqual(shy.kept.map(k=>k.reason).sort(),['no-state','not-settled']);
  // A directory where arrival is the settlement says so, and the unknown state
  // is still unknown: being in the directory does not overrule what it says.
  const told=run(st,[st.family('checks','checks',{settledByDirectory:true})]);
  assert.deepEqual(moving(told),['checks/stateless.json']);
});

// ---------------------------------------------------------------------------
// What something live still holds
// ---------------------------------------------------------------------------

test('nothing the reference set still holds is moved, by its own id, by one it carries, or by its name',t=>{
  const st=state(t);
  st.put('outbox/own.json',settledDelivery('own',400));
  st.put('outbox/nested.json',settledDelivery('nested',400,{draftId:'group-7'}));
  st.put('outbox/deep.json',settledDelivery('deep',400,{part:{of:'bubble-3'}}));
  st.put('outbox/named.json',settledDelivery('different-id-entirely',400));
  st.put('outbox/free.json',settledDelivery('free',400));
  const made=run(st,[st.family('outbox','outbox')],{references:['own','group-7','bubble-3','named']});
  assert.deepEqual(moving(made),['outbox/free.json']);
  for(const name of ['own.json','nested.json','deep.json','named.json'])
    assert.equal(made.kept.find(k=>k.name===name).reason,'referenced');
  // The session is on every record, so matching it would keep the directory for
  // ever. It is deliberately not an identifier.
  assert.equal(recordIdentifiers({sessionId:'s1',conversationId:'c1'}).size,0);
  assert.ok(recordIdentifiers({task_id:'t1',inputIds:['i1','i2']}).has('i2'));
});

test('a reader fixture that still resolves a record is a reference, and the record stays',async t=>{
  const st=state(t);
  // The three readers named in the acceptance list, each pointed at one record.
  // Whatever any of them can still name is in the reference set the host builds,
  // and the plan must leave every one of them where it is.
  const held=['reply-group-live','outbox-prior-live','work-review-input'];
  for(const id of held)st.put('outbox/'+id+'.json',settledDelivery(id,400));
  st.put('outbox/nobody-wants-this.json',settledDelivery('nobody-wants-this',400));
  const made=run(st,[st.family('outbox','outbox')],{references:held});
  assert.deepEqual(moving(made),['outbox/nobody-wants-this.json']);
  // And the record a live work review resolves by name is genuinely still there
  // for it after the plan is carried out.
  apply(made,{dryRun:false});
  for(const id of held)assert.ok(fs.existsSync(path.join(st.root,'outbox',id+'.json')));
});

// ---------------------------------------------------------------------------
// A receipt and the record it vouches for
// ---------------------------------------------------------------------------

/** The two families as the hosts have them: a delivery record, and a receipt
 * named after the event the record produced. */
function pair(st,{settled=true,days=400}={}) {
  const receiptKey=id=>sha(`delivery:feishu:${id}:accepted`);
  st.put('outbox/a.json',settled?settledDelivery('a',days):{id:'a',state:'unconfirmed',checkedAt:ago(days)});
  st.put('outbox/b.json',settledDelivery('b',days));
  st.put('events/receipts/'+receiptKey('a')+'.json',{id:`delivery:feishu:a:accepted`,digest:'d-a',at:ago(days)});
  st.put('events/receipts/'+receiptKey('b')+'.json',{id:`delivery:feishu:b:accepted`,digest:'d-b',at:ago(days)});
  return {receiptKey,families:[
    st.family('outbox','outbox'),
    st.family('receipts','events/receipts',{settledByDirectory:true,timeFromPartner:true,
      // The receipt names the delivery it settled; that name is the record's id.
      requires:{family:'outbox',key:record=>/^delivery:[^:]+:(.+):[^:]+$/.exec(record?.id??'')?.[1]??null}})]};
}

test('a receipt moves with the record it vouches for, and never before it',t=>{
  const st=state(t),{receiptKey,families}=pair(st);
  const made=run(st,families);
  // Both pairs move, and in the whole ordered list every receipt comes after the
  // record it belongs to. An interruption between them leaves a receipt with
  // nothing to vouch for, which costs bytes; the other order costs a second send.
  assert.equal(made.moves.length,4);
  for(const move of made.moves.filter(m=>m.family==='receipts')) {
    const partner=made.moves.findIndex(m=>m.family==='outbox'&&m.name===move.pairedWith.name);
    assert.ok(partner>=0&&partner<made.moves.indexOf(move));
  }
  apply(made,{dryRun:false});
  assert.deepEqual(st.names('outbox'),[]);
  assert.deepEqual(st.archived('outbox'),['a.json','b.json']);
  assert.deepEqual(st.archived('events/receipts'),[receiptKey('a')+'.json',receiptKey('b')+'.json'].sort());
});

test('a receipt whose record has to stay, stays with it',t=>{
  const st=state(t),{receiptKey,families}=pair(st,{settled:false});
  const made=run(st,families);
  assert.deepEqual(moving(made),['outbox/b.json','receipts/'+receiptKey('b')+'.json']);
  const stayed=made.kept.find(k=>k.name===receiptKey('a')+'.json');
  assert.equal(stayed.reason,'paired-record-stays');
  assert.equal(stayed.detail,'still-in-flight');
});

test('a receipt whose record is already gone is free once it can date itself, and one that names nothing stays',t=>{
  const st=state(t),{receiptKey,families}=pair(st);
  // An earlier pass already archived this record; nothing walks it any more, and
  // this receipt says for itself how long ago it settled.
  fs.rmSync(path.join(st.root,'outbox','a.json'));
  // A receipt whose partner cannot even be worked out is never guessed at.
  st.put('events/receipts/garbled.json',{id:'not-a-delivery-name',digest:'d-x',at:ago(400)});
  const made=run(st,families);
  assert.ok(moving(made).includes('receipts/'+receiptKey('a')+'.json'));
  assert.equal(made.kept.find(k=>k.name==='garbled.json').reason,'unpaired');
});

test('a receipt written before receipts carried a time borrows its record’s, and stays without one',t=>{
  const st=state(t),{families}=pair(st);
  // Every receipt already on disk is an identity and a digest and nothing else.
  // What it does have is the record it vouches for, which is dated and which it
  // can only ever move behind, so that is the age the pair is judged by.
  const undated=sha('delivery:feishu:c:accepted');
  st.put('events/receipts/'+undated+'.json',{id:'delivery:feishu:c:accepted',digest:'d-c'});
  st.put('outbox/c.json',settledDelivery('c',400));
  assert.ok(moving(run(st,families)).includes('receipts/'+undated+'.json'));
  // Its record is young, so the pair is young: the receipt does not go early.
  st.put('outbox/c.json',settledDelivery('c',1));
  assert.ok(!moving(run(st,families)).includes('receipts/'+undated+'.json'));
  // And with the record gone there is nothing left to date it by at all. It
  // stays: this module would sooner keep a few bytes for ever than move
  // something whose age it cannot state.
  fs.rmSync(path.join(st.root,'outbox','c.json'));
  const orphan=run(st,families);
  assert.ok(!moving(orphan).includes('receipts/'+undated+'.json'));
  assert.equal(orphan.kept.find(k=>k.name===undated+'.json').reason,'no-usable-timestamp');
  // A family whose partner family is not in the plan moves none of itself.
  const alone=run(st,[families[1]]);
  assert.deepEqual(moving(alone),[]);
  assert.match(alone.warnings.join('\n'),/which is not in this plan/);
});

test('applying refuses a receipt whose record did not move after all',t=>{
  const st=state(t),{receiptKey,families}=pair(st);
  const made=run(st,families);
  // Between the report and the decision the record was rewritten. It is left
  // alone — and so, therefore, is the receipt that only moves behind it.
  st.put('outbox/a.json',settledDelivery('a',400,{messageId:'rewritten'}));
  const result=apply(made,{dryRun:false});
  assert.deepEqual(result.skipped.map(s=>[s.name,s.reason]).sort(),
    [['a.json','changed-since-plan'],[receiptKey('a')+'.json','paired-record-did-not-move']].sort());
  assert.ok(fs.existsSync(path.join(st.root,'events/receipts',receiptKey('a')+'.json')));
});

// ---------------------------------------------------------------------------
// Planning reads; only applying moves
// ---------------------------------------------------------------------------

test('planning changes nothing and answers the same way however often it is asked',t=>{
  const st=state(t);
  st.put('outbox/a.json',settledDelivery('a',400));
  st.put('outbox/b.json',settledDelivery('b',1));
  const before=filesUnder(st.base);
  const families=[st.family('outbox','outbox')];
  const first=run(st,families),second=run(st,families);
  assert.deepEqual(filesUnder(st.base),before);
  assert.deepEqual(first.moves,second.moves);
  assert.deepEqual(first.kept,second.kept);
  // And planning from records already in hand needs no directory at all.
  const pure=plan({now:NOW,families:[{name:'held',directory:'/nowhere/outbox',root:'/nowhere',archive:'/kept',
    entries:[{name:'a.json',file:'/nowhere/outbox/a.json',record:settledDelivery('a',400),bytes:10,sha256:'x'}]}]});
  assert.deepEqual(pure.moves.map(m=>m.to),[path.join('/kept','outbox','a.json')]);
});

test('a dry run moves nothing, and is what a pass does unless it is told otherwise',t=>{
  const st=state(t);
  st.put('outbox/a.json',settledDelivery('a',400));
  const before=filesUnder(st.base);
  const made=run(st,[st.family('outbox','outbox')]);
  const dry=apply(made);
  assert.equal(dry.dryRun,true);
  assert.deepEqual(dry.moved,[]);
  assert.deepEqual(dry.skipped.map(s=>s.reason),['dry-run']);
  assert.deepEqual(filesUnder(st.base),before);
  // The pass a host runs on a timer is a dry run until somebody says otherwise.
  const pass=prunePass({families:[st.family('outbox','outbox')],root:st.root,archive:st.archive,now:NOW});
  assert.equal(pass.applied.dryRun,true);
  assert.deepEqual(filesUnder(st.base),before);
  assert.match(pass.report,/Nothing has been moved/);
});

test('applying moves the bytes and deletes nothing: every file is still somewhere',t=>{
  const st=state(t);
  for(const [id,days] of [['a',400],['b',400],['c',1]])st.put('outbox/'+id+'.json',settledDelivery(id,days));
  const before=filesUnder(st.base).map(file=>fs.readFileSync(file,'utf8')).sort();
  const made=run(st,[st.family('outbox','outbox')]);
  const result=apply(made,{dryRun:false});
  assert.equal(result.moved.length,2);
  assert.deepEqual(st.names('outbox'),['c.json']);
  assert.deepEqual(st.archived('outbox'),['a.json','b.json']);
  assert.deepEqual(filesUnder(st.base).map(file=>fs.readFileSync(file,'utf8')).sort(),before);
  assert.equal(fs.statSync(path.join(st.archive,'outbox','a.json')).mode&0o777,0o600);
});

test('a file whose bytes changed since the report is left where it is',t=>{
  const st=state(t);
  st.put('outbox/a.json',settledDelivery('a',400));
  const made=run(st,[st.family('outbox','outbox')]);
  st.put('outbox/a.json',settledDelivery('a',400,{messageId:'different'}));
  const result=apply(made,{dryRun:false});
  assert.deepEqual(result.skipped.map(s=>s.reason),['changed-since-plan']);
  assert.ok(fs.existsSync(path.join(st.root,'outbox','a.json')));
  // A file that went away between the two is a fact, not a failure.
  const again=run(st,[st.family('outbox','outbox')]);
  fs.rmSync(path.join(st.root,'outbox','a.json'));
  assert.deepEqual(apply(again,{dryRun:false}).skipped.map(s=>s.reason),['source-gone']);
});

test('an archived path that is already taken is never written over',t=>{
  const st=state(t);
  st.put('outbox/a.json',settledDelivery('a',400));
  const kept=path.join(st.archive,'outbox','a.json');
  fs.mkdirSync(path.dirname(kept),{recursive:true});
  fs.writeFileSync(kept,'{"id":"a","state":"accepted","earlier":true}');
  const made=run(st,[st.family('outbox','outbox')]);
  const result=apply(made,{dryRun:false});
  assert.deepEqual(result.skipped.map(s=>s.reason),['archive-occupied']);
  // Both survive: the one in the archive is untouched and the live one stayed.
  assert.equal(JSON.parse(fs.readFileSync(kept,'utf8')).earlier,true);
  assert.ok(fs.existsSync(path.join(st.root,'outbox','a.json')));
});

// ---------------------------------------------------------------------------
// What is never a candidate at all
// ---------------------------------------------------------------------------

test('process sidecars, pid files and quarantined locks are never candidates',t=>{
  const st=state(t);
  // The sidecar is what lets a recycled pid be told apart from its holder, and a
  // broken lock is the only surviving record that an attempt was cut short.
  st.put('locks/host.pid.owner.json',{pid:4242,started:'Thu Sep 18 09:00:00 2026',at:ago(400)});
  st.put('locks/worker.pid.owner.json',{pid:4243,started:'Thu Sep 18 09:00:00 2026',at:ago(400)});
  st.put('locks/send.lock',{holder:{pid:4242},nonce:'n',at:ago(400)});
  st.put('locks/broken/send.lock.stale.1',{holder:{pid:1},nonce:'n',at:ago(400)});
  st.put('locks/settled.json',{id:'settled',state:'accepted',at:ago(400)});
  const made=run(st,[st.family('locks','locks')]);
  assert.deepEqual(moving(made),['locks/settled.json']);
  // It is the shape that is protected, not a list of names: every sidecar any
  // host writes beside a pid file is covered, whatever that host calls it.
  for(const name of ['host.pid.owner.json','worker.pid.owner.json','anything.pid','anything.pid.owner.json','send.lock'])
    assert.ok(protectedName(name),name);
  // A family pointed straight at the quarantine is refused rather than obeyed.
  const refused=run(st,[st.family('broken','locks/broken')]);
  assert.deepEqual(refused.moves,[]);
  assert.match(refused.warnings.join('\n'),/itself an archive or a quarantine/);
});

test('a file that cannot be parsed is reported and never moved',t=>{
  const st=state(t);
  fs.mkdirSync(path.join(st.root,'outbox'),{recursive:true});
  fs.writeFileSync(path.join(st.root,'outbox','torn.json'),'{"id":"torn"');
  st.put('outbox/a.json',settledDelivery('a',400));
  // A replaced revision and a temporary file are not records and are not read.
  st.put('outbox/a.json.prev',settledDelivery('a',400));
  const made=run(st,[st.family('outbox','outbox')]);
  assert.deepEqual(moving(made),['outbox/a.json']);
  assert.equal(made.families[0].unreadable,1);
  assert.equal(made.kept.find(k=>k.name==='torn.json').reason,'unreadable');
});

test('a directory whose archive would sit inside it, or the other way about, is refused',t=>{
  const st=state(t);
  st.put('outbox/a.json',settledDelivery('a',400));
  const inside=run(st,[{name:'outbox',directory:path.join(st.root,'outbox'),root:st.root,archive:path.join(st.root,'outbox','kept')}]);
  assert.deepEqual(inside.moves,[]);
  assert.match(inside.warnings.join('\n'),/contain one another/);
  const adrift=run(st,[{name:'outbox',directory:path.join(st.root,'outbox'),root:path.join(st.base,'elsewhere'),archive:st.archive}]);
  assert.deepEqual(adrift.moves,[]);
  assert.match(adrift.warnings.join('\n'),/not under its root/);
});

test('a cap can only ever leave a receipt behind, never strand one',t=>{
  const st=state(t),{families}=pair(st);
  const made=run(st,families,{limit:3});
  assert.equal(made.moves.length,3);
  assert.equal(made.deferred,1);
  // The report counts what the plan holds, not what it held before the cap.
  assert.equal(made.families.reduce((total,family)=>total+family.moving,0),3);
  // Everything left out is a receipt; no record is moved past its receipt.
  assert.deepEqual([...new Set(made.moves.filter(m=>m.family==='outbox').map(m=>m.name))].sort(),['a.json','b.json']);
  apply(made,{dryRun:false});
  assert.deepEqual(st.names('outbox'),[]);
  assert.equal(st.names('events/receipts').length,1);
});

// ---------------------------------------------------------------------------
// A reader that misses looks in the archive
// ---------------------------------------------------------------------------

test('an archived record is found by the reader that missed it, and a corrupt live one is not hidden',t=>{
  const st=state(t);
  const file=st.put('outbox/a.json',settledDelivery('a',400));
  apply(run(st,[st.family('outbox','outbox')]),{dryRun:false});
  assert.equal(fs.existsSync(file),false);
  const found=readThroughArchive(file,{root:st.root,archive:st.archive});
  assert.equal(found.source,'archive');
  assert.equal(found.value.id,'a');
  assert.equal(existsThroughArchive(file,{root:st.root,archive:st.archive}),path.join(st.archive,'outbox','a.json'));
  // Without an archive configured, the reader answers exactly as it always did.
  assert.equal(readThroughArchive(file).state,'missing');
  assert.equal(archivePathFor('/somewhere/else.json',{root:st.root,archive:st.archive}),null);
  // A live file that is damaged is damaged. Answering from the archive instead
  // would turn a torn revision into a silent success.
  st.put('outbox/b.json',settledDelivery('b',400));
  fs.writeFileSync(path.join(st.archive,'outbox','b.json')===''?'':path.join(st.root,'outbox','b.json'),'{');
  assert.equal(readThroughArchive(path.join(st.root,'outbox','b.json'),{root:st.root,archive:st.archive}).state,'corrupt');
});

test('a share decision that was archived still refuses to say the same thing twice',async t=>{
  const st=state(t);
  const directory=path.join(st.root,'share-checks');
  const request={text:'the same thing',reply_id:'input-1'};
  const key=createHash('sha256').update(JSON.stringify(request)).digest('hex');
  // The decision was made and then archived after it settled.
  st.put('share-checks/'+key+'.json',{state:'silent',choice:{action:'silent'},at:ago(400)});
  apply(run(st,[st.family('share-checks','share-checks',{settled:['silent','merged','ready','accepted','canceled']})]),{dryRun:false});
  assert.deepEqual(st.names('share-checks'),[]);
  const calls=[];
  const guard=new ReplyGuard({directory,transportManifest:false,archivedState:{root:st.root,archive:st.archive},
    call:(name,payload)=>{calls.push(name);return Promise.resolve({state:'ready',text:payload.text});}});
  assert.equal((await guard.check(request)).state,'silent');
  assert.deepEqual(calls,[]);
  // A guard with no archive configured behaves as it did before any of this.
  const blind=new ReplyGuard({directory,transportManifest:false,call:()=>Promise.resolve({state:'ready',text:'x'})});
  assert.equal((await blind.check(request)).state,'ready');
});

test('an archived receipt still stops the same delivery being ingested a second time',t=>{
  const st=state(t);
  const directory=path.join(st.root,'events');
  const archivedState={root:st.root,archive:st.archive};
  const event={id:'delivery:feishu:a:accepted',kind:'delivery',at:ago(400),text:'…'};
  // The receipt records when it settled, which is how it can later be told it is
  // old without anybody reading the file's own timestamps.
  const journal=new MemoryEventJournal({directory,call:async()=>({state:'recorded'}),archivedState,clock:()=>NOW-400*86400000});
  journal.append(event);journal.acknowledge(event);
  assert.equal(journal.append(event).state,'recorded');
  // The receipt is moved to the archive. Nothing about what it proves changed.
  apply(run(st,[st.family('receipts','events/receipts',{settledByDirectory:true,timeFields:['at']})],{}),{dryRun:false});
  assert.deepEqual(st.names('events/receipts'),[]);
  assert.equal(journal.append(event).state,'recorded');
  assert.deepEqual(st.names('events'),['receipts']);
  // A journal that was never told about the archive queues it again, which is
  // exactly why the wiring is not optional once anything has been moved.
  const blind=new MemoryEventJournal({directory,call:async()=>({state:'recorded'})});
  assert.equal(blind.append(event).state,'queued');
});

test('the work review resolves a delivery whose outbox record was archived',async t=>{
  const st=state(t);
  const inputDirectory=path.join(st.root,'inputs'),outboxDirectory=path.join(st.root,'outbox');
  st.put('inputs/i1.json',{id:'i1',canonicalSessionId:'s1',senderId:'owner',text:'hello',createdAt:ago(400)});
  st.put('outbox/d1.json',settledDelivery('d1',400));
  apply(run(st,[st.family('outbox','outbox')]),{dryRun:false});
  assert.deepEqual(st.names('outbox'),[]);
  const snapshot={inputs:[{id:'i1',state:'accepted'}],
    task:{id:'t1',inputIds:['i1'],deliveries:{d1:{state:'accepted',messageId:'m-d1'}},tools:{}}};
  const evidence=workEvidence({sessionId:'s1',inputDirectory,outboxDirectory,
    deferredDirectory:path.join(st.root,'deferred'),lastReply:async()=>null,
    archivedState:{root:st.root,archive:st.archive}});
  const collected=await evidence.collect(snapshot);
  assert.deepEqual(collected.receipts.d1,{state:'accepted',messageId:'m-d1',sourceHash:collected.receipts.d1.sourceHash});
  assert.equal(collected.input.outputs[0].receivedByServer,true);
});

// ---------------------------------------------------------------------------
// The report, and the document a file-mover cannot help
// ---------------------------------------------------------------------------

test('the report names every move, where it would go, and that none of it has happened',t=>{
  const st=state(t);
  st.put('outbox/a.json',settledDelivery('a',400));
  st.put('outbox/held.json',settledDelivery('held',400));
  st.put('outbox/young.json',settledDelivery('young',1));
  const made=run(st,[st.family('outbox','outbox')],{references:['held']});
  const report=formatPlan(made);
  assert.match(report,/a dry run\. Nothing below has moved\./);
  assert.match(report,/outbox\/a\.json/);
  assert.match(report,/1 referenced, 1 too-recent/);
  // The destination is where the record would really land, name and all, not
  // the archive root it happens to sit under.
  assert.ok(report.includes(path.join(st.archive,'outbox')));
  assert.equal(made.families[0].archivedDirectory,path.join(st.archive,'outbox'));
  assert.ok(!report.includes('outbox/young.json'));
  assert.match(formatPlan(made,{limit:0}),/and 1 more/);
});

test('a document is counted and never rewritten',t=>{
  const st=state(t);
  // The one state file that is not a directory of records but a document full of
  // them, rewritten whole on every event. Nothing here moves any of it.
  const document={revision:9,inputs:{
    i1:{id:'i1',state:'accepted',at:NOW-400*86400000},
    i2:{id:'i2',state:'unconfirmed',at:NOW-400*86400000},
    i3:{id:'i3',state:'accepted',at:NOW-86400000},
    i4:{id:'i4',state:'accepted',at:NOW-400*86400000,taskId:'t-live'},
    i5:{id:'i5',state:'accepted'}},tasks:{}};
  const counted=documentPressure(document,{collections:['inputs','tasks','absent'],references:['t-live'],now:NOW});
  const inputs=counted.collections.find(c=>c.name==='inputs');
  assert.deepEqual([inputs.rows,inputs.stale,inputs.inFlight,inputs.referenced,inputs.undatable],[5,1,1,1,1]);
  assert.equal(counted.collections.find(c=>c.name==='absent').present,false);
  assert.ok(counted.bytes>0);
  const pass=prunePass({families:[],root:st.root,archive:st.archive,now:NOW,document,collections:['inputs']});
  assert.match(pass.report,/A file-mover never rewrites these/);
  assert.deepEqual(pass.plan.moves,[]);
});

test('the open records of a document are references, and the finished ones are not',()=>{
  const document={inputs:{
    i1:{id:'i1',state:'accepted',taskId:'t-done'},
    i2:{id:'i2',state:'unconfirmed',taskId:'t-live'},
    i3:{id:'i3',state:'selected'},
    i4:{id:'i4'}},
    tasks:{t1:{id:'t1',status:'completed'},t2:{id:'t2',status:'running',inputIds:['i9']}}};
  const held=liveReferences(document,{collections:['inputs','tasks']});
  // Everything unfinished, what it points at, and a record too shapeless to
  // judge; nothing that has settled.
  assert.deepEqual([...held].sort(),['i2','i3','i4','i9','t-live','t2'].sort());
  assert.equal(held.has('t-done'),false);
  assert.equal(held.has('t1'),false);
  // Held against the same records, the plan keeps every one of them.
  const entry=(name,record)=>({name,file:'/s/inputs/'+name,record,bytes:1,sha256:'x'});
  const made=plan({now:NOW,references:[...held],families:[{name:'inputs',directory:'/s/inputs',root:'/s',archive:'/k',
    entries:[entry('i2.json',{id:'i2',state:'accepted',at:ago(400)}),entry('free.json',{id:'free',state:'accepted',at:ago(400)})]}]});
  assert.deepEqual(made.moves.map(m=>m.name),['free.json']);
});

test('a plan made with nothing held says so in its own report',()=>{
  const made=plan({now:NOW,families:[]});
  assert.match(formatPlan(made),/the reference set was empty/);
});

test('the settled list is the short one, and everything about it is frozen',()=>{
  for(const list of [NEVER_ARCHIVED,SETTLED_STATES])assert.ok(Object.isFrozen(list));
  for(const state of NEVER_ARCHIVED)assert.ok(!SETTLED_STATES.includes(state));
  assert.equal(DEFAULT_AGE_MS,30*24*60*60*1000);
});

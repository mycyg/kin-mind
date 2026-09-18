#!/usr/bin/env node
/** Print what the state pruner would move, and move nothing.
 *
 * This script cannot archive a file. It does not import `apply`, and there is no
 * argument that makes it do so: the report is separated from the act on purpose,
 * so that the list can be read, argued with and thrown away before anybody
 * decides whether a single file should move. Running it against a live host is
 * as safe as `ls` — it opens files for reading and writes nothing anywhere.
 *
 *   node scripts/state-prune-report.mjs <configuration.json> [--show N]
 *
 * `--show` shortens the printed list only. It never changes what the plan holds,
 * so the totals above the list are always the whole answer.
 *
 * The configuration describes directories, not this repository, so keep it
 * outside the checkout next to the state it is about. Relative paths in it are
 * resolved against the configuration file's own directory.
 *
 *   {
 *     "olderThanDays": 30,
 *     "root": "<the directory the archive mirrors>",
 *     "archive": "<where archived records would go>",
 *     "families": [
 *       {"name": "outbox", "directory": "outbox"},
 *       {"name": "receipts", "directory": "events/receipts",
 *        "settledByDirectory": true, "timeFromPartner": true,
 *        "requires": {"family": "outbox", "pattern": "^delivery:[^:]+:(.+):[^:]+$"}}
 *     ],
 *     "document": {"file": "router.json",
 *                  "collections": ["inputs", "tasks", "requests", "notices"],
 *                  "references": true},
 *     "referenceFiles": ["live-reference-set.json"],
 *     "references": []
 *   }
 *
 * `references` is what something running still holds: routing tasks that are
 * open, reply groups still waiting, contact batches that have not settled,
 * grants that have not been spent. With `document.references` the open records
 * of that document are added, which covers the first of those four and none of
 * the other three — supply the rest through `referenceFiles`, each a JSON array
 * of identifiers. A report made without them names more than may really move,
 * and says so at the end. */
import fs from 'node:fs';
import path from 'node:path';
import {survey,plan,formatPlan,documentPressure,liveReferences,DEFAULT_AGE_MS} from '../adapters/state-pruner.mjs';

const args=process.argv.slice(2);
const configFile=args.find(argument=>!argument.startsWith('--'));
if(!configFile) {
  process.stderr.write('usage: node scripts/state-prune-report.mjs <configuration.json> [--show N]\n');
  process.exit(2);
}
const showArgument=args.find(argument=>argument.startsWith('--show'));
const show=showArgument?Number(showArgument.includes('=')?showArgument.split('=')[1]:args[args.indexOf(showArgument)+1]):Infinity;
if(showArgument&&!Number.isFinite(show)){process.stderr.write('--show wants a number\n');process.exit(2);}

const configuration=JSON.parse(fs.readFileSync(configFile,'utf8'));
const base=path.dirname(path.resolve(configFile));
const resolve=value=>value===undefined?undefined:path.resolve(base,value);
const readJson=file=>JSON.parse(fs.readFileSync(resolve(file),'utf8'));

const root=resolve(configuration.root),archive=resolve(configuration.archive);
const families=(configuration.families??[]).map(family=>({...family,
  directory:resolve(family.directory),root:resolve(family.root)??root,archive:resolve(family.archive)??archive,
  // A pairing rule travels as a pattern rather than as code: a report is not a
  // place to be running expressions out of a file somebody left lying about.
  ...(family.requires?{requires:{family:family.requires.family,
    key:record=>new RegExp(family.requires.pattern).exec(record?.id??'')?.[1]??null}}:{})}));

const document=configuration.document?.file?readJson(configuration.document.file):null;
const collections=configuration.document?.collections??[];
const references=new Set(configuration.references??[]);
for(const file of configuration.referenceFiles??[])for(const id of readJson(file))references.add(id);
if(document&&configuration.document.references!==false)for(const id of liveReferences(document,{collections}))references.add(id);

const olderThanMs=configuration.olderThanDays?configuration.olderThanDays*24*60*60*1000:DEFAULT_AGE_MS;
const made=plan({families:survey(families,{root,archive}),references:[...references],olderThanMs});
const documents=document?documentPressure(document,{collections,references:[...references],olderThanMs}):null;

process.stdout.write(`Reference set: ${references.size} identifiers something still holds.\n`);
process.stdout.write(formatPlan(made,{documents,limit:show})+'\n');
process.stdout.write('This script has no way to move a file. Nothing in the state directories was touched.\n');

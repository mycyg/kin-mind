import fs from 'node:fs';
import path from 'node:path';
import {MemeLibrary} from '../adapters/meme-library.mjs';
const args=process.argv.slice(2),get=k=>args[args.indexOf(k)+1];
if(!args.includes('--manifest')||!args.includes('--library'))throw Error('Use --manifest /pack/manifest.json --library /private/memes [--apply]; default is validation only.');
const file=path.resolve(get('--manifest'));
const result=new MemeLibrary(path.resolve(get('--library'))).importPack(JSON.parse(fs.readFileSync(file,'utf8')),path.dirname(file),{dryRun:!args.includes('--apply')});
console.log(JSON.stringify(result));

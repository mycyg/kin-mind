import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const core=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export const installedRoots=Object.freeze(JSON.parse(fs.readFileSync(path.join(core,'adapters/installed-roots.json'),'utf8')));
export function projectRoot(name){
 const override=process.env['KIN_'+name.toUpperCase().replaceAll('-','_')+'_ROOT'];
 if(override)return override;
 if(name==='core')return core;
 const sibling=path.resolve(core,'..',name);
 return fs.existsSync(sibling)?sibling:installedRoots[name];
}
// A host data override must never redirect imports into its state directory.
export function projectCodeRoot(name) {
 const key='KIN_'+name.toUpperCase().replaceAll('-','_');
 const override=process.env[key+'_CODE_ROOT']||(name==='host'?null:process.env[key+'_ROOT']);
 if(override)return override;
 if(name==='core')return core;
 const sibling=path.resolve(core,'..',name);
 return fs.existsSync(sibling)?sibling:installedRoots[name];
}
export const projectModule=(name,file)=>import(pathToFileURL(path.join(projectCodeRoot(name),file)).href);

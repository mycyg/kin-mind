import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const core=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
// Installed checkouts are named by the private caller (`installed`), never by this public repository.
export function projectRoot(name,installed={}){
 const override=process.env['KIN_'+name.toUpperCase().replaceAll('-','_')+'_ROOT'];
 if(override)return override;
 if(name==='core')return core;
 const sibling=path.resolve(core,'..',name);
 return fs.existsSync(sibling)?sibling:installed[name];
}
// A host data override must never redirect imports into its state directory.
export function projectCodeRoot(name,installed={}) {
 const key='KIN_'+name.toUpperCase().replaceAll('-','_');
 const override=process.env[key+'_CODE_ROOT']||(name==='host'?null:process.env[key+'_ROOT']);
 if(override)return override;
 if(name==='core')return core;
 const sibling=path.resolve(core,'..',name);
 return fs.existsSync(sibling)?sibling:installed[name];
}
export const projectModule=(name,file,installed)=>import(pathToFileURL(path.join(projectCodeRoot(name,installed),file)).href);

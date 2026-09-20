import {readdirSync,existsSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve} from 'node:path';
const root=resolve(import.meta.dirname,'..');
const folder=resolve(root,'docs/diagrams');
for(const name of readdirSync(folder).filter(n=>n.endsWith('.mmd'))){
 for(const format of ['svg']){
  const args=[resolve(root,'node_modules/@mermaid-js/mermaid-cli/src/cli.js'),'-i',resolve(folder,name),'-o',resolve(folder,name.replace('.mmd','.'+format)),'-c',resolve(folder,'mermaid.json'),'-b','#fcfbf7','-w','1800','-s','2'];
  if(process.env.CI) args.push("-p",resolve(root,".github/puppeteer.json"));
  const r=spawnSync(process.execPath,args,{stdio:'inherit'});if(r.status!==0)process.exit(r.status??1);
 }
}

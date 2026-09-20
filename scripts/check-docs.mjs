import {readFileSync,existsSync} from 'node:fs';
for(const language of ['README.md','README.en.md','README.ja.md']){
 const text=readFileSync(language,'utf8');
 for(const value of ['Kin','MemoryPalace','DeepSeek','Codex','docs/kin-mind.md'])if(!text.includes(value))throw Error(`${language}: missing ${value}`);
}
for(const name of ['overview','write-correct','recall-context','background','proactive-contact'])for(const ext of ['mmd','svg']){
 const file=`docs/diagrams/${name}.${ext}`;
 if(!existsSync(file)||readFileSync(file).length<100)throw Error(`Missing diagram ${file}`);
}
console.log('Kin Mind editions and inherited architecture diagrams present');

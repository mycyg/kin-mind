import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {fileURLToPath} from 'node:url';

/** One lazy worker owned by the host. Requests share the loaded model; replies
 * retain their existing transport ID and a completed audio file for resume. */
export class LocalSpeech {
  constructor({python,model,ffmpeg,directory,voice='serena',timeoutMs=180000,spawnImpl=spawn}) {
    Object.assign(this,{python,model,ffmpeg,directory,voice,timeoutMs,spawnImpl});
    this.queue=Promise.resolve();this.child=null;this.pending=null;
  }
  start() {
    if(this.child)return;
    const env=Object.fromEntries(['PATH','HOME','TMPDIR','LANG'].filter(k=>process.env[k]).map(k=>[k,process.env[k]]));
    Object.assign(env,{HF_HUB_OFFLINE:'1',TRANSFORMERS_OFFLINE:'1',PYTHONDONTWRITEBYTECODE:'1',TOKENIZERS_PARALLELISM:'false'});
    const child=this.spawnImpl(this.python,['-u',fileURLToPath(new URL('../scripts/local_speech.py',import.meta.url)),this.model,this.ffmpeg],{env,stdio:['pipe','pipe','ignore']});
    this.child=child;
    const lines=createInterface({input:child.stdout});
    lines.on('line',line=>{
      let result;try{result=JSON.parse(line);}catch{return;}
      const pending=this.pending;if(!pending)return;
      this.pending=null;clearTimeout(pending.timer);
      if(result.ok)pending.resolve(result);else pending.reject(Object.assign(Error('Local speech failed'),{code:'TTS_FAILED',stage:result.stage}));
    });
    const failed=()=>{
      lines.close();if(this.child!==child)return;this.child=null;
      if(this.pending){clearTimeout(this.pending.timer);this.pending.reject(Object.assign(Error('Local speech worker stopped'),{code:'TTS_FAILED'}));this.pending=null;}
    };
    child.on('error',failed);child.on('exit',failed);child.stdin.on('error',failed);
  }
  synthesize({id,text,style=''}) {
    const run=async()=>{
      await fs.mkdir(this.directory,{recursive:true,mode:0o700});
      const file=path.join(this.directory,encodeURIComponent(id)+'.ogg');
      try {const data=await fs.readFile(file);if(data.length){let metrics={};try{metrics=JSON.parse(await fs.readFile(file+'.json','utf8'));}catch{}return {...metrics,data,name:'Kin-voice.ogg',cached:true};}}catch(error){if(error.code!=='ENOENT')throw error;}
      this.start();
      const result=await new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{this.pending=null;this.child?.kill();this.child=null;reject(Object.assign(Error('Local speech timed out'),{code:'TTS_FAILED'}));},this.timeoutMs);
        this.pending={resolve,reject,timer};
        this.child.stdin.write(JSON.stringify({text,style,voice:this.voice,file})+'\n');
      });
      await fs.writeFile(file+'.json',JSON.stringify(result),{mode:0o600});
      return {...result,data:await fs.readFile(file),name:'Kin-voice.ogg',cached:false};
    };
    const pending=this.queue.then(run);this.queue=pending.catch(()=>{});return pending;
  }
  close(){this.child?.stdin.end();}
}

export async function speechDelivery(delivery,channel,synthesize) {
  const audio=await synthesize({id:delivery.id,text:delivery.text,style:delivery.speech.style});
  const {text,speech,...rest}=delivery;
  return {...rest,media:{type:channel==='feishu'?'audio':'file',name:audio.name,data:audio.data,
    ...(audio.durationMs?{duration:audio.durationMs}:{}),transcript:text}};
}

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {CreationVerifier} from '../../adapters/creation-verifier.mjs';

const sha=value=>createHash('sha256').update(value).digest('hex');
// A renderer stand-in: it receives exactly what the real worker would, then answers as told.
function renderer(answer){
  const launched=[];
  const spawnImpl=(command,args,options)=>{
    const child=new EventEmitter();Object.assign(child,{pid:999999,stdout:new PassThrough(),stderr:new PassThrough(),stdin:new PassThrough(),kill:()=>{}});
    let input='';child.stdin.on('data',b=>input+=b);child.stdin.on('finish',()=>answer(child,JSON.parse(input)));
    launched.push({command,args,options});return child;
  };
  return {spawnImpl,launched};
}
function produced(t,files){
  const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'kin-creation-'));t.after(()=>fs.rmSync(workspace,{recursive:true,force:true}));
  const artifacts=Object.entries(files).map(([name,text])=>{const file=path.join(workspace,name);fs.writeFileSync(file,text);return {path:file,sha256:sha(text)};});
  return {state:'produced',artifacts,receipt:{workspace}};
}

test('verification never replaces the work: without a browser the artifact stays, marked unavailable',async t=>{
  const result=produced(t,{'page.html':'<p>作品</p>','notes.md':'# 笔记'});
  const verifier=new CreationVerifier({playwrightModule:path.join(os.tmpdir(),'no-such-playwright.mjs'),...renderer(()=>assert.fail('no render'))});
  assert.equal(verifier.capabilities().static_html_render,false);
  const checked=await verifier.verify(result);
  assert.deepEqual(checked.artifacts,result.artifacts);
  assert.deepEqual(checked.host_verification,[{state:'unavailable',source_sha256:result.artifacts[0].sha256,reason:'static-browser-unavailable'}]);
  assert.equal(await verifier.verify({state:'failed'}).then(r=>r.state),'failed');
});

test('the renderer gets only the workspace, the file and its digest, and a bad answer is a failed check',async t=>{
  const result=produced(t,{'a.html':'<p>一</p>','b.html':'<p>二</p>','c.html':'<p>三</p>'});
  const module=fileURLToPath(import.meta.url),answers=[
    (child,request)=>{child.stdout.end(JSON.stringify({state:'verified',source_sha256:request.sha256}));child.emit('close',0);},
    child=>{child.stdout.end('not json');child.emit('close',0);},
  ];
  const fake=renderer((child,request)=>answers.shift()(child,request));
  const verifier=new CreationVerifier({playwrightModule:module,...fake});
  const checked=await verifier.verify(result);
  assert.equal(fake.launched.length,2,'at most two HTML artifacts are rendered');
  assert.deepEqual(checked.host_verification.map(c=>c.state),['verified','failed']);
  assert.equal(checked.host_verification[1].reason,'invalid-render-receipt');
  assert.ok(checked.host_verification.every((c,i)=>c.source_sha256===result.artifacts[i].sha256));
  assert.deepEqual(Object.keys(fake.launched[0].options.env).filter(k=>!['PATH','HOME','TMPDIR','LANG'].includes(k)),[]);
});

test('the render worker carries the creation step\'s mark and nothing else it is handed (CR5-MM-04)',async t=>{
  const result=produced(t,{'a.html':'<p>一</p>'});
  const fake=renderer((child,request)=>{child.stdout.end(JSON.stringify({state:'verified',source_sha256:request.sha256}));child.emit('close',0);});
  const verifier=new CreationVerifier({playwrightModule:fileURLToPath(import.meta.url),...fake});
  const mark='4f1c2b3a-0d9e-4a8b-9c7d-6e5f4a3b2c1d';
  await verifier.verify(result,{env:{KIN_WORKER_MARK:mark,FEISHU_APP_SECRET:'must-not-pass'}});
  const env=fake.launched[0].options.env;
  assert.equal(env.KIN_WORKER_MARK,mark,'the worker and the browser it starts are the step\'s');
  assert.deepEqual(Object.keys(env).filter(k=>!['PATH','HOME','TMPDIR','LANG','KIN_WORKER_MARK'].includes(k)),[]);
});

test('a renderer that hangs is stopped at its limit or when the run is interrupted',async t=>{
  const result=produced(t,{'a.html':'<p>一</p>'});
  const slow=new CreationVerifier({playwrightModule:fileURLToPath(import.meta.url),timeoutMs:50,...renderer(()=>{})});
  assert.equal((await slow.verify(result)).host_verification[0].reason,'render-timeout');
  const controller=new AbortController();
  const waiting=new CreationVerifier({playwrightModule:fileURLToPath(import.meta.url),timeoutMs:60000,...renderer(()=>controller.abort())});
  assert.equal((await waiting.verify(result,{signal:controller.signal})).host_verification[0].reason,'interrupted');
});

// The real worker in a real headless browser, when this checkout has Playwright (the console's).
const playwright=process.env.KIN_TEST_PLAYWRIGHT_MODULE??fileURLToPath(new URL('../../console/node_modules/playwright/index.mjs',import.meta.url));
test('the real renderer draws the page with scripts and network off, and refuses a changed file',{skip:!fs.existsSync(playwright)&&'needs Playwright (npm ci --prefix console)'},async t=>{
  const page='<html><body><h1>合成作品</h1><img src="https://example.invalid/x.png"><script>document.body.textContent="script ran"</script></body></html>';
  const result=produced(t,{'page.html':page});
  const verifier=new CreationVerifier({playwrightModule:playwright,executablePath:process.env.KIN_TEST_CHROMIUM});
  const [check]=(await verifier.verify(result)).host_verification;
  assert.equal(check.state,'verified',JSON.stringify(check));
  assert.deepEqual(check.checks.map(c=>c.viewport_width),[390,1024]);
  // The page is drawn from its markup alone: its script never ran.
  assert.ok(check.checks.every(c=>c.text.includes('合成作品')&&!c.text.includes('script ran')),JSON.stringify(check.checks.map(c=>c.text)));
  assert.ok(check.checks.every(c=>fs.existsSync(c.image.path)&&c.image.path.startsWith(fs.realpathSync(result.receipt.workspace))));
  fs.writeFileSync(result.artifacts[0].path,page+'<!-- changed -->');
  assert.equal((await verifier.verify(result)).host_verification[0].state,'failed');
});

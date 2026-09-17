/** Trusted host process. Input pages get an opaque, network-free origin, no
 * scripts, downloads, service workers, external assets or local file URLs. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const hash=b=>createHash('sha256').update(b).digest('hex');
let input='';for await(const chunk of process.stdin)input+=chunk;
const request=JSON.parse(input);
const {chromium}=await import(pathToFileURL(process.argv[2]).href);
const browser=await chromium.launch({headless:true,executablePath:process.argv[3]||undefined,timeout:15000});
try{
 const root=fs.realpathSync(request.workspace),source=fs.realpathSync(request.path);
 if(!source.startsWith(root+path.sep))throw Error('outside-workspace');
 const html=fs.readFileSync(source);if(html.length>2*1024*1024||hash(html)!==request.sha256)throw Error('source-changed-or-large');
 const output=path.join(root,'.host-verification');fs.mkdirSync(output,{recursive:true,mode:0o700});
 if(fs.realpathSync(output)!==output)throw Error('verification-directory-symlink');
 const checks=[];
 for(const width of [390,1024]){
  const context=await browser.newContext({viewport:{width,height:820},javaScriptEnabled:false,serviceWorkers:'block',acceptDownloads:false});
  const url='https://kin-artifact.invalid/'+request.sha256;
  const blocked=[];
  await context.route('**/*',route=>route.request().url()===url?route.fulfill({status:200,contentType:'text/html; charset=utf-8',
   headers:{'Content-Security-Policy':"default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; sandbox"},body:html}): (blocked.push(route.request().resourceType()),route.abort()));
  const page=await context.newPage();await page.goto(url,{waitUntil:'load',timeout:10000});
  const layout=await page.evaluate(()=>({text:document.body?.innerText??'',width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight}));
  if(!layout.text.trim()||layout.height>8000||layout.width>4096)throw Error('empty-or-over-budget-layout');
  const file=path.join(output,request.sha256+'-'+width+'.png');
  // Refuse a creator-controlled symlink at the output boundary.
  if(fs.existsSync(file)&&fs.lstatSync(file).isSymbolicLink())throw Error('verification-output-symlink');
  await page.screenshot({path:file,fullPage:true,timeout:10000});
  const bytes=fs.readFileSync(file);
  checks.push({viewport_width:width,layout_width:layout.width,layout_height:layout.height,horizontal_overflow:layout.width>width,
   blocked_resources:blocked.length,text:layout.text.slice(0,18000),image:{path:file,sha256:hash(bytes),bytes:bytes.length}});
  await context.close();
 }
 console.log(JSON.stringify({state:'verified',method:'host-static-browser-v1',source_sha256:request.sha256,browser:browser.version(),
  capabilities:{scripts:false,network:false,external_files:false},checks}));
}finally{await browser.close();}

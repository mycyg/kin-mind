import assert from 'node:assert/strict'
import { build } from 'esbuild'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'node:http'
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
// Optional rendered-UI regression. Supply existing test dependencies via KIN_TEST_REACT_MODULES
// (a node_modules directory), KIN_TEST_PLAYWRIGHT_MODULE (module entry) and KIN_TEST_BROWSER.
const require = createRequire(import.meta.url)
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const source = process.env.KIN_TEST_SOURCE_ROOT || root
const output = join(root, '.dev/world-ui')
mkdirSync(output, { recursive: true })
const react = process.env.KIN_TEST_REACT_MODULES
const { chromium } = await import(pathToFileURL(process.env.KIN_TEST_PLAYWRIGHT_MODULE || require.resolve('playwright')).href)
writeFileSync(join(output, 'entry.tsx'), `import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { World } from ${JSON.stringify(join(source, 'kin/src/client/World.tsx'))};
function App() { const [place, go] = useState(); return <World home={{state:{desires:[]},plans:[]}} place={place} go={go} openSettings={()=>{}} talk={()=>{}} /> }
createRoot(document.getElementById('root')).render(<App />);`)
await build({ entryPoints: [join(output, 'entry.tsx')], outfile: join(output, 'bundle.js'), bundle: true,
  jsx: 'automatic', platform: 'browser', ...(react ? { alias: { react: join(react, 'react'), 'react-dom': join(react, 'react-dom') } } : {}) })
const server=createServer((q,s)=>{s.setHeader('Content-Type',q.url==='/bundle.js'?'text/javascript':'text/html');s.end(q.url==='/bundle.js'?readFileSync(join(output, 'bundle.js')):'<div id="root"></div><script src="/bundle.js"></script>')}).listen(0,'127.0.0.1')
await new Promise(r=>server.once('listening',r))
const browser=await chromium.launch({headless:true,executablePath:process.env.KIN_TEST_BROWSER})
try {
const page=await browser.newPage();page.setDefaultTimeout(5000); const errors=[];page.on('pageerror',e=>errors.push(e.message))
let failFirst=false,failMore=false,delayFirst=true,moreCalls=0
const diaries=Array.from({length:65},(_,i)=>({source_id:'synthetic-'+i,at:'2026-01-01T12:00:00Z',text:'Synthetic page '+i,replies:[]}))
await page.route('**/api/kin/**',async route=>{
 const path=new URL(route.request().url());let data={}
 if(path.pathname==='/api/kin/diary') {const cursor=Number(path.searchParams.get('cursor')||0);if(!cursor&&delayFirst)await new Promise(r=>setTimeout(r,400));if(cursor){moreCalls++;await new Promise(r=>setTimeout(r,100))}if((!cursor&&failFirst)||(cursor&&failMore))return route.fulfill({status:503,json:{error:'Synthetic unavailable'}}); data={entries:diaries.slice(cursor,cursor+30),total:65,cursor:cursor+30<65?cursor+30:null,replies:'disabled'}}
 if(path.pathname==='/api/kin/dreams')data={dreams:[]}
 if(path.pathname==='/api/kin/letters')data={items:[]}
 if(path.pathname==='/api/kin/explore')data={explorations:[]}
 if(path.pathname==='/api/kin/outing')data={creation:{current:null,last:{title:'Synthetic artifact',state:'completed',at:'2026-01-01T12:00:00Z',summary:'Technical report\nverification notes',folder:'/synthetic/output'},beat:null}}
 if(path.pathname.includes('calendar'))data={items:[]}
 await route.fulfill({json:data})
})
const url='http://127.0.0.1:'+server.address().port
await page.goto(url)
await page.getByText('正在翻日记…',{exact:true}).waitFor()
assert.equal(await page.getByText('还没写',{exact:true}).count(),0)
await page.getByText('65 页',{exact:true}).waitFor()
await page.locator('[data-tile="diary"]').click()
await page.getByText('Synthetic page 29',{exact:true}).waitFor()
assert.equal(await page.locator('.kin-paper').count(),30)
failMore=true;await page.getByRole('button',{name:'看更早的日记'}).click();await page.getByText(/Synthetic unavailable/).waitFor()
assert.equal(await page.locator('.kin-paper').count(),30)
failMore=false;await page.getByRole('button',{name:'再试一次'}).click();await page.getByText('Synthetic page 59',{exact:true}).waitFor()
assert.equal(await page.locator('.kin-paper').count(),60)
await page.getByRole('button',{name:'看更早的日记'}).dblclick();await page.getByText('Synthetic page 64',{exact:true}).waitFor()
assert.equal(await page.locator('.kin-paper').count(),65);assert.equal(moreCalls,3)
assert.equal(await page.getByRole('button',{name:'看更早的日记'}).count(),0)
await page.getByRole('button',{name:'回到小世界'}).click();await page.locator('[data-tile="wishes"]').click()
await page.getByText('查看详情',{exact:true}).waitFor()
assert.equal(await page.getByText('文件位置：/synthetic/output',{exact:true}).isVisible(),false)
await page.getByText('查看详情',{exact:true}).click();assert.equal(await page.getByText('文件位置：/synthetic/output',{exact:true}).isVisible(),true)
await page.getByText('查看详情',{exact:true}).click();assert.equal(await page.getByText('文件位置：/synthetic/output',{exact:true}).isVisible(),false)
failFirst=true;delayFirst=false;await page.reload();await page.getByText('日记暂时没读出来，点开重试',{exact:true}).waitFor();assert.equal(await page.getByText('还没写',{exact:true}).count(),0)
await page.locator('[data-tile="diary"]').click();await page.getByText(/Synthetic unavailable/).waitFor();failFirst=false;await page.getByRole('button',{name:'再试一次'}).click();await page.getByText('Synthetic page 29',{exact:true}).waitFor()
assert.deepEqual(errors,[])
console.log('PASS: rendered World loading/error/total, three diary pages, retry preserving pages, repeated clicks, Back/reopen, Outing collapsed/expanded details; no browser errors')
await page.screenshot({path:join(output, 'diary-browser.png'),fullPage:true})
}finally{await browser.close();server.close()}

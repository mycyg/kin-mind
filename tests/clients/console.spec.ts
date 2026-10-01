import {test,expect} from '../../console/node_modules/@playwright/test/index.mjs';

test.beforeEach(async({page})=>{await page.goto('/#token=test-console-local');await expect(page.getByRole('heading',{name:'每一段经历，都有来处。'})).toBeVisible()});

test('overview, scoped import, correction, provenance and restore',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.screenshot({path:'test-results/overview.png',fullPage:true});
  await page.getByRole('button',{name:'添加来源',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'添加来源'});
  const title='浏览器导入 '+Date.now();
  await dialog.getByLabel('标题',{exact:true}).fill(title);
  await dialog.getByLabel('内容',{exact:true}).fill('Browser test: migration uses isolated target.');
  await dialog.getByRole('button',{name:'保存来源'}).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole('button',{name:'记忆浏览',exact:true}).click();
  await page.getByLabel('搜索当前范围',{exact:true}).fill(title);
  await page.getByRole('button').filter({hasText:title}).click();
  const drawer=page.getByRole('dialog',{name:'记忆详情'});
  await expect(drawer.getByText('Browser test: migration uses isolated target.')).toBeVisible();
  await drawer.getByRole('button',{name:'纠正',exact:true}).click();
  await drawer.getByLabel('更正后的内容').fill('Browser test: migration validated and corrected.');
  await drawer.getByRole('button',{name:'保存纠正'}).click();
  await expect(drawer.getByText('r2',{exact:true})).toBeVisible();
  await drawer.getByRole('button',{name:'来源',exact:true}).click();
  await drawer.locator('.source-row button').first().click();
  await expect(drawer.locator('.trace')).toContainText('console');
  await drawer.getByRole('button',{name:'修订',exact:true}).click();
  await expect(drawer.locator('.revision')).toHaveCount(2);
  await drawer.getByRole('button',{name:'内容',exact:true}).click();
  await drawer.getByRole('button',{name:'归档',exact:true}).click();
  await expect(drawer.locator('.badge.archived')).toBeVisible();
  await drawer.getByRole('button',{name:'恢复',exact:true}).click();
  await expect(drawer.locator('.badge.active')).toBeVisible();
  await page.screenshot({path:'test-results/correction.png',fullPage:true});
  expect(errors).toEqual([]);
});

test('recall explanation, graph and all console sections',async({page})=>{
  for(const view of ['时间线与连续性','知识与附件','日记与自述','冲突与纠正','主动联系','设置']){
    await page.locator('nav').getByRole('button',{name:view,exact:true}).click();await expect(page.getByRole('heading',{name:view,exact:true})).toBeVisible();
  }
  await page.getByRole('button',{name:'主题与关系',exact:true}).click();
  await expect(page.locator('canvas')).toBeVisible();
  await page.getByRole('button',{name:'2D',exact:true}).click();
  await page.screenshot({path:'test-results/graph.png',fullPage:true});
  await page.getByRole('button',{name:'召回实验室',exact:true}).click();
  await page.locator('#recall-query').fill('数据库迁移');
  await page.getByRole('button',{name:'召回',exact:true}).click();
  await expect(page.locator('.context-output')).toContainText('迁移');
  await expect(page.getByText('过滤理由',{exact:true})).toBeVisible();
  await page.screenshot({path:'test-results/recall.png',fullPage:true});
});

test('mobile layout and reduced motion',async({page})=>{
  await page.setViewportSize({width:390,height:844});await page.emulateMedia({reducedMotion:'reduce'});
  await expect(page.getByRole('heading',{name:'每一段经历，都有来处。'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button',{name:'菜单',exact:true}).click();await page.getByRole('button',{name:'主动联系',exact:true}).first().click();
  await page.screenshot({path:'test-results/mobile.png',fullPage:true});
});

test('schedule pause, snooze and cancellation remain visible',async({page})=>{
 const headers={Authorization:'Bearer test-console-local'};
 const created=await page.request.post('/v1/sources',{headers,data:{namespace:'browser-schedule',key:String(Date.now()),text:'Browser schedule fixture',kind:'reminder'}});
 const source=await (await page.request.get(`/v1/sources/${(await created.json()).id}`,{headers})).json();
 await page.locator('nav').getByRole('button',{name:'主动联系',exact:true}).click();
 await page.getByLabel('记忆 id',{exact:true}).fill(source.record_ids[0]);await page.getByLabel('到期时间',{exact:true}).fill('2030-09-09T10:00');
 await page.getByRole('button',{name:'添加',exact:true}).click();
 const row=page.locator('.schedule-row').filter({hasText:source.record_ids[0].slice(0,25)});
 await expect(row).toContainText('已调度');await row.getByRole('button',{name:'暂停',exact:true}).click();await expect(row).toContainText('已暂停');
 await row.getByRole('button',{name:'延后一小时',exact:true}).click();await expect(row).toContainText('已调度');
 await row.getByRole('button',{name:'取消',exact:true}).click();await expect(row).toContainText('已取消');
 await page.screenshot({path:'test-results/scheduling.png',fullPage:true});
});

test('backup download and deletion preview use public operations',async({page})=>{
 await page.locator('nav').getByRole('button',{name:'设置',exact:true}).click();
 const wait=page.waitForEvent('download');await page.getByRole('button',{name:'下载完整备份',exact:true}).click();expect((await wait).suggestedFilename()).toMatch(/^backup_.*tar.gz$/);
 const headers={Authorization:'Bearer test-console-local'};const title='Delete only fixture '+Date.now();
 await page.request.post('/v1/sources',{headers,data:{namespace:'browser-delete',key:title,title,text:'Synthetic erase closure'}});
 await page.locator('nav').getByRole('button',{name:'记忆浏览',exact:true}).click();
 await page.getByLabel('搜索当前范围',{exact:true}).fill(title);
 await page.getByRole('button').filter({hasText:title}).click();const drawer=page.getByRole('dialog',{name:'记忆详情'});
 await drawer.getByRole('button',{name:'永久删除…',exact:true}).click();await expect(drawer.locator('.delete-preview')).toContainText('1 条记录');
 await drawer.getByRole('button',{name:'确认永久删除',exact:true}).click();await expect(drawer).not.toBeVisible();await expect(page.getByRole('button').filter({hasText:title})).toHaveCount(0);
});

test('correction loads a complete long record before saving',async({page})=>{
 const headers={Authorization:'Bearer test-console-local'};
 const title='Long correction '+Date.now();
 const content='Original complete paragraph. '.repeat(800)+'PRESERVE THE FINAL SENTENCE';
 const receipt=await (await page.request.post('/v1/sources',{headers,data:{namespace:'browser-long',key:title,title,text:content}})).json();
 const source=await (await page.request.get(`/v1/sources/${receipt.id}`,{headers})).json();
 await page.locator('nav').getByRole('button',{name:'记忆浏览',exact:true}).click();
 await page.getByLabel('搜索当前范围',{exact:true}).fill(title);
 await page.getByRole('button').filter({hasText:title}).click();
 const drawer=page.getByRole('dialog',{name:'记忆详情'});
 await drawer.getByRole('button',{name:'纠正',exact:true}).click();
 await expect(drawer.getByLabel('更正后的内容')).toHaveValue(content);
 await drawer.getByLabel('更正后的内容').fill('Corrected opening. '+content);
 await drawer.getByRole('button',{name:'保存纠正'}).click();
 await expect(drawer.getByText('r2',{exact:true})).toBeVisible();
 const first=await (await page.request.get(`/v1/memories/${source.record_ids[0]}?length=32000&budget=32000`,{headers})).json();
 expect(first.content).toBe('Corrected opening. '+content);
});


test('event graph exposes delivery coverage, source-backed edges and recorded body',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.getByRole('button',{name:'主题与关系',exact:true}).click();
  await expect(page.locator('.graph-legend')).toContainText('主观联想');
  const timeline=page.getByRole('region',{name:'事件时间线'});
  await timeline.getByRole('button').filter({hasText:'隔离目录中的恢复验证已经通过。'}).first().click();
  await expect(page.getByRole('region',{name:'图谱详情'})).toContainText('已分享');
  await page.getByRole('button',{name:'查看对应消息'}).first().click();
  await expect(page.getByRole('region',{name:'实际发送正文'})).toContainText('隔离目录中的恢复验证已经通过。');
  await expect(page.getByRole('region',{name:'实际发送正文'})).toContainText('synthetic-message');
  await page.screenshot({path:'test-results/event-coverage.png',fullPage:true});
  await page.getByRole('button',{name:'3D',exact:true}).click();
  await page.getByRole('button',{name:'2D',exact:true}).click();
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(errors).toEqual([]);
});

test('opening a record reads and writes nothing, and a slow earlier search never replaces a newer one',async({page})=>{
  const writes:string[]=[];page.on('request',r=>{if(r.method()!=='GET')writes.push(r.url())});
  await page.locator('nav').getByRole('button',{name:'记忆浏览',exact:true}).click();
  await page.getByRole('button').filter({hasText:'下次一起读完这篇论文'}).click();
  await expect(page.getByRole('dialog',{name:'记忆详情'})).toContainText('下次对话继续阅读论文的实验章节');
  expect(writes).toEqual([]);
  await page.getByRole('button',{name:'关闭详情'}).click();
  let held=false;
  await page.route('**/v1/memories?*',async route=>{
    if(new URL(route.request().url()).searchParams.get('query')==='花园'&&!held){held=true;await new Promise(r=>setTimeout(r,1500));}
    await route.continue().catch(()=>{});
  });
  const search=page.getByLabel('搜索当前范围',{exact:true});
  await search.fill('花园');await page.waitForTimeout(500);
  await search.fill('论文');
  await expect(page.getByRole('button').filter({hasText:'下次一起读完这篇论文'})).toBeVisible();
  await page.waitForTimeout(1600);
  expect(held).toBe(true);
  await expect(page.getByRole('button').filter({hasText:'共同经历：秋日的花园'})).toHaveCount(0);
  await expect(page.getByRole('button').filter({hasText:'下次一起读完这篇论文'})).toBeVisible();
});

test('continued reading never joins pages of two revisions',async({page})=>{
  const headers={Authorization:'Bearer test-console-local'};
  const title='Paged revision '+Date.now();
  const receipt=await (await page.request.post('/v1/sources',{headers,data:{namespace:'browser-paged',key:title,title,text:'First revision paragraph. '.repeat(700)+'END'}})).json();
  const id=(await (await page.request.get(`/v1/sources/${receipt.id}`,{headers})).json()).record_ids[0];
  await page.locator('nav').getByRole('button',{name:'记忆浏览',exact:true}).click();
  await page.getByLabel('搜索当前范围',{exact:true}).fill(title);
  await page.getByRole('button').filter({hasText:title}).click();
  const drawer=page.getByRole('dialog',{name:'记忆详情'});
  await expect(drawer.getByRole('button',{name:'继续读取'})).toBeVisible();
  const record=await (await page.request.get(`/v1/memories/${id}`,{headers})).json();
  expect((await page.request.post(`/v1/memories/${id}/revisions`,{headers,data:{expected_revision:record.revision,command_id:'paged-'+Date.now(),action:'correct',content:'Second revision. '.repeat(700),reason:'Changed while the first page was open'}})).ok()).toBe(true);
  await drawer.getByRole('button',{name:'继续读取'}).click();
  await expect(page.getByRole('alert').first()).toContainText('记忆已更新');
  await expect(drawer.getByText('Second revision.')).toHaveCount(0);
});

test('a contact policy is edited field by field and keeps its own scope and callback',async({page})=>{
  const headers={Authorization:'Bearer test-console-local'};
  const scope={project:'personal',persona:'Kin',collection:'default',world:'real'};
  const channel='http://127.0.0.1:8320/contact/reminder';
  expect((await page.request.put('/v1/contact/policies',{headers,data:{id:'browser-reminders',scope,enabled:true,channel,timezone:'Asia/Shanghai',quiet_start:0,quiet_end:0,max_per_day:5}})).ok()).toBe(true);
  await page.locator('nav').getByRole('button',{name:'主动联系',exact:true}).click();
  await page.getByRole('button',{name:'配置策略'}).click();
  await page.getByLabel('编辑策略').selectOption('browser-reminders');
  await expect(page.getByLabel('回调地址')).toHaveValue(channel);
  await expect(page.getByLabel('启用发送')).toBeChecked();
  await expect(page.getByLabel('时区')).toHaveValue('Asia/Shanghai');
  await page.getByLabel('安静时段开始').fill('23');
  await page.getByRole('button',{name:'保存策略'}).click();
  await expect(page.locator('.notice')).toContainText('联系策略已保存');
  const saved=(await (await page.request.get('/v1/contact/policies',{headers})).json()).items.find((p:any)=>p.id==='browser-reminders').data;
  expect(saved).toMatchObject({scope,enabled:true,channel,timezone:'Asia/Shanghai',quiet_start:23,quiet_end:0,max_per_day:5});
});

test('a failed web import says why and keeps the dialog open',async({page})=>{
  await page.route('**/v1/sources/url',route=>route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'Too many redirects'})}));
  await page.getByRole('button',{name:'添加来源',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'添加来源'});
  await dialog.getByLabel('网页地址').fill('https://example.invalid/page');
  await dialog.getByRole('button',{name:'保存来源'}).click();
  await expect(dialog.getByRole('alert')).toContainText('Too many redirects');
  await expect(dialog).toBeVisible();
});

test('settings forms change only what was edited, over what the service holds now',async({page})=>{
  const headers={Authorization:'Bearer test-console-local'};
  const research={startup:3000,passive:300,cumulative:9000};
  expect((await page.request.put('/v1/settings/budgets',{headers,data:{research}})).ok()).toBe(true);
  await page.locator('nav').getByRole('button',{name:'设置',exact:true}).click();
  await expect(page.locator('input[name="tool-startup"]')).toHaveValue('2000');
  const summary={endpoint:'http://127.0.0.1:9/v1',model:'synthetic-summary'};
  expect((await page.request.put('/v1/settings/models',{headers,data:{summary}})).ok()).toBe(true);
  const support={startup:1000,passive:100,cumulative:5000};
  expect((await page.request.put('/v1/settings/budgets',{headers,data:{research,support}})).ok()).toBe(true);
  await page.locator('input[name="tool-startup"]').fill('2500');
  await page.getByRole('button',{name:'保存预算'}).click();
  await expect(page.locator('.notice')).toContainText('上下文预算已保存');
  const budgets=await (await page.request.get('/v1/settings/budgets',{headers})).json();
  expect(budgets).toMatchObject({research,support,tool:{startup:2500}});
  expect(Object.keys(budgets.tool)).toEqual(['startup']);
  await page.getByLabel('API 端点').fill('http://127.0.0.1:9/v1');
  await page.getByLabel('模型名称').fill('synthetic-extraction');
  await page.getByRole('button',{name:'保存 extraction'}).click();
  await expect(page.locator('.notice')).toContainText('模型配置已保存');
  const models=await (await page.request.get('/v1/settings/models',{headers})).json();
  expect(models.summary).toMatchObject(summary);
  expect(models.extraction).toMatchObject({model:'synthetic-extraction'});
});

test('picking a node or typing a filter keeps the graph scene',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  const graphReads:string[]=[];
  page.on('request',r=>{if(new URL(r.url()).pathname==='/v1/graph')graphReads.push(r.url());});
  // Let the connection's scope debounce expire while a node detail is in flight.
  await page.route('**/v1/graph/object/**',async route=>{
    await new Promise(resolve=>setTimeout(resolve,450));
    await route.continue();
  });
  await page.getByRole('button',{name:'主题与关系',exact:true}).click();
  await expect(page.locator('canvas')).toHaveCount(1);
  const canvas=await page.locator('canvas').elementHandle();
  const detail=page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/v1/graph/object/'));
  await page.getByRole('region',{name:'事件时间线'}).getByRole('button').first().click();
  await detail;
  await expect(page.getByRole('region',{name:'图谱详情'})).not.toContainText('点开事件或连线');
  await page.getByLabel('人物、项目或旧事').fill('迁移');
  expect(await canvas!.evaluate(el=>el.isConnected)).toBe(true);
  await expect(page.locator('canvas')).toHaveCount(1);
  expect(graphReads).toHaveLength(1);
  expect(errors).toEqual([]);
});

test('jobs are read by state, the ones needing attention first',async({page})=>{
  await page.getByRole('button',{name:'整理当前范围'}).first().click();
  await expect(page.locator('.notice')).toContainText('任务已加入处理队列');
  const tab=page.getByRole('tab',{name:/等待处理 [1-9]/});
  await expect(tab).toBeVisible();await tab.click();
  await expect(page.locator('.job-row').filter({hasText:'organize'}).first()).toBeVisible();
});

test('a JSON attachment previews as text',async({page})=>{
  const headers={Authorization:'Bearer test-console-local'};
  const title='JSON attachment '+Date.now();
  const receipt=await (await page.request.post('/v1/sources/upload',{headers,multipart:{
    metadata:JSON.stringify({namespace:'browser-json',key:title,title,media_type:'application/json',authority:'document'}),
    file:{name:'data.json',mimeType:'application/json',buffer:Buffer.from('{"kept":"as stored"}')}}})).json();
  // An attachment's own record waits for the parser; this record cites the stored file directly.
  const record=await (await page.request.post('/v1/memories',{headers,data:{command_id:'json-'+Date.now(),record:{kind:'knowledge',title,content:'A record citing a JSON attachment',source_ids:[receipt.id]}}})).json();
  await page.locator('nav').getByRole('button',{name:'记忆浏览',exact:true}).click();
  await page.getByLabel('搜索当前范围',{exact:true}).fill(title);
  await page.getByRole('button').filter({hasText:title}).click();
  const drawer=page.getByRole('dialog',{name:'记忆详情'});
  await expect(drawer.getByText(record.id)).toBeVisible();
  await drawer.getByRole('button',{name:'来源',exact:true}).click();
  await drawer.locator('.source-row button').first().click();
  await drawer.getByRole('button',{name:'预览附件'}).click();
  await expect(drawer.locator('.attachment-preview pre')).toHaveText('{"kept":"as stored"}');
});

test('a kin context recall shows its text and opens only the records in its index',async({page})=>{
  const headers={Authorization:'Bearer test-console-local'};
  const title='Context index '+Date.now();
  const receipt=await (await page.request.post('/v1/sources',{headers,data:{namespace:'browser-context',key:title,title,text:'Context index record body.'}})).json();
  const id=(await (await page.request.get(`/v1/sources/${receipt.id}`,{headers})).json()).record_ids[0];
  // What a scope with memory context answers (ContextRecallResult): ids read at a record's revision
  // or at a derived view's digest, and the rendered context as text.
  const items=[{id:'affect',revision:'e3c5'.repeat(16),depth:'original'},{id,revision:1,depth:'original'}];
  await page.route('**/v1/recall',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
    state:'ready',items,index:items,text:'合成的上下文正文',tokens:12,budget:2000,accounts:{},generation:3,latency_ms:4.2,
    cursor:null,session_used:0,instruction_authority:'data'})}));
  await page.getByRole('button',{name:'召回实验室',exact:true}).click();
  await page.locator('#recall-query').fill('上下文');
  await page.getByRole('button',{name:'召回',exact:true}).click();
  await expect(page.locator('.context-output')).toHaveText('合成的上下文正文');
  await expect(page.locator('.recall-stats')).toContainText('2 条记录');
  await expect(page.locator('.result-link.derived')).toHaveText('affect');
  await expect(page.getByRole('button',{name:'affect'})).toHaveCount(0);
  await page.locator('button.result-link').filter({hasText:id}).click();
  await expect(page.getByRole('dialog',{name:'记忆详情'})).toContainText('Context index record body.');
});

test('the scope picker lists every page of scopes and reads them again after a refresh',async({page})=>{
  // CR-MEM-13: more scopes than one page holds; the picker follows the cursor to the end.
  const scopes=Array.from({length:250},(_,i)=>({project:`p${String(i).padStart(3,'0')}`,persona:'Kin',collection:'default',world:'real'}));
  const asked:string[]=[];
  await page.route('**/v1/scopes*',route=>{
    const url=new URL(route.request().url());
    const cursor=url.searchParams.get('cursor'),limit=Number(url.searchParams.get('limit')||100);
    asked.push(cursor??'');
    const start=cursor?scopes.findIndex(s=>JSON.stringify(s)===cursor)+1:0,items=scopes.slice(start,start+limit);
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,
      cursor:start+limit<scopes.length?JSON.stringify(items.at(-1)):null,default_scope:scopes[0]})});
  });
  const picker=page.getByLabel('已有范围');
  await picker.focus();
  await expect(picker.locator('option')).toHaveCount(251);
  await expect(picker.locator('option').last()).toHaveText('p249 / Kin / default / real');
  expect(asked.length).toBe(2);
  await page.getByRole('button',{name:'刷新'}).click();
  await page.locator('body').click();
  await picker.focus();
  await expect.poll(()=>asked.length).toBe(4);
});

test('a reminder the host answered 2xx reads as handed to the host, not as delivered',async({page})=>{
  // CR2-INT-07: a 202 means the host took it into its own queue; nobody has received it yet.
  await page.route('**/v1/contact/outbox*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
    items:[{id:'delivery_'+'a'.repeat(32),state:'sent',data:{text:'该出门散步了'}}],cursor:null})}));
  await page.locator('nav').getByRole('button',{name:'主动联系',exact:true}).click();
  const row=page.locator('.outbox-row').filter({hasText:'该出门散步了'});
  await expect(row.locator('.badge')).toHaveText('已交给宿主');
  await expect(row).not.toContainText('已发送');
});

test('a letter to Kin is sealed until its day: only its date shows, and it can be erased',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  const words='Sealed browser letter '+Date.now();
  const tomorrow=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Singapore'}).format(Date.now()+86400000);
  await page.locator('nav').getByRole('button',{name:'日记与自述',exact:true}).click();
  const panel=page.getByRole('region',{name:'时光信与暗房'});
  const rows=panel.locator('.sealed-row').filter({hasText:`一封 ${tomorrow} 才能打开的信`});
  await expect(panel.getByLabel('写给 Kin 的信')).toBeVisible();
  const before=await rows.count();
  await panel.getByLabel('写给 Kin 的信').fill(words);
  await panel.getByLabel('打开日期').fill(tomorrow);
  await panel.getByRole('button',{name:'封存',exact:true}).click();
  await expect(rows).toHaveCount(before+1);
  await expect(page.getByText(words)).toHaveCount(0);
  const headers={Authorization:'Bearer test-console-local'};
  expect(await (await page.request.get('/v1/sealed',{headers})).text()).not.toContain(words);
  expect(await (await page.request.post('/v1/recall',{headers,data:{query:words}})).text()).not.toContain(words);
  await rows.first().getByRole('button',{name:'永久删除…',exact:true}).click();
  await rows.first().getByRole('button',{name:'确认永久删除',exact:true}).click();
  await expect(rows).toHaveCount(before);
  expect(errors).toEqual([]);
});

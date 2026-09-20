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

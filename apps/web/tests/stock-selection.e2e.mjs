// Isolated browser state and intercepted writes preserve every real task.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
const require = createRequire(new URL('../../../../deepseek-harness-cnc/package.json', import.meta.url));
const { chromium } = require('playwright');
const artifacts = fileURLToPath(new URL('../../../.tmp-progress-validation/stock-selection/', import.meta.url));
await mkdir(artifacts,{recursive:true});
const jobId='1b67812ff09247f2a8c2d8abf65211f5', path=`/api/v1/jobs/${jobId}`;
let job=await (await fetch(`http://localhost:3082${path}`)).json();
const stockData=await (await fetch(`http://localhost:3082${path}/stock-selection`)).json();
let rule=stockData.current_rule, applied=false;
function selection() {
  const proposed=structuredClone(stockData.proposed_stock);
  proposed.selection.rule=rule;
  proposed.diameter_mm=proposed.selection.part_max_outer_diameter_mm+rule.diameter_increment_mm;
  const stock=applied ? job.plan.stock : stockData.stock;
  return {...stockData,current_rule:rule,stock,proposed_stock:proposed,
    view:{...stockData.view,before_samples:stockData.view.before_samples.map(sample=>({...sample,outer_radius:stock.diameter_mm/2})),after_samples:stockData.view.after_samples.map(sample=>({...sample,outer_radius:stock.diameter_mm/2}))}};
}
const browser=await chromium.launch({headless:true,channel:'msedge'});
const page=await browser.newPage({viewport:{width:1360,height:850}});
const errors=[]; page.on('pageerror',error=>errors.push(error.message));
await page.addInitScript(({jobId,filename})=>{
  sessionStorage.setItem('nexus.process-studio.login.v1','admin');
  localStorage.setItem('nexus.process-studio.task.v1',JSON.stringify({jobId,filename,view:{stage:'process',following:false}}));
},{jobId,filename:job.filename});
await page.route(`**${path}`,route=>route.fulfill({json:job}));
await page.route(`**${path}/stock-selection`,route=>route.fulfill({json:selection()}));
await page.route('**/api/v1/process-rules/stock-selection',route=>{
  if(route.request().method()==='PUT') rule={...route.request().postDataJSON(),revision:rule.revision+1};
  return route.fulfill({json:rule});
});
await page.route(`**${path}/stock-selection/apply-rule`,route=>{
  job={...job,plan:{...job.plan,stock:{...job.plan.stock,...selection().proposed_stock}}};applied=true;
  return route.fulfill({json:job});
});
try {
  await page.goto('http://localhost:3082/');
  await page.getByRole('button',{name:'第一步：毛坯选择',exact:true}).click();
  await page.getByRole('region',{name:'毛坯选择详情'}).waitFor();
  await page.getByText('实心圆棒 · 初始内径 0 mm',{exact:true}).waitFor();
  await page.locator('.model-viewer canvas').first().waitFor();
  await page.screenshot({path:artifacts+'solid-stock.png'});
  await page.getByRole('button',{name:'配置毛坯选择规则',exact:true}).click();
  await page.getByLabel('直径增量（mm）',{exact:true}).fill('0.4');
  await page.getByRole('button',{name:'保存规则',exact:true}).click();
  await page.getByText('规则已保存，新任务自动采用；已有任务可在毛坯选择中单独应用。',{exact:true}).waitFor();
  assert.equal(rule.diameter_increment_mm,.4);
  await page.screenshot({path:artifacts+'rule-config.png'});
  await page.getByRole('button',{name:'关闭毛坯规则',exact:true}).click();
  await page.getByRole('button',{name:'将当前规则应用到此任务',exact:true}).click();
  await page.getByText('已按当前规则更新毛坯，原有刀路与仿真需重新计算。',{exact:true}).waitFor();
  assert.equal(job.plan.stock.diameter_mm,stockData.proposed_stock.selection.part_max_outer_diameter_mm+.4);
  await page.reload();
  await page.getByRole('button',{name:'第一步：毛坯选择',exact:true}).click();
  await page.getByText('毛坯直径 = 最大外径 + 0.4 mm',{exact:true}).waitFor();
  await page.getByRole('button',{name:'配置毛坯选择规则',exact:true}).click();
  await page.getByLabel('直径增量（mm）',{exact:true}).fill('0.2');
  await page.getByRole('button',{name:'保存规则',exact:true}).click();
  await page.getByText('规则已保存，新任务自动采用；已有任务可在毛坯选择中单独应用。',{exact:true}).waitFor();
  await page.screenshot({path:artifacts+'rule-default.png'});
  await page.getByRole('button',{name:'关闭毛坯规则',exact:true}).click();
  await page.getByRole('button',{name:'将当前规则应用到此任务',exact:true}).click();
  await page.getByText('毛坯直径 = 最大外径 + 0.2 mm',{exact:true}).waitFor();
  // The async STL load builds the Three.js stock mesh after the inspector updates.
  await page.waitForTimeout(2000);
  await page.screenshot({path:artifacts+'solid-stock-default.png'});
  assert.ok(await page.locator('.studio-setup .studio-operation').count()>0,'Actual cutting operations remain selectable after viewing stock');
  assert.deepEqual(errors,[]);
  console.log('PASS 毛坯作为第一步可查看实心初始状态，规则可保存、应用并在刷新后显示；实际任务未修改');
  console.log(artifacts);
} finally { await browser.close(); }

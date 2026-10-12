// Freeze solver evidence in a separate browser; never submit or change a live job.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const artifacts = fileURLToPath(new URL('../../../.tmp-progress-validation/',import.meta.url));
await mkdir(artifacts,{recursive:true});
const require = createRequire(new URL('../../../../deepseek-harness-cnc/package.json', import.meta.url));
const { chromium } = require('playwright');
const jobId = process.env.MACHINING_TEST_JOB || 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const path = `/api/v1/jobs/${jobId}`;
let job, preview, geometry, chain;
if (!process.env.MACHINING_TEST_JOB) {
  // Run the real deterministic provider in temporary test storage, independent of live edits.
  const code = "import sys,json,tempfile,pathlib;sys.path.insert(0,'/app/tests');from pytest import MonkeyPatch;from test_process_preview import prepare;from test_l32_operation_trial import rotational,snapshot;from app.process_preview import operation_preview,cumulative_playback;from app.agent.l32_operation_trial import accept_trial,run_independent_trial;\nwith tempfile.TemporaryDirectory() as folder:\n p=pathlib.Path(folder);source,first,state=prepare(MonkeyPatch(),p);accepted=accept_trial(job=source,state=state,trial=first,operation_id='OP10');second,_=run_independent_trial(job=source,operation_id='OP20',rotational=rotational(),snapshot=snapshot(),state=accepted,resolution_mm=0.5);(p/'agent-l32-trial-OP10.json').write_text(json.dumps(first));(p/'agent-l32-operation-trial.json').write_text(json.dumps(second));(p/'harness-l32-operation-state.json').write_text(json.dumps(accepted));source.model_url='/api/v1/jobs/'+source.id+'/files/model.stl';print(json.dumps({'job':source.model_dump(mode='json'),'preview':operation_preview(source.id,'OP20'),'chain':cumulative_playback(source.id,'OP20'),'geometry':rotational().model_dump(mode='json')}))";
  ({ job, preview, geometry, chain } = JSON.parse(execFileSync('docker',['exec','seksun-cnc-api-1','python','-c',code],{encoding:'utf8'})));
} else {
 job = await (await fetch(`http://127.0.0.1:8090${path}`)).json();
 for (const operation of job.plan.setups.flatMap(setup => setup.operations)) {
  const response = await fetch(`http://127.0.0.1:8090${path}/agent/l32/operations/${operation.id}/preview`);
  if (response.ok) { const value = await response.json(); if(value.toolpath && value.axis) { preview=value; break; } }
}
}
assert.ok(preview, 'Real revision-valid trial evidence is required');
if (!chain) chain = await (await fetch(`http://127.0.0.1:8090${path}/agent/l32/playback?through_operation_id=${preview.operation_id}`)).json();
const browser = await chromium.launch({ headless:true, channel:'msedge' });
const page = await browser.newPage({ viewport:{width:1360,height:850} });
const errors=[]; page.on('pageerror', error=>errors.push(error.message));
let phase='simulation', revision=0, stale=false;
await page.addInitScript(({jobId,filename}) => {
  sessionStorage.setItem('nexus.process-studio.login.v1','admin');
  localStorage.setItem('nexus.process-studio.task.v1',JSON.stringify({jobId,filename,view:{stage:'features',following:true}}));
}, {jobId,filename:job.filename});
await page.route(`**${path}`, route=>route.fulfill({json:job}));
if (geometry) {
  await page.route(`**${path}/files/rotational-features.json`,route=>route.fulfill({json:geometry}));
  const points=geometry.profiles[0].points;
  let stl='solid target\n';
  for(let row=0;row<points.length-1;row++) for(let i=0;i<32;i++) {
    const vertex=(p,angle)=>[p.radius*Math.cos(angle),p.radius*Math.sin(angle),p.z].join(' ');
    const a=i*Math.PI/16,b=(i+1)*Math.PI/16;
    for(const triangle of [[vertex(points[row],a),vertex(points[row],b),vertex(points[row+1],b)],[vertex(points[row],a),vertex(points[row+1],b),vertex(points[row+1],a)]])
      stl+=`facet normal 0 0 0\nouter loop\n${triangle.map(p=>`vertex ${p}`).join('\n')}\nendloop\nendfacet\n`;
  }
  stl+='endsolid target';
  await page.route(`**${path}/files/model.stl`,route=>route.fulfill({body:stl,contentType:'model/stl'}));
}
await page.route(`**${path}/agent/workspace`, route=>route.fulfill({json:{
  job_id:jobId,events:[{event_id:`preview-${phase}-${revision}`,stage:'harness_operation_trial',kind:'validation',status:'blocked',created_at:new Date().toISOString(),metrics:{operation_id:preview.operation_id},viewer:{mode:phase,operation_id:preview.operation_id},title:'单道工序试算'}],active_event_id:`preview-${phase}-${revision}`,artifacts:[],
}}));
await page.route(`**${path}/agent/l32/operations/*/preview`, route=>stale ? route.fulfill({status:409,json:{detail:'stale'}}) : route.fulfill({json:{...preview,revision:`${preview.revision}:${revision}`}}));
await page.route(`**${path}/agent/l32/playback?*`,route=>route.fulfill({json:{...chain,previews:chain.previews.map(item=>({...item,revision:`${item.revision}:${revision}`}))}}));
// Cumulative CAD snapshots are a separate planned preview, not the trial solver output.
await page.route(`**${path}/l32/material-snapshots`, route=>route.fulfill({json:{operations:[]}}));
const progress=()=>page.getByLabel('仿真进度');
const setProgress=async value=>{
  await progress().press('Home');
  if(value===1000) await progress().press('End');
  else for(let count=0;count<value/100;count++) await progress().press('PageUp');
  assert.equal(Number(await progress().inputValue()),value);
};
try {
  await page.goto('http://127.0.0.1:3082');
  await page.waitForFunction(()=>Number(document.querySelector('input[aria-label="仿真进度"]')?.value)>25);
  assert.equal(await page.getByRole('button',{name:'仿真验证',exact:true}).getAttribute('aria-pressed'),'true');
  await page.locator('.studio-machining-state').getByText(/试算结果回放/).waitFor();
  assert.ok(await page.locator('.model-viewer canvas').count()>0);
  const start=Number(await progress().inputValue());
  await page.waitForFunction(min=>Number(document.querySelector('input[aria-label="仿真进度"]')?.value)>min+40,start);
  console.log('PASS 真实工序试算在无最终 CAM 时自动进入仿真并播放');
  if (chain.complete && geometry) {
    await page.getByRole('button',{name:/暂停/}).click();
    assert.equal(await page.getByRole('button',{name:'累计',exact:true}).getAttribute('class'),'active');
    await setProgress(0);
    await page.waitForFunction(()=>document.querySelector('.model-viewer')?.dataset.materialFrame==='0');
    const stock=Number(await page.locator('.model-viewer').getAttribute('data-material-volume'));
    await page.screenshot({path:artifacts+'cumulative-stock.png'});
    await setProgress(500);
    await page.waitForFunction(initial=>Number(document.querySelector('.model-viewer')?.dataset.materialVolume)<initial,stock);
    const halfway=Number(await page.locator('.model-viewer').getAttribute('data-material-volume'));
    await setProgress(1000);
    await page.waitForFunction(expected=>Math.abs(Number(document.querySelector('.model-viewer')?.dataset.materialVolume)-expected)<1e-5,preview.evidence.remaining_volume_mm3);
    const final=Number(await page.locator('.model-viewer').getAttribute('data-material-volume'));
    assert.ok(stock>=halfway && halfway>=final);
    await page.screenshot({path:artifacts+'cumulative-final.png'});
    await setProgress(0);
    await page.waitForFunction(initial=>Math.abs(Number(document.querySelector('.model-viewer')?.dataset.materialVolume)-initial)<1e-5,stock);
    await page.getByRole('button',{name:'▶ 播放',exact:true}).click();
    console.log('PASS 毛坯→跨工序累计去除→最终余料体积与真实求解器一致，倒放恢复毛坯');
  }
  await page.screenshot({path:`${artifacts}${geometry ? 'machining-follow-fixture' : 'machining-follow'}.png`});
  await page.getByRole('button',{name:/暂停/}).click();
  const paused=await progress().inputValue();
  await page.waitForTimeout(2800);
  assert.equal(await progress().inputValue(),paused);
  console.log('PASS 相同证据轮询不重播，不覆盖手动暂停');
  revision++;
  await page.getByRole('button',{name:/暂停/}).waitFor();
  await page.waitForFunction(()=>Number(document.querySelector('input[aria-label="仿真进度"]')?.value)>25);
  console.log('PASS 新试算版本自动重新回放');
  await page.getByRole('button',{name:/跟随智能体/}).click();
  await page.getByRole('button',{name:'▶ 播放',exact:true}).waitFor();
  const frozen=await progress().inputValue();
  phase='toolpath'; revision++;
  await page.waitForTimeout(2800);
  assert.equal(await progress().inputValue(),frozen);
  assert.equal(await page.getByRole('button',{name:'仿真验证',exact:true}).getAttribute('aria-pressed'),'true');
  console.log('PASS 自由查看时停止自动播放和阶段切换');
  await page.getByRole('button',{name:/自由查看/}).click();
  await page.waitForFunction(()=>document.querySelector('.studio-tabs button:nth-child(3)')?.getAttribute('aria-pressed')==='true');
  await page.waitForFunction(()=>Number(document.querySelector('input[aria-label="仿真进度"]')?.value)>25);
  console.log('PASS 恢复跟随后刀路阶段自动显示并回放');
  stale=true;
  await page.locator('.studio-machining-state').getByText('工序或材料已更新，等待重新试算').waitFor();
  assert.equal(await progress().count(),0);
  assert.deepEqual(errors,[]);
  console.log('PASS 陈旧证据清除，无前端异常');
} finally { await browser.close(); }

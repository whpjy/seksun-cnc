// Isolated fixture; no live engineering job is changed.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
const require = createRequire(new URL('../../../../deepseek-harness-cnc/package.json', import.meta.url));
const { chromium } = require('playwright');
const base = process.env.PROGRESS_TEST_URL || 'http://127.0.0.1:3082';
const browser = await chromium.launch({ headless: true, channel: process.env.PROGRESS_BROWSER_CHANNEL || 'msedge' });
const page = await browser.newPage({ viewport: { width: 1000, height: 650 } });
const errors = [], requested = [];
let translationRequests = 0;
page.on('request', request => { if (request.url().includes('/api/process-studio/translate')) translationRequests++; });
page.on('pageerror', error => errors.push(error.message));
let mode = 'translate';
await page.route('**/api/process-studio/translate', async route => {
  const { texts } = route.request().postDataJSON();
  requested.push(...texts);
  await new Promise(resolve => setTimeout(resolve, 500));
  if (mode === 'offline') return route.fulfill({ status: 503, body: '{}' });
  return route.fulfill({ json: { translations: texts.map(text => mode === 'junk' ? '请提供待翻译文本' : mode === 'wrapper' ? '{"待翻译文本":"正在检查工序目录及可用定义。"}' : text.includes('toolpaths') ? '正在生成刀具路径。' : text.includes('collision') ? '正在检查碰撞。' : '正在检查工序目录及可用定义。') } });
});
const displayed = page.locator('.studio-transcript-line[data-ready="true"]:visible .studio-transcript-body>span');
const waitForText = text => page.waitForFunction(expected => document.querySelector('.studio-transcript-line[data-ready="true"]:not(.studio-transcript-previous) .studio-transcript-body>span')?.textContent === expected, text);
const first = 'Inspect the operation catalog and available definitions.';
const second = 'Generate toolpaths for the machining operation.';
try {
  await page.addInitScript(() => localStorage.setItem('seksun.process-studio.translate-to-chinese.v1', 'true'));
  await page.goto(`${base}/tests/progress-display.html`);
  await page.waitForFunction(() => !!window.progressFixture);
  assert.equal(await page.getByRole('switch', { name: '中文翻译' }).count(), 0);
  assert.equal(await displayed.innerText(), 'N');
  await page.evaluate(() => window.progressFixture.setText('Native reasoning arrives immediately'));
  assert.equal(await displayed.innerText(), 'Native reasoning arrives immediately');
  await page.waitForTimeout(100);
  assert.equal(requested.length, 0);
  await page.evaluate(() => window.progressFixture.setText('N'));
  await page.evaluate(() => window.progressFixture.setTranslateToChinese(true));
  console.log('PASS: code configuration defaults off, legacy saved preference is ignored, and no switch is shown');
  assert.equal(await displayed.count(), 0);
  assert.equal(await page.getByText('正在思考 ·', { exact: true }).isVisible(), false);
  assert.equal(await page.locator('.studio-translation-caret').count(), 0);
  assert.equal(await page.getByText('正在分析工艺信息…', { exact: true }).count(), 0);
  await page.evaluate(async first => {
    for (let index = 0; index < 160; index++) {
      window.progressFixture.setText(`${first}\n\nOpen paragraph still being generated ${index}`);
      await new Promise(resolve => setTimeout(resolve, 5));
    }
  }, first);
  await waitForText('正在检查工序目录及可用定义。');
  assert.deepEqual(requested, [first]);
  console.log('PASS: first paragraph appears while later English is still streaming; no placeholder or repeated translation');

  await page.evaluate(({ first, second }) => window.progressFixture.setText(`${first}\n\n${second}\n\nStill generating`), { first, second });
  assert.equal(await displayed.innerText(), '正在检查工序目录及可用定义。');
  await waitForText('正在生成刀具路径。');
  await page.getByRole('button', { name: '切换历史' }).click();
  assert.equal(await displayed.innerText(), '正在检查工序目录及可用定义。\n正在生成刀具路径。');
  assert.equal(await page.getByText('查看原文', { exact: true }).count(), 0);
  assert.equal(await page.getByText(first, { exact: true }).count(), 0);
  console.log('PASS: paragraphs append in order; compact shows latest, history retains previous paragraphs');

  await page.evaluate(({ first, second }) => { window.progressFixture.setText(`${first}\n\n${second}\n\nCheck collision for the final path`); window.progressFixture.setRunning(false); }, { first, second });
  await page.waitForFunction(() => document.querySelector('.studio-transcript-line[data-ready="true"]:not(.studio-transcript-previous) .studio-transcript-body>span')?.textContent.endsWith('正在检查碰撞。'));
  assert.equal(await displayed.innerText(), '正在检查工序目录及可用定义。\n正在生成刀具路径。\n正在检查碰撞。');
  assert.equal(requested.filter(text => text === first).length, 1);
  console.log('PASS: stream end flushes final unterminated paragraph without losing earlier content');

  await page.getByRole('button', { name: '切换历史' }).click();
  await page.evaluate(() => { window.progressFixture.setRunning(true); window.progressFixture.setText('Review a changed catalog\n\n'); });
  await page.waitForTimeout(90);
  assert.equal(await displayed.innerText(), '正在检查碰撞。');
  await page.evaluate(() => window.progressFixture.setText('Generate new toolpaths\n\n'));
  await page.waitForTimeout(420);
  assert.notEqual(await displayed.innerText(), '正在检查工序目录及可用定义。');
  await waitForText('正在生成刀具路径。');
  assert.equal(await displayed.count(), 1);
  console.log('PASS: previous progress stays visible while translating; new result replaces it without duplicate rows or stale results');

  mode = 'offline';
  const failedResponse = page.waitForResponse(response => response.url().includes('/api/process-studio/translate') && response.status() === 503);
  await page.evaluate(() => { window.progressFixture.setRunning(false); window.progressFixture.setText('Translate a different failed paragraph'); });
  await failedResponse;
  await page.waitForTimeout(50);
  assert.equal(await displayed.innerText(), '正在生成刀具路径。');
  assert.equal(await page.getByText('（本段中文转换暂不可用）', { exact: true }).count(), 0);
  const afterFailure = requested.length;
  await page.waitForTimeout(1000);
  assert.equal(requested.length, afterFailure);
  assert.equal(await page.getByText('Translate a different failed paragraph', { exact: true }).count(), 0);
  console.log('PASS: failure silently preserves previous progress, no translator error or retry storm');

  mode = 'wrapper';
  await page.evaluate(() => window.progressFixture.setText('Inspect a wrapped translation response'));
  await waitForText('正在检查工序目录及可用定义。');
  assert.equal(await page.getByText('待翻译文本', { exact: false }).count(), 0);
  mode = 'junk';
  const junkResponse = page.waitForResponse(response => response.url().includes('/api/process-studio/translate'));
  await page.evaluate(() => window.progressFixture.setText('Inspect a translator instruction echo'));
  await junkResponse;
  await page.waitForTimeout(50);
  assert.equal(await displayed.innerText(), '正在检查工序目录及可用定义。');
  assert.equal(await page.getByText('请提供待翻译文本', { exact: true }).count(), 0);
  console.log('PASS: JSON wrappers are stripped and translator instruction echoes never enter the thought log');
  const afterJunk = requested.length;

  await page.evaluate(() => window.progressFixture.setText('正在检查 STEP 模型和 G54'));
  await waitForText('正在检查 STEP 模型和 G54');
  assert.equal(requested.length, afterJunk);
  assert.deepEqual(errors, []);
  console.log('PASS: Chinese streams directly; identifiers retained; no browser errors');

  mode = 'translate';
  await page.evaluate(() => {
    window.progressFixture.setRunning(true);
    window.progressFixture.setText('');
    window.progressFixture.setEvents([{ seq: 1, type: 'tool/result', time: 1, data: { message: { source: { toolName: 'mcp__cnc_trial_l32_operation' }, content: [{ type: 'text', text: '{}' }] } } }]);
  });
  await waitForText('计算刀路并试算工序 · 结果已返回');
  await page.evaluate(() => {
    window.progressFixture.setAttemptId('after-operation-trial');
    window.progressFixture.setText('Before adding new operations inspect the catalog\n\n');
  });
  await page.waitForTimeout(100);
  assert.equal(await displayed.innerText(), '计算刀路并试算工序 · 结果已返回');
  assert.equal(await page.locator('.studio-transcript-line').count(), 1);
  await waitForText('正在检查工序目录及可用定义。');
  assert.equal(await page.locator('.studio-transcript-line').count(), 1);
  assert.equal(await page.locator('.studio-transcript-previous').count(), 0);
  assert.equal(await page.getByText('计算刀路并试算工序 · 结果已返回', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '切换历史' }).click();
  assert.equal(await page.getByText('计算刀路并试算工序 · 结果已返回', { exact: true }).count(), 1);
  await page.getByRole('button', { name: '切换历史' }).click();
  assert.equal(await page.getByText('计算刀路并试算工序 · 结果已返回', { exact: true }).count(), 0);
  console.log('PASS: operation result is replaced in the DOM by new reasoning; history toggle does not pin it');
  await page.evaluate(() => window.progressFixture.setEvents([]));

  await page.unroute('**/api/process-studio/translate');
  await page.getByRole('button', { name: '切换历史' }).click();
  const started = Date.now();
  await page.evaluate(() => { window.progressFixture.setRunning(true); window.progressFixture.setText('Now check the operation catalog.\n\nThe next paragraph is still being generated'); });
  await page.waitForFunction(() => {
    const text = document.querySelector('.studio-transcript-line[data-ready="true"]:not(.studio-transcript-previous) .studio-transcript-body>span')?.textContent || '';
    return text.includes('目录') && !text.includes('不可用');
  }, null, { timeout: 15000 });
  console.log(`PASS: real model first paragraph rendered before stream end in ${Date.now() - started}ms`);
  await page.evaluate(() => { window.progressFixture.setRunning(false); window.progressFixture.setText('Now check the operation catalog.\n\nAvailable definitions include drilling and turning.'); });
  await page.waitForFunction(() => {
    const text = document.querySelector('.studio-transcript-line[data-ready="true"]:not(.studio-transcript-previous) .studio-transcript-body>span')?.textContent || '';
    return text.includes('目录') && text.includes('钻') && text.includes('车');
  }, null, { timeout: 15000 });
  const artifact = new URL('../../../.tmp-progress-validation/chinese-progress.png', import.meta.url);
  await mkdir(new URL('.', artifact), { recursive: true });
  await page.screenshot({ path: decodeURIComponent(artifact.pathname).replace(/^\/(\w:)/, '$1'), clip: { x: 40, y: 80, width: 580, height: 150 } });
  console.log('PASS: real 3082 interface appends both translated paragraphs');
  await page.evaluate(() => window.progressFixture.setTranslateToChinese(false));
  assert.equal(await displayed.innerText(), 'Now check the operation catalog.\n\nAvailable definitions include drilling and turning.');
  const afterDisable = translationRequests;
  await page.evaluate(() => window.progressFixture.setText('Native streaming continues without translation'));
  assert.equal(await displayed.innerText(), 'Native streaming continues without translation');
  await page.waitForTimeout(1000);
  assert.equal(translationRequests, afterDisable);
  await page.reload();
  await page.waitForFunction(() => !!window.progressFixture);
  assert.equal(await page.getByRole('switch', { name: '中文翻译' }).count(), 0);
  assert.equal(await displayed.innerText(), 'N');
  await page.evaluate(() => window.progressFixture.setTranslateToChinese(true));
  await page.reload();
  await page.waitForFunction(() => !!window.progressFixture);
  assert.equal(await displayed.innerText(), 'N');
  assert.equal(await page.getByRole('switch', { name: '中文翻译' }).count(), 0);
  await page.evaluate(() => window.progressFixture.setText('Now: the operation catalog. Available definitions include drilling and turning.'));
  await page.screenshot({ path: decodeURIComponent(artifact.pathname).replace(/^\/(\w:)/, '$1'), clip: { x: 40, y: 80, width: 580, height: 150 } });
  console.log('PASS: reload restores the code-configured default, regardless of browser storage or test overrides');
} finally { await browser.close(); }

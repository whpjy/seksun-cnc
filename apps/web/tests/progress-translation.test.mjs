import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const source = await readFile(new URL('../src/process-studio/translation.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { cleanTranslation, needsTranslation, translationSource, translationSegments, streamingTranslationSegments, translatedSegments, TranslationQueue } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

test('wrappers are stripped, translator errors and English echoes are rejected', () => {
  for (const text of ['待翻译文本：正在检查工序目录。', '{"待翻译文本":"正在检查工序目录。"}', '```json\n{"translation":"正在检查工序目录。"}\n```']) assert.equal(cleanTranslation(text), '正在检查工序目录。');
  for (const text of ['翻译不可用', '（本段中文转换暂不可用）', '请提供待翻译文本', '{"待翻译文本":"Read the catalog."}']) assert.equal(cleanTranslation(text), null);
});

test('failed paragraphs are skipped silently without blocking subsequent genuine translations', async () => {
  const parts = ['Read the first catalog.', 'Read the missing catalog.', 'Read the final catalog.'];
  const queue = new TranslationQueue(async texts => texts.map(text => text === parts[1] ? '翻译不可用' : text === parts[0] ? '{"待翻译文本":"第一段真实译文"}' : '最后一段真实译文'), 0, 3, 5);
  const cleanup = parts.map((part, index) => queue.subscribe(`row:segment:${index}`, part, () => {}, 100 - index));
  await wait(30);
  assert.equal(translatedSegments(queue, 'row', parts), '第一段真实译文\n最后一段真实译文');
  cleanup.forEach(unsubscribe => unsubscribe());
});

test('paragraph boundaries are stable; stream end flushes the open tail', () => {
  const first = 'Read the catalog and inspect definitions.';
  const second = 'Generate toolpaths for the operation.';
  const text = `${first}\n\n${second}\n\nStill generating`;
  assert.deepEqual(translationSegments(text), [first, second]);
  assert.deepEqual(translationSegments(text + ' further analysis'), [first, second]);
  assert.deepEqual(translationSegments(text, true), [first, second, 'Still generating']);
});

test('long paragraphs translate before completion without losing their beginning or tail', () => {
  const text = ('Inspect the machining operation parameters and candidate tool geometry ').repeat(100);
  const parts = translationSegments(text, true);
  assert.ok(parts.length > 10);
  assert.ok(parts.every(part => part.length <= 480));
  assert.equal(parts.join('').replace(/\s/g, ''), text.replace(/\s/g, ''));
  assert.ok(translationSegments(text).length > 0);
});

test('an idle-flushed tail never disappears when token generation resumes', () => {
  const settled = 'Read the catalog.\n\nGenerate toolpaths';
  const resumed = `${settled} for the current operation`;
  assert.deepEqual(streamingTranslationSegments(resumed, true, settled), ['Read the catalog.', 'Generate toolpaths']);
  assert.deepEqual(streamingTranslationSegments(`${resumed}\n\n`, true, settled), ['Read the catalog.', 'Generate toolpaths for the current operation']);
  assert.deepEqual(streamingTranslationSegments('New unrelated analysis', true, settled), []);
});

test('requests overlap and publish individually, preserving order when replies are reversed', async () => {
  const pending = [];
  let concurrent = 0, maximum = 0;
  const queue = new TranslationQueue(texts => new Promise(resolve => {
    concurrent++;
    maximum = Math.max(maximum, concurrent);
    pending.push({ texts, finish(value) { concurrent--; resolve([value]); } });
  }), 0, 1, 5, 2);
  const parts = ['Read the first catalog.', 'Read the second catalog.', 'Read the third catalog.'];
  const cleanup = parts.map((part, i) => queue.subscribe(`row:segment:${i}`, part, () => {}, 100 - i));
  await wait(40);
  assert.deepEqual(pending.map(item => item.texts), [[parts[0]], [parts[1]]]);
  pending[1].finish('第二段译文');
  await wait(20);
  assert.equal(translatedSegments(queue, 'row', parts), '');
  pending[0].finish('第一段译文');
  await wait(10);
  assert.equal(translatedSegments(queue, 'row', parts), '第一段译文\n第二段译文');
  pending[2].finish('第三段译文');
  await wait(10);
  assert.equal(translatedSegments(queue, 'row', parts), '第一段译文\n第二段译文\n第三段译文');
  assert.equal(translatedSegments(queue, 'committed', parts), '第一段译文\n第二段译文\n第三段译文');
  assert.equal(maximum, 2);
  cleanup.forEach(unsubscribe => unsubscribe());
});

test('detect prose and mixed language without translating CNC identifiers or code', () => {
  for (const text of ['Now', 'Thinking…', 'Now: the operation catalog.', '正在检查 available definitions']) assert.equal(needsTranslation(text), true);
  for (const text of ['STEP', '正在检查 STEP 模型和 G54', 'mcp__cnc_read_catalog OP10', '`read the file`', 'https://example.com/file']) assert.equal(needsTranslation(text), false);
});

test('streaming uses completed sentences; final output includes the unfinished tail', () => {
  assert.equal(translationSource('Read catalog. Now check', true), 'Read catalog.');
  assert.equal(translationSource('Read catalog. Now check'), 'Read catalog. Now check');
  assert.equal(translationSource('x'.repeat(3000)).length, 2400);
});

test('hundreds of stream deltas coalesce to one latest request and cache survives commit', async () => {
  const calls = [];
  const queue = new TranslationQueue(async texts => { calls.push(texts); return texts.map(() => '正在检查工序目录'); }, 0);
  let unsubscribe;
  for (let i = 0; i < 200; i++) {
    unsubscribe?.();
    unsubscribe = queue.subscribe('live', `Read catalog ${i}`, () => {});
  }
  await wait(360);
  assert.deepEqual(calls, [['Read catalog 199']]);
  assert.equal(queue.get('live', 'Read catalog 199'), '正在检查工序目录');
  unsubscribe();
  assert.equal(queue.get('committed', 'Read catalog 199'), '正在检查工序目录');
});

test('late result cannot overwrite a changed step, but same sentence can keep its translation', async () => {
  let finish;
  const queue = new TranslationQueue(() => new Promise(resolve => { finish = resolve; }), 0);
  let unsubscribe = queue.subscribe('row', 'Read catalog', () => {});
  await wait(330);
  unsubscribe();
  unsubscribe = queue.subscribe('row', 'Generate toolpaths', () => {});
  finish(['检查工序目录']);
  await wait(10);
  assert.equal(queue.get('row', 'Generate toolpaths'), undefined);
  unsubscribe();
  assert.equal(queue.get('row', 'Read catalog extended'), undefined);
});

test('unmount removes queued history; errors do not loop or leak English into translated output', async () => {
  let calls = 0;
  const queue = new TranslationQueue(async () => { calls++; throw new Error('offline'); }, 0);
  queue.subscribe('hidden', 'Read hidden history', () => {})();
  const unsubscribe = queue.subscribe('visible', 'Read visible history', () => {});
  await wait(700);
  assert.equal(calls, 1);
  assert.equal(queue.get('visible', 'Read visible history'), undefined);
  unsubscribe();
});

test('same sentence keeps its readable translation while additional tokens are pending', async () => {
  let calls = 0;
  const queue = new TranslationQueue(async () => { calls++; return calls === 1 ? ['正在检查工序目录'] : [null]; }, 0);
  let unsubscribe = queue.subscribe('row', 'Read catalog', () => {});
  await wait(350);
  unsubscribe();
  unsubscribe = queue.subscribe('row', 'Read catalog and definitions', () => {});
  assert.equal(queue.get('row', 'Read catalog and definitions'), '正在检查工序目录');
  await wait(350);
  assert.equal(queue.get('row', 'Read catalog and definitions'), '正在检查工序目录');
  unsubscribe();
});

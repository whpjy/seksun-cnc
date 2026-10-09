import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const source = await readFile(new URL('../src/process-studio/harness.ts', import.meta.url), 'utf8');
const progressSource = await readFile(new URL('../src/process-studio/progress.ts', import.meta.url), 'utf8');
const options = { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } };
const progressUrl = `data:text/javascript;base64,${Buffer.from(ts.transpileModule(progressSource, options).outputText).toString('base64')}`;
const transcriptSource = await readFile(new URL('../src/process-studio/transcript.ts', import.meta.url), 'utf8');
const transcriptUrl = `data:text/javascript;base64,${Buffer.from(ts.transpileModule(transcriptSource, options).outputText).toString('base64')}`;
const compiled = ts.transpileModule(source, options).outputText.replace('"./progress"', JSON.stringify(progressUrl)).replace('"./transcript"', JSON.stringify(transcriptUrl));
const { jobIdIn, activitiesFrom } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const { streamProgressFrom, elapsedLabel } = await import(progressUrl);
const { transcriptStreamFrom, transcriptEntries, blocksFromRecords } = await import(transcriptUrl);

test('deployed CNC operation names drive actual observation stages and targets', () => {
  const calls = [
    ['mcp__cnc_inspect_geometry', {}, 'features'],
    ['mcp__cnc__revise_process_operation', {operation_id:'OP40'}, 'process'],
    ['functions.mcp__cnc_observe_model', {mode:'toolpath',operation_id:'OP40',feature_ids:['MF-1']}, 'toolpath'],
    ['mcp__cnc_trial_l32_operation', {operation_id:'OP40'}, 'simulation'],
    ['mcp__cnc_remove_process_operation', {operation_id:'OP40'}, 'process'],
  ];
  calls.forEach(([name,args,stage],seq) => {
    const action=activitiesFrom([{seq,type:'tool/call',time:seq,data:{callId:String(seq),name,arguments:JSON.stringify(args)}}])[0];
    assert.equal(action.navigable,true);
    assert.equal(action.stage,stage);
    assert.equal(action.operationId,args.operation_id);
    if(args.feature_ids) assert.deepEqual(action.featureIds,args.feature_ids);
  });
});

test('execution results match their own call when parallel operations finish out of order', () => {
  const events = [
    {seq:1,type:'tool/call',data:{callId:'a',name:'mcp__cnc_add_process_operation'}},
    {seq:2,type:'tool/call',data:{callId:'b',name:'mcp__cnc_trial_l32_operation'}},
    {seq:3,type:'tool/result',data:{message:{source:{callId:'a'},content:[{type:'text',text:'{}'}]}}},
    {seq:4,type:'tool/result',data:{message:{source:{callId:'b'},isError:true,content:[{type:'text',text:'failed'}]}}},
    {seq:5,type:'tool/result',data:{message:{source:{toolName:'read_file'},content:[{type:'text',text:'content'}]}}},
  ];
  const entries = transcriptEntries(events,null);
  assert.equal(entries[2].blocks[0].name,'mcp__cnc_add_process_operation');
  assert.equal(entries[3].blocks[0].name,'mcp__cnc_trial_l32_operation');
  assert.equal(entries[3].label,'工具返回错误');
  assert.equal(entries[4].blocks[0].name,'read_file');
});
const labelSource = await readFile(new URL('../src/process-studio/tool-labels.ts', import.meta.url), 'utf8');
const { toolLabel } = await import(`data:text/javascript;base64,${Buffer.from(ts.transpileModule(labelSource, options).outputText).toString('base64')}`);
const questionSource = await readFile(new URL('../src/process-studio/questions.ts', import.meta.url), 'utf8');
const questionCompiled = ts.transpileModule(questionSource, options).outputText.replace('"./harness"', JSON.stringify(`data:text/javascript;base64,${Buffer.from('export async function rpc() {}').toString('base64')}`));
const { questionsIn, answersComplete } = await import(`data:text/javascript;base64,${Buffer.from(questionCompiled).toString('base64')}`);
const resultSource = await readFile(new URL('../src/process-studio/results.ts', import.meta.url), 'utf8');
const { finalText, deliveriesIn, outcome, operationState } = await import(`data:text/javascript;base64,${Buffer.from(ts.transpileModule(resultSource,options).outputText).toString('base64')}`);

test('results distinguish accepted trials from production release and recover deliverables', () => {
  const events=[{type:'assistant/message',seq:1,data:{message:{content:[{type:'reasoning',text:'hidden'},{type:'text',text:'Final [报告](/workspace/report.md)'}]}}},{type:'turn/end',seq:2,data:{}}];
  assert.equal(finalText(events),'Final [报告](/workspace/report.md)');
  assert.equal(deliveriesIn(events,finalText(events))[0].path,'/workspace/report.md');
  const trials={accepted_operation_ids:['OP10'],production_ready:false};
  const job={plan:{coverage:{status:'incomplete',production_ready:false,covered_count:0,target_count:3}}};
  assert.equal(outcome(job,null,trials).label,'部分验证通过');
  assert.equal(outcome(job,null,trials).ready,false);
  assert.equal(operationState('OP10',trials,null),'试算已接受');
  assert.equal(jobIdIn('**job_id：`1eb3e4e2996a4f6b852170da47bb7cc2`**'),'1eb3e4e2996a4f6b852170da47bb7cc2');
});

test('human questions are scoped to the current session and require explicit valid answers', () => {
  const questions = [{id:'a',question:'选择材料',options:[{label:'钢'},{label:'铝'}]},{id:'b',question:'补充要求'}];
  const frame = {type:'waterfall',event:'user-questions/request',agentId:'current',eventId:'e',request:{questions}};
  assert.equal(questionsIn(frame,'other','c'),null);
  assert.equal(questionsIn({...frame,event:'user-approval/request'},'current','c'),null);
  assert.equal(questionsIn(frame,'current','c').clientId,'c');
  assert.equal(answersComplete(questions,[]),false);
  assert.equal(answersComplete(questions,[{id:'a',selected:['钢']}]),false);
  assert.equal(answersComplete(questions,[{id:'a',selected:['钢','铝']},{id:'b',selected:[],custom:'继续'}]),false);
  assert.equal(answersComplete(questions,[{id:'a',selected:['不存在的选项']},{id:'b',selected:[],custom:'继续'}]),false);
  assert.equal(answersComplete(questions,[{id:'a',selected:['钢']},{id:'b',selected:[],custom:'继续'}]),true);
});

test('actual question tool and namespaced tools display specific Chinese labels', () => {
  for (const name of ['ask_user_question', 'functions.ask_user_question', 'tools:ask_user_question', 'mcp__harness__ask_user_question', 'mcp__harness_ask_user_question']) {
    assert.equal(toolLabel(name), '请求用户确认与补充信息');
  }
  assert.equal(toolLabel('mcp__cnc__trial_l32_operation'), '计算刀路并试算工序');
  assert.equal(toolLabel('mcp__cnc_trial_l32_operation'), '计算刀路并试算工序');
  assert.equal(toolLabel('job_output'), '读取后台任务输出');
  assert.equal(toolLabel(''), '准备下一步操作');
  assert.equal(toolLabel('unknown_tool'), '执行辅助操作');
});

test('original reasoning streams verbatim and final blocks do not duplicate deltas', () => {
  let live = transcriptStreamFrom(null, {type:'start',attemptId:'a',revision:1,step:7});
  live = transcriptStreamFrom(live, {type:'chunk',attemptId:'a',revision:2,chunk:{type:'reasoning-delta',index:0,text:'先检查'}});
  live = transcriptStreamFrom(live, {type:'chunk',attemptId:'a',revision:3,chunk:{type:'reasoning-delta',index:0,text:'孔的方向。'}});
  assert.equal(live.blocks[0].text,'先检查孔的方向。');
  assert.equal(live.blocks[0].kind,'reasoning');
  live = transcriptStreamFrom(live, {type:'chunk',attemptId:'a',revision:4,chunk:{type:'block-end',index:0,block:{type:'reasoning',text:'先检查孔的方向。'}}});
  assert.equal(live.blocks[0].text,'先检查孔的方向。');
  assert.equal(transcriptStreamFrom(live,{type:'chunk',attemptId:'a',revision:3,chunk:{type:'reasoning-delta',index:0,text:'重复'} }),live);
  live = transcriptStreamFrom(live,{type:'end',attemptId:'a',revision:5,outcome:{kind:'committed',seq:9}});
  assert.equal(transcriptEntries([],live)[0].blocks[0].text,'先检查孔的方向。');
  const events=[{type:'assistant/message',seq:9,time:1,data:{step:7,stream:[{type:'reasoning-chunks',index:0,texts:['先检查','孔的方向。']}]}}];
  assert.equal(transcriptEntries(events,live).length,1);
});

test('reconnect and completed history retain all original reasoning and response blocks', () => {
  const records=[{type:'reasoning-chunks',index:0,texts:['思考甲\n','思考乙']},{type:'text-chunks',index:1,texts:['回复正文']}];
  const live=transcriptStreamFrom(null,{revision:6,activeAttempt:{attemptId:'a',step:4,stream:records}});
  assert.deepEqual(live.blocks,blocksFromRecords(records));
  assert.equal(live.blocks[0].text,'思考甲\n思考乙');
  assert.equal(live.blocks[1].text,'回复正文');
  const entries=transcriptEntries([{type:'tool/result',seq:3,time:1,data:{message:{content:[{type:'text',text:'完整工具结果'}]}}}],live);
  assert.equal(entries[0].blocks[0].text,'完整工具结果');
  assert.equal(entries[1].blocks[0].text,'思考甲\n思考乙');
});

test('job discovery reads named job fields inside MCP text, ignoring attachment ids', () => {
  const jobId = '1234567890abcdef1234567890abcdef';
  assert.equal(jobIdIn({ attachmentId: jobId }), undefined);
  assert.equal(jobIdIn({ message: { content: [{ type: 'text', text: JSON.stringify({ job_id: jobId }) }] } }), jobId);
  assert.equal(jobIdIn('job_id: ' + jobId), jobId);
  assert.equal(jobIdIn({ job_id: 'invalid' }), undefined);
});

test('live model updates expose phase and counts without retaining model content', () => {
  let progress = streamProgressFrom(null, {type:'start', attemptId:'a1', revision:1}, 1000);
  progress = streamProgressFrom(progress, {type:'chunk', attemptId:'a1', revision:2, time:1200, chunk:{type:'reasoning-delta', text:'private model content'}}, 1200);
  assert.equal(progress.phase, 'analysis');
  assert.equal(progress.receivedChars, 21);
  assert.equal(JSON.stringify(progress).includes('private'), false);
  const stale = streamProgressFrom(progress, {type:'chunk', attemptId:'a1', revision:2, chunk:{type:'text-delta',text:'duplicate'}}, 1300);
  assert.equal(stale, progress);
  progress = streamProgressFrom(progress, {type:'chunk', attemptId:'a1', revision:3, time:1400, chunk:{type:'tool-call-delta',name:'mcp__cnc__add_process_operation',argumentsDelta:'{}'}}, 1400);
  assert.equal(progress.phase, 'tool');
  assert.equal(streamProgressFrom(progress, {type:'end',attemptId:'old',revision:4}, 1500), progress);
  assert.equal(streamProgressFrom(progress, {type:'end',attemptId:'a1',revision:4}, 1500), null);
});

test('reconnect restores the active phase, received characters and last actual update', () => {
  const progress = streamProgressFrom(null, {revision:8,activeAttempt:{attemptId:'a1',stream:[
    {type:'chunk',time:1000,chunk:{type:'block-start',blockType:'reasoning'}},
    {type:'reasoning-chunks',time0:1100,dt:[200,300],texts:['甲','乙','丙']},
  ]}}, 9000);
  assert.equal(progress.startedAt, 1000);
  assert.equal(progress.lastAt, 1600);
  assert.equal(progress.receivedChars, 3);
  assert.equal(progress.phase, 'analysis');
  assert.equal(streamProgressFrom(progress,{revision:9},9000),null);
  assert.equal(elapsedLabel(1000,66000),'1 分 5 秒');
});

test('repair can return to design and failed trials retain the addressed operation', () => {
  const events = [
    { type: 'tool/call', seq: 1, time: 1, data: { callId: 'trial', name: 'mcp__cnc__trial_l32_operation', arguments: '{"operation_id":"OP10"}' } },
    { type: 'tool/result', seq: 2, time: 2, data: { message: { source: { callId: 'trial' }, isError: true } } },
    { type: 'tool/call', seq: 3, time: 3, data: { callId: 'repair', name: 'mcp__cnc__revise_process_operation', arguments: '{"operation_id":"OP10","feature_ids":["HF1"]}' } },
  ];
  const activities = activitiesFrom(events);
  assert.equal(activities[0].status, 'error');
  assert.equal(activities[0].stage, 'simulation');
  assert.equal(activities[0].operationId, 'OP10');
  assert.equal(activities[1].stage, 'process');
  assert.deepEqual(activities[1].featureIds, ['HF1']);
  assert.equal(activities[1].status, 'running');
});

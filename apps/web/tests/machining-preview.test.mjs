import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
const source = await readFile(new URL('../src/process-studio/machining-preview.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { previewSegments, previewMaterial, cumulativeMaterial, machiningEventStage } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
const fixture = () => ({ operation_id:'OP10', revision:'1', channel_id:'main', source_cutoff_z:10,
  axis:{ origin:{x:0,y:0,z:0}, direction:{x:0,y:0,z:2} },
  toolpath:{ channels:[{id:'main', commands:[{type:'select_tool', axes:{}}, {type:'rapid_move', axes:{X:24,Z:5}}, {type:'feed_move', axes:{X:20}}, {type:'feed_move', axes:{Z:2}}]}] },
  before_samples:[{z:2,outer_radius:12,inner_radius:0}], after_samples:[{z:2,outer_radius:10,inner_radius:0}] });
test('actual IR converts diameter coordinates and preserves modal axes', () => {
  const segments=previewSegments(fixture());
  assert.equal(segments.length,2);
  assert.equal(Math.hypot(segments[0].x1,segments[0].y1),12);
  assert.equal(Math.hypot(segments[0].x2,segments[0].y2),10);
  assert.equal(segments[1].z2,2);
  assert.equal(segments[1].operation_id,'OP10');
});
test('sub spindle reverses the axial coordinate around the cutoff datum', () => {
  const preview={...fixture(),channel_id:'sub'};
  assert.equal(previewSegments(preview)[1].z2,8);
  assert.equal(previewMaterial(preview).after_samples[0].z,8);
});
test('independent sub trials preserve source coordinates, including absent cutoff', () => {
  for (const source_cutoff_z of [10, null]) {
    const preview = {...fixture(), channel_id:'sub', coordinate_frame:'source_model', source_cutoff_z};
    assert.equal(previewSegments(preview)[1].z2,2);
    assert.equal(previewMaterial(preview).after_samples[0].z,2);
  }
  assert.equal(previewSegments({...fixture(),channel_id:'sub',source_cutoff_z:null})[1].z2,2);
});
test('missing geometry and unsupported arc do not fabricate a cutting path', () => {
  assert.deepEqual(previewSegments({...fixture(),axis:null}),[]);
  const preview=fixture(); preview.toolpath.channels[0].commands[2].type='arc_move';
  assert.equal(previewSegments(preview).length,1);
});
test('backend trials and candidate checks focus simulation rather than default features', () => {
  assert.equal(machiningEventStage({stage:'harness_operation_trial'}),'simulation');
  assert.equal(machiningEventStage({stage:'harness_candidate_sandbox'}),'simulation');
  assert.equal(machiningEventStage({stage:'harness_auto_repair'}),'process');
  assert.equal(machiningEventStage({stage:'harness_plan',viewer:{mode:'toolpath'}}),'toolpath');
});
test('solver checkpoints preserve stock and exact intermediate radii without interpolation', () => {
  const preview={...fixture(),material_frames:[
    {command_index:2,fraction:.5,changes:[[0,11,0]]},
    {command_index:2,fraction:1,changes:[[0,10,0]]},
    {command_index:3,fraction:1,changes:[]},
  ]};
  const stage=previewMaterial(preview);
  assert.deepEqual(stage.material_frames.map(frame=>frame.samples[0].outer_radius),[12,11,10,10]);
  assert.equal(stage.material_frames[0].progress,0);
  assert.equal(stage.material_frames.at(-1).progress,1);
  assert.deepEqual(stage.material_frames.at(-1).samples,preview.after_samples);
  const reversed=previewMaterial({...preview,channel_id:'sub'});
  assert.equal(reversed.material_frames[1].samples[0].z,8);
});
test('cumulative playback starts at first stock, preserves operation boundaries, and requires recordings', () => {
  const first={...fixture(),material_frames:[{command_index:3,fraction:1,changes:[[0,10,0]]}]};
  const second={...first,operation_id:'OP20',before_samples:first.after_samples,
    after_samples:[{z:2,outer_radius:8,inner_radius:0}],
    material_frames:[{command_index:3,fraction:1,changes:[[0,8,0]]}]};
  const stage=cumulativeMaterial([first,second]);
  assert.deepEqual(stage.before_samples,first.before_samples);
  assert.deepEqual(stage.after_samples,second.after_samples);
  assert.deepEqual(stage.material_frames.map(frame=>[frame.progress,frame.samples[0].outer_radius]),[[0,12],[.5,10],[.5,10],[1,8]]);
  assert.deepEqual(stage.material_frames.at(-1).samples,second.after_samples);
  assert.equal(cumulativeMaterial([first,fixture()]),null);
  assert.equal(cumulativeMaterial([]),null);
});

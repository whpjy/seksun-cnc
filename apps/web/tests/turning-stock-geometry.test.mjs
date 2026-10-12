import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
const source = await readFile(new URL('../src/turning-stock-geometry.ts', import.meta.url), 'utf8');
const three = new URL('../node_modules/three/build/three.module.js', import.meta.url).href;
const code = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText.replace('from "three"', `from "${three}"`);
const {createTurningStockGeometry} = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
function endArea(geometry, z) {
  const p = geometry.getAttribute('position'), indices = geometry.getIndex(); let area=0;
  for(let i=0;i<indices.count;i+=3) {
    const a=indices.getX(i),b=indices.getX(i+1),c=indices.getX(i+2);
    if ([a,b,c].every(index => Math.abs(p.getY(index)-z)<1e-5))
      area += Math.abs((p.getX(b)-p.getX(a))*(p.getZ(c)-p.getZ(a))-(p.getZ(b)-p.getZ(a))*(p.getX(c)-p.getX(a)))/2;
  }
  return area;
}
test('initial solid cylinder closes both ends', () => {
  const geometry=createTurningStockGeometry([-2,2].map(z=>({z,outer_radius:12.1,inner_radius:0})));
  for(const z of [-2,2]) assert.ok(Math.abs(endArea(geometry,z)-Math.PI*12.1**2)<1);
});
test('recorded solid and bored remainders close both faces without filling actual bore', () => {
  for(const inner_radius of [0,3]) {
    const geometry=createTurningStockGeometry([-2,0,2].map(z=>({z,outer_radius:6,inner_radius})));
    for(const z of [-2,2]) assert.ok(Math.abs(endArea(geometry,z)-Math.PI*(36-inner_radius**2))<.3);
  }
});

import {SideHoleMaterialPreview} from './SideHoleMaterialPreview';
import {useState} from 'react';
export type HoleStrategies={strategies:Array<{id:string;name:string;target_contact_mm3:number|null;remaining_hole_stock_mm3:number|null;geometry_status:string;stock_mesh_url?:string;detail?:string}>;backoffs:Array<{feature_id:string;backoff_mm:number}>;limitations:string[];cutoff_continuation?:{status:string;detail?:string;removed_volume_mm3?:number;stock_mesh_url?:string}};
export function SideHoleStrategies({comparison}:{comparison:HoleStrategies}) {
  const [preview,setPreview]=useState<string|null>(null);
  return <div className="drawing-hole-draft" aria-label="孔底加工方案比较"><strong>孔底加工方案比较 · 尚未接受工序</strong>
    <p className="stock-note">横向滚动查看完整对比；点击候选步骤预览余料。</p>
    <div style={{overflowX:'auto'}}><table style={{minWidth:440,tableLayout:'fixed'}}><colgroup><col style={{width:145}}/><col style={{width:95}}/><col style={{width:95}}/><col style={{width:105}}/></colgroup><thead><tr><th>候选步骤</th><th>成品侵入 mm³</th><th>孔段残料 mm³</th><th>名义几何检查</th></tr></thead><tbody>{comparison.strategies.map(row=><tr key={row.id}><td><button type="button" className="stock-action" disabled={!row.stock_mesh_url} onClick={()=>setPreview(row.stock_mesh_url || null)}>{row.name}</button></td><td>{row.target_contact_mm3?.toFixed(6) ?? '无法计算'}</td><td>{row.remaining_hole_stock_mm3?.toFixed(6) ?? '无法计算'}</td><td>{row.geometry_status==='passed'?'该项通过，待验证':row.geometry_status==='unavailable'?'计算受阻':'未通过'}</td></tr>)}</tbody></table></div>
    {comparison.backoffs.map(h=><p className="stock-note" key={h.feature_id}>{h.feature_id} · 回退钻尖长度 {h.backoff_mm.toFixed(4)} mm</p>)}
    {comparison.limitations.map((text,i)=><p className="studio-warning" key={i}>{text}</p>)}
    {comparison.cutoff_continuation && <p className="stock-note">候选切断继承三维余料：{comparison.cutoff_continuation.status==='passed'?'实体计算通过，尚未接受工序':comparison.cutoff_continuation.detail || '未通过'} {comparison.cutoff_continuation.stock_mesh_url && <button type="button" className="stock-action" onClick={()=>setPreview(comparison.cutoff_continuation!.stock_mesh_url!)}>查看切断后候选材料</button>}</p>}
    {preview && <SideHoleMaterialPreview url={preview} onClose={()=>setPreview(null)}/>}
  </div>;
}

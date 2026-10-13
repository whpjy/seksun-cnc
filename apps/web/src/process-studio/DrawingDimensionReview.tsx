import { useRef, useState } from "react";
import type { Job } from "../types";
import type { DrawingAudit } from "./DrawingEvidence";
import { SideHoleToolInputs, emptySideHoleTool, drillGeometryRequest } from "./SideHoleToolInputs";
import { SideHoleCheckResult, type ExtendedSideHoleEvidence } from "./SideHoleCheckResult";
import {SideHoleStrategies,type HoleStrategies} from './SideHoleStrategies';

type Dimension = NonNullable<DrawingAudit["dimensions"]>[number];
const roles: Record<string,string> = {outer_diameter:"外圆直径",inner_diameter:"同轴内径",radial_hole:"侧向孔直径",axial_hole:"同轴孔直径",length:"长度",angle:"角度",groove:"槽尺寸",unknown:"待明确"};
type HoleDraft = { holes: Array<{feature_id:string;axis_offset_mm:number;axial_position_mm:number;centerline_feed_length_mm:number;requires_transverse_positioning:boolean}>; limitations:string[] };
type HoleCheck = ExtendedSideHoleEvidence;

function DimensionEditor({job,id,dimension,running,onChanged}:{job:Job;id:string;dimension:Dimension;running:boolean;onChanged:(job:Job)=>void}) {
  const audit=job.drawing_analysis!;
  const review=audit.dimension_reviews?.[id];
  const d={...dimension,...review};
  const [role,setRole]=useState(d.role),[nominal,setNominal]=useState(String(d.nominal)),[quantity,setQuantity]=useState(String(d.quantity));
  const [upper,setUpper]=useState(d.tolerance_upper == null ? "" : String(d.tolerance_upper)),[lower,setLower]=useState(d.tolerance_lower == null ? "" : String(d.tolerance_lower));
  const [selected,setSelected]=useState<string[]>(review?.cad_feature_ids || []),[reviewer,setReviewer]=useState(review?.reviewer || ""),[basis,setBasis]=useState(review?.evidence || "");
  const [busy,setBusy]=useState(false),[error,setError]=useState(""),[draft,setDraft]=useState<HoleDraft|null>(null);
  const [check,setCheck]=useState<HoleCheck|null>(null);
  const [toolSettings,setToolSettings]=useState(emptySideHoleTool);
  const [comparison,setComparison]=useState<HoleStrategies|null>(null);
  const comparisonRevision=useRef(0);
  function invalidateComparison() {comparisonRevision.current++;setComparison(null);}
  const candidates=audit.geometry_candidates?.[id] || [];
  const features=job.analysis?.cylindrical_features || [];
  const diameterRole=["outer_diameter","inner_diameter","radial_hole","axial_hole"].includes(role);
  async function save(event:React.FormEvent) {
    event.preventDefault();setBusy(true);setError("");
    try {
      const response=await fetch(`/api/v1/jobs/${job.id}/drawing-dimensions/${id}/review`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({revision_counter:audit.revision_counter,role,nominal:Number(nominal),quantity:Number(quantity),tolerance_upper:upper===""?null:Number(upper),tolerance_lower:lower===""?null:Number(lower),cad_feature_ids:diameterRole?selected:[],reviewer,evidence:basis})});
      const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:"请检查尺寸和审核依据");onChanged(data);
    } catch(reason) {setError((reason as Error).message);} finally {setBusy(false);}
  }
  async function inspectDraft() {
    setBusy(true);setError("");setDraft(null);setCheck(null);
    const ids=selected.length?selected:candidates;
    try {
      const response=await fetch(`/api/v1/jobs/${job.id}/side-hole-draft?feature_ids=${encodeURIComponent(ids.join(","))}`);
      const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:"孔位草案生成失败");setDraft(data);
    } catch(reason) {setError((reason as Error).message);} finally {setBusy(false);}
  }
  async function inspectSolid() {
    setBusy(true);setError("");setCheck(null);
    const ids=selected.length?selected:candidates;
    try {
      const geometry=drillGeometryRequest(toolSettings);
      const response=await fetch(`/api/v1/jobs/${job.id}/side-hole-check?feature_ids=${encodeURIComponent(ids.join(","))}`,{method:"POST",...(geometry?{headers:{"Content-Type":"application/json"},body:JSON.stringify(geometry)}:{})});
      const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:"实体检查失败");setCheck(data);
    } catch(reason) {setError((reason as Error).message);} finally {setBusy(false);}
  }
  async function compareStrategies() {
    setBusy(true);setError('');setComparison(null);
    const revision=++comparisonRevision.current;
    try {
      const geometry=drillGeometryRequest(toolSettings);
      if(!geometry)throw new Error('请先填写钻尖角与刀具几何依据，再比较孔底加工方案');
      const ids=selected.length?selected:candidates;
      const response=await fetch(`/api/v1/jobs/${job.id}/side-hole-strategies?feature_ids=${encodeURIComponent(ids.join(','))}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(geometry)});
      const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'孔底方案比较失败');if(comparisonRevision.current===revision)setComparison(data);
    } catch(reason) {if(comparisonRevision.current===revision)setError((reason as Error).message);} finally {setBusy(false);}
  }
  return <form className="drawing-dimension-form" onSubmit={event=>void save(event)}>
    <p className="stock-note">第{dimension.page}页 · {dimension.raw_text || `${dimension.role} ${dimension.nominal}`} · {id}</p>
    <label>标注类型<select value={role} onChange={e=>{setRole(e.target.value);setSelected([]);setDraft(null);setCheck(null);invalidateComparison();}}>{Object.entries(roles).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
    <div className="drawing-number-fields"><label>名义值<input required type="number" min="0.001" step="any" value={nominal} onChange={e=>setNominal(e.target.value)}/></label><label>数量<input required type="number" min="1" step="1" value={quantity} onChange={e=>setQuantity(e.target.value)}/></label></div>
    <div className="drawing-number-fields"><label>上偏差<input type="number" step="any" value={upper} placeholder="未标注" onChange={e=>setUpper(e.target.value)}/></label><label>下偏差<input type="number" step="any" value={lower} placeholder="未标注" onChange={e=>setLower(e.target.value)}/></label></div>
    {diameterRole && <fieldset><legend>关联 CAD 特征</legend><p className="stock-note">候选：{candidates.join("、") || "暂无"}。需核对 PDF 引线后手动选择。</p><div className="drawing-feature-options">{features.map(f=><label key={f.id}><input type="checkbox" disabled={f.review_state==="excluded"} checked={selected.includes(f.id)} onChange={e=>{setSelected(e.target.checked?[...selected,f.id]:selected.filter(x=>x!==f.id));setDraft(null);setCheck(null);invalidateComparison();}}/>{f.id} · Ø{f.diameter.toFixed(3)} · {f.kind==="hole"?"孔":"外圆"}{f.review_state==="excluded"?" · 已排除":""}</label>)}</div></fieldset>}
    {role==="radial_hole" && <><button type="button" className="stock-action" disabled={busy || !job.plan || !(selected.length || candidates.length)} onClick={()=>void inspectDraft()}>查看侧向孔刀路草案</button>{draft && <div className="drawing-hole-draft"><strong>孔位中心线 · 尚未验证</strong>{draft.holes.map(h=><p key={h.feature_id} className="stock-note">{h.feature_id} · 偏置 {h.axis_offset_mm.toFixed(3)} mm · 轴向位置 {h.axial_position_mm.toFixed(3)} mm · 中心线进给 {h.centerline_feed_length_mm.toFixed(3)} mm{h.requires_transverse_positioning?" · 需横向定位":""}</p>)}{draft.limitations.map((text,i)=><p key={i} className="studio-warning">{text}</p>)}</div>}</>}
    {role==="radial_hole" && <><SideHoleToolInputs value={toolSettings} disabled={busy || running} onChange={value=>{setToolSettings(value);setCheck(null);invalidateComparison();}}/><button type="button" className="stock-action" disabled={busy || running || !job.plan || !(selected.length || candidates.length)} onClick={()=>void inspectSolid()}>{busy?"计算中…":"检查侧孔实体材料去除"}</button><button type="button" className="stock-action" disabled={busy || running || !job.plan || !(selected.length || candidates.length)} onClick={()=>void compareStrategies()}>比较孔底加工方案</button>{check && <SideHoleCheckResult check={check}/>} {comparison && <SideHoleStrategies comparison={comparison}/>}</>}
    <label>审核人<input required maxLength={100} value={reviewer} onChange={e=>setReviewer(e.target.value)}/></label><label>图纸页码、引线与核对依据<textarea required maxLength={1000} value={basis} onChange={e=>setBasis(e.target.value)}/></label>
    <p className="stock-note">保存确认图纸读数与特征关联，仍需加工验证和尺寸检测。修改后需重算已有工序。</p>
    <button className="stock-action primary" disabled={busy || running || !job.plan || role==="unknown"}>{busy?"处理中…":"保存读数与关联"}</button>{!job.plan && <p className="stock-note">请先建立工艺草案再保存审核。</p>}{error && <p role="alert" className="studio-warning">{error}</p>}
  </form>;
}

export function DrawingDimensionReviews({job,running,onChanged}:{job:Job;running:boolean;onChanged:(job:Job)=>void}) {
  const [active,setActive]=useState<string|null>(null);
  const audit=job.drawing_analysis;
  if (!audit?.dimensions?.length) return null;
  return <details className="drawing-dimension-reviews"><summary>尺寸与 CAD 关联审核 · {Object.keys(audit.dimension_reviews || {}).length}/{audit.dimensions.length}</summary><p className="stock-note">逐项核对标注类型、孔数量及上下偏差。原始识别结果保留，确认读数不会自动通过质量验收。</p>{audit.dimensions.map((dimension,index)=>{
    const id=`PDF-DIM-${index+1}`,review=audit.dimension_reviews?.[id],d={...dimension,...review};
    return <div className="drawing-dimension-row" key={id}><button type="button" className="stock-action" aria-expanded={active===id} onClick={()=>setActive(active===id?null:id)}>{id} · {roles[d.role] || d.role} {d.nominal}{d.quantity>1?` ×${d.quantity}`:""} · {review?"读数已核对 · 待检测":"待核对"}</button>{active===id && <DimensionEditor key={`${id}-${audit.revision_counter}`} job={job} id={id} dimension={dimension} running={running} onChanged={onChanged}/>}</div>;
  })}</details>;
}

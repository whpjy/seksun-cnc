import {useEffect,useRef,useState} from 'react';
import type {Job} from '../types';
import './cutoff-setup.css';

type Configuration={inventory_id:string;station:string;retained_side:string;clamp_confirmed:boolean;verified_by:string;evidence:string;
  end_radius_mm:number;grip_z_min_mm:number;grip_z_max_mm:number;grip_diameter_mm:number;minimum_grip_length_mm:number;
  synchronization_rpm:number;clamp_force_n:number;required_clamp_force_n:number;sub_spindle_motion?:Motion|null};
type Motion={open_bore_diameter_mm:number;envelope_outer_diameter_mm:number;body_length_mm:number;approach_distance_mm:number;withdrawal_distance_mm:number;z2_origin_mm:number;z2_sign:number;z2_limits:number[];x2_position_mm:number;x2_limits:number[];envelope_confirmed:boolean;evidence:string};
type Setup={revision?:string;configuration?:Configuration};
type Check={code:string;status:string;detail:string};
const numeric=[['end_radius_mm','切断终点半径 mm'],['grip_z_min_mm','夹持起点（源轴向Z）mm'],['grip_z_max_mm','夹持终点（源轴向Z）mm'],
  ['grip_diameter_mm','夹持直径 mm'],['minimum_grip_length_mm','要求最小夹持长度 mm'],['synchronization_rpm','主背轴同步转速 rpm'],
  ['clamp_force_n','确认夹紧力 N'],['required_clamp_force_n','要求最小夹紧力 N']] as const;
const statusLabel:Record<string,string>={passed:'该项通过',failed:'未通过',review:'待确认',not_run:'尚未验证'};

export function CutoffSetupPanel({job,running,onChanged}:{job:Job;running:boolean;onChanged:(job:Job)=>void}) {
  const operations=job.plan?.setups.flatMap(s=>s.operations).filter(o=>o.type==='turn_cutoff') || [];
  const [id,setId]=useState(operations[0]?.id || ''),[saved,setSaved]=useState<Setup>({}),[values,setValues]=useState<Record<string,string>>({});
  const [inventory,setInventory]=useState(''),[station,setStation]=useState(''),[side,setSide]=useState(''),[confirmed,setConfirmed]=useState(false),[by,setBy]=useState(''),[basis,setBasis]=useState('');
  const [busy,setBusy]=useState(false),[loading,setLoading]=useState(true),[error,setError]=useState(''),[checks,setChecks]=useState<Check[]>([]);
  const [dirty,setDirty]=useState(false);
  const [motionEnabled,setMotionEnabled]=useState(false),[motion,setMotion]=useState<Record<string,string>>({}),[motionConfirmed,setMotionConfirmed]=useState(false),[motionBasis,setMotionBasis]=useState('');
  const motionFields=[['open_bore_diameter_mm','背轴张开内径 mm'],['envelope_outer_diameter_mm','背轴外包络直径 mm'],['body_length_mm','背轴包络长度 mm'],['approach_distance_mm','背轴接近距离 mm'],['withdrawal_distance_mm','背轴取件距离 mm'],['z2_origin_mm','背轴Z2坐标原点 mm'],['z2_sign','背轴Z2方向（1或-1）'],['z2_low','背轴Z2下限'],['z2_high','背轴Z2上限'],['x2_position_mm','同轴对齐时X2指令位置'],['x2_low','背轴X2下限'],['x2_high','背轴X2上限']];
  const [tools,setTools]=useState<Array<{inventory_id:string;station:string;tool_kind:string;cutoff_geometry_verified:boolean}>>([]);
  const requestRevision=useRef(0);
  useEffect(()=>{const m=saved.configuration?.sub_spindle_motion;setMotionEnabled(Boolean(m));setMotion(m?{...Object.fromEntries(Object.entries(m).filter(([,v])=>typeof v==='number').map(([k,v])=>[k,String(v)])),z2_low:String(m.z2_limits[0]),z2_high:String(m.z2_limits[1]),x2_low:String(m.x2_limits[0]),x2_high:String(m.x2_limits[1])}:{});setMotionConfirmed(m?.envelope_confirmed || false);setMotionBasis(m?.evidence || '');},[saved.configuration]);
  const operationIds=operations.map(o=>o.id).join('|');
  useEffect(()=>{if(!operations.some(o=>o.id===id))setId(operations[0]?.id || '');},[operationIds,id]);
  function changed(){requestRevision.current++;setChecks([]);setDirty(true);}
  useEffect(()=>{let cancelled=false;setLoading(true);setError('');setSaved({});setValues({});setInventory('');setStation('');setSide('');setConfirmed(false);setBy('');setBasis('');changed();
    if(!id){setLoading(false);return;}
    fetch(`/api/v1/jobs/${job.id}/cutoff-setup/${id}`).then(async r=>{const data=await r.json();if(!r.ok)throw Error(data.detail);return data as Setup;}).then(data=>{if(cancelled)return;setSaved(data);setDirty(false);const c=data.configuration;if(c){setValues(Object.fromEntries(numeric.map(([key])=>[key,String(c[key])])));setInventory(c.inventory_id);setStation(c.station);setSide(c.retained_side);setConfirmed(c.clamp_confirmed);setBy(c.verified_by);setBasis(c.evidence);}}).catch(e=>{if(!cancelled)setError(e.message);}).finally(()=>{if(!cancelled)setLoading(false);});return()=>{cancelled=true;requestRevision.current++;};
  },[job.id,id]);
  async function refreshTools(){try{const r=await fetch(`/api/v1/machines/l32/instances/${job.machine_instance_id}/tools`);if(!r.ok)throw Error('现场刀具读取失败');setTools((await r.json()).tools || []);}catch(e){setError((e as Error).message);}}
  useEffect(()=>{void refreshTools();},[job.machine_instance_id]);
  async function save(e:React.FormEvent){e.preventDefault();setBusy(true);setError('');changed();try{
    const moving=motionEnabled?{...Object.fromEntries(motionFields.filter(([k])=>!['z2_low','z2_high','x2_low','x2_high'].includes(k)).map(([key])=>[key,Number(motion[key])])),z2_limits:[Number(motion.z2_low),Number(motion.z2_high)],x2_limits:[Number(motion.x2_low),Number(motion.x2_high)],envelope_confirmed:motionConfirmed,evidence:motionBasis}:null;
    const response=await fetch(`/api/v1/jobs/${job.id}/cutoff-setup/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({inventory_id:inventory,station,retained_side:side,clamp_confirmed:confirmed,verified_by:by,evidence:basis,sub_spindle_motion:moving,...Object.fromEntries(numeric.map(([key])=>[key,Number(values[key])])),expected_revision:saved.revision || null})});
    const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'请检查切断与夹持参数');setSaved(data);setDirty(false);
    const jobResponse=await fetch(`/api/v1/jobs/${job.id}`);if(!jobResponse.ok)throw Error('配置已保存，请刷新任务');onChanged(await jobResponse.json());
  }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function trial(){setBusy(true);setError('');setChecks([]);const revision=++requestRevision.current;try{
    const response=await fetch(`/api/v1/jobs/${job.id}/agent/l32/operations/${id}/trial`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'请先完成前序工序试算接受');
    if(revision===requestRevision.current){setChecks(data.evidence?.cutoff_checks || []);if(!data.evidence?.cutoff_checks?.length)setError(data.detail || '尚未形成继承三维材料的切断检查证据');}
  }catch(e){if(revision===requestRevision.current)setError((e as Error).message);}finally{setBusy(false);}}
  if(!operations.length)return null;
  return <details className="drawing-dimension-reviews cutoff-setup-panel"><summary>切断与接料验证 · {saved.revision?'已配置，待试算':'待确认'}</summary>
    <p className="stock-note">先保存上方机床坐标并上传夹具，在资源库登记实测切断刀。夹持范围沿识别主轴的轴向Z坐标填写，不能直接使用STEP全局Z。检查切断运动和分离后的静态夹持；背轴动态转接仍需后续验证。</p>
    <p className="stock-note">当前切断中心轴向Z：{operations.find(o=>o.id===id)?.parameters.z_mm ?? '待确认'} mm。夹紧力检查比对声明值与要求，不代替夹持力学计算。</p>
    <label className="stock-note">切断工序<select value={id} disabled={busy || running} onChange={e=>setId(e.target.value)}>{operations.map(o=><option key={o.id} value={o.id}>{o.id} · {o.name}</option>)}</select></label>
    <button className="stock-action" disabled={busy || running} onClick={()=>void refreshTools()}>刷新切断刀列表</button>
    <form className="drawing-dimension-form" onSubmit={e=>void save(e)}><fieldset disabled={loading || busy || running}>
      <label>已确认的实际切断刀<select required value={inventory} onChange={e=>{setInventory(e.target.value);changed();}}><option value="">请选择实刀</option>{tools.filter(t=>t.tool_kind==='cutoff' && t.cutoff_geometry_verified).map(t=><option key={t.inventory_id} value={t.inventory_id}>{t.inventory_id} · {t.station}</option>)}</select></label>
      <label>切断实际刀位<input required maxLength={64} value={station} onChange={e=>{setStation(e.target.value);changed();}}/></label>
      <div className="drawing-number-fields">{numeric.map(([key,label])=><label key={key}>{label}<input required type="number" step={key==='synchronization_rpm'?'1':'any'} value={values[key] || ''} onChange={e=>{setValues({...values,[key]:e.target.value});changed();}}/></label>)}</div>
      <label>切断后成品侧<select required value={side} onChange={e=>{setSide(e.target.value);changed();}}><option value="">请选择</option><option value="+Z">源轴向 +Z 侧</option><option value="-Z">源轴向 -Z 侧</option></select></label>
      <label>切断与夹持确认人<input required value={by} maxLength={120} onChange={e=>{setBy(e.target.value);changed();}}/></label>
      <label>刀位、夹持范围和夹紧力依据<textarea required maxLength={1000} value={basis} onChange={e=>{setBasis(e.target.value);changed();}}/></label>
      <label className="cutoff-confirmation"><input type="checkbox" checked={confirmed} onChange={e=>{setConfirmed(e.target.checked);changed();}}/>已核对夹紧力及其最小要求</label>
      <label className="cutoff-confirmation"><input type="checkbox" checked={motionEnabled} onChange={e=>{setMotionEnabled(e.target.checked);changed();}}/>配置背轴接近与取件运动</label>
      {motionEnabled && <><p className="stock-note">圆柱包络必须覆盖全部背轴移动部件。固定夹具STEP应排除这些移动部件。X2是确认同轴对齐后的控制器坐标；Z2原点和方向须按现场坐标确认。</p>{motionFields.map(([key,label])=><label key={key}>{label}<input required type="number" step="any" value={motion[key] || ''} onChange={e=>{setMotion({...motion,[key]:e.target.value});changed();}}/></label>)}<label>背轴包络与坐标依据<textarea required value={motionBasis} onChange={e=>{setMotionBasis(e.target.value);changed();}}/></label><label className="cutoff-confirmation"><input type="checkbox" checked={motionConfirmed} onChange={e=>{setMotionConfirmed(e.target.checked);changed();}}/>已确认此保守包络覆盖实际背轴移动部件</label></>}
      <button className="stock-action">保存切断与夹持配置</button>
    </fieldset></form>
    <button className="stock-action" disabled={busy || running || !saved.revision || dirty} onClick={()=>void trial()}>{busy?'处理中…':'试算切断与接料'}</button>
    {dirty && <p className="stock-note">参数已修改，请先保存，再重新试算。</p>}
    {checks.map(c=><p className={c.status==='passed'?'stock-note':'studio-warning'} key={c.code}>{statusLabel[c.status] || c.status} · {c.detail}</p>)}
    {error && <p role="alert" className="studio-warning">{error}</p>}
  </details>;
}

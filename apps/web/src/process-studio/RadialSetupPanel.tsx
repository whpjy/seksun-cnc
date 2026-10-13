import {useEffect,useState} from 'react';
import type {Job} from '../types';

type Configuration={source_zero_direction:string;machine_origin:number[];x_programming:string;axis_limits:Record<string,number[]>;retract_x_mm:number;module_id:string;station:string;operation_stations?:Record<string,string>;evidence:string;fixture_attachment:string};
type Setup={revision?:string;configuration?:Configuration;fixture?:{sha256:string}};
const axes=['X1','Y1','Z1'];
export function RadialSetupPanel({job,running,onChanged}:{job:Job;running:boolean;onChanged:(job:Job)=>void}) {
  const [saved,setSaved]=useState<Setup>({}),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [zero,setZero]=useState(''),[origin,setOrigin]=useState(['','','']),[limits,setLimits]=useState([['',''],['',''],['','']]);
  const [unit,setUnit]=useState(''),[retract,setRetract]=useState(''),[module,setModule]=useState(''),[station,setStation]=useState(''),[evidence,setEvidence]=useState(''),[attachment,setAttachment]=useState('stationary');
  const [stations,setStations]=useState<Record<string,string>>({});
  const [tools,setTools]=useState<Array<{inventory_id:string;tool_kind:string;station:string;physical_geometry_verified:boolean}>>([]),[bindings,setBindings]=useState<Record<string,string>>({});
  const [toolRevision,setToolRevision]=useState(0);
  useEffect(()=>{let cancelled=false;if(job.machine_instance_id)fetch(`/api/v1/machines/l32/instances/${job.machine_instance_id}/tools`).then(async r=>{if(!r.ok)throw Error('现场刀具读取失败');return r.json();}).then(data=>{if(!cancelled)setTools(data.tools || []);}).catch(e=>{if(!cancelled)setError(e.message);});return()=>{cancelled=true;};},[job.machine_instance_id,toolRevision]);
  useEffect(()=>{let cancelled=false;fetch(`/api/v1/jobs/${job.id}/radial-setup`).then(async r=>{const data=await r.json();if(!r.ok)throw new Error(data.detail);return data as Setup;}).then(data=>{if(cancelled)return;setSaved(data);const c=data.configuration;if(c){setZero(c.source_zero_direction);setOrigin(c.machine_origin.map(String));setLimits(axes.map(a=>c.axis_limits[a].map(String)));setUnit(c.x_programming);setRetract(String(c.retract_x_mm));setModule(c.module_id);setStation(c.station);setEvidence(c.evidence);setAttachment(c.fixture_attachment);setStations(c.operation_stations || {});}}).catch(e=>{if(!cancelled)setError(e.message);}).finally(()=>{if(!cancelled)setLoading(false);});return()=>{cancelled=true;};},[job.id]);
  async function refresh(data:Setup){setSaved(data);const r=await fetch(`/api/v1/jobs/${job.id}`);if(!r.ok)throw new Error('配置已保存，任务刷新失败，请刷新页面');onChanged(await r.json());}
  async function save(event:React.FormEvent){event.preventDefault();setBusy(true);setError('');try{
    const response=await fetch(`/api/v1/jobs/${job.id}/radial-setup`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_zero_direction:zero,machine_origin:origin.map(Number),x_programming:unit,axis_limits:Object.fromEntries(axes.map((a,i)=>[a,limits[i].map(Number)])),retract_x_mm:Number(retract),module_id:module,station,operation_stations:Object.fromEntries(Object.entries(stations).filter(([,v])=>v.trim())),evidence,fixture_attachment:attachment,expected_revision:saved.revision || null})});
    const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'请检查坐标、行程及依据');await refresh(data);
  }catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function upload(file:File){setBusy(true);setError('');try{const body=new FormData();body.append('file',file);const r=await fetch(`/api/v1/jobs/${job.id}/radial-setup/fixture`,{method:'POST',body});const data=await r.json();if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'夹具上传失败');await refresh(data);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  async function bind(id:string){setBusy(true);setError('');try{const r=await fetch(`/api/v1/jobs/${job.id}/radial-setup/operations/${id}/tool`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({inventory_id:bindings[id],expected_revision:saved.revision})});const data=await r.json();if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'实刀绑定失败');onChanged(data);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  const operations=job.plan?.setups.flatMap(s=>s.operations).filter(o=>['radial_drilling','radial_hole_finishing'].includes(o.type)) || [];
  return <details className="drawing-dimension-reviews"><summary>侧孔机床坐标与夹具 · {saved.revision?'已配置':'待确认'}</summary>
    <p className="stock-note">填写本次装夹的实际坐标和有效行程。主轴映射到机床 +Z，机床原点为物理 XYZ 毫米；X 行程与退刀值按所选编程方式填写。保存后侧孔及切断试算需重新计算。</p>
    <form className="drawing-dimension-form" onSubmit={e=>void save(e)}><fieldset disabled={loading || busy || running}>
      <label>源模型零度径向<select required value={zero} onChange={e=>setZero(e.target.value)}><option value="">请选择</option>{['+X','-X','+Y','-Y','+Z','-Z'].map(v=><option key={v}>{v}</option>)}</select></label>
      <div className="drawing-number-fields">{['X','Y','Z'].map((a,i)=><label key={a}>轴心机床{a} mm<input required type="number" step="any" value={origin[i]} onChange={e=>setOrigin(origin.map((v,j)=>j===i?e.target.value:v))}/></label>)}</div>
      <label>X 编程方式<select required value={unit} onChange={e=>setUnit(e.target.value)}><option value="">请选择</option><option value="radius">半径</option><option value="diameter">直径</option></select></label>
      {axes.map((a,i)=><div className="drawing-number-fields" key={a}>{['下限','上限'].map((label,j)=><label key={j}>{a}{label}<input required type="number" step="any" value={limits[i][j]} onChange={e=>setLimits(limits.map((v,k)=>k===i?v.map((n,l)=>l===j?e.target.value:n):v))}/></label>)}</div>)}
      <label>安全退刀X<input required type="number" min="0.001" step="any" value={retract} onChange={e=>setRetract(e.target.value)}/></label>
      <label>实际动力模块<input required value={module} maxLength={64} onChange={e=>setModule(e.target.value)} placeholder="现场已安装模块编号"/></label>
      <label>默认实际刀位<input required value={station} maxLength={64} onChange={e=>setStation(e.target.value)}/></label>
      {operations.map(o=><label key={o.id}>{o.id} · {o.name} 刀位<input value={stations[o.id] || ''} maxLength={64} placeholder="留空使用默认刀位" onChange={e=>setStations({...stations,[o.id]:e.target.value})}/></label>)}
      <label>夹具安装位置<select value={attachment} onChange={e=>setAttachment(e.target.value)}><option value="stationary">机床固定夹具</option><option value="main_spindle">随主轴旋转（保守全角包络检查）</option></select></label>
      <label>坐标、刀位与行程确认依据<textarea required maxLength={1000} value={evidence} onChange={e=>setEvidence(e.target.value)}/></label>
      <button className="stock-action" disabled={loading || busy || running}>{busy?'处理中…':'保存侧孔机床配置'}</button>
    </fieldset></form>
    {!!operations.length && <button className="stock-action" disabled={busy || running} onClick={()=>setToolRevision(v=>v+1)}>刷新现场刀具列表</button>}
    {operations.map(o=><div key={o.id}><label className="stock-note">{o.id} 绑定现场实刀{ o.tool.inventory_id?` · ${o.tool.inventory_id}`:''}<select value={bindings[o.id] || ''} disabled={!saved.revision || busy || running} onChange={e=>setBindings({...bindings,[o.id]:e.target.value})}><option value="">请选择已确认刀具</option>{tools.filter(t=>t.physical_geometry_verified && t.tool_kind===(o.type==='radial_drilling'?'drill':'end_mill')).map(t=><option key={t.inventory_id} value={t.inventory_id}>{t.inventory_id} · {t.station}</option>)}</select></label><button className="stock-action" disabled={!saved.revision || !bindings[o.id] || busy || running} onClick={()=>void bind(o.id)}>绑定 {o.id} 实刀并更新尺寸</button></div>)}
    <label className="stock-note">夹具 STEP（机床 XYZ 毫米，包含全部相关夹具及夹持部件）<input type="file" accept=".step,.stp" disabled={!saved.revision || busy || running} onChange={e=>{const f=e.target.files?.[0];if(f)void upload(f);e.target.value='';}}/></label>
    <p className="stock-note">{saved.fixture?`已绑定夹具 ${saved.fixture.sha256.slice(0,12)}，仍需试算碰撞校验`:'尚未绑定夹具实体，侧孔工序无法通过接受校验'}</p>
    {error && <p role="alert" className="studio-warning">{error}</p>}
  </details>;
}

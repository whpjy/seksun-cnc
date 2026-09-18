import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Eye, LoaderCircle, Plus, Save, Trash2, X } from "lucide-react";
import type { Catalogs, Job, Operation, OperationDefinition } from "./types";
import { ToolKindIcon } from "./ToolKindIcon";

type Value = string | number | boolean;

function defaults(definition: OperationDefinition): Record<string, Value> {
  return Object.fromEntries(definition.parameters.filter((item) => item.default != null).map((item) => [item.key,item.default as Value]));
}

function compatibleFeatureIds(definition: OperationDefinition, operation?: Operation): string[] {
  const ids = operation?.feature_ids ?? [];
  if (definition.id === "turn_grooving") return ids.filter((id) => id.startsWith("TPF-"));
  if (definition.engine.provider === "turning") return ids.filter((id) => id.startsWith("RP-"));
  return ids;
}

function compatibleSetupFeatureIds(definition: OperationDefinition, operations: Operation[]): string[] {
  for (const operation of operations) {
    const ids = compatibleFeatureIds(definition, operation);
    if (ids.length) return ids;
  }
  return [];
}

function ParameterFields({ definition, values, onChange }: { definition: OperationDefinition; values: Record<string,Value>; onChange: (values: Record<string,Value>) => void }) {
  return <div className="process-parameter-grid">{definition.parameters.map((parameter) => {
    const value = values[parameter.key] ?? parameter.default ?? "";
    return <label key={parameter.key}><span>{parameter.label}{parameter.unit ? ` / ${parameter.unit}` : ""}</span>{parameter.type === "boolean"
      ? <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange({ ...values,[parameter.key]:event.target.checked })} />
      : parameter.type === "enum"
        ? <select value={String(value)} onChange={(event) => onChange({ ...values,[parameter.key]:event.target.value })}>{parameter.choices.map((choice) => <option key={choice}>{choice}</option>)}</select>
        : <input type="number" value={Number(value)} min={parameter.minimum ?? undefined} max={parameter.maximum ?? undefined} step={parameter.type === "integer" ? 1 : "any"} onChange={(event) => onChange({ ...values,[parameter.key]:Number(event.target.value) })} />}</label>;
  })}</div>;
}

export function ProcessDesigner({ job, catalogs, apiUrl, onUpdated, onPreview, onEngineeringReview, onClose, readOnly }: {
  job: Job; catalogs: Catalogs; apiUrl: (path: string) => string;
  onUpdated: (job: Job, operationId?: string) => void; onPreview: (operation: Operation) => void | Promise<void>;
  onEngineeringReview?: (operation: Operation) => void;
  onClose: () => void; readOnly: boolean;
}) {
  const [setupId,setSetupId] = useState(job.plan?.setups[0]?.id ?? "");
  const setup = job.plan?.setups.find((item) => item.id === setupId) ?? job.plan?.setups[0];
  const [selectedId,setSelectedId] = useState(setup?.operations[0]?.id ?? "");
  const selected = setup?.operations.find((item) => item.id === selectedId) ?? setup?.operations[0];
  const definition = catalogs.operations.find((item) => item.id === (selected?.definition_id || selected?.type));
  const [name,setName] = useState(selected?.name ?? "");
  const [toolId,setToolId] = useState(selected?.tool.id ?? "");
  const [parameters,setParameters] = useState<Record<string,Value>>(selected?.parameters ?? {});
  const [adding,setAdding] = useState(false);
  const [newDefinitionId,setNewDefinitionId] = useState("turn_facing");
  const [insertAfterId,setInsertAfterId] = useState(selected?.id ?? "");
  const [newName,setNewName] = useState("");
  const newDefinition = catalogs.operations.find((item) => item.id === newDefinitionId);
  const [newToolId,setNewToolId] = useState("TURN-OD-R");
  const [newParameters,setNewParameters] = useState<Record<string,Value>>(() => newDefinition ? defaults(newDefinition) : {});
  const [busy,setBusy] = useState(false);
  const [message,setMessage] = useState("");
  const l32Definitions = useMemo(() => {
    const supported = new Set(["turn_facing","turn_od_roughing","turn_od_finishing","turn_grooving","turn_cutoff"]);
    return catalogs.operations.filter((item) => item.engine.provider === "turning" && supported.has(item.id));
  },[catalogs.operations]);

  function choose(operation: Operation) {
    setSelectedId(operation.id); setName(operation.name); setToolId(operation.tool.id);
    setParameters(operation.parameters); setInsertAfterId(operation.id); setAdding(false); setMessage("");
  }
  function chooseSetup(id: string) {
    const next = job.plan?.setups.find((item) => item.id === id);
    setSetupId(id); if (next?.operations[0]) choose(next.operations[0]);
  }
  function chooseNewDefinition(id: string) {
    const next = catalogs.operations.find((item) => item.id === id);
    if (!next) return;
    setNewDefinitionId(id); setNewToolId(next.tool.default_tool_id); setNewParameters(defaults(next)); setNewName(next.name);
  }
  async function request(path: string, init: RequestInit): Promise<Job> {
    const response = await fetch(apiUrl(path),init); const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "工序修改失败"); return payload as Job;
  }
  async function save() {
    if (!setup || !selected || !definition) return; setBusy(true); setMessage("");
    try {
      const updated = await request(`/api/v1/jobs/${job.id}/setups/${setup.id}/operations/${selected.id}`,{ method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({ name,tool_id:toolId,parameters }) });
      const updatedOperation = updated.plan?.setups.flatMap((item) => item.operations).find((item) => item.id === selected.id);
      onUpdated(updated,selected.id); setMessage("工序已保存，正在重新生成作用效果…");
      if (updatedOperation) await onPreview(updatedOperation);
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "工序保存失败"); } finally { setBusy(false); }
  }
  async function create() {
    if (!setup || !newDefinition) return; const neighbour = setup.operations.find((item) => item.id === insertAfterId) ?? setup.operations.at(-1);
    const featureIds = compatibleFeatureIds(newDefinition,neighbour).length
      ? compatibleFeatureIds(newDefinition,neighbour)
      : compatibleSetupFeatureIds(newDefinition,setup.operations);
    setBusy(true); setMessage("");
    try {
      const updated = await request(`/api/v1/jobs/${job.id}/setups/${setup.id}/operations`,{ method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ definition_id:newDefinition.id,feature_ids:featureIds,tool_id:newToolId,name:newName || newDefinition.name,parameters:newParameters,insert_after_operation_id:insertAfterId || null }) });
      const oldIds = new Set(setup.operations.map((item) => item.id)); const created = updated.plan?.setups.find((item) => item.id === setup.id)?.operations.find((item) => !oldIds.has(item.id));
      onUpdated(updated,created?.id); setAdding(false); setMessage("新工序已插入，正在生成作用效果…");
      if (created) await onPreview(created);
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "新增工序失败"); } finally { setBusy(false); }
  }
  async function remove() {
    if (!setup || !selected) return; setBusy(true); setMessage("");
    try { const updated = await request(`/api/v1/jobs/${job.id}/setups/${setup.id}/operations/${selected.id}`,{method:"DELETE"}); onUpdated(updated); setSelectedId(""); setMessage("工序已删除"); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "删除工序失败"); } finally { setBusy(false); }
  }
  async function move(direction: -1|1) {
    if (!setup || !selected) return; const ids=setup.operations.map((item)=>item.id), index=ids.indexOf(selected.id), target=index+direction;
    if (target<0 || target>=ids.length) return; [ids[index],ids[target]]=[ids[target],ids[index]]; setBusy(true);
    try { const updated=await request(`/api/v1/jobs/${job.id}/setups/${setup.id}/operations/reorder`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({operation_ids:ids})}); onUpdated(updated,selected.id); }
    catch(reason){setMessage(reason instanceof Error?reason.message:"排序失败");} finally{setBusy(false);}
  }
  async function toggleEnabled() {
    if (!setup || !selected) return; setBusy(true); setMessage("");
    try {
      const updated=await request(`/api/v1/jobs/${job.id}/setups/${setup.id}/operations/${selected.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:!selected.enabled})});
      const updatedOperation=updated.plan?.setups.flatMap((item)=>item.operations).find((item)=>item.id===selected.id);
      onUpdated(updated,selected.id); setMessage(selected.enabled?"工序已抑制":"工序已启用，正在生成作用效果…");
      if (!selected.enabled && updatedOperation) await onPreview(updatedOperation);
    } catch(reason){setMessage(reason instanceof Error?reason.message:"工序状态修改失败");} finally{setBusy(false);}
  }

  return <div className="process-designer-backdrop" onMouseDown={onClose}><section className="process-designer" onMouseDown={(event)=>event.stopPropagation()} aria-label="工序设计器">
    <header><div><small>MANUFACTURING PROCESS DESIGN</small><strong>工序设计器</strong></div><span>{job.plan?.setups.reduce((sum,item)=>sum+item.operations.length,0) ?? 0} 道工序</span><button aria-label="关闭工序设计器" onClick={onClose}><X size={17}/></button></header>
    <div className="process-designer-body"><aside>
      <label className="process-setup-select">装夹<select value={setup?.id} onChange={(event)=>chooseSetup(event.target.value)}>{job.plan?.setups.map((item)=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      {!readOnly && <button className="process-add" onClick={()=>{setAdding(true);chooseNewDefinition(l32Definitions[0]?.id ?? catalogs.operations[0]?.id ?? "");}}><Plus size={14}/>插入工序</button>}
      <div className="process-sequence">{setup?.operations.map((operation,index)=><button key={operation.id} className={`${operation.id===selected?.id&&!adding?"active":""} ${operation.enabled?"":"disabled"}`} onClick={()=>choose(operation)}><span>{String(index+1).padStart(2,"0")}</span><div><strong>{operation.name}</strong><small>{operation.id} · {operation.tool.name}</small></div><em>{operation.enabled?"参与":"抑制"}</em></button>)}</div>
    </aside><main>{adding && newDefinition ? <>
      <div className="process-editor-title"><div><small>NEW OPERATION</small><strong>插入新工序</strong></div></div>
      <div className="process-editor-scroll"><label>工序类型<select value={newDefinition.id} onChange={(event)=>chooseNewDefinition(event.target.value)}>{(job.device_id==="citizen-cincom-l32"?l32Definitions:catalogs.operations.filter((item)=>item.manual_enabled)).map((item)=><option key={item.id} value={item.id}>{item.category} · {item.name}</option>)}</select></label><label>插入到<select value={insertAfterId} onChange={(event)=>setInsertAfterId(event.target.value)}>{setup?.operations.map((item)=><option key={item.id} value={item.id}>{item.id} {item.name} 之后</option>)}</select></label><label>工序名称<input value={newName} onChange={(event)=>setNewName(event.target.value)}/></label><label>刀具<select value={newToolId} onChange={(event)=>setNewToolId(event.target.value)}>{catalogs.tools.filter((tool)=>newDefinition.tool.accepts.includes(tool.kind)).map((tool)=><option key={tool.id} value={tool.id}>{tool.name}</option>)}</select></label><ParameterFields definition={newDefinition} values={newParameters} onChange={setNewParameters}/><p className="process-context-note">几何引用、主轴、通道与加工侧从插入位置继承；新工序仅为草案，必须重新生成刀路和仿真。</p></div>
      <footer><button onClick={()=>setAdding(false)}>取消</button><button className="primary" disabled={busy} onClick={create}>{busy?<LoaderCircle className="spin" size={14}/>:<Plus size={14}/>}插入工序</button></footer>
    </> : selected && definition ? <>
      <div className="process-editor-title"><div><small>{selected.id} · {selected.type}</small><strong>{selected.name}</strong></div><button onClick={()=>onPreview(selected)}><Eye size={14}/>查看作用效果</button></div>
      <div className="process-editor-scroll"><div className="process-tool-row"><ToolKindIcon kind={selected.tool.kind}/><label>刀具<select disabled={readOnly} value={toolId} onChange={(event)=>setToolId(event.target.value)}>{catalogs.tools.filter((tool)=>definition.tool.accepts.includes(tool.kind)).map((tool)=><option key={tool.id} value={tool.id}>{tool.name}</option>)}</select></label></div><label>工序名称<input disabled={readOnly} value={name} onChange={(event)=>setName(event.target.value)}/></label><ParameterFields definition={definition} values={parameters} onChange={readOnly?()=>{}:setParameters}/><div className="process-trace"><span>几何引用</span><strong>{selected.feature_ids.join("、")||"无"}</strong><span>通道 / 主轴 / 加工侧</span><strong>{selected.channel_id??"—"} / {selected.spindle_id??"—"} / {selected.workpiece_side??"—"}</strong></div></div>
      <footer>{!readOnly&&<><button onClick={()=>move(-1)} disabled={busy}><ArrowUp size={14}/>上移</button><button onClick={()=>move(1)} disabled={busy}><ArrowDown size={14}/>下移</button>{selected.source==="automatic"&&["turn_grooving","turn_threading","turn_id_roughing","turn_id_finishing","axial_drilling"].includes(selected.type)&&<button onClick={()=>onEngineeringReview?.(selected)} disabled={busy}>工程审核</button>}<button onClick={toggleEnabled} disabled={busy}>{selected.enabled?"抑制":"启用"}</button><button className="danger" onClick={remove} disabled={busy}><Trash2 size={14}/>删除</button><button className="primary" onClick={save} disabled={busy}>{busy?<LoaderCircle className="spin" size={14}/>:<Save size={14}/>}保存工序</button></>}</footer>
    </>:<p className="process-empty">选择一道工序开始设计。</p>}{message&&<div className="process-message">{message}</div>}</main></div>
  </section></div>;
}

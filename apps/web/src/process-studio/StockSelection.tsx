import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import type { Job, TurningStageView } from "../types";
import "./stock-selection.css";

export type StockRule = { diameter_increment_mm: number; front_allowance_mm: number; back_allowance_mm: number; diameter_round_up_mm: number; revision: number };
type Stock = { diameter_mm: number; length_mm: number; selection?: { rule: StockRule; part_max_outer_diameter_mm: number; diameter_source?: string; drawing_evidence?: string; stock_note?: string } };
export type StockSelectionData = { stock: Stock; proposed_stock: Stock; current_rule: StockRule; view: TurningStageView };

export function StockSelectionPanel({ data, jobId, running, onChanged, onRules }: {
  data: StockSelectionData | null; jobId: string; running: boolean; onChanged: (job: Job) => void; onRules: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function apply() {
    setBusy(true); setMessage("");
    try {
      const response = await fetch(`/api/v1/jobs/${jobId}/stock-selection/apply-rule`, { method: "POST" });
      if (!response.ok) throw new Error((await response.json()).detail || "毛坯更新失败");
      onChanged(await response.json());
      setMessage("已按当前规则更新毛坯，原有刀路与仿真需重新计算。");
    } catch (reason) { setMessage((reason as Error).message); }
    finally { setBusy(false); }
  }
  if (!data) return <p className="stock-note">正在读取毛坯尺寸与选择依据…</p>;
  const selection = data.stock.selection;
  return <section className="stock-selection-panel" aria-label="毛坯选择详情">
    <h4>第一步 · 毛坯选择</h4><p className="stock-note">实心圆棒 · 初始内径 0 mm</p>
    <div className="studio-kv"><span>毛坯直径</span><b>Ø{data.stock.diameter_mm.toFixed(3)} mm</b></div>
    <div className="studio-kv"><span>毛坯长度</span><b>{data.stock.length_mm.toFixed(3)} mm</b></div>
    {selection ? <>
      <div className="studio-kv"><span>零件最大外径</span><b>Ø{selection.part_max_outer_diameter_mm.toFixed(3)} mm</b></div>
      <p className="stock-note">尺寸来源：{selection.diameter_source === "drawing" ? "二维图纸" : "STEP 几何"}。{selection.drawing_evidence}</p>
      <p className="stock-formula">毛坯直径 = 最大外径 + {selection.rule.diameter_increment_mm} mm{selection.rule.diameter_round_up_mm > 0 ? `，按 ${selection.rule.diameter_round_up_mm} mm 向上取整` : ""}</p>
      <p className="stock-note">规则版本 {selection.rule.revision} · 正面余量 {selection.rule.front_allowance_mm} mm · 背面余量 {selection.rule.back_allowance_mm} mm</p>
    </> : <p className="stock-note">此任务采用历史毛坯尺寸，尚未保存新规则快照。</p>}
    <button className="stock-action" onClick={onRules}>配置毛坯选择规则</button>
    <p className="stock-note">当前规则建议：Ø{data.proposed_stock.diameter_mm.toFixed(3)} × {data.proposed_stock.length_mm.toFixed(3)} mm</p>
    <button className="stock-action primary" disabled={busy || running} onClick={() => void apply()}>{busy ? "正在应用…" : "将当前规则应用到此任务"}</button>
    <p className="stock-note">更新毛坯后，已有工序需要重新试算。{running ? "请先等待或停止当前规划。" : ""}</p>
    {message && <p className="stock-note" role="status">{message}</p>}
  </section>;
}

export function StockRuleDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [rule, setRule] = useState<StockRule | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [policy, setPolicy] = useState<{finishing_allowance_mm:number|null;customer_confirmed:boolean;evidence:string;revision:number}|null>(null);
  useEffect(() => { const controller = new AbortController(); fetch("/api/v1/process-rules/machining", {signal:controller.signal}).then(async r => {if(!r.ok)throw new Error("工序规则读取失败");setPolicy(await r.json());}).catch(e => {if(!controller.signal.aborted)setMessage(e.message);});return()=>controller.abort();}, []);
  useEffect(() => { dialog.current?.showModal(); const controller = new AbortController();
    fetch("/api/v1/process-rules/stock-selection", { signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error("规则读取失败"); setRule(await response.json()); })
      .catch(reason => { if (!controller.signal.aborted) setMessage(reason.message); });
    return () => { controller.abort(); };
  }, []);
  async function save(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setMessage("");
    try {
      const response = await fetch("/api/v1/process-rules/stock-selection", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rule) });
      if (!response.ok) throw new Error("规则保存失败，请检查参数范围。");
      setRule(await response.json()); setMessage("规则已保存，新任务自动采用；已有任务可在毛坯选择中单独应用。"); onSaved();
    } catch (reason) { setMessage((reason as Error).message); } finally { setBusy(false); }
  }
  const fields: { key: Exclude<keyof StockRule, "revision">; label: string; max: number }[] = [
    { key: "diameter_increment_mm", label: "直径增量（mm）", max: 20 },
    { key: "front_allowance_mm", label: "正面长度余量（mm）", max: 100 },
    { key: "back_allowance_mm", label: "背面长度余量（mm）", max: 100 },
    { key: "diameter_round_up_mm", label: "直径向上取整步长（mm，0 表示不取整）", max: 20 },
  ];
  return <dialog ref={dialog} className="stock-rule-dialog" onCancel={onClose} aria-labelledby="stock-rule-title">
    <header><div><small>工艺规则</small><h2 id="stock-rule-title">毛坯选择规则</h2></div><button aria-label="关闭毛坯规则" onClick={onClose}><X size={20} /></button></header>
    <p>按零件几何计算实心毛坯尺寸，无需毛坯库。</p>
    <form onSubmit={event => void save(event)}>{rule && <>
      {fields.map(field => <label key={field.key}>{field.label}<input type="number" required min="0" max={field.max} step="0.001" value={rule[field.key]} onChange={event => setRule({ ...rule, [field.key]: Number(event.target.value) })} /></label>)}
      <div className="stock-rule-example">例如最大外径 Ø24 mm，直径增量 {rule.diameter_increment_mm} mm：毛坯直径 Ø{(rule.diameter_round_up_mm ? Math.ceil((24 + rule.diameter_increment_mm) / rule.diameter_round_up_mm - 1e-9) * rule.diameter_round_up_mm : 24 + rule.diameter_increment_mm).toFixed(3)} mm。</div>
      <p>直径增量是总直径差，0.2 mm 对应单边余量 0.1 mm。长度按零件轴向长度加两端余量计算。</p>
      <button type="submit" className="stock-action primary" disabled={busy}>{busy ? "正在保存…" : "保存规则"}</button>
    </>}{message && <p role="status">{message}</p>}</form>
    {policy && <form onSubmit={event => {event.preventDefault();setBusy(true);setMessage("");void fetch("/api/v1/process-rules/machining",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(policy)}).then(async r=>{const data=await r.json();if(!r.ok)throw new Error(data.detail || "工序规则保存失败");setPolicy(data);setMessage("工序规则已保存，新任务采用此版本。已有任务继续使用其规则快照。");}).catch(e=>setMessage(e.message)).finally(()=>setBusy(false));}}>
      <h3>粗加工后保留的精加工余量</h3><label>单边余量（mm，未确认可留空）<input type="number" min="0" max="5" step="0.001" value={policy.finishing_allowance_mm ?? ""} onChange={e=>setPolicy({...policy,finishing_allowance_mm:e.target.value===""?null:Number(e.target.value)})}/></label>
      <label>客户依据<input value={policy.evidence} placeholder="例如客户工艺卡或本次确认记录" onChange={e=>setPolicy({...policy,evidence:e.target.value})}/></label>
      <label><input type="checkbox" checked={policy.customer_confirmed} onChange={e=>setPolicy({...policy,customer_confirmed:e.target.checked})}/>已与客户确认此规则</label>
      <p>确认后，新任务的外圆与内孔粗加工默认保留该单边余量；是否需要粗加工仍取决于残料与可达性。刀具和切削参数需另外审核。</p><button className="stock-action primary" disabled={busy}>保存工序规则</button>
    </form>}
  </dialog>;
}

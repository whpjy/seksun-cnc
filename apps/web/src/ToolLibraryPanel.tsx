import { useEffect, useMemo, useState } from "react";
import { Check, LoaderCircle, Pencil, Search, X } from "lucide-react";
import type { Tool } from "./types";
import { ToolKindIcon } from "./ToolKindIcon";
import { toolKindLabel } from "./toolKindLabels";

type PhysicalTool = {
  inventory_id: string;
  machine_instance_id: string;
  catalog_tool_id: string;
  custom_name: string;
  custom_kind: string;
  catalog_tool_name: string;
  tool_kind: string;
  station: string;
  measured_diameter_mm: number | null;
  measured_cutting_width_mm: number | null;
  measured_stickout_mm: number | null;
  measured_holder_diameter_mm: number | null;
  notes: string;
  active: boolean;
  verification_state: "recorded";
};

type ToolForm = {
  inventory_id: string;
  catalog_tool_id: string;
  custom_name: string;
  custom_kind: string;
  station: string;
  measured_diameter_mm: string;
  measured_cutting_width_mm: string;
  measured_stickout_mm: string;
  measured_holder_diameter_mm: string;
  notes: string;
  active: boolean;
};

const EMPTY_FORM: ToolForm = {
  inventory_id: "", catalog_tool_id: "", custom_name: "", custom_kind: "grooving", station: "", measured_diameter_mm: "",
  measured_cutting_width_mm: "", measured_stickout_mm: "",
  measured_holder_diameter_mm: "", notes: "", active: true,
};

function nullableNumber(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}

export function ToolLibraryPanel({
  machineInstanceId, catalogTools, apiUrl, onClose, readOnly,
}: {
  machineInstanceId: string | null | undefined;
  catalogTools: Tool[];
  apiUrl: (path: string) => string;
  onClose: () => void;
  readOnly: boolean;
}) {
  const [tools, setTools] = useState<PhysicalTool[]>([]);
  const [loading, setLoading] = useState(Boolean(machineInstanceId));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<ToolForm>(EMPTY_FORM);

  useEffect(() => {
    if (!machineInstanceId) return;
    let cancelled = false;
    fetch(apiUrl(`/api/v1/machines/l32/instances/${machineInstanceId}/tools`))
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "现场刀具读取失败");
        return payload as { tools: PhysicalTool[] };
      })
      .then((payload) => { if (!cancelled) setTools(payload.tools); })
      .catch((reason: Error) => { if (!cancelled) setError(reason.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [apiUrl, machineInstanceId]);

  const filteredPhysical = useMemo(() => tools.filter((tool) =>
    `${tool.inventory_id} ${tool.catalog_tool_name} ${tool.station}`.toLowerCase().includes(search.toLowerCase()),
  ), [tools, search]);
  const filteredCatalog = useMemo(() => catalogTools.filter((tool) =>
    `${tool.id} ${tool.name} ${tool.kind}`.toLowerCase().includes(search.toLowerCase()),
  ), [catalogTools, search]);
  const selectedTemplate = catalogTools.find((tool) => tool.id === form.catalog_tool_id);

  function beginEdit(tool?: PhysicalTool) {
    setError("");
    setEditingId(tool?.inventory_id ?? null);
    setForm(tool ? {
      inventory_id: tool.inventory_id, catalog_tool_id: tool.catalog_tool_id,
      custom_name: tool.custom_name, custom_kind: tool.custom_kind || "grooving",
      station: tool.station, measured_diameter_mm: String(tool.measured_diameter_mm ?? ""),
      measured_cutting_width_mm: String(tool.measured_cutting_width_mm ?? ""),
      measured_stickout_mm: String(tool.measured_stickout_mm ?? ""),
      measured_holder_diameter_mm: String(tool.measured_holder_diameter_mm ?? ""),
      notes: tool.notes, active: tool.active,
    } : { ...EMPTY_FORM });
    setShowForm(true);
  }

  async function saveTool() {
    if (!machineInstanceId || saving || !form.inventory_id.trim() || (!selectedTemplate && (!form.custom_name.trim() || !form.custom_kind))) return;
    setSaving(true);
    setError("");
    try {
      const path = `/api/v1/machines/l32/instances/${machineInstanceId}/tools`;
      const response = await fetch(apiUrl(editingId ? `${path}/${editingId}` : path), {
        method: editingId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          inventory_id: form.inventory_id.trim(), catalog_tool_id: form.catalog_tool_id,
          custom_name: form.catalog_tool_id ? "" : form.custom_name.trim(),
          custom_kind: form.catalog_tool_id ? "" : form.custom_kind,
          station: form.station.trim(), measured_diameter_mm: nullableNumber(form.measured_diameter_mm),
          measured_cutting_width_mm: nullableNumber(form.measured_cutting_width_mm),
          measured_stickout_mm: nullableNumber(form.measured_stickout_mm),
          measured_holder_diameter_mm: nullableNumber(form.measured_holder_diameter_mm),
          notes: form.notes.trim(), active: form.active,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "保存现场刀具失败");
      const saved = payload as PhysicalTool;
      setTools((current) => editingId
        ? current.map((item) => item.inventory_id === editingId ? saved : item)
        : [...current, saved]);
      setShowForm(false);
      setEditingId(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存现场刀具失败");
    } finally {
      setSaving(false);
    }
  }

  return <section className="inspection-popover tool-library-popover" aria-label="刀具库">
    <header>
      <div><small>TOOL LIBRARY</small><strong>刀具库</strong></div>
      <span>{catalogTools.length} 款刀具</span>
      <button aria-label="关闭刀具库" onClick={onClose}><X size={15} /></button>
    </header>
    <div className="tool-library-controls">
      <label className="tool-library-search"><Search size={13} /><input aria-label="搜索刀具" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="编号、名称或刀位" /></label>
    </div>
    <div className="tool-library-content">
      <div className="tool-library-list">{filteredCatalog.map((tool) => <article key={tool.id}>
        <div className="tool-library-item-layout"><ToolKindIcon kind={tool.kind} /><div className="tool-library-item-detail">
          <div className="tool-library-item-head"><strong>{tool.id}</strong></div>
          <p>{tool.name}</p><small>{toolKindLabel(tool.kind)} · {tool.cutting_width_mm != null ? `刃宽 ${tool.cutting_width_mm} mm` : `直径 Ø${tool.diameter_mm} mm`} · 伸出参考 {tool.stickout_mm} mm</small>
        </div></div>
      </article>)}</div>
      {machineInstanceId && <>
          {showForm && <div className="tool-library-form">
            <strong>{editingId ? `编辑 ${editingId}` : "登记现场刀具"}</strong>
            <div className="tool-library-form-preview"><ToolKindIcon kind={selectedTemplate?.kind ?? form.custom_kind} /><span>{toolKindLabel(selectedTemplate?.kind ?? form.custom_kind)}<small>类型示意图</small></span></div>
            <label>实物编号<input value={form.inventory_id} disabled={Boolean(editingId)} maxLength={64} onChange={(event) => setForm({ ...form, inventory_id: event.target.value })} placeholder="例如 L32-GROOVE-01" /></label>
            <label>刀具来源<select value={form.catalog_tool_id} onChange={(event) => setForm({ ...form, catalog_tool_id: event.target.value })}><option value="">现场自定义刀具</option>{catalogTools.map((tool) => <option key={tool.id} value={tool.id}>{tool.id} · {tool.name}</option>)}</select></label>
            {!form.catalog_tool_id && <><label>现场刀具名称<input value={form.custom_name} onChange={(event) => setForm({ ...form, custom_name: event.target.value })} placeholder="例如 0.8 mm 外切槽刀" /></label><label>刀具类型<select value={form.custom_kind} onChange={(event) => setForm({ ...form, custom_kind: event.target.value })}><option value="grooving">外切槽刀</option><option value="turning_od">外圆车刀</option><option value="turning_id">内孔车刀</option><option value="end_mill">立铣刀</option><option value="drill">钻头</option><option value="cutoff">切断刀</option></select></label></>}
            <label>设备刀位<input value={form.station} onChange={(event) => setForm({ ...form, station: event.target.value })} placeholder="例如主轴刀位 T05" /></label>
            <div className="tool-library-form-grid">
              <label>实测刃宽 mm<input type="number" min="0.001" step="0.001" value={form.measured_cutting_width_mm} onChange={(event) => setForm({ ...form, measured_cutting_width_mm: event.target.value })} /></label>
              <label>实测直径 mm<input type="number" min="0.001" step="0.001" value={form.measured_diameter_mm} onChange={(event) => setForm({ ...form, measured_diameter_mm: event.target.value })} /></label>
              <label>实测伸出 mm<input type="number" min="0.001" step="0.001" value={form.measured_stickout_mm} onChange={(event) => setForm({ ...form, measured_stickout_mm: event.target.value })} /></label>
              <label>夹持直径 mm<input type="number" min="0.001" step="0.001" value={form.measured_holder_diameter_mm} onChange={(event) => setForm({ ...form, measured_holder_diameter_mm: event.target.value })} /></label>
            </div>
            <label>备注<textarea rows={2} value={form.notes} onChange={(event) => setForm({ ...form, notes: event.target.value })} /></label>
            {editingId && <label className="tool-library-active"><input type="checkbox" checked={form.active} onChange={(event) => setForm({ ...form, active: event.target.checked })} />在用</label>}
            <div className="tool-library-form-actions"><button onClick={() => setShowForm(false)}>取消</button><button className="primary" disabled={saving || !form.inventory_id.trim() || (!selectedTemplate && (!form.custom_name.trim() || !form.custom_kind))} onClick={saveTool}>{saving ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}保存记录</button></div>
          </div>}
          {!loading && filteredPhysical.length > 0 && <div className="tool-library-list">{filteredPhysical.map((tool) => <article key={tool.inventory_id} className={!tool.active ? "inactive" : ""}>
            <div className="tool-library-item-layout"><ToolKindIcon kind={tool.tool_kind} /><div className="tool-library-item-detail">
              <div className="tool-library-item-head"><strong>{tool.inventory_id}</strong><span>{tool.active ? "已登记" : "停用"}</span></div>
              <p>{tool.catalog_tool_name}</p>
              <small>{toolKindLabel(tool.tool_kind)} · {tool.station || "刀位未填写"} · {tool.measured_cutting_width_mm == null ? "刃宽未实测" : `刃宽 ${tool.measured_cutting_width_mm} mm`} · {tool.measured_stickout_mm == null ? "伸出未实测" : `伸出 ${tool.measured_stickout_mm} mm`}</small>
              {tool.notes && <small>{tool.notes}</small>}
            </div></div>
            {!readOnly && <button aria-label={`编辑 ${tool.inventory_id}`} onClick={() => beginEdit(tool)}><Pencil size={12} />编辑</button>}
          </article>)}</div>}
      </>}
      {filteredCatalog.length === 0 && filteredPhysical.length === 0 && <p className="tool-library-empty">没有匹配的刀具</p>}
      {error && <p className="tool-library-error" role="alert">{error}</p>}
    </div>
  </section>;
}

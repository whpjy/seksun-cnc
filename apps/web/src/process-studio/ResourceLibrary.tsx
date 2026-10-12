import { useEffect, useMemo, useRef, useState } from "react";
import { Library, LoaderCircle, Search, X } from "lucide-react";
import type { Catalogs, DeviceLibrary, OperationDefinition } from "../types";
import { ToolLibraryPanel } from "../ToolLibraryPanel";
import "./resource-library.css";

const sections = [{ id: "operations", name: "工序库" }, { id: "tools", name: "刀具库" }, { id: "devices", name: "设备库" }] as const;
const apiUrl = (path: string) => path;
const maturityLabels = { planned: "规划中", experimental: "试验阶段", generated: "已生成", validated: "已验证", production: "生产级" };

/** Browse shared resources without a job; physical tools use the current job's machine. */
export default function ResourceLibrary({ machineInstanceId, onClose }: { machineInstanceId?: string | null; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [section, setSection] = useState<(typeof sections)[number]["id"]>("operations");
  const [catalogs, setCatalogs] = useState<Catalogs | null>(null);
  const [operations, setOperations] = useState<OperationDefinition[]>([]);
  const [devices, setDevices] = useState<DeviceLibrary["devices"]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const element = dialog.current!;
    const previousFocus = document.activeElement;
    element.showModal();
    return () => {
      element.close();
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    async function load<T>(path: string): Promise<T> {
      const response = await fetch(path, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) });
      if (!response.ok) throw new Error(`资源加载失败（${response.status}）`);
      return response.json();
    }
    Promise.all([
      load<Catalogs>("/api/v1/catalogs"),
      load<{ definitions: OperationDefinition[] }>("/api/v1/operation-library"),
      load<DeviceLibrary>("/api/v1/device-library"),
    ]).then(([catalog, library, deviceLibrary]) => {
      if (!active) return;
      setCatalogs(catalog); setOperations(library.definitions); setDevices(deviceLibrary.devices);
    }).catch(() => { if (active) setError("资源库暂时无法加载，请重试。"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [retry]);
  const device = devices.find(item => item.id === deviceId);
  const visibleOperations = useMemo(() => operations.filter(item =>
    (!device || device.operation_bindings?.some(binding => binding.operation_id === item.id))
    && `${item.name} ${item.id} ${item.category} ${item.description}`.toLowerCase().includes(search.trim().toLowerCase()),
  ), [operations, device, search]);
  const visibleDevices = devices.filter(item => `${item.display_name} ${item.category_label} ${item.manufacturer} ${item.model}`.toLowerCase().includes(search.trim().toLowerCase()));
  const counts = { operations: operations.length, tools: catalogs?.tools.length || 0, devices: devices.length };
  return <dialog ref={dialog} className="studio-resource-dialog" aria-labelledby="resource-title" onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) { const box = event.currentTarget.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) onClose(); } }}>
    <header className="studio-resource-heading"><div><Library size={21} /><div><h2 id="resource-title">资源库</h2><p>浏览可用工序、刀具与设备</p></div></div><button aria-label="关闭资源库" onClick={onClose}><X size={20} /></button></header>
    <nav className="studio-resource-tabs" aria-label="资源分类">{sections.map(item => <button key={item.id} aria-pressed={section === item.id} onClick={() => { setSection(item.id); setSearch(""); }}>{item.name}<span>{loading ? "…" : counts[item.id]}</span></button>)}</nav>
    <div className="studio-resource-body">
      {loading ? <p className="studio-resource-state" role="status"><LoaderCircle className="spin" size={20} />正在加载资源…</p> : error ? <div className="studio-resource-state" role="alert"><p>{error}</p><button onClick={() => { setLoading(true); setError(""); setRetry(value => value + 1); }}>重新加载</button></div> : section === "tools" ? <>
        <p className="studio-resource-note">{machineInstanceId ? "当前任务的机床实物刀具与通用刀具目录" : "通用刀具目录；关联机床的任务可查看并登记实物刀具。"}</p>
        <ToolLibraryPanel machineInstanceId={machineInstanceId} catalogTools={catalogs?.tools || []} apiUrl={apiUrl} onClose={onClose} readOnly={!machineInstanceId} />
      </> : <>
        <div className="studio-resource-filters"><label><Search size={16} /><input aria-label="搜索资源" placeholder={section === "operations" ? "搜索工序名称、类别或编号" : "搜索设备名称、厂家或型号"} value={search} onChange={event => setSearch(event.target.value)} /></label>{section === "operations" && <select aria-label="筛选设备" value={deviceId} onChange={event => setDeviceId(event.target.value)}><option value="">全部设备</option>{devices.map(item => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select>}</div>
        <p className="studio-resource-note">{section === "operations" ? `共 ${visibleOperations.length} 项工序${device ? ` · ${device.display_name}` : ""}` : `共 ${visibleDevices.length} 台设备`}</p>
        <div className="studio-resource-grid">{section === "operations" ? visibleOperations.map(item => <article key={item.id}><div className="studio-resource-card-meta"><span>{item.category}</span><span>{maturityLabels[item.maturity] || item.maturity}</span></div><h3>{item.name}</h3><p>{item.description}</p><small>{item.id}</small><details><summary>参数与适用刀具</summary><p>默认刀具：{catalogs?.tools.find(tool => tool.id === item.tool.default_tool_id)?.name || item.tool.default_tool_id || "未指定"}</p>{item.parameters.map(parameter => <p key={parameter.key}>{parameter.label}{parameter.unit ? `（${parameter.unit}）` : ""}</p>)}</details></article>) : visibleDevices.map(item => <article key={item.id}><div className="studio-resource-card-meta"><span>{item.category_label}</span><span>{item.library_status === "supported" ? "已接入" : "适配中"}</span></div><h3>{item.display_name}</h3><p>厂家：{item.manufacturer || item.brand || "未记录"}<br />控制系统：{[item.controller.manufacturer, item.controller.model].filter(Boolean).join(" ") || "未记录"}</p><small>{item.record_kind === "physical" ? "实物设备" : "设备模板"} · {item.id}</small><details><summary>加工能力与待确认项</summary>{item.capabilities.map(capability => <p key={capability.code}>{capability.name} · {capability.status === "supported" ? "支持" : capability.status === "conditional" ? "有条件支持" : "未验证"}</p>)}{item.required_confirmation.map(text => <p key={text}>待确认：{text}</p>)}</details></article>)}</div>
        {(section === "operations" ? visibleOperations.length : visibleDevices.length) === 0 && <p className="studio-resource-state">没有符合条件的资源，请调整搜索或筛选条件。</p>}
      </>}
    </div>
  </dialog>;
}

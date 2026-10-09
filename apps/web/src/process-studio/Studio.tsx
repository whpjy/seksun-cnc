import { useEffect, useMemo, useRef, useState } from "react";
import { Box, ChevronDown, Code2, FileUp, Focus, History, Layers3, LoaderCircle, Maximize2, MousePointer2, Pause, ScanSearch, ShieldCheck, Sparkles, Wrench, X } from "lucide-react";
import { ModelViewer } from "../ModelViewer";
import { featureDisplayName } from "../featurePresentation";
import type { AgentWorkspace, CamResult, Job, ManufacturingFeature, RotationalFeatureAnalysis, RotationalManufacturingFeature, ToolpathSegment } from "../types";
import { activitiesFrom, follow, jobIdIn, PROCESS_PROMPT, rpc, upload } from "./harness";
import type { Activity, JournalEvent, Stage } from "./harness";
import HarnessTranscript from "./HarnessTranscript";
import Interaction from "./Interaction";
import ResultCard from "./ResultCard";
import AccountMenu from "./AccountMenu";
import FeaturePanel from "./FeaturePanel";
import GCodePanel from "./GCodePanel";
import HistoryDialog from "./HistoryDialog";
import useFloatingConsole from "./useFloatingConsole";
import { operationState, outcome } from "./results";
import type { TrialState } from "./results";
import type { LiveTranscript } from "./transcript";
import type { StreamProgress } from "./progress";

const tabs = [
  { id: "features", label: "特征识别", icon: ScanSearch }, { id: "process", label: "工序设计", icon: Layers3 },
  { id: "toolpath", label: "刀路计算", icon: Wrench }, { id: "simulation", label: "仿真验证", icon: ShieldCheck }, { id: "gcode", label: "G 代码", icon: Code2 },
] as const;
type Task = { sessionId?: string; jobId?: string; filename: string; view?: { stage: Stage; following: boolean } };
const storageKey = "nexus.process-studio.task.v1";
const emptyIds: string[] = [];
const emptySegments: ToolpathSegment[] = [];
function retain<T>(previous: T, next: T): T { return JSON.stringify(previous) === JSON.stringify(next) ? previous : next; }
function restore(): Task | null {
  try { const task = JSON.parse(localStorage.getItem(storageKey) || "null"); return task && typeof task.filename === "string" ? task : null; }
  catch { return null; }
}
function restoreView(task: Task | null): { stage: Stage; following: boolean } {
  const view = task?.view;
  return view && tabs.some(tab => tab.id === view.stage) && typeof view.following === "boolean"
    ? view : { stage: "features", following: true };
}
async function get<T>(path: string, optional = false): Promise<T | null> {
  const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(15000) });
  if (optional && response.status === 404) return null;
  if (!response.ok) throw new Error(`工程数据加载失败 (${response.status})`);
  return response.json() as Promise<T>;
}
function stageOf(event: AgentWorkspace["events"][number]): Stage {
  if (event.viewer?.mode === "toolpath" || event.viewer?.mode === "刀路") return "toolpath";
  if (event.stage.includes("repair")) return "process";
  if (event.stage.includes("cam") || event.stage.includes("validation") || event.stage.includes("trial") || event.stage.includes("execution")) return "simulation";
  if (event.stage.includes("toolpath")) return "toolpath";
  if (event.viewer?.kind === "operation" || event.stage.includes("process") || event.subgraph === "planning") return "process";
  return "features";
}
function featuresOf(job: Job | null, rotational: RotationalFeatureAnalysis | null): ManufacturingFeature[] {
  const analysis = job?.analysis;
  const features: ManufacturingFeature[] = analysis ? [...analysis.cylindrical_features.filter(feature => feature.kind === "hole"), ...analysis.prismatic_features, ...(analysis.planar_machining_features || []), ...analysis.internal_profile_features] : [];
  for (const feature of rotational?.features || []) {
    const profile = rotational?.profiles.find(item => item.id === feature.profile_id);
    const axis = rotational?.axes.find(item => item.id === profile?.axis_id);
    if (!axis || feature.review_state === "excluded") continue;
    const z = (feature.z_start + feature.z_end) / 2;
    const radius = Math.max(feature.radius_start, feature.radius_end);
    const transformed: RotationalManufacturingFeature = { id: feature.id, kind: feature.kind, source: "rotational", profile_id: feature.profile_id,
      center: { x: axis.origin.x + axis.direction.x * z, y: axis.origin.y + axis.direction.y * z, z: axis.origin.z + axis.direction.z * z },
      axis: axis.direction, radius, diameter: radius * 2, length: Math.max(feature.width_mm, 0.05), width_mm: feature.width_mm, depth_mm: feature.depth_mm,
      confidence: feature.confidence, review_state: feature.review_state, review_reasons: feature.review_reasons };
    features.push(transformed);
  }
  return features.filter(feature => feature.review_state !== "excluded");
}

export default function Studio({ onLogout }: { onLogout: () => void }) {
  const [task, setTask] = useState<Task | null>(restore);
  const [stage, setStage] = useState<Stage>(() => restoreView(task).stage);
  const [following, setFollowing] = useState(() => restoreView(task).following);
  const [job, setJob] = useState<Job | null>(null);
  const [workspace, setWorkspace] = useState<AgentWorkspace | null>(null);
  const [cam, setCam] = useState<CamResult | null>(null);
  const [trials, setTrials] = useState<TrialState | null>(null);
  const [segments, setSegments] = useState<ToolpathSegment[]>([]);
  const [rotational, setRotational] = useState<RotationalFeatureAnalysis | null>(null);
  const [journal, setJournal] = useState<JournalEvent[]>([]);
  const [streamProgress, setStreamProgress] = useState<StreamProgress | null>(null);
  const [liveTranscript, setLiveTranscript] = useState<LiveTranscript | null>(null);
  const [connection, setConnection] = useState(false);
  const [engineReady, setEngineReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dataError, setDataError] = useState("");
  const [uploadOpen, setUploadOpen] = useState(!task);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [history, setHistory] = useState<Array<{ id: string; filename: string; operation_count: number; created_at?: string; sessionId?: string }>>([]);
  const [expanded, setExpanded] = useState(false);
  const [consoleMinimized, setConsoleMinimized] = useState(true);
  const [questionWaiting, setQuestionWaiting] = useState(false);
  const [details, setDetails] = useState(true);
  const [selectedFeatures, setSelectedFeatures] = useState<string[]>([]);
  const [selectedOperation, setSelectedOperation] = useState<string>();
  const [genericProgramAvailable, setGenericProgramAvailable] = useState(false);
  const [programStates, setProgramStates] = useState<Record<string, string>>({});
  const floatingConsole = useFloatingConsole();
  const [snapshots, setSnapshots] = useState<{ urls: string[]; stages: { operationId: string; start: number; count: number }[] }>({ urls: [], stages: [] });
  const [snapshotError, setSnapshotError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const seqs = useRef(new Map<number, JournalEvent>());
  const submitting = useRef(false);
  const followingRef = useRef(following);
  const focusKey = useRef("");
  const activities = useMemo(() => activitiesFrom(journal), [journal]);
  const currentAction = activities.filter(activity => activity.navigable !== false).at(-1);
  const analysis = job?.analysis;
  const features = useMemo(() => featuresOf(job, rotational), [analysis, rotational]);
  const operations = job?.plan?.setups.flatMap(setup => setup.operations) || [];
  const operation = operations.find(item => item.id === selectedOperation);
  const latest = workspace?.events.at(-1);
  const activeEvent = workspace?.events.find(item => item.event_id === workspace.active_event_id) || latest;
  const executionEvent = journal.filter(item => item.type === "turn/start" || item.type === "step/start" || item.type === "turn/end").at(-1);
  const liveRunning = streamProgress !== null || executionEvent?.type === "turn/start" || executionEvent?.type === "step/start";
  const lifecycle = workspace?.orchestration?.lifecycle;
  const ended = Boolean(job && (executionEvent?.type === "turn/end" || !task?.sessionId));
  const result = outcome(job, workspace, trials);
  const status = busy ? "正在上传模型" : task?.sessionId && !connection ? "执行记录重连中" : questionWaiting ? "等待你的回答" : liveRunning ? "智能体正在设计" : ended ? result.label : lifecycle === "completed" ? "方案已归档" : lifecycle === "blocked" ? "设计受阻" : lifecycle === "waiting_human" ? "等待确认" : task ? "查看工程成果" : "等待上传模型";
  const filteredSegments = useMemo(() => selectedOperation ? segments.filter(segment => segment.operation_id === selectedOperation) : segments, [segments, selectedOperation]);
  const gcodeArtifact = workspace?.artifacts.find(item => /gcode|\.nc$|\.tap$|\.ngc$/i.test(item.filename || item.url));
  const gcodeUrl = cam?.files.gcode && task?.jobId ? `/api/v1/jobs/${task.jobId}/files/${encodeURIComponent(cam.files.gcode)}` : gcodeArtifact?.url;

  const focus = (action?: Activity, event?: AgentWorkspace["events"][number]) => {
    if (!followingRef.current) return;
    const key = action ? `${action.id}:${action.status}` : event?.event_id;
    if (!key || key === focusKey.current) return;
    focusKey.current = key;
    setStage(action?.stage || (event ? stageOf(event) : "features"));
    const operationId = action?.operationId || event?.viewer?.operation_id;
    setSelectedOperation(operationId);
    setSelectedFeatures(action?.featureIds?.length ? action.featureIds : event?.viewer?.feature_ids?.length ? event.viewer.feature_ids : operations.find(item => item.id === operationId)?.feature_ids || emptyIds);
  };
  const changeFollowing = (enabled: boolean) => {
    followingRef.current = enabled; setFollowing(enabled);
    if (enabled) { focusKey.current = ""; focus(currentAction, activeEvent); }
  };
  const selectTask = (next: Task | null) => {
    if (next && task?.jobId === next.jobId && task?.sessionId === next.sessionId) return;
    setJob(null); setWorkspace(null); setCam(null); setTrials(null); setSegments(emptySegments); setRotational(null);
    setSnapshots({ urls: [], stages: [] }); setSelectedFeatures(emptyIds); setSelectedOperation(undefined);
    setGenericProgramAvailable(false); setProgramStates({});
    setJournal([]); setStreamProgress(null); setLiveTranscript(null); setConnection(false); setDataError(""); setSnapshotError("");
    focusKey.current = ""; followingRef.current = true; setFollowing(true); setStage("features"); setTask(next);
  };

  useEffect(() => {
    if (task) {
      try { localStorage.setItem(storageKey, JSON.stringify({ ...task, view: { stage, following } })); }
      catch { /* Viewing a task must still work when browser storage is unavailable. */ }
    } else {
      try { localStorage.removeItem(storageKey); } catch { /* Optional browser cache. */ }
    }
    if (task?.jobId && task.sessionId) {
      try { const links = JSON.parse(localStorage.getItem("nexus.process-studio.sessions.v1") || "{}"); links[task.jobId] = task.sessionId; localStorage.setItem("nexus.process-studio.sessions.v1",JSON.stringify(links)); } catch { /* A corrupt optional cache must not interrupt a task. */ }
    }
  }, [task, stage, following]);
  useEffect(() => {
    let active = true;
    const check = async () => { try { await rpc("session/modelCatalog", {}); if (active) setEngineReady(true); } catch { if (active) setEngineReady(false); } };
    void check(); const timer = setInterval(check, 15000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  useEffect(() => {
    seqs.current.clear();
    if (!task?.sessionId) return;
    let publishTimer: ReturnType<typeof setTimeout> | undefined;
    let knownJobId = task.jobId;
    const publish = () => {
      publishTimer = undefined;
      const all = [...seqs.current.values()].sort((a, b) => a.seq - b.seq);
      setJournal(all);
      focus(activitiesFrom(all).filter(activity => activity.navigable !== false).at(-1));
    };
    const stop = follow(task.sessionId, events => {
      let changed = false;
      for (const event of events) {
        if (seqs.current.has(event.seq)) continue;
        seqs.current.set(event.seq, event); changed = true;
      }
      if (changed && !publishTimer) publishTimer = setTimeout(publish, 250);
      if (knownJobId) return;
      for (const event of [...events].reverse()) {
        if (event.type !== "tool/call" && event.type !== "tool/result") continue;
        const jobId = jobIdIn(event.data);
        if (jobId) { knownJobId = jobId; setTask(current => current && current.jobId !== jobId ? { ...current, jobId } : current); break; }
      }
    }, setConnection, setError, setStreamProgress, setLiveTranscript);
    return () => { clearTimeout(publishTimer); stop(); };
  }, [task?.sessionId]);
  useEffect(() => {
    if (!task?.jobId) return;
    let active = true;
    const jobId = task.jobId;
    const refresh = async () => {
      try {
        // Publish geometry as soon as it arrives; optional machining evidence cannot block it.
        const results = await Promise.allSettled([
          get<Job>(`/api/v1/jobs/${jobId}`).then(next => {
            if (!active || !next) return;
            setJob(previous => retain(previous, { ...next, analysis: retain(previous?.analysis ?? null, next.analysis), plan: retain(previous?.plan ?? null, next.plan) }));
            setDataError("");
          }).catch(reason => { if (active) setDataError(reason instanceof Error ? reason.message : "模型数据加载失败"); }),
          get<AgentWorkspace>(`/api/v1/jobs/${jobId}/agent/workspace`).then(next => {
            if (!active) return;
            setWorkspace(previous => retain(previous, next));
            if (!seqs.current.size && next) focus(undefined, next.events.find(event => event.event_id === next.active_event_id) || next.events.at(-1));
          }),
          Promise.allSettled([
            get<CamResult>(`/api/v1/jobs/${jobId}/cam`, true),
            get<{ segments?: ToolpathSegment[] } | ToolpathSegment[]>(`/api/v1/jobs/${jobId}/files/toolpath.json`, true),
          ]).then(([camResult, pathResult]) => {
            if (!active) return;
            const nextCam = camResult.status === "fulfilled" ? camResult.value : null;
            const path = pathResult.status === "fulfilled" ? pathResult.value : null;
            if (camResult.status === "fulfilled") setCam(previous => retain(previous, nextCam));
            const nextSegments = nextCam?.preview_segments || (Array.isArray(path) ? path : path?.segments);
            if (nextSegments) setSegments(previous => retain(previous, nextSegments));
            if (camResult.status === "rejected") throw camResult.reason;
            if (pathResult.status === "rejected") throw pathResult.reason;
          }),
          get<RotationalFeatureAnalysis>(`/api/v1/jobs/${jobId}/files/rotational-features.json`, true).then(next => { if (active) setRotational(previous => retain(previous, next)); }),
          get<TrialState>(`/api/v1/jobs/${jobId}/agent/l32/operation-trials/state`, true).then(next => { if (active) setTrials(previous => retain(previous, next)); }),
        ]);
        // Keep the last valid evidence when an optional endpoint is temporarily unavailable.
        const failure = results.find(result => result.status === "rejected");
        if (active && failure?.status === "rejected") setDataError(`部分工程数据暂不可用：${failure.reason instanceof Error ? failure.reason.message : "加载失败"}`);
      } catch (reason) { if (active) setDataError(reason instanceof Error ? reason.message : "工程数据暂不可用"); }
    };
    // Schedule after completion to avoid overlapping slow geometry requests.
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => { await refresh(); if (active) timer = setTimeout(tick, 2500); };
    void tick(); return () => { active = false; clearTimeout(timer); };
  }, [task?.jobId]);
  useEffect(() => {
    if (!task?.jobId || !operations.length) return;
    let active = true;
    void get<{ operations: Array<{ operation_id: string; files: string[] }> }>(`/api/v1/jobs/${task.jobId}/l32/material-snapshots`, true).then(manifest => {
      if (!active) return;
      const urls: string[] = []; const stages: { operationId: string; start: number; count: number }[] = [];
      for (const item of manifest?.operations || []) { stages.push({ operationId: item.operation_id, start: urls.length, count: item.files.length }); urls.push(...item.files.map(file => `/api/v1/jobs/${task.jobId}/files/${encodeURIComponent(file)}`)); }
      setSnapshots(previous => retain(previous, { urls, stages })); setSnapshotError("");
    }).catch(reason => { if (active) setSnapshotError(reason.message); });
    return () => { active = false; };
  }, [stage, task?.jobId, workspace?.active_event_id, operations.length]);

  const selectUploadFile = (files: File[]) => {
    if (busy || !files.length) return;
    setError("");
    if (files.length !== 1 || !/\.(stp|step)$/i.test(files[0].name)) {
      setUploadFile(null); setError("请选择一个 STEP 或 STP 三维模型文件。"); return;
    }
    setUploadFile(files[0]);
  };
  const openUpload = () => { setUploadFile(null); setError(""); setUploadOpen(true); if (input.current) input.current.value = ""; };
  const start = async (file?: File) => {
    if (!file || submitting.current) return;
    if (!/\.(stp|step)$/i.test(file.name)) { setError("请选择 STEP 或 STP 三维模型文件。"); return; }
    submitting.current = true; setBusy(true); setError("");
    try {
      const created = await rpc<{ sessionId: string }>("session/create", { request: {} });
      const receiptId = await upload(created.sessionId, file);
      await rpc("session/rename", { request: { sessionId: created.sessionId, title: `工艺设计 · ${file.name}` } });
      await rpc("session/prompt", { request: { sessionId: created.sessionId, requestId: crypto.randomUUID(), mode: "queue", clientTimeZone: "Asia/Shanghai", content: [{ type: "file", receiptId }, { type: "text", text: PROCESS_PROMPT }] } });
      selectTask({ sessionId: created.sessionId, filename: file.name }); setUploadOpen(false); setUploadFile(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "任务创建失败"); }
    finally { submitting.current = false; setBusy(false); if (input.current) input.current.value = ""; }
  };
  const openHistory = async () => {
    setHistoryOpen(true); setHistoryLoading(true); setHistoryError("");
    try {
      const [items, sessions] = await Promise.all([get<typeof history>("/api/v1/jobs"), rpc<{items: Array<{sessionId:string;projections?:{values?:{turnOutline?:Array<{response?:string}>}}}>}>("session/list",{_request:{}}).catch(()=>null)]);
      let links: Record<string,string> = {};
      try { links = JSON.parse(localStorage.getItem("nexus.process-studio.sessions.v1") || "{}"); } catch { /* Ignore corrupt cached links. */ }
      for (const session of [...(sessions?.items || [])].reverse()) for (const turn of session.projections?.values?.turnOutline || []) {
        const id = jobIdIn(turn.response); if (id) links[id] = session.sessionId;
      }
      setHistory((items || []).map(item=>({...item,sessionId:links[item.id]})));
    } catch (reason) { setHistoryError(String((reason as Error).message)); }
    finally { setHistoryLoading(false); }
  };
  const deleteHistoryTask = async (item: (typeof history)[number]) => {
    if (task?.jobId === item.id && liveRunning) throw new Error("任务仍在执行中，暂时不能删除");
    const response = await fetch(`/api/v1/jobs/${encodeURIComponent(item.id)}`, { method: "DELETE", signal: AbortSignal.timeout(15000) });
    if (!response.ok && response.status !== 404) {
      let message = `删除失败 (${response.status})`;
      try { const body = await response.json(); if (typeof body.detail === "string") message = body.detail; } catch { /* Non-JSON response. */ }
      throw new Error(message);
    }
    setHistory(previous => previous.filter(record => record.id !== item.id));
    try {
      const links = JSON.parse(localStorage.getItem("nexus.process-studio.sessions.v1") || "{}");
      delete links[item.id]; localStorage.setItem("nexus.process-studio.sessions.v1", JSON.stringify(links));
    } catch { /* Optional browser cache. */ }
    if (task?.jobId === item.id) selectTask(null);
  };
  const inspectFeature = (id: string) => { changeFollowing(false); setSelectedFeatures([id]); };
  const inspectOperation = (id: string) => { changeFollowing(false); setSelectedOperation(id); setSelectedFeatures(operations.find(item => item.id === id)?.feature_ids || emptyIds); };
  const cancel = async () => {
    try { await rpc("session/cancel", { request: { sessionId: task?.sessionId } }); } catch (reason) { setError((reason as Error).message); }
  };
  const viewedFeatures = selectedFeatures.length ? selectedFeatures : operation?.feature_ids || emptyIds;
  const focusedFeature = features.find(feature => feature.id === selectedFeatures[0]);
  const focusTitle = operation ? operation.name : selectedFeatures.length === 1 && focusedFeature ? featureDisplayName(focusedFeature) : following ? currentAction?.label || activeEvent?.title || "零件工程视图" : tabs.find(tab => tab.id === stage)?.label;
  const setup = job?.plan?.setups.find(item => item.operations.some(candidate => candidate.id === selectedOperation));
  const axis = setup?.work_axis;
  const stableWorkAxis = useMemo(() => axis, [axis?.x, axis?.y, axis?.z]);
  const consoleCompact = consoleMinimized && !questionWaiting;

  return <main className="studio">
    <input ref={input} hidden type="file" accept=".stp,.step" onChange={event => selectUploadFile(Array.from(event.target.files || []))} />
    <header className="studio-header">
      <div className="studio-brand"><span className="studio-brand-mark"><Box size={23} /></span><div><strong>NEXUS <span>智能工艺设计</span></strong><small>AI PROCESS DESIGN STUDIO</small></div></div>
      <div className="studio-document"><span className="studio-status"><i className={liveRunning || busy ? "pulse" : ""} />{status}</span><strong title={task?.filename}>{task?.filename || "新建零件工艺"}</strong></div>
      <div className="studio-header-actions"><button disabled={busy} onClick={() => void openHistory()}><History size={16} />历史任务</button><button onClick={openUpload} disabled={busy}><FileUp size={16} />新建任务</button><AccountMenu onLogout={onLogout} /></div>
    </header>
    <section className={`studio-canvas${stage === "gcode" ? " studio-code-canvas" : ""}`} onPointerDown={event => { if ((event.target as HTMLElement).tagName === "CANVAS") changeFollowing(false); }}>
      {job?.model_url && stage !== "gcode" && <ModelViewer modelUrl={job.model_url} features={features} selectedFeatureIds={viewedFeatures} onSelectFeature={inspectFeature}
        viewMode={stage === "features" ? "特征" : stage === "process" ? "工艺" : stage === "toolpath" ? "刀路" : "仿真"}
        toolpathSegments={stage === "toolpath" || stage === "simulation" ? filteredSegments : emptySegments} topologyEdges={job.analysis?.visual_edges}
        activeOperationId={selectedOperation} activeOperationLabel={operation?.name} workAxis={stableWorkAxis}
        showFeatureSummary={false} showFeatureAnnotations={stage === "features"} operationContextVisible
        releaseContextOnCleanup backgroundColor={0xdce7f1}
        isolatedFeatureId={stage === "features" && selectedFeatures.length === 1 ? selectedFeatures[0] : null}
        compositionInsetLeftRatio={0} compositionInsetRightRatio={0}
        simulation={stage === "simulation" ? cam?.simulation : null} animateToolpath={stage === "simulation"}
        materialSnapshotUrls={stage === "simulation" ? snapshots.urls : emptyIds} materialSnapshotStages={stage === "simulation" ? snapshots.stages : undefined}
        profileBoundaries={stage === "toolpath" || stage === "simulation" ? cam?.profile_boundaries : undefined} toolpathLoaded={segments.length > 0} />}
      <nav className="studio-tabs" aria-label="工艺设计阶段">{tabs.map(tab => <button key={tab.id} aria-pressed={stage === tab.id} className={stage === tab.id ? "active" : ""} onClick={() => { setStage(tab.id); changeFollowing(false); }}><tab.icon size={17} />{tab.label}</button>)}</nav>
      <div className="studio-follow"><button className={following ? "active" : ""} onClick={() => changeFollowing(!following)}>{following ? <Focus size={16} /> : <MousePointer2 size={16} />}{following ? "跟随智能体" : "自由查看"}<ChevronDown size={12} /></button><button title="显示工程详情" aria-label="显示工程详情" onClick={() => setDetails(!details)}><Layers3 size={18} /></button><button title="全屏工作台" aria-label="全屏工作台" onClick={() => document.fullscreenElement ? void document.exitFullscreen() : void document.documentElement.requestFullscreen()}><Maximize2 size={17} /></button></div>
      {task && stage !== "gcode" && <div className="studio-focus"><small>{tabs.find(tab => tab.id === stage)?.label} <span>/ {following ? "智能体当前观察" : "手动观察"}</span></small><strong>{focusTitle}</strong>{task.jobId && <code>PART {task.jobId.slice(0, 8).toUpperCase()}</code>}</div>}
      {!job?.model_url && stage !== "gcode" && !uploadOpen && <div className={`studio-await${details && job ? " with-inspector" : ""}`} role="status" aria-live="polite"><LoaderCircle size={34} className={busy || liveRunning ? "spin" : ""} /><h2>{task ? "等待三维模型就绪" : "从一个零件开始"}</h2><p>{task ? "解析完成后，模型与制造特征将在这里显示。" : "上传 STEP / STP，智能体将开始工艺设计。"}</p></div>}
      {stage === "gcode" && <GCodePanel key={task?.jobId} jobId={task?.jobId} existingUrl={gcodeUrl} onAvailability={setGenericProgramAvailable} onProgramStates={setProgramStates} selectedOperationId={selectedOperation} onSelectOperationId={setSelectedOperation} detailsVisible={details} />}
      {details && job && <aside className={`studio-inspector${stage === "features" ? " studio-feature-inspector" : ""}`}><header><div><small>ENGINEERING DETAILS</small><strong>{stage === "features" ? `制造特征 · ${features.length}` : stage === "process" ? "装夹与工序" : stage === "toolpath" ? "刀路轨迹" : stage === "gcode" ? "工序代码" : "验证结果"}</strong></div><button aria-label="收起详情" onClick={() => setDetails(false)}><X size={16} /></button></header>
        <div className="studio-inspector-body">
          {stage === "features" ? <FeaturePanel key={task?.jobId} features={features} selectedIds={viewedFeatures} onSelect={inspectFeature} onShowAll={() => { changeFollowing(false); setSelectedFeatures([]); setSelectedOperation(undefined); }} />
          : <>{stage !== "simulation" && job.plan?.setups.map((setup, index) => <div className="studio-setup" key={setup.id}><h4><Box size={15} />装夹 {String(index + 1).padStart(2, "0")} <span>{setup.name}</span></h4>{setup.operations.map((item, opIndex) => <button className={`studio-operation ${selectedOperation === item.id ? "selected" : ""}`} key={item.id} onClick={() => inspectOperation(item.id)}><span>{String(opIndex + 1).padStart(2, "0")}</span><div><strong>{item.name}</strong><small>{item.tool.name} · {item.id}</small><em className="studio-operation-status">{stage === "gcode" ? gcodeUrl ? "整件程序已归档" : programStates[item.id] || "尚未生成" : operationState(item.id, trials, workspace)}</em></div></button>)}</div>)}
            {stage === "toolpath" && <div className="studio-evidence"><span>当前轨迹段数</span><b>{filteredSegments.length.toLocaleString()}</b><small>{segments.length ? "读取已归档刀路" : "等待智能体计算刀路"}</small></div>}
            {stage === "simulation" && <>
              <h4 className="studio-validation-title">单道工序试算</h4>
              <div className="studio-kv"><span>已接受工序</span><b>{trials?.accepted_operation_ids.length || 0} / {operations.length}</b></div>
              {operations.map(item => <button className="studio-target" key={item.id} onClick={() => inspectOperation(item.id)}><strong>{item.name}</strong><small>{operationState(item.id,trials,workspace)} · {snapshots.stages.some(snapshot => snapshot.operationId === item.id) ? "点击定位并查看材料去除" : "点击定位 · 未归档材料快照"}</small></button>)}
              <h4 className="studio-validation-title">累计材料去除</h4>
              <div className="studio-kv"><span>已归档材料快照</span><b>{snapshots.urls.length} 帧</b></div>
              <div className="studio-kv"><span>包含工序</span><b>{snapshots.stages.length} 道</b></div>
              {!snapshots.urls.length && <p className="studio-panel-empty">{ended ? "本次任务未归档材料快照" : "等待材料去除快照"}</p>}
              <h4 className="studio-validation-title">整件验证</h4>
              <div className="studio-evidence"><ShieldCheck size={25} /><strong>{result.coverage ? `制造目标覆盖 ${result.coverage.covered_count}/${result.coverage.target_count}` : cam ? "整件验证证据已归档" : ended ? "整件验证证据未归档" : "等待整件验证证据"}</strong><small>{result.ready ? "已通过现有验证门禁" : "当前为验证草案，尚不可用于生产"}</small></div>
              {result.coverage?.targets.filter(target => target.state !== "covered").map(target => <button className="studio-target" key={target.id} onClick={() => { changeFollowing(false); setSelectedFeatures(target.source_feature_ids); setSelectedOperation(target.covered_by[0]); }}><strong>{target.kind === "pocket" ? "型腔精加工" : target.id.includes("NONROTATIONAL") ? "非回转外形" : target.kind === "outer_profile" ? "回转轮廓" : target.label}</strong><small>{target.state === "review" ? "需要人工确认" : "尚未完整覆盖"} · 点击定位相关特征</small></button>)}
              {result.coverage?.issues.map((issue,index)=><p className="studio-warning" key={index}>{issue}</p>)}
              {cam && <><div className="studio-kv"><span>整件仿真记录</span><b>{cam.simulation.operation_snapshots?.length || 0}</b></div><div className="studio-kv"><span>安全状态</span><b>{cam.safety}</b></div></>}
              {cam?.simulation.warnings.map((warning,index)=><p className="studio-warning" key={index}>{warning}</p>)}
              {snapshotError && <p className="studio-warning">{snapshotError}</p>}
            </>}
            {!operations.length && stage === "process" && <p className="studio-panel-empty">智能体建立工艺草案后，装夹与工序顺序会显示在这里。</p>}</>}
        </div><footer><i /><span>{job.machine || "Citizen Cincom L32"}<small>{job.material || "材料待确认"}</small></span></footer>
      </aside>}
      {(error || dataError || job?.error) && <div className="studio-error" role="alert"><span>{error || dataError || job?.error}</span><button aria-label="关闭提示" onClick={() => { setError(""); setDataError(""); }}><X size={15} /></button></div>}
      <section ref={floatingConsole.ref} style={floatingConsole.style} className={`studio-console ${expanded ? "expanded" : ""} ${consoleCompact ? "minimized" : ""}`} aria-label="智能体任务进度"><header {...floatingConsole.headerProps} title="拖动移动智能体窗口"><div><Sparkles size={16} /><strong>工艺设计智能体</strong><span className={liveRunning ? "running" : ""}>{status}</span></div><div><button onClick={() => setConsoleMinimized(!consoleCompact)} aria-label={consoleCompact ? "展开智能体窗口" : "最小化智能体窗口"} aria-expanded={!consoleCompact}><ChevronDown size={14} style={{ transform: consoleCompact ? "rotate(180deg)" : undefined }} /></button>{liveRunning && task?.sessionId && <button onClick={() => void cancel()} title="停止当前任务"><Pause size={14} /></button>}<button onClick={() => { setExpanded(!expanded); setConsoleMinimized(false); }} aria-label={expanded ? "收起历史动态" : "查看历史动态"}><History size={15} /></button></div></header><p className="studio-console-summary">{ended && !liveRunning ? `${result.label} · ${genericProgramAvailable ? "已生成通用 G 代码" : "查看任务结果"}` : currentAction?.label || status}</p>{ended && !liveRunning ? <><ResultCard key={task?.jobId} job={job} workspace={workspace} trials={trials} events={journal} sessionId={task?.sessionId} programAvailable={Boolean(gcodeUrl)} genericProgramAvailable={genericProgramAvailable} onValidation={() => { setStage("simulation"); setDetails(true); changeFollowing(false); }} />{expanded && <HarnessTranscript events={journal} live={null} expanded running={false} />}</> : <HarnessTranscript events={journal} live={liveTranscript} expanded={expanded && !questionWaiting} running={liveRunning && !questionWaiting} />}{task?.sessionId && <Interaction key={task.sessionId} sessionId={task.sessionId} onWaiting={setQuestionWaiting} onSubmitted={() => changeFollowing(true)} />}</section>
      <div className="studio-metrics"><span><b>{features.length}</b> 特征</span><span><b>{operations.length}</b> 工序</span><span><b>{segments.length.toLocaleString()}</b> 刀路段</span><span className="studio-draft">{ended ? `${result.label} · ${result.ready ? "验证门禁通过" : "尚不可用于生产"}` : "工程草案 · 待审查"}</span></div>
      {uploadOpen && <div className="studio-modal-backdrop studio-upload-backdrop"><section className="studio-upload" role="dialog" aria-modal="true" aria-label="新建智能工艺设计">
        <header><span><FileUp size={17} />新建智能工艺设计</span><button aria-label="关闭上传" disabled={busy} onClick={() => setUploadOpen(false)}><X size={18} /></button></header>
        <div className="studio-upload-body">
          <div className="studio-model-drop" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); selectUploadFile(Array.from(event.dataTransfer.files)); }}>
            <div className="studio-model-upload-row"><div className="studio-model-upload-icon"><Layers3 size={25} /></div><div className="studio-model-upload-copy"><h2>上传三维模型</h2><p>选择或拖入一个 STEP / STP 文件，智能体将自动分析并设计加工工艺。</p></div><button className="studio-model-select" disabled={busy} onClick={() => input.current?.click()}><FileUp size={14} />选择文件</button></div>
            <div className={`studio-model-file ${uploadFile ? "has-file" : ""}`}><Box size={14} /><strong>STP/STEP</strong><span title={uploadFile?.name}>{uploadFile?.name || "等待选择"}</span>{uploadFile && <small>{uploadFile.size < 1024 * 1024 ? `${(uploadFile.size / 1024).toFixed(1)} KB` : `${(uploadFile.size / 1024 / 1024).toFixed(1)} MB`}</small>}</div>
          </div>
          {error && <p className="studio-upload-error" role="alert">{error}</p>}
        </div>
        <footer><button className="studio-upload-cancel" disabled={busy} onClick={() => setUploadOpen(false)}>取消</button><button className="studio-upload-start" disabled={busy || !uploadFile || !engineReady} onClick={() => void start(uploadFile || undefined)}>{busy ? <LoaderCircle size={14} className="spin" /> : <FileUp size={14} />}{busy ? "正在创建任务…" : "开始工艺设计"}</button></footer>
      </section></div>}
      {historyOpen && <HistoryDialog onDelete={deleteHistoryTask} items={history} loading={historyLoading} error={historyError} onClose={() => setHistoryOpen(false)} onRetry={() => void openHistory()} onSelect={item => { selectTask({ jobId: item.id, sessionId: item.sessionId, filename: item.filename }); setError(""); setHistoryOpen(false); setUploadOpen(false); }} />}
    </section>
  </main>;
}











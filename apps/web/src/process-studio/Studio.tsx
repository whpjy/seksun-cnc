import { useEffect, useMemo, useRef, useState } from "react";
import { Box, ChevronDown, Code2, FileUp, Focus, History, Layers3, Library, Link2, LoaderCircle, Maximize2, MousePointer2, Pause, ScanSearch, ShieldCheck, Sparkles, Wrench, X } from "lucide-react";
import { ModelViewer } from "../ModelViewer";
import { featureDisplayName } from "../featurePresentation";
import type { AgentWorkspace, CamResult, Job, ManufacturingFeature, RotationalFeatureAnalysis, RotationalManufacturingFeature, ToolpathSegment } from "../types";
import { activitiesFrom, follow, jobIdIn, PROCESS_PROMPT, rpc, upload } from "./harness";
import type { Activity, JournalEvent, Stage } from "./harness";
import HarnessTranscript from "./HarnessTranscript";
import { processStudioDisplayConfig } from "./display-config";
import Interaction from "./Interaction";
import ResultCard from "./ResultCard";
import AccountMenu from "./AccountMenu";
import FeaturePanel from "./FeaturePanel";
import GCodePanel from "./GCodePanel";
import HistoryDialog from "./HistoryDialog";
import ResourceLibrary from "./ResourceLibrary";
import { StockRuleDialog, StockSelectionPanel } from "./StockSelection";
import { DrawingEvidencePanel } from "./DrawingEvidence";
import CandidateRoute from "./CandidateRoute";
import {RadialSetupPanel} from './RadialSetupPanel';
import {CutoffSetupPanel} from './CutoffSetupPanel';
import {BarHoldingPanel} from './BarHoldingPanel';
import type { StockSelectionData } from "./StockSelection";
import useFloatingConsole from "./useFloatingConsole";
import { operationState, outcome } from "./results";
import type { TrialState } from "./results";
import type { LiveTranscript } from "./transcript";
import type { StreamProgress } from "./progress";
import { cumulativeMaterial, machiningEventStage, previewMaterial, previewSegments } from "./machining-preview";
import type { MachiningPreview } from "./machining-preview";

const tabs = [
  { id: "features", label: "特征识别", icon: ScanSearch }, { id: "process", label: "工序设计", icon: Layers3 },
  { id: "toolpath", label: "刀路计算", icon: Wrench }, { id: "simulation", label: "仿真验证", icon: ShieldCheck }, { id: "gcode", label: "G 代码", icon: Code2 },
] as const;
type Task = { sessionId?: string; jobId?: string; filename: string; view?: { stage: Stage; following: boolean } };
const storageKey = "nexus.process-studio.task.v1";
const emptyIds: string[] = [];
const emptySegments: ToolpathSegment[] = [];
const emptyFeatures: ManufacturingFeature[] = [];
function retain<T>(previous: T, next: T): T { return JSON.stringify(previous) === JSON.stringify(next) ? previous : next; }
function restore(): Task | null {
  const linkedId = new URL(window.location.href).searchParams.get("task");
  let cached: Task | null = null;
  try { const value = JSON.parse(localStorage.getItem(storageKey) || "null"); if (value && typeof value.filename === "string") cached = value; } catch { /* Optional cache. */ }
  if (linkedId !== null) {
    if (!/^[a-f0-9]{32}$/i.test(linkedId)) return null;
    if (cached?.jobId === linkedId) return cached;
    let sessionId: string | undefined;
    try { sessionId = JSON.parse(localStorage.getItem("nexus.process-studio.sessions.v1") || "{}")[linkedId]; } catch { /* Optional local session association. */ }
    return { jobId: linkedId, sessionId, filename: "正在加载任务…" };
  }
  return cached;
}
function taskAddress(jobId?: string): string {
  const url = new URL(window.location.href);
  if (jobId) url.searchParams.set("task", jobId); else url.searchParams.delete("task");
  return url.href;
}
function restoreView(task: Task | null): { stage: Stage; following: boolean } {
  const view = task?.view;
  return view && tabs.some(tab => tab.id === view.stage) && typeof view.following === "boolean"
    ? view : { stage: "features", following: true };
}
async function get<T>(path: string, optional = false): Promise<T | null> {
  const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(15000) });
  if (optional && response.status === 404) return null;
  if (!response.ok) throw new Error(response.status === 404 && /^\/api\/v1\/jobs\/[^/]+$/.test(path)
    ? "任务不存在或已删除，请从历史任务选择其他任务。" : `工程数据加载失败 (${response.status})`);
  return response.json() as Promise<T>;
}
function stageOf(event: AgentWorkspace["events"][number]): Stage {
  if (event.stage.includes("trial") && event.kind === "tool_call" && event.status === "running") return "toolpath";
  return machiningEventStage(event);
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
  const translateToChinese = processStudioDisplayConfig.translateToChinese;
  const [task, setTask] = useState<Task | null>(restore);
  const [stage, setStage] = useState<Stage>(() => restoreView(task).stage);
  const [following, setFollowing] = useState(() => restoreView(task).following);
  const [job, setJob] = useState<Job | null>(null);
  const [workspace, setWorkspace] = useState<AgentWorkspace | null>(null);
  const [cam, setCam] = useState<CamResult | null>(null);
  const [trials, setTrials] = useState<TrialState | null>(null);
  const [machining, setMachining] = useState<MachiningPreview | null>(null);
  const [machiningError, setMachiningError] = useState("");
  const [playbackChain, setPlaybackChain] = useState<{ complete: boolean; previews: MachiningPreview[]; message?: string; missing_operation_id?: string } | null>(null);
  const [playbackMode, setPlaybackMode] = useState<"single" | "cumulative">("cumulative");
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
  const [linkCopied, setLinkCopied] = useState(false);
  const [resourceOpen, setResourceOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [history, setHistory] = useState<Array<{ id: string; filename: string; operation_count: number; created_at?: string; sessionId?: string }>>([]);
  const [expanded, setExpanded] = useState(false);
  const [consoleMinimized, setConsoleMinimized] = useState(false);
  const [questionWaiting, setQuestionWaiting] = useState(false);
  const [details, setDetails] = useState(true);
  const [selectedFeatures, setSelectedFeatures] = useState<string[]>([]);
  const [selectedOperation, setSelectedOperation] = useState<string>();
  const [stockRulesOpen, setStockRulesOpen] = useState(false);
  const [stockRuleRevision, setStockRuleRevision] = useState(0);
  const [stockSelection, setStockSelection] = useState<StockSelectionData | null>(null);
  const [stockError, setStockError] = useState("");
  const stockJobRef = useRef<string | undefined>(undefined);
  const stockKey = JSON.stringify(job?.plan?.stock || null);
  useEffect(() => {
    if (stockJobRef.current !== task?.jobId) setStockSelection(null);
    stockJobRef.current = task?.jobId; setStockError("");
    if (!task?.jobId || job?.device_id !== "citizen-cincom-l32" || !job.plan) return;
    const controller = new AbortController();
    get<StockSelectionData>(`/api/v1/jobs/${task.jobId}/stock-selection`).then(data => {
      if (!controller.signal.aborted) setStockSelection(data);
    }).catch(reason => { if (!controller.signal.aborted) setStockError((reason as Error).message); });
    return () => controller.abort();
  }, [task?.jobId, job?.device_id, stockKey, stockRuleRevision]);
  const [genericProgramAvailable, setGenericProgramAvailable] = useState(false);
  const [programStates, setProgramStates] = useState<Record<string, string>>({});
  const floatingConsole = useFloatingConsole();
  const [snapshots, setSnapshots] = useState<{ urls: string[]; stages: { operationId: string; start: number; count: number }[] }>({ urls: [], stages: [] });
  const [snapshotError, setSnapshotError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const drawingInput = useRef<HTMLInputElement>(null);
  const [drawingFile, setDrawingFile] = useState<File | null>(null);
  const seqs = useRef(new Map<number, JournalEvent>());
  const submitting = useRef(false);
  const followingRef = useRef(following);
  const focusKey = useRef("");
  const activities = useMemo(() => activitiesFrom(journal), [journal]);
  const currentAction = activities.filter(activity => activity.navigable !== false).at(-1);
  const currentActionRef = useRef(currentAction);
  currentActionRef.current = currentAction;
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
    if (action && event && Date.parse(event.created_at || "") > (action.completedAt || action.time)) action = undefined;
    const key = action ? `${action.id}:${action.status}` : event?.event_id;
    if (!key || key === focusKey.current) return;
    focusKey.current = key;
    const nextStage = action?.stage === "simulation" && action.status === "running" && action.label.includes("计算刀路") ? "toolpath" : action?.stage || (event ? stageOf(event) : "features");
    setStage(nextStage);
    const operationId = action?.operationId || event?.viewer?.operation_id || (typeof event?.metrics?.operation_id === "string" ? event.metrics.operation_id : undefined);
    setSelectedOperation(previous => operationId || (nextStage === "toolpath" || nextStage === "simulation" ? previous : undefined));
    setSelectedFeatures(action?.featureIds?.length ? action.featureIds : event?.viewer?.feature_ids?.length ? event.viewer.feature_ids : operations.find(item => item.id === operationId)?.feature_ids || emptyIds);
  };
  const changeFollowing = (enabled: boolean) => {
    followingRef.current = enabled; setFollowing(enabled);
    if (enabled) { focusKey.current = ""; focus(currentAction, activeEvent); }
  };
  const selectTask = (next: Task | null, navigate = true) => {
    if (next && task?.jobId === next.jobId && task?.sessionId === next.sessionId) return;
    if (navigate && taskAddress(next?.jobId) !== window.location.href) window.history.pushState(null, "", taskAddress(next?.jobId));
    setLinkCopied(false);
    setJob(null); setWorkspace(null); setCam(null); setTrials(null); setSegments(emptySegments); setRotational(null);
    setSnapshots({ urls: [], stages: [] }); setSelectedFeatures(emptyIds); setSelectedOperation(undefined);
    setMachining(null); setMachiningError("");
    setPlaybackChain(null); setPlaybackMode("cumulative");
    setGenericProgramAvailable(false); setProgramStates({});
    setJournal([]); setStreamProgress(null); setLiveTranscript(null); setConnection(false); setDataError(""); setSnapshotError("");
    focusKey.current = ""; followingRef.current = true; setFollowing(true); setStage("features"); setTask(next);
  };

  useEffect(() => {
    // Migrate cached tasks and attach the job created by a running session.
    if (task?.jobId && new URL(window.location.href).searchParams.get("task") !== task.jobId) {
      window.history.replaceState(null, "", taskAddress(task.jobId));
    }
  }, [task?.jobId]);
  useEffect(() => {
    const navigate = () => {
      const linked = new URL(window.location.href).searchParams.get("task");
      selectTask(linked ? restore() : null, false);
      setUploadOpen(false); setHistoryOpen(false);
    };
    window.addEventListener("popstate", navigate);
    return () => window.removeEventListener("popstate", navigate);
  }, [task?.jobId, task?.sessionId]);
  useEffect(() => { if (job && task?.jobId === job.id) setTask(current => current?.jobId === job.id && current.filename !== job.filename ? { ...current, filename: job.filename } : current); }, [job?.id, job?.filename]);
  const copyTaskLink = async () => {
    try { await navigator.clipboard.writeText(taskAddress(task?.jobId)); setLinkCopied(true); }
    catch { setError("无法自动复制，请直接复制浏览器地址栏中的任务链接。"); }
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
      let missingJob = false;
      try {
        // Publish geometry as soon as it arrives; optional machining evidence cannot block it.
        const results = await Promise.allSettled([
          get<Job>(`/api/v1/jobs/${jobId}`).then(next => {
            if (!active || !next) return;
            setJob(previous => retain(previous, { ...next, analysis: retain(previous?.analysis ?? null, next.analysis), plan: retain(previous?.plan ?? null, next.plan) }));
            setDataError("");
          }).catch(reason => { missingJob = reason instanceof Error && reason.message.includes("任务不存在或已删除"); if (active) setDataError(reason instanceof Error ? reason.message : "模型数据加载失败"); }),
          get<AgentWorkspace>(`/api/v1/jobs/${jobId}/agent/workspace`).then(next => {
            if (!active) return;
            setWorkspace(previous => retain(previous, next));
            if (next) focus(currentActionRef.current, next.events.find(event => event.event_id === next.active_event_id) || next.events.at(-1));
          }),
          Promise.allSettled([
            get<CamResult>(`/api/v1/jobs/${jobId}/cam`, true),
            get<{ segments?: ToolpathSegment[]; preview_segments?: ToolpathSegment[] } | ToolpathSegment[]>(`/api/v1/jobs/${jobId}/files/toolpath.json`, true),
          ]).then(([camResult, pathResult]) => {
            if (!active) return;
            const nextCam = camResult.status === "fulfilled" ? camResult.value : null;
            const path = pathResult.status === "fulfilled" ? pathResult.value : null;
            if (camResult.status === "fulfilled") setCam(previous => retain(previous, nextCam));
            const nextSegments = nextCam?.preview_segments || (Array.isArray(path) ? path : path?.preview_segments || path?.segments);
            if (nextSegments) setSegments(previous => retain(previous, nextSegments));
            if (camResult.status === "rejected") throw camResult.reason;
            if (pathResult.status === "rejected") throw pathResult.reason;
          }),
          get<RotationalFeatureAnalysis>(`/api/v1/jobs/${jobId}/files/rotational-features.json`, true).then(next => { if (active) setRotational(previous => retain(previous, next)); }),
          get<TrialState>(`/api/v1/jobs/${jobId}/agent/l32/operation-trials/state`, true).then(next => { if (active) setTrials(previous => retain(previous, next)); }),
        ]);
        // Keep the last valid evidence when an optional endpoint is temporarily unavailable.
        const failure = results.find(result => result.status === "rejected");
        if (active && !missingJob && failure?.status === "rejected") setDataError(`部分工程数据暂不可用：${failure.reason instanceof Error ? failure.reason.message : "加载失败"}`);
      } catch (reason) { if (active) setDataError(reason instanceof Error ? reason.message : "工程数据暂不可用"); }
    };
    // Schedule after completion to avoid overlapping slow geometry requests.
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => { await refresh(); if (active) timer = setTimeout(tick, 2500); };
    void tick(); return () => { active = false; clearTimeout(timer); };
  }, [task?.jobId]);
  useEffect(() => {
    if (!task?.jobId || job?.device_id !== "citizen-cincom-l32" || !selectedOperation || (stage !== "toolpath" && stage !== "simulation")) return;
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const response = await fetch(`/api/v1/jobs/${task.jobId}/agent/l32/operations/${encodeURIComponent(selectedOperation)}/preview`, { cache: "no-store", signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) });
        if (!active) return;
        if (response.status === 404 || response.status === 409) {
          setPlaybackChain(null);
          setMachining(null); setMachiningError(response.status === 409 ? "工序或材料已更新，等待重新试算" : "等待当前工序的试算刀路与材料结果");
        } else {
          if (!response.ok) throw new Error("试算证据暂时无法加载");
          const preview = await response.json() as MachiningPreview;
          if (!active) return;
          const chainResponse = await fetch(`/api/v1/jobs/${task.jobId}/agent/l32/playback?through_operation_id=${encodeURIComponent(selectedOperation)}`, { cache: "no-store", signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) });
          if (!chainResponse.ok) throw new Error("累计材料记录暂时无法加载");
          const chain = await chainResponse.json() as NonNullable<typeof playbackChain>;
          if (!active) return;
          setPlaybackChain(previous => retain(previous, chain));
          setMachining(previous => retain(previous, preview)); setMachiningError("");
        }
      } catch (reason) { if (active) setMachiningError(reason instanceof Error ? reason.message : "试算证据暂时无法加载"); }
      if (active) timer = setTimeout(refresh, 2000);
    };
    void refresh();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [task?.jobId, job?.device_id, selectedOperation, stage]);
  useEffect(() => {
    if (!task?.jobId || !operations.length || (job?.device_id === "citizen-cincom-l32" && !cam)) return;
    let active = true;
    void get<{ operations: Array<{ operation_id: string; files: string[] }> }>(`/api/v1/jobs/${task.jobId}/l32/material-snapshots`, true).then(manifest => {
      if (!active) return;
      const urls: string[] = []; const stages: { operationId: string; start: number; count: number }[] = [];
      for (const item of manifest?.operations || []) { stages.push({ operationId: item.operation_id, start: urls.length, count: item.files.length }); urls.push(...item.files.map(file => `/api/v1/jobs/${task.jobId}/files/${encodeURIComponent(file)}`)); }
      setSnapshots(previous => retain(previous, { urls, stages })); setSnapshotError("");
    }).catch(reason => { if (active) setSnapshotError(reason.message); });
    return () => { active = false; };
  }, [stage, task?.jobId, workspace?.active_event_id, operations.length, job?.device_id, cam]);

  const selectUploadFile = (files: File[]) => {
    if (busy || !files.length) return;
    setError("");
    if (files.length !== 1 || !/\.(stp|step)$/i.test(files[0].name)) {
      setUploadFile(null); setError("请选择一个 STEP 或 STP 三维模型文件。"); return;
    }
    setUploadFile(files[0]);
  };
  const selectDrawingFile = (files: File[]) => {
    if (busy || !files.length) return;
    if (files.length !== 1 || !/\.pdf$/i.test(files[0].name)) { setDrawingFile(null); setError("请选择一个 PDF 二维图纸文件。"); return; }
    setDrawingFile(files[0]); setError("");
  };
  const openUpload = () => { setUploadFile(null); setDrawingFile(null); setError(""); setUploadOpen(true); if (input.current) input.current.value = ""; if (drawingInput.current) drawingInput.current.value = ""; };
  const start = async (file?: File) => {
    if (!file || !drawingFile || submitting.current) return;
    if (!/\.(stp|step)$/i.test(file.name)) { setError("请选择 STEP 或 STP 三维模型文件。"); return; }
    submitting.current = true; setBusy(true); setError("");
    try {
      const created = await rpc<{ sessionId: string }>("session/create", { request: {} });
      const receiptId = await upload(created.sessionId, file);
      const drawingReceiptId = await upload(created.sessionId, drawingFile);
      await rpc("session/rename", { request: { sessionId: created.sessionId, title: `工艺设计 · ${file.name}` } });
      await rpc("session/prompt", { request: { sessionId: created.sessionId, requestId: crypto.randomUUID(), mode: "queue", clientTimeZone: "Asia/Shanghai", content: [{ type: "file", receiptId }, { type: "file", receiptId: drawingReceiptId }, { type: "text", text: PROCESS_PROMPT }] } });
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
  const stockSelected = selectedOperation === "stock" && (stage === "process" || stage === "simulation");
  const inspectStock = () => { changeFollowing(false); setStage("process"); setSelectedOperation("stock"); setSelectedFeatures([]); setDetails(true); };
  const focusTitle = stockSelected ? "毛坯选择 · 初始实心毛坯" : operation ? operation.name : selectedFeatures.length === 1 && focusedFeature ? featureDisplayName(focusedFeature) : following ? currentAction?.label || activeEvent?.title || "零件工程视图" : tabs.find(tab => tab.id === stage)?.label;
  const setup = job?.plan?.setups.find(item => item.operations.some(candidate => candidate.id === selectedOperation));
  const axis = setup?.work_axis;
  const stableWorkAxis = useMemo(() => axis, [axis?.x, axis?.y, axis?.z]);
  const consoleCompact = consoleMinimized && !questionWaiting;
  const activePreview = machining?.operation_id === selectedOperation ? machining : null;
  const solidPreviewUrl=stage==='simulation' && activePreview?.evidence.material_domain==='exact_3d_occ' ? activePreview.evidence.stock_mesh_url : undefined;
  const chainReady = Boolean(playbackChain?.complete && playbackChain.previews.at(-1)?.operation_id === selectedOperation);
  const effectivePlaybackMode = playbackMode === "cumulative" && chainReady ? "cumulative" : "single";
  const trialSegments = useMemo(() => effectivePlaybackMode === "cumulative" ? playbackChain!.previews.flatMap(previewSegments) : activePreview ? previewSegments(activePreview) : emptySegments, [effectivePlaybackMode, playbackChain, activePreview]);
  const trialMaterial = useMemo(() => effectivePlaybackMode === "cumulative" ? cumulativeMaterial(playbackChain!.previews) : activePreview ? previewMaterial(activePreview) : null, [effectivePlaybackMode, playbackChain, activePreview]);
  const visibleSegments = trialSegments.length ? trialSegments : filteredSegments;
  const previewRevision = effectivePlaybackMode === "cumulative" ? playbackChain!.previews.map(preview => preview.revision).join(":") : activePreview?.revision || "";
  const setupCards = job?.plan?.setups.map((setup, index) => <div className="studio-setup" key={setup.id}><h4><Box size={15} />装夹 {String(index + 1).padStart(2, "0")} <span>{setup.name}</span></h4>{setup.operations.map((item, opIndex) => <button className={`studio-operation ${selectedOperation === item.id ? "selected" : ""}`} key={item.id} onClick={() => inspectOperation(item.id)}><span>{String(job?.device_id === "citizen-cincom-l32" ? operations.findIndex(operation => operation.id === item.id) + 2 : opIndex + 1).padStart(2, "0")}</span><div><strong>{item.name}</strong><small>{item.tool.name} · {item.id}</small><em className="studio-operation-status">{stage === "gcode" ? gcodeUrl ? "整件程序已归档" : programStates[item.id] || "尚未生成" : operationState(item.id, trials, workspace)}</em></div></button>)}</div>);

  return <main className="studio">
    <input ref={input} hidden type="file" accept=".stp,.step" onChange={event => selectUploadFile(Array.from(event.target.files || []))} />
    <input ref={drawingInput} hidden type="file" accept=".pdf,application/pdf" onChange={event => selectDrawingFile(Array.from(event.target.files || []))} />
    <header className="studio-header">
      <div className="studio-brand"><span className="studio-brand-mark"><Box size={23} /></span><div><strong>NEXUS <span>智能工艺设计</span></strong><small>AI PROCESS DESIGN STUDIO</small></div></div>
      <div className="studio-document"><span className="studio-status"><i className={liveRunning || busy ? "pulse" : ""} />{status}</span><strong title={task?.filename}>{task?.filename || "新建零件工艺"}</strong></div>
      <div className="studio-header-actions">{task?.jobId && <button onClick={() => void copyTaskLink()}><Link2 size={16} />{linkCopied ? "链接已复制" : "复制任务链接"}</button>}<button onClick={() => setStockRulesOpen(true)}><Wrench size={16} />工艺规则</button><button onClick={() => setResourceOpen(true)}><Library size={16} />资源库</button><button disabled={busy} onClick={() => void openHistory()}><History size={16} />历史任务</button><button onClick={openUpload} disabled={busy}><FileUp size={16} />新建任务</button><AccountMenu onLogout={onLogout} /></div>
    </header>
    {resourceOpen && <ResourceLibrary machineInstanceId={job?.machine_instance_id} onClose={() => setResourceOpen(false)} />}
    {stockRulesOpen && <StockRuleDialog onClose={() => setStockRulesOpen(false)} onSaved={() => setStockRuleRevision(value => value + 1)} />}
    <section className={`studio-canvas${stage === "gcode" ? " studio-code-canvas" : ""}`} onPointerDown={event => { if ((event.target as HTMLElement).tagName === "CANVAS") changeFollowing(false); }}>
      {job?.model_url && stage !== "gcode" && <ModelViewer modelUrl={solidPreviewUrl || job.model_url} features={stockSelected || solidPreviewUrl ? emptyFeatures : features} selectedFeatureIds={stockSelected || solidPreviewUrl ? emptyIds : viewedFeatures} onSelectFeature={inspectFeature}
        viewMode={stockSelected ? "仿真" : stage === "features" ? "特征" : stage === "process" ? "工艺" : stage === "toolpath" ? "刀路" : "仿真"}
        toolpathSegments={stage === "toolpath" || stage === "simulation" ? visibleSegments : emptySegments} topologyEdges={stockSelected || solidPreviewUrl ? undefined : job.analysis?.visual_edges}
        activeOperationId={selectedOperation} activeOperationLabel={stockSelected ? "毛坯选择" : operation?.name} workAxis={stableWorkAxis}
        showFeatureSummary={false} showFeatureAnnotations={stage === "features"} operationContextVisible={!stockSelected}
        releaseContextOnCleanup backgroundColor={0xdce7f1}
        isolatedFeatureId={stage === "features" && selectedFeatures.length === 1 ? selectedFeatures[0] : null}
        compositionInsetLeftRatio={0} compositionInsetRightRatio={0}
        simulation={stage === "simulation" && !stockSelected && !trialMaterial && !solidPreviewUrl ? cam?.simulation : null} animateToolpath={!stockSelected && !solidPreviewUrl && (stage === "simulation" || stage === "toolpath")}
        autoPlay={following && visibleSegments.length > 0} playbackRevision={previewRevision} playbackMode={effectivePlaybackMode} onPlaybackModeChange={setPlaybackMode} cumulativeAvailable={chainReady}
        turningStage={stockSelected ? stockSelection?.view : stage === "simulation" ? trialMaterial : null}
        materialSnapshotUrls={stage === "simulation" && !stockSelected && !activePreview ? snapshots.urls : emptyIds} materialSnapshotStages={stage === "simulation" && !stockSelected && !activePreview ? snapshots.stages : undefined}
        profileBoundaries={stage === "toolpath" || stage === "simulation" ? cam?.profile_boundaries : undefined} toolpathLoaded={visibleSegments.length > 0} />}
      {!stockSelected && (stage === "toolpath" || stage === "simulation") && <div className={`studio-machining-state ${activePreview && !activePreview.can_accept ? "blocked" : ""}`} role="status"><strong>{selectedOperation || "整件加工"} · {activePreview ? "试算结果回放" : visibleSegments.length ? "刀路结果回放" : "正在等待加工证据"}</strong><span>{activePreview ? `${visibleSegments.length} 段刀路 · ${effectivePlaybackMode === "cumulative" ? "从毛坯累计切削" : "单工序切削"} · ${activePreview.can_accept ? "试算可接受（DRAFT）" : activePreview.reason === "profile_overcut" ? "检测到过切，未通过验证" : "验证受阻"}` : machiningError || "刀路与仿真结果生成后将自动展示"}</span>{trialMaterial && <small>按仿真器记录的切削状态回放，最终显示实际余料。</small>}{activePreview && !chainReady && <small>累计回放暂不可用：{playbackChain?.missing_operation_id} {playbackChain?.message || "等待前序材料记录"}</small>}</div>}
      <nav className="studio-tabs" aria-label="工艺设计阶段">{tabs.map(tab => <button key={tab.id} aria-pressed={stage === tab.id} className={stage === tab.id ? "active" : ""} onClick={() => { setStage(tab.id); changeFollowing(false); }}><tab.icon size={17} />{tab.label}</button>)}</nav>
      <div className="studio-follow"><button className={following ? "active" : ""} onClick={() => changeFollowing(!following)}>{following ? <Focus size={16} /> : <MousePointer2 size={16} />}{following ? "跟随智能体" : "自由查看"}<ChevronDown size={12} /></button><button title="显示工程详情" aria-label="显示工程详情" onClick={() => setDetails(!details)}><Layers3 size={18} /></button><button title="全屏工作台" aria-label="全屏工作台" onClick={() => document.fullscreenElement ? void document.exitFullscreen() : void document.documentElement.requestFullscreen()}><Maximize2 size={17} /></button></div>
      {task && stage !== "gcode" && <div className="studio-focus"><small>{tabs.find(tab => tab.id === stage)?.label} <span>/ {following ? "智能体当前观察" : "手动观察"}</span></small><strong>{focusTitle}</strong>{task.jobId && <code>PART {task.jobId.slice(0, 8).toUpperCase()}</code>}</div>}
      {!job?.model_url && stage !== "gcode" && !uploadOpen && <div className={`studio-await${details && job ? " with-inspector" : ""}`} role="status" aria-live="polite"><LoaderCircle size={34} className={busy || liveRunning ? "spin" : ""} /><h2>{task ? "等待三维模型就绪" : "从一个零件开始"}</h2><p>{task ? "解析完成后，模型与制造特征将在这里显示。" : "上传 STEP / STP，智能体将开始工艺设计。"}</p></div>}
      {stage === "gcode" && <GCodePanel key={task?.jobId} jobId={task?.jobId} existingUrl={gcodeUrl} onAvailability={setGenericProgramAvailable} onProgramStates={setProgramStates} selectedOperationId={selectedOperation} onSelectOperationId={setSelectedOperation} detailsVisible={details} />}
      {details && job && <aside className={`studio-inspector${stage === "features" ? " studio-feature-inspector" : ""}`}><header><div><small>ENGINEERING DETAILS</small><strong>{stage === "features" ? `制造特征 · ${features.length}` : stage === "process" ? "装夹与工序" : stage === "toolpath" ? "刀路轨迹" : stage === "gcode" ? "工序代码" : "验证结果"}</strong></div><button aria-label="收起详情" onClick={() => setDetails(false)}><X size={16} /></button></header>
        <div className="studio-inspector-body">
          {stage==='process' && job.device_id==='citizen-cincom-l32' && job.plan && <RadialSetupPanel key={job.id} job={job} running={liveRunning} onChanged={updated=>{setJob(updated);setCam(null);setTrials(null);setMachining(null);setPlaybackChain(null);}}/>}
          {stage==='process' && job.device_id==='citizen-cincom-l32' && job.plan && <CutoffSetupPanel key={job.id} job={job} running={liveRunning} onChanged={updated=>{setJob(updated);setCam(null);setTrials(null);setMachining(null);setPlaybackChain(null);}}/>}
          {stage==='process' && job.device_id==='citizen-cincom-l32' && job.plan && <BarHoldingPanel key={job.id} job={job} running={liveRunning} onChanged={updated=>{setJob(updated);setCam(null);setTrials(null);setMachining(null);setPlaybackChain(null);}}/>}
          {job.drawing_filename && <DrawingEvidencePanel key={`${job.id}-${job.drawing_analysis?.status}-${stockSelected}`} job={job} running={liveRunning} onChanged={updated => { setJob(updated); setCam(null); setTrials(null); setMachining(null); setPlaybackChain(null); setStockRuleRevision(v => v + 1); }} />}
          {job.device_id === "citizen-cincom-l32" && job.plan && stage !== "features" && stage !== "gcode" && <div className="studio-stock-step"><button className={`studio-operation ${stockSelected ? "selected" : ""}`} onClick={inspectStock} aria-label="第一步：毛坯选择"><span>01</span><div><strong>毛坯选择</strong><small>实心圆棒{stockSelection ? ` · Ø${stockSelection.stock.diameter_mm.toFixed(3)} × ${stockSelection.stock.length_mm.toFixed(3)} mm` : " · 点击查看"}</small><em className="studio-operation-status">点击显示加工前毛坯</em></div></button></div>}
          {stockSelected && task?.jobId ? <><StockSelectionPanel key={task.jobId} data={stockSelection} jobId={task.jobId} running={liveRunning} onRules={() => setStockRulesOpen(true)} onChanged={updated => { setJob(updated); setCam(null); setTrials(null); setMachining(null); setPlaybackChain(null); }} />{stockError && <p className="stock-note" role="status">{stockError}</p>}</> : <>
          {stage === "features" ? <FeaturePanel key={task?.jobId} features={features} selectedIds={viewedFeatures} onSelect={inspectFeature} onShowAll={() => { changeFollowing(false); setSelectedFeatures([]); setSelectedOperation(undefined); }} />
          : <>{stage === "process" && job.device_id === "citizen-cincom-l32" && job.plan && <CandidateRoute key={job.id} jobId={job.id} onSelect={(features, operation) => { changeFollowing(false); setSelectedFeatures(features); setSelectedOperation(operation); }} />}{stage !== "simulation" && setupCards}
            {stage === "toolpath" && <div className="studio-evidence"><span>当前轨迹段数</span><b>{visibleSegments.length.toLocaleString()}</b><small>{activePreview ? "读取当前工序的真实试算刀路" : segments.length ? "读取已归档刀路" : "等待智能体计算刀路"}</small></div>}
            {stage === "simulation" && <>
              {job.device_id === "citizen-cincom-l32" && <p className="studio-warning">车削轮廓试算通过后，仍需验证各孔槽的实际材料去除。二维图纸的公差、粗糙度和质量要求需要单独检验。</p>}
              <h4 className="studio-validation-title">单道工序试算</h4>
              {activePreview && <div className="studio-evidence"><strong>{activePreview.can_accept ? "试算可接受（DRAFT）" : "试算未通过"}</strong><small>{activePreview.reason === "profile_overcut" ? "检测到过切" : activePreview.reason ? "存在阻断，需修正后重新试算" : "仍需整件验证"}</small><span>本次试算材料去除</span><b>{activePreview.evidence.removed_volume_mm3?.toFixed(2) || "—"} mm³</b></div>}
              {activePreview?.evidence.reachability_checks?.filter(check => check.status === "failed").map(check => <p className="studio-warning" key={check.id}>{check.message}{check.id === "boring_bar_entry" && typeof check.measured_value === "number" && typeof check.limit_value === "number" ? `：现有孔径 Ø${(2 * check.measured_value).toFixed(2)} mm，当前镗杆要求至少 Ø${(2 * check.limit_value).toFixed(2)} mm` : ""}</p>)}
              {activePreview?.evidence.diagnostic_hints?.map((hint, index) => <p className="studio-warning" key={`hint-${index}`}>{hint}</p>)}
              {solidPreviewUrl && <p className="stock-note">当前显示本次三维实体试算的真实剩余材料快照；尚无逐刀动画。{activePreview?.can_accept?'仍需整件验证。':'工序尚未接受，不能作为最终成品。'}</p>}
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
          </>}
          {stockSelected && setupCards}
        </div><footer><i /><span>{job.machine || "Citizen Cincom L32"}<small>{job.material || "材料待确认"}</small></span></footer>
      </aside>}
      {(error || dataError || job?.error) && <div className="studio-error" role="alert"><span>{error || dataError || job?.error}</span><button aria-label="关闭提示" onClick={() => { setError(""); setDataError(""); }}><X size={15} /></button></div>}
      <section ref={floatingConsole.ref} style={floatingConsole.style} className={`studio-console ${expanded ? "expanded" : ""} ${consoleCompact ? "minimized" : ""}`} aria-label="智能体任务进度"><header {...floatingConsole.headerProps} title="拖动移动智能体窗口"><div><Sparkles size={16} /><strong>工艺设计智能体</strong><span className={liveRunning ? "running" : ""}>{status}</span></div><div><button onClick={() => setConsoleMinimized(!consoleCompact)} aria-label={consoleCompact ? "展开智能体窗口" : "最小化智能体窗口"} aria-expanded={!consoleCompact}><ChevronDown size={14} style={{ transform: consoleCompact ? "rotate(180deg)" : undefined }} /></button>{liveRunning && task?.sessionId && <button onClick={() => void cancel()} title="停止当前任务"><Pause size={14} /></button>}<button onClick={() => { setExpanded(!expanded); setConsoleMinimized(false); }} aria-label={expanded ? "收起历史动态" : "查看历史动态"}><History size={15} /></button></div></header><p className="studio-console-summary">{ended && !liveRunning ? `${result.label} · ${genericProgramAvailable ? "已生成通用 G 代码" : "查看任务结果"}` : currentAction?.label || status}</p>{ended && !liveRunning ? <><ResultCard key={task?.jobId} job={job} workspace={workspace} trials={trials} events={journal} sessionId={task?.sessionId} programAvailable={Boolean(gcodeUrl)} genericProgramAvailable={genericProgramAvailable} onValidation={() => { setStage("simulation"); setDetails(true); changeFollowing(false); }} />{expanded && <HarnessTranscript key={String(task?.sessionId) + String(translateToChinese)} translateToChinese={translateToChinese} events={journal} live={null} expanded running={false} />}</> : <HarnessTranscript key={String(task?.sessionId) + String(translateToChinese)} translateToChinese={translateToChinese} events={journal} live={liveTranscript} expanded={expanded && !questionWaiting} running={liveRunning && !questionWaiting} />}{task?.sessionId && <Interaction key={task.sessionId} sessionId={task.sessionId} onWaiting={setQuestionWaiting} onSubmitted={() => changeFollowing(true)} />}</section>
      <div className="studio-metrics"><span><b>{features.length}</b> 特征</span><span><b>{operations.length}</b> 工序</span><span><b>{segments.length.toLocaleString()}</b> 刀路段</span><span className="studio-draft">{ended ? `${result.label} · ${result.ready ? "验证门禁通过" : "尚不可用于生产"}` : "工程草案 · 待审查"}</span></div>
      {uploadOpen && <div className="studio-modal-backdrop studio-upload-backdrop"><section className="studio-upload" role="dialog" aria-modal="true" aria-label="新建智能工艺设计">
        <header><span><FileUp size={17} />新建智能工艺设计</span><button aria-label="关闭上传" disabled={busy} onClick={() => setUploadOpen(false)}><X size={18} /></button></header>
        <div className="studio-upload-body">
          <div className="studio-model-drop" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); selectUploadFile(Array.from(event.dataTransfer.files)); }}>
            <div className="studio-model-upload-row"><div className="studio-model-upload-icon"><Layers3 size={25} /></div><div className="studio-model-upload-copy"><h2>上传三维模型</h2><p>选择或拖入一个 STEP / STP 文件，智能体将自动分析并设计加工工艺。</p></div><button className="studio-model-select" disabled={busy} onClick={() => input.current?.click()}><FileUp size={14} />选择文件</button></div>
            <div className={`studio-model-file ${uploadFile ? "has-file" : ""}`}><Box size={14} /><strong>STP/STEP</strong><span title={uploadFile?.name}>{uploadFile?.name || "等待选择"}</span>{uploadFile && <small>{uploadFile.size < 1024 * 1024 ? `${(uploadFile.size / 1024).toFixed(1)} KB` : `${(uploadFile.size / 1024 / 1024).toFixed(1)} MB`}</small>}</div>
          </div>
          <div className="studio-model-drop" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); selectDrawingFile(Array.from(event.dataTransfer.files)); }}>
            <div className="studio-model-upload-row"><div className="studio-model-upload-icon"><FileUp size={25} /></div><div className="studio-model-upload-copy"><h2>上传二维图纸</h2><p>配对同一零件的 PDF，读取材料、公差与最大外径，按外径加配置余量选择实心毛坯。</p></div><button className="studio-model-select" disabled={busy} onClick={() => drawingInput.current?.click()}>选择图纸</button></div>
            <div className={`studio-model-file ${drawingFile ? "has-file" : ""}`}><strong>PDF</strong><span title={drawingFile?.name}>{drawingFile?.name || "等待选择"}</span></div>
          </div>
          {error && <p className="studio-upload-error" role="alert">{error}</p>}
        </div>
        <footer><button className="studio-upload-cancel" disabled={busy} onClick={() => setUploadOpen(false)}>取消</button><button className="studio-upload-start" disabled={busy || !uploadFile || !drawingFile || !engineReady} onClick={() => void start(uploadFile || undefined)}>{busy ? <LoaderCircle size={14} className="spin" /> : <FileUp size={14} />}{busy ? "正在创建任务…" : "开始工艺设计"}</button></footer>
      </section></div>}
      {historyOpen && <HistoryDialog onDelete={deleteHistoryTask} items={history} loading={historyLoading} error={historyError} onClose={() => setHistoryOpen(false)} onRetry={() => void openHistory()} onSelect={item => { selectTask({ jobId: item.id, sessionId: item.sessionId, filename: item.filename }); setError(""); setHistoryOpen(false); setUploadOpen(false); }} />}
    </section>
  </main>;
}











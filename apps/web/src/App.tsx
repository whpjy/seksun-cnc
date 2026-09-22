import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bot,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  CircleDot,
  FileUp,
  History,
  Info,
  Layers3,
  Library,
  LoaderCircle,
  Pencil,
  Play,
  Rotate3D,
  ShieldCheck,
  Trash2,
  UserRound,
  Wrench,
  X,
} from "lucide-react";
import { ModelViewer } from "./ModelViewer";
import { L32ProgramViewer } from "./L32ProgramViewer";
import { L32Workbench } from "./L32Workbench";
import { ToolLibraryPanel } from "./ToolLibraryPanel";
import { ProcessDesigner } from "./ProcessDesigner";
import type { BacksideDraftResult, CamResult, Catalogs, DeviceLibrary, Job, ManufacturingFeature, Operation, RotationalFeatureAnalysis, SpatialDefectRegion, ToolpathSegment, TurningDraftResult, TurningStageView, Vec3, WholePartDraftResult } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const APP_NAME = (import.meta.env.VITE_APP_NAME ?? "NEXUS CNC").trim() || "NEXUS CNC";
const PAGE_TITLE = (import.meta.env.VITE_PAGE_TITLE ?? "智能工艺规划").trim() || "智能工艺规划";
const APP_LOGO_TEXT = (import.meta.env.VITE_APP_LOGO_TEXT ?? "N").trim().slice(0, 2) || "N";
const EMPTY_TOOLPATH_SEGMENTS: ToolpathSegment[] = [];
const EMPTY_PROFILE_BOUNDARIES: { operation_id: string; setup_id: string; work_axis: Vec3; points: Vec3[] }[] = [];
const EMPTY_MATERIAL_SNAPSHOTS: { urls: string[]; stages: { operationId: string; start: number; count: number }[] } = { urls: [], stages: [] };
function apiUrl(path: string) {
  return `${API_BASE}${path}`;
}

type PlanningProgressEvent = {
  stage: string;
  message: string;
  detail?: string;
  percent: number;
  feature_count?: number;
  hole_count?: number;
  requirement_count?: number;
  matched_count?: number;
  setup_count?: number;
  operation_count?: number;
  coverage_score?: number;
  warning?: string;
};

type CachedPreviewResponse<T> = {
  ok: boolean;
  status: number;
  payload: T;
};

type L32TurningPreview = {
  operationId: string;
  channelId: "main" | "sub";
  sourceCutoffZ: number;
  stockRadius: number;
  draft: TurningDraftResult;
};

type L32GroovePreview = {
  operationId: string;
  stockRadius: number;
  strips: Array<{ z_min_mm: number; z_max_mm: number; cut_to_radius_mm: number }>;
};

type L32GeometricMove = { kind: "rapid" | "feed"; point: { x: number; y: number; z: number } };
type L32GeometricDraft = {
  moves: L32GeometricMove[];
  bound_machine_has_required_module?: boolean | null;
  access_direction?: { x: number; y: number; z: number };
  access_sign?: -1 | 1;
};

type L32MaterialSnapshotManifest = {
  operations: Array<{ operation_id: string; files: string[] }>;
};

const l32MaterialManifestRequests = new Map<string, Promise<L32MaterialSnapshotManifest>>();

function l32DraftsToSegments(operation: Operation, drafts: L32GeometricDraft[]): ToolpathSegment[] {
  const segments: ToolpathSegment[] = [];
  for (const [draftIndex, draft] of drafts.entries()) {
    let previous: L32GeometricMove | null = null;
    for (const move of draft.moves) {
      if (previous) segments.push({
        operation_id: operation.id,
        motion: move.kind === "rapid" ? "rapid" : "cut",
        x1: previous.point.x, y1: previous.point.y, z1: previous.point.z,
        x2: move.point.x, y2: move.point.y, z2: move.point.z,
        setup_id: `${operation.channel_id ?? "main"}-${draftIndex + 1}`,
        work_axis: draft.access_direction ?? { x: 0, y: draft.access_sign ?? 1, z: 0 },
      });
      previous = move;
    }
  }
  return segments.filter((segment) => Math.hypot(
    segment.x2 - segment.x1, segment.y2 - segment.y1, segment.z2 - segment.z1,
  ) > 1e-6);
}

function l32TurningPreviewToSegments(preview: L32TurningPreview, sourceAxis: RotationalFeatureAnalysis["axes"][number]): ToolpathSegment[] {
  const axisLength = Math.hypot(sourceAxis.direction.x, sourceAxis.direction.y, sourceAxis.direction.z) || 1;
  const axis = { x: sourceAxis.direction.x / axisLength, y: sourceAxis.direction.y / axisLength, z: sourceAxis.direction.z / axisLength };
  const helper = Math.abs(axis.z) < 0.9 ? { x: 0, y: 0, z: 1 } : { x: 0, y: 1, z: 0 };
  const cross = { x: axis.y * helper.z - axis.z * helper.y, y: axis.z * helper.x - axis.x * helper.z, z: axis.x * helper.y - axis.y * helper.x };
  const crossLength = Math.hypot(cross.x, cross.y, cross.z) || 1;
  const radial = { x: cross.x / crossLength, y: cross.y / crossLength, z: cross.z / crossLength };
  const point = (x: number, z: number) => {
    const axial = preview.channelId === "sub" ? preview.sourceCutoffZ - z : z;
    const radius = Math.abs(x) / 2;
    return { x: sourceAxis.origin.x + axis.x * axial + radial.x * radius, y: sourceAxis.origin.y + axis.y * axial + radial.y * radius, z: sourceAxis.origin.z + axis.z * axial + radial.z * radius };
  };
  const segments: ToolpathSegment[] = [];
  for (const channel of preview.draft.toolpath.channels) {
    const position: Record<string, number> = {};
    for (const command of channel.commands) {
      const previous = { ...position };
      for (const [name, value] of Object.entries(command.axes)) position[name.toUpperCase()] = value;
      if (!["rapid_move", "feed_move", "arc_move"].includes(command.type)
        || previous.X === undefined || previous.Z === undefined || position.X === undefined || position.Z === undefined) continue;
      const start = point(previous.X, previous.Z), end = point(position.X, position.Z);
      if (Math.hypot(end.x - start.x, end.y - start.y, end.z - start.z) <= 1e-6) continue;
      segments.push({ operation_id: preview.operationId, motion: command.type === "rapid_move" ? "rapid" : "cut", x1: start.x, y1: start.y, z1: start.z, x2: end.x, y2: end.y, z2: end.z, setup_id: channel.id, work_axis: radial });
    }
  }
  return segments;
}

function l32GroovePreviewToSegments(preview: L32GroovePreview, sourceAxis: RotationalFeatureAnalysis["axes"][number]): ToolpathSegment[] {
  const axisLength = Math.hypot(sourceAxis.direction.x, sourceAxis.direction.y, sourceAxis.direction.z) || 1;
  const axis = { x: sourceAxis.direction.x / axisLength, y: sourceAxis.direction.y / axisLength, z: sourceAxis.direction.z / axisLength };
  const helper = Math.abs(axis.z) < 0.9 ? { x: 0, y: 0, z: 1 } : { x: 0, y: 1, z: 0 };
  const cross = { x: axis.y * helper.z - axis.z * helper.y, y: axis.z * helper.x - axis.x * helper.z, z: axis.x * helper.y - axis.y * helper.x };
  const crossLength = Math.hypot(cross.x, cross.y, cross.z) || 1;
  const radial = { x: cross.x / crossLength, y: cross.y / crossLength, z: cross.z / crossLength };
  const point = (z: number, radius: number) => ({
    x: sourceAxis.origin.x + axis.x * z + radial.x * radius,
    y: sourceAxis.origin.y + axis.y * z + radial.y * radius,
    z: sourceAxis.origin.z + axis.z * z + radial.z * radius,
  });
  const segments: ToolpathSegment[] = [];
  for (const strip of preview.strips) {
    const axial = (strip.z_min_mm + strip.z_max_mm) / 2;
    const safe = point(axial, preview.stockRadius + 0.5);
    const start = point(axial, preview.stockRadius);
    const end = point(axial, strip.cut_to_radius_mm);
    segments.push(
      { operation_id: preview.operationId, motion: "rapid", x1: safe.x, y1: safe.y, z1: safe.z, x2: start.x, y2: start.y, z2: start.z, setup_id: "main", work_axis: radial },
      { operation_id: preview.operationId, motion: "cut", x1: start.x, y1: start.y, z1: start.z, x2: end.x, y2: end.y, z2: end.z, setup_id: "main", work_axis: radial },
      { operation_id: preview.operationId, motion: "rapid", x1: end.x, y1: end.y, z1: end.z, x2: safe.x, y2: safe.y, z2: safe.z, setup_id: "main", work_axis: radial },
    );
  }
  return segments;
}

const PLANNING_STAGE_ORDER = [
  "uploading", "geometry_analysis", "draft_planning", "ai_planning", "ai_integration",
  "process_generation", "coverage_validation", "completed",
];
const DEVICE_OPERATION_GROUP_ALIASES: Record<string, string[]> = {
  "钻孔": ["孔加工"],
  "倒角": ["边加工"],
  "小型型腔": ["型腔加工"],
  "雕刻": ["engraving"],
};
const L32_DEVICE_ID = "citizen-cincom-l32";
const l32First = (devices: DeviceLibrary["devices"]) => [...devices].sort(
  (left, right) => Number(right.id === L32_DEVICE_ID) - Number(left.id === L32_DEVICE_ID),
);

function NewJobDialog({ open, onClose, onCreated, canClose = true }: {
  open: boolean;
  onClose: () => void;
  onCreated: (job: Job) => void;
  canClose?: boolean;
}) {
  const stepInputRef = useRef<HTMLInputElement>(null);
  const planningStreamRef = useRef<EventSource | null>(null);
  const [stepFile, setStepFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [progressEvents, setProgressEvents] = useState<PlanningProgressEvent[]>([]);
  const [newJobDevices, setNewJobDevices] = useState<DeviceLibrary["devices"]>([]);
  const [selectedNewJobDeviceId, setSelectedNewJobDeviceId] = useState("");

  useEffect(() => () => planningStreamRef.current?.close(), []);

  useEffect(() => {
    if (!open || newJobDevices.length) return;
    fetch(apiUrl("/api/v1/device-library"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: DeviceLibrary) => {
        const devices = l32First(payload.devices);
        setNewJobDevices(devices);
        setSelectedNewJobDeviceId((current) => current || devices[0]?.id || "");
      })
      .catch(() => setError("加工设备暂时无法加载"));
  }, [newJobDevices.length, open]);

  useEffect(() => {
    if (!open || !canClose) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, canClose, onClose, open]);

  const submit = async () => {
    if (!stepFile || !selectedNewJobDeviceId) return;
    setBusy(true);
    setError("");
    setProgressEvents([{ stage: "uploading", message: "正在上传三维模型", percent: 2 }]);
    const form = new FormData();
    form.append("step", stepFile);
    form.append("device_id", selectedNewJobDeviceId);
    try {
      const response = await fetch(apiUrl("/api/v1/jobs/start"), { method: "POST", body: form });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "分析失败");
      const pendingJob = payload as Job;
      const stream = new EventSource(apiUrl(`/api/v1/jobs/${pendingJob.id}/events`));
      planningStreamRef.current = stream;
      stream.onmessage = async (event) => {
        const update = JSON.parse(event.data) as PlanningProgressEvent;
        setProgressEvents((current) => {
          const withoutStage = current.filter((item) => item.stage !== update.stage);
          return [...withoutStage, update].sort(
            (left, right) => PLANNING_STAGE_ORDER.indexOf(left.stage) - PLANNING_STAGE_ORDER.indexOf(right.stage),
          );
        });
        if (update.stage === "error") {
          stream.close();
          planningStreamRef.current = null;
          setError(update.message);
          setBusy(false);
          return;
        }
        if (update.stage === "completed") {
          stream.close();
          planningStreamRef.current = null;
          try {
            const jobResponse = await fetch(apiUrl(`/api/v1/jobs/${pendingJob.id}`));
            const completedJob = await jobResponse.json();
            if (!jobResponse.ok || completedJob.status !== "completed") {
              throw new Error(completedJob.detail || completedJob.error || "无法加载生成结果");
            }
            setStepFile(null);
            onCreated(completedJob as Job);
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : "无法加载生成结果");
            setBusy(false);
          }
        }
      };
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法连接分析服务");
      setBusy(false);
    }
  };

  if (!open) return null;

  return (
    <div className="new-job-backdrop" onMouseDown={() => canClose && !busy && onClose()}>
      <section className="new-job-dialog" role="dialog" aria-modal="true" aria-labelledby="new-job-title" onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <div><span className="dialog-icon"><FileUp size={18} /></span><div><strong id="new-job-title">新建工艺任务</strong><small>上传三维模型，自动识别特征并规划工序</small></div></div>
          {canClose && <button aria-label="关闭新建任务" disabled={busy} onClick={onClose}><X size={17} /></button>}
        </header>
        {busy ? <div className="planning-progress" aria-live="polite">
          <div className="planning-progress-head">
            <div><LoaderCircle className="spin" size={18} /><strong>{progressEvents.at(-1)?.message ?? "正在分析并生成工艺"}</strong></div>
            <span>{Math.round(progressEvents.at(-1)?.percent ?? 0)}%</span>
          </div>
          <div className="planning-progress-track"><i style={{ width: `${progressEvents.at(-1)?.percent ?? 0}%` }} /></div>
          <div className="planning-progress-stages">
            {progressEvents.filter((item) => item.stage !== "completed").slice(-5).map((item, index, items) => <div className={index === items.length - 1 ? "active" : "done"} key={item.stage}>
              {index === items.length - 1 ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}
              <span className="planning-stage-copy">
                <span>{item.message}</span>
                {index === items.length - 1 && item.detail && <small className="planning-stage-detail">{item.detail}</small>}
              </span>
              {item.operation_count !== undefined && <small className="planning-stage-meta">{item.setup_count} 次装夹 · {item.operation_count} 道工序</small>}
            </div>)}
          </div>
        </div> : <><div className="upload-pair single-file">
          <button
            className={`drop-zone upload-step ${stepFile ? "has-file" : ""}`}
            onClick={() => stepInputRef.current?.click()}
            onDragOver={(event) => { event.preventDefault(); event.currentTarget.classList.add("is-dragging"); }}
            onDragLeave={(event) => event.currentTarget.classList.remove("is-dragging")}
            onDrop={(event) => {
              event.preventDefault();
              event.currentTarget.classList.remove("is-dragging");
              const candidate = event.dataTransfer.files?.[0];
              if (!candidate) return;
              if (!/\.(step|stp)$/i.test(candidate.name)) { setError("请选择 STEP 或 STP 三维模型"); return; }
              setError("");
              setStepFile(candidate);
            }}
          >
            <input ref={stepInputRef} type="file" accept=".step,.stp" hidden onChange={(event) => { setError(""); setStepFile(event.target.files?.[0] ?? null); }} />
            <span className="upload-type">STEP</span>
            <Box size={28} />
            <strong>{stepFile ? stepFile.name : "三维模型"}</strong>
            <small>{stepFile ? `${(stepFile.size / 1024 / 1024).toFixed(2)} MB · 点击重新选择` : "精确 B-Rep 几何与制造特征"}</small>
          </button>
        </div>
        <section className="new-job-device-picker" aria-label="选择加工设备">
          <div><strong>加工设备</strong><small>工艺将按照所选设备能力生成</small></div>
          <select aria-label="加工设备" value={selectedNewJobDeviceId} onChange={(event) => setSelectedNewJobDeviceId(event.target.value)}>
            {newJobDevices.length === 0 && <option value="">正在加载设备…</option>}
            {newJobDevices.map((device) => <option key={device.id} value={device.id}>{device.display_name} · {device.category_label}</option>)}
          </select>
        </section>
        {error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}
        <button className="primary-action" disabled={!stepFile || !selectedNewJobDeviceId || busy} onClick={submit}>
          分析并生成工艺 <ChevronRight size={17} />
        </button>
        </>}
        {busy && error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}
      </section>
    </div>
  );
}

type JobHistoryItem = {
  id: string;
  status: "processing" | "completed" | "failed";
  filename: string;
  created_at: string;
  material: string;
  machine: string;
  process_kind: "subtractive" | "sheet_forming" | null;
  setup_count: number;
  operation_count: number;
};

function HistoryDialog({ activeJobId, onClose, onSelected }: { activeJobId?: string; onClose: () => void; onSelected: (job: Job) => void }) {
  const [items, setItems] = useState<JobHistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [openingId, setOpeningId] = useState("");
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(apiUrl("/api/v1/jobs"))
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "历史记录加载失败");
        const history = payload as JobHistoryItem[];
        setItems(activeJobId ? history.filter((item) => item.id !== activeJobId) : history);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "历史记录加载失败"))
      .finally(() => setLoading(false));
  }, [activeJobId]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const openHistoryJob = async (item: JobHistoryItem) => {
    setOpeningId(item.id);
    setError("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${item.id}`));
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "任务加载失败");
      onSelected(payload as Job);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务加载失败");
    } finally {
      setOpeningId("");
    }
  };

  const clearHistory = async () => {
    const confirmation = activeJobId
      ? `确定清空 ${items.length} 条历史记录吗？当前打开的任务会保留。`
      : `确定清空全部 ${items.length} 条历史记录吗？`;
    if (!items.length || !window.confirm(confirmation)) return;
    setClearing(true);
    setError("");
    try {
      const query = activeJobId ? `?preserve_job_id=${encodeURIComponent(activeJobId)}` : "";
      const response = await fetch(apiUrl(`/api/v1/jobs${query}`), { method: "DELETE" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "历史记录清空失败");
      setItems([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史记录清空失败");
    } finally {
      setClearing(false);
    }
  };

  return <div className="history-backdrop" onMouseDown={onClose}>
    <section className="history-dialog" role="dialog" aria-modal="true" aria-labelledby="history-title" onMouseDown={(event) => event.stopPropagation()}>
      <header>
        <div><History size={17} /><strong id="history-title">历史记录</strong></div>
        <div className="history-header-actions">
          <button className="history-clear" disabled={loading || clearing || items.length === 0} onClick={clearHistory}>
            {clearing ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}清空历史记录
          </button>
          <button aria-label="关闭历史记录" onClick={onClose}><X size={16} /></button>
        </div>
      </header>
      <div className="history-list">
        {loading && <p className="history-empty"><LoaderCircle className="spin" size={17} />正在加载历史任务…</p>}
        {!loading && error && <p className="history-error"><AlertTriangle size={16} />{error}</p>}
        {!loading && !error && items.length === 0 && <p className="history-empty">暂无历史任务</p>}
        {!loading && items.map((item) => <button key={item.id} disabled={Boolean(openingId)} onClick={() => openHistoryJob(item)}>
          <span className={`history-status ${item.status}`} />
          <div><strong>{item.filename}</strong><small>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</small></div>
          <div><span>{item.setup_count} 次装夹 · {item.operation_count} 道工序</span><small>{item.material} · {item.machine}</small></div>
          {openingId === item.id ? <LoaderCircle className="spin" size={15} /> : <ChevronRight size={15} />}
        </button>)}
      </div>
    </section>
  </div>;
}

function Workbench({ initialJob, onNew, onHistory, readOnly = false }: { initialJob: Job; onNew: () => void; onHistory: () => void; readOnly?: boolean }) {
  const [job, setJob] = useState(initialJob);
  const jobSnapshotRef = useRef(JSON.stringify(initialJob));
  const operationPopoverRef = useRef<HTMLElement>(null);
  const [selectedOperation, setSelectedOperation] = useState<Operation | null>(job.plan?.setups[0]?.operations[0] ?? null);
  const [selectedFeatureIds, setSelectedFeatureIds] = useState<string[]>(selectedOperation?.feature_ids ?? []);
  const [activeMode, setActiveMode] = useState("工艺");
  const [generatingCam, setGeneratingCam] = useState(false);
  const [loadingCam, setLoadingCam] = useState(readOnly);
  const [camResult, setCamResult] = useState<CamResult | null>(null);
  const [, setCamError] = useState("");
  const [camProgress, setCamProgress] = useState<{
    stage: string;
    message: string;
    percent: number;
    setup_id?: string;
    operation_id?: string;
    current?: number;
    total?: number;
  } | null>(null);
  const [clearance, setClearance] = useState(job.plan?.safety?.clearance_mm ?? 3);
  const [viseGripHeight, setViseGripHeight] = useState(job.plan?.safety?.vise_grip_height_mm ?? 1.5);
  const [supportThickness, setSupportThickness] = useState(job.plan?.safety?.support_thickness_mm ?? 3);
  const [safetyMessage, setSafetyMessage] = useState("");
  const [showSimulationChecks, setShowSimulationChecks] = useState(false);
  const [applyingRemediation, setApplyingRemediation] = useState(false);
  const [inspectionPanel, setInspectionPanel] = useState<"tools" | null>(null);
  const inspectionPanelRef = useRef<HTMLElement>(null);
  const [catalogs, setCatalogs] = useState<Catalogs | null>(null);
  const [deviceLibrary, setDeviceLibrary] = useState<DeviceLibrary | null>(null);
  const [showResourceLibrary, setShowResourceLibrary] = useState(false);
  const [selectedLibraryDeviceId, setSelectedLibraryDeviceId] = useState("");
  const [deviceInfoId, setDeviceInfoId] = useState<string | null>(null);
  const [operationMessage, setOperationMessage] = useState("");
  const [operationBusy, setOperationBusy] = useState(false);
  const [solidBusy, setSolidBusy] = useState(false);
  const [parameterEdits, setParameterEdits] = useState<Record<string, Record<string, string | number | boolean>>>({});
  const [structureExpanded, setStructureExpanded] = useState(true);
  const [showOperationDetails, setShowOperationDetails] = useState(false);
  const [editingOperationDetails, setEditingOperationDetails] = useState(false);
  const [toolDraftId, setToolDraftId] = useState(selectedOperation?.tool.id ?? "");
  const [operationPopoverPosition, setOperationPopoverPosition] = useState({ top: 110, left: 326, anchorY: 28 });
  const [playbackMode, setPlaybackMode] = useState<"single" | "cumulative">("single");
  const [playbackResetToken, setPlaybackResetToken] = useState(0);
  const [showL32Workbench, setShowL32Workbench] = useState(false);
  const [showL32Program, setShowL32Program] = useState(false);
  const [showProcessDesigner, setShowProcessDesigner] = useState(false);
  const [l32Program, setL32Program] = useState<WholePartDraftResult | null>(null);
  const [l32Rotational, setL32Rotational] = useState<RotationalFeatureAnalysis | null>(null);
  const [l32OperationPreview, setL32OperationPreview] = useState<L32TurningPreview | null>(null);
  const [l32TurningPreviews, setL32TurningPreviews] = useState<Record<string, L32TurningPreview>>({});
  const [l32GroovePreviews, setL32GroovePreviews] = useState<Record<string, L32GroovePreview>>({});
  const [l32MillingPreviews, setL32MillingPreviews] = useState<Record<string, ToolpathSegment[]>>({});
  const [l32GrooveSegments, setL32GrooveSegments] = useState<Record<string, ToolpathSegment[]>>({});
  const [l32MaterialSnapshots, setL32MaterialSnapshots] = useState<{ key: string; files: Record<string, string[]> } | null>(null);
  const [l32MaterialSnapshotError, setL32MaterialSnapshotError] = useState<{ key: string; message: string } | null>(null);
  const [l32MillingPreview, setL32MillingPreview] = useState<{
    operationId: string;
    segments: ToolpathSegment[];
  } | null>(null);
  const [previewingL32OperationId, setPreviewingL32OperationId] = useState<string | null>(null);
  const l32PreviewRequestRef = useRef(0);
  const l32PreviewResponseCacheRef = useRef(new Map<string, Promise<CachedPreviewResponse<unknown>>>());
  const l32PrewarmedOperationIdsRef = useRef(new Set<string>());
  const [loadingL32Program, setLoadingL32Program] = useState(initialJob.device_id === "citizen-cincom-l32");
  const [warmingL32Previews, setWarmingL32Previews] = useState(false);

  const fetchL32PreviewJson = useCallback(<T,>(
    key: string,
    path: string,
    init?: RequestInit,
  ): Promise<CachedPreviewResponse<T>> => {
    const existing = l32PreviewResponseCacheRef.current.get(key);
    if (existing) return existing as Promise<CachedPreviewResponse<T>>;
    const request = fetch(apiUrl(path), init).then(async (response) => ({
      ok: response.ok,
      status: response.status,
      payload: await response.json() as T,
    }));
    l32PreviewResponseCacheRef.current.set(key, request as Promise<CachedPreviewResponse<unknown>>);
    void request.then((result) => {
      if (!result.ok) l32PreviewResponseCacheRef.current.delete(key);
    }, () => l32PreviewResponseCacheRef.current.delete(key));
    return request;
  }, []);

  useEffect(() => {
    jobSnapshotRef.current = JSON.stringify(job);
  }, [job]);

  useEffect(() => {
    let cancelled = false;
    let refreshing = false;
    const refreshJob = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        const response = await fetch(apiUrl(`/api/v1/jobs/${initialJob.id}`), { cache: "no-store" });
        if (!response.ok || cancelled) return;
        const refreshed = await response.json() as Job;
        if (cancelled) return;
        const refreshedSnapshot = JSON.stringify(refreshed);
        if (refreshedSnapshot === jobSnapshotRef.current) return;
        jobSnapshotRef.current = refreshedSnapshot;
        setJob(refreshed);
        const refreshedOperations = refreshed.plan?.setups.flatMap((setup) => setup.operations) ?? [];
        setSelectedOperation((current) => (
          refreshedOperations.find((operation) => operation.id === current?.id)
          ?? refreshedOperations[0]
          ?? null
        ));
      } catch {
        // Keep the already loaded task visible; the next focus/visibility
        // event retries synchronization.
      } finally {
        refreshing = false;
      }
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshJob();
    };
    // `initialJob` is already the complete payload returned by task creation
    // or history selection. Fetching it again immediately replaces all nested
    // geometry arrays with new references and forces ModelViewer to tear down
    // and reload the same STL, which produces several visible flashes. Keep
    // synchronization for later focus/visibility changes only.
    window.addEventListener("focus", refreshJob);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", refreshJob);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [initialJob.id]);

  const operations = useMemo(
    () => job.plan?.setups.flatMap((setup) => setup.operations) ?? [],
    [job.plan?.setups],
  );
  const operationTools = useMemo(
    () => Object.fromEntries(operations.map((operation) => [operation.id, {
      name: operation.name,
      tool_name: operation.tool.name,
      diameter_mm: operation.tool.diameter_mm,
      stickout_mm: operation.tool.stickout_mm,
      holder_diameter_mm: operation.tool.holder_diameter_mm,
      kind: operation.tool.kind,
      drill_point_angle_deg: Number(operation.parameters.drill_point_angle_deg ?? 118),
      spindle_rpm: Number(operation.parameters.spindle_rpm ?? 0),
      feed_rate_mm_min: Number(operation.parameters.feed_rate_mm_min ?? 0),
    }])),
    [operations],
  );
  const spatialDefects = useMemo(() => {
    const regions = camResult?.verification.metrics.defect_regions ?? [];
    const samples = camResult?.verification.metrics.defect_samples ?? [];
    return regions.length ? { regions, samples } : null;
  }, [camResult]);
  const isSheetForming = job.plan?.process_kind === "sheet_forming";
  const isL32 = job.device_id === "citizen-cincom-l32";

  useEffect(() => {
    if (!isL32) return undefined;
    let cancelled = false;
    Promise.all([
      fetch(apiUrl(`/api/v1/jobs/${job.id}/turning/analyze`), { method: "POST" })
        .then(async (response) => {
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || "无法读取回转轴");
          return payload as RotationalFeatureAnalysis;
        }),
      fetch(apiUrl(`/api/v1/jobs/${job.id}/files/turning-whole-program-draft.json`))
        .then(async (response) => {
          if (response.status === 404) return null;
          if (!response.ok) throw new Error("无法读取整件刀路");
          return response.json() as Promise<WholePartDraftResult>;
        }),
    ])
      .then(([rotational, program]) => {
        if (cancelled) return;
        setL32Rotational(rotational);
        setL32Program(program);
      })
      .catch((error: Error) => {
        if (!cancelled) setOperationMessage(error.message);
      })
      .finally(() => {
        if (!cancelled) setLoadingL32Program(false);
      });
    return () => { cancelled = true; };
  }, [isL32, job.id]);
  const automationBlocked = job.plan?.automation_status === "unsupported";
  const holes = useMemo(
    () => job.analysis?.cylindrical_features.filter((item) => item.kind === "hole" && item.review_state !== "excluded") ?? [],
    [job.analysis?.cylindrical_features],
  );
  const prismaticFeatures = useMemo(
    () => job.analysis?.prismatic_features?.filter((item) => item.review_state !== "excluded") ?? [],
    [job.analysis?.prismatic_features],
  );
  const internalProfiles = useMemo(
    () => job.analysis?.internal_profile_features?.filter((item) => item.review_state !== "excluded") ?? [],
    [job.analysis?.internal_profile_features],
  );
  const manufacturingFeatures = useMemo<ManufacturingFeature[]>(
    () => [...holes, ...prismaticFeatures, ...internalProfiles],
    [holes, internalProfiles, prismaticFeatures],
  );
  const excludedCount = job.analysis?.cylindrical_features.filter((item) => item.kind === "hole" && item.review_state === "excluded").length ?? 0;
  const prismaticExcludedCount = job.analysis?.prismatic_features?.filter((item) => item.review_state === "excluded").length ?? 0;
  const internalProfileExcludedCount = job.analysis?.internal_profile_features?.filter((item) => item.review_state === "excluded").length ?? 0;
  const reviewCount = manufacturingFeatures.filter((item) => item.review_state === "review").length;
  const firstReviewFeature = manufacturingFeatures.find((item) => item.review_state === "review");
  const coverage = job.plan?.coverage;
  const profileAxialComplete = job.plan?.stock.profile_axial_complete;
  const l32WholePartBlockers = useMemo(() => {
    if (!isL32) return [];
    const reasons: string[] = [];
    const incompleteTargets = coverage?.targets.filter((target) => target.state !== "covered") ?? [];
    for (const target of incompleteTargets) {
      if (target.kind === "pocket") reasons.push(target.covered_by.length
        ? `${target.label} 已规划粗铣/精铣，尚有尖角残料需要后续工艺处理`
        : `${target.label} 尚无型腔粗加工和精加工工序`);
      else if (target.id.startsWith("TARGET-NONROTATIONAL-OUTER-")) reasons.push(target.covered_by.length
        ? `${target.label} 已规划动力刀具粗铣/精铣，整件材料状态尚待连续扫掠验证`
        : `${target.label} 尚无动力刀具轮廓粗铣、精铣和材料验证`);
      else if (target.id.startsWith("TARGET-RP-") && profileAxialComplete === false) reasons.push(`${target.label} 只识别到局部轴向轮廓，OP20/OP30 已关联但不能代表整段外形完成`);
      else reasons.push(`${target.label} 尚未被完整工序覆盖`);
    }
    const aspectRatio = Number(l32Rotational?.evidence.transverse_aspect_ratio ?? 1);
    if (aspectRatio < 0.9) {
      reasons.push(`零件横截面长宽比 ${aspectRatio.toFixed(3)}，不是完整轴对称实体`);
    }
    const acceptedProfile = l32Rotational?.profiles.find(
      (profile) => profile.side === "outer" && profile.review_state === "accepted",
    );
    const sourceAxis = l32Rotational?.axes.find((axis) => axis.id === acceptedProfile?.axis_id);
    if (l32Rotational && sourceAxis?.review_state !== "accepted") {
      reasons.push("回转轴尚未随外轮廓完成确认");
    }
    return reasons;
  }, [coverage?.targets, isL32, profileAxialComplete, l32Rotational]);
  const l32WholePartBlocked = l32WholePartBlockers.length > 0;
  const sourceSolids = Number(job.analysis?.topology.source_solids ?? job.analysis?.topology.solids ?? 1);
  const solidCandidates = job.analysis?.solid_candidates ?? [];
  const selectedSolidIndex = Number(job.analysis?.topology.selected_solid_index ?? solidCandidates.find((item) => item.selected)?.index ?? 1);
  const requestedProgress = Number(new URLSearchParams(window.location.search).get("progress") ?? 0);
  const initialSimulationProgress = Number.isFinite(requestedProgress) ? Math.min(1, Math.max(0, requestedProgress)) : 0;
  const validationStatus = !camResult
    ? null
    : camResult.collision.status === "failed" || camResult.verification.status === "failed"
      ? "failed"
      : camResult.verification.status === "warning"
        ? "warning"
        : "passed";
  const cutOperationIds = useMemo(
    () => new Set(isSheetForming
      ? camResult?.generated_operations ?? []
      : camResult?.preview_segments.filter((segment) => segment.motion === "cut").map((segment) => segment.operation_id) ?? []),
    [camResult?.generated_operations, camResult?.preview_segments, isSheetForming],
  );
  useEffect(() => {
    if (!showOperationDetails) return undefined;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!operationPopoverRef.current?.contains(event.target as Node)) {
        setEditingOperationDetails(false);
        setShowOperationDetails(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setEditingOperationDetails(false);
        setShowOperationDetails(false);
      }
    };
    window.addEventListener("pointerdown", closeOnOutsideClick);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("pointerdown", closeOnOutsideClick);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [showOperationDetails]);

  useEffect(() => {
    if (!inspectionPanel) return undefined;
    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!inspectionPanelRef.current?.contains(event.target as Node)) setInspectionPanel(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => event.key === "Escape" && setInspectionPanel(null);
    window.addEventListener("pointerdown", closeOnOutsideClick);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("pointerdown", closeOnOutsideClick);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [inspectionPanel]);

  const selectedFeatures = useMemo(
    () => manufacturingFeatures.filter((feature) => selectedFeatureIds.includes(feature.id)),
    [manufacturingFeatures, selectedFeatureIds],
  );
  const viewerFeatures = useMemo(
    () => activeMode === "特征" ? manufacturingFeatures : activeMode === "工艺" ? selectedFeatures : [],
    [activeMode, manufacturingFeatures, selectedFeatures],
  );
  const selectedSetup = useMemo(
    () => job.plan?.setups.find((setup) => setup.operations.some((operation) => operation.id === selectedOperation?.id)) ?? job.plan?.setups[0],
    [job.plan?.setups, selectedOperation?.id],
  );
  const selectedDefinition = useMemo(
    () => catalogs?.operations.find((item) => item.id === (selectedOperation?.definition_id || selectedOperation?.type)) ?? null,
    [catalogs?.operations, selectedOperation?.definition_id, selectedOperation?.type],
  );
  const displayedTool = editingOperationDetails
    ? catalogs?.tools.find((tool) => tool.id === toolDraftId) ?? selectedOperation?.tool
    : selectedOperation?.tool;
  const parameterDraft = selectedOperation
    ? parameterEdits[selectedOperation.id] ?? selectedOperation.parameters
    : {};
  useEffect(() => {
    fetch(apiUrl("/api/v1/catalogs"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: Catalogs) => setCatalogs(payload))
      .catch(() => setOperationMessage("工序库暂时无法加载"));
  }, []);

  useEffect(() => {
    fetch(apiUrl("/api/v1/device-library"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: DeviceLibrary) => {
        const devices = l32First(payload.devices);
        setDeviceLibrary({ ...payload, devices });
        setSelectedLibraryDeviceId((current) => current || devices[0]?.id || "");
      })
      .catch(() => setDeviceLibrary({ schema_version: "1.0.0", devices: [] }));
  }, []);

  useEffect(() => {
    if (!showResourceLibrary) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setShowResourceLibrary(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [showResourceLibrary]);

  useEffect(() => {
    if (!deviceInfoId) return undefined;
    const closeDeviceInfo = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element) || !target.closest(".device-info-popover, .operation-device-info")) setDeviceInfoId(null);
    };
    window.addEventListener("click", closeDeviceInfo);
    return () => window.removeEventListener("click", closeDeviceInfo);
  }, [deviceInfoId]);

  const l32Axis = useMemo(() => {
    if (!l32Rotational) return null;
    const acceptedProfile = l32Rotational.profiles.find(
      (profile) => profile.side === "outer" && profile.review_state === "accepted",
    ) ?? l32Rotational.profiles.find((profile) => profile.side === "outer");
    return l32Rotational.axes.find((axis) => axis.id === acceptedProfile?.axis_id)
      ?? l32Rotational.axes[0]
      ?? null;
  }, [l32Rotational]);

  useEffect(() => {
    if (!isL32 || !job.machine_instance_id || !l32Rotational) return undefined;
    let cancelled = false;
    const warmOperation = async (operation: Operation) => {
      if (operation.enabled === false || cancelled) return;
      let results: CachedPreviewResponse<unknown>[] = [];
      if (operation.type === "turn_grooving") {
        const grooveResults = await Promise.all([
          fetchL32PreviewJson<{
            grooves: Array<{
              operation_id: string;
              draft: {
                stock_radius_mm: number;
                strips: Array<{ z_min_mm: number; z_max_mm: number; cut_to_radius_mm: number }>;
              };
            }>;
          }>(`${job.id}:front-groove-geometry`, `/api/v1/jobs/${job.id}/l32/front-groove-geometry`),
          fetchL32PreviewJson(`${job.id}:front-groove-sweep`, `/api/v1/jobs/${job.id}/l32/front-groove-sweep-check`),
        ]);
        results = grooveResults;
        const groove = grooveResults[0].payload.grooves.find((item) => item.operation_id === operation.id);
        if (!cancelled && grooveResults.every((result) => result.ok) && groove) {
          const preview = {
            operationId: operation.id,
            stockRadius: groove.draft.stock_radius_mm,
            strips: groove.draft.strips,
          };
          setL32GroovePreviews((current) => ({
            ...current,
            [operation.id]: preview,
          }));
          if (l32Axis) setL32GrooveSegments((current) => ({ ...current, [operation.id]: l32GroovePreviewToSegments(preview, l32Axis) }));
        }
      } else if (operation.type === "pocket_roughing" || operation.type === "pocket_finishing") {
        const featureId = operation.feature_ids.find((id) => id.startsWith("MF-"));
        if (!featureId) return;
        const pocketResults = await Promise.all([
          fetchL32PreviewJson<L32GeometricDraft>(`${job.id}:pocket:${featureId}:draft`, `/api/v1/jobs/${job.id}/l32/catalog-back-pocket/${featureId}/draft`),
          fetchL32PreviewJson(`${job.id}:pocket:${featureId}:sweep`, `/api/v1/jobs/${job.id}/l32/catalog-back-pocket/${featureId}/sweep-check`),
        ]);
        results = pocketResults;
        if (!cancelled && pocketResults.every((result) => result.ok)) {
          setL32MillingPreviews((current) => ({
            ...current,
            [operation.id]: l32DraftsToSegments(operation, [pocketResults[0].payload]),
          }));
        }
      } else if (operation.type === "live_tool_contour_roughing" || operation.type === "live_tool_contour_finishing") {
        const roughing = operation.type === "live_tool_contour_roughing";
        const draftPath = roughing ? "catalog-exterior-toolpaths" : "catalog-ear-toolpaths";
        const checkPath = roughing ? "catalog-exterior-sweep-check" : "catalog-ear-sweep-check";
        const millingResults = await Promise.all([
          fetchL32PreviewJson<{ exterior_drafts?: L32GeometricDraft[]; side_drafts?: L32GeometricDraft[] }>(`${job.id}:${draftPath}`, `/api/v1/jobs/${job.id}/l32/${draftPath}`),
          fetchL32PreviewJson(`${job.id}:${checkPath}`, `/api/v1/jobs/${job.id}/l32/${checkPath}`),
        ]);
        results = millingResults;
        if (!cancelled && millingResults.every((result) => result.ok)) {
          const drafts = millingResults[0].payload.exterior_drafts ?? millingResults[0].payload.side_drafts ?? [];
          setL32MillingPreviews((current) => ({
            ...current,
            [operation.id]: l32DraftsToSegments(operation, drafts),
          }));
        }
      } else if (operation.channel_id !== "sub") {
        const profile = l32Rotational.profiles.find((item) => operation.feature_ids.includes(item.id));
        if (!profile || profile.review_state !== "accepted") return;
        const zValues = profile.points.map((point) => point.z);
        const cutoffOperation = operations.find((item) => item.type === "turn_cutoff");
        const cutoffZ = Number(cutoffOperation?.parameters.finished_back_datum_z_mm ?? cutoffOperation?.parameters.z_mm);
        const stockZMin = Number.isFinite(cutoffZ)
          ? Math.min(Math.min(...zValues) - 2, cutoffZ - 0.5)
          : Math.min(...zValues) - 2;
        const stockRadius = Number(job.plan?.stock.diameter_mm ?? 0) / 2
          || Math.max(...profile.points.map((point) => point.radius));
        const turningResult = await fetchL32PreviewJson<TurningDraftResult>(
          `${job.id}:turning:${operation.id}:${JSON.stringify(operation)}`,
          `/api/v1/jobs/${job.id}/turning/draft`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              machine_instance_id: job.machine_instance_id,
              operation,
              profile,
              stock_radius_mm: stockRadius,
              initial_bore_radius_mm: 0,
              z_min_mm: stockZMin,
              z_max_mm: Math.max(...zValues) + 2,
              resolution_mm: 0.1,
            }),
          },
        );
        results = [turningResult];
        if (!cancelled && turningResult.ok) {
          const preview: L32TurningPreview = {
            operationId: operation.id,
            channelId: "main",
            sourceCutoffZ: 0,
            stockRadius,
            draft: turningResult.payload,
          };
          setL32TurningPreviews((current) => ({ ...current, [operation.id]: preview }));
        }
      }
      if (!cancelled && results.length > 0 && results.every((result) => result.ok)) {
        l32PrewarmedOperationIdsRef.current.add(operation.id);
      }
    };
    void (async () => {
      setWarmingL32Previews(true);
      let nextIndex = 0;
      const worker = async () => {
        while (!cancelled) {
          const operation = operations[nextIndex++];
          if (!operation) return;
          try {
            await warmOperation(operation);
          } catch {
            // A failed precomputation is retried when the operation is opened.
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(3, operations.length) }, worker));
      if (!cancelled) setWarmingL32Previews(false);
    })();
    return () => { cancelled = true; };
  }, [fetchL32PreviewJson, isL32, job.id, job.machine_configuration_hash, job.machine_instance_id, job.plan?.stock.diameter_mm, l32Axis, l32Rotational, operations]);

  useEffect(() => {
    // The endpoint consumes rotational-features.json. Wait for the analysis
    // request above to finish so initial page load cannot race its file write.
    if (!isL32 || !job.plan || !l32Rotational) return undefined;
    let cancelled = false;
    const requestKey = `${job.id}:${job.machine_configuration_hash ?? "unbound"}:${JSON.stringify(job.plan.setups)}`;
    let request = l32MaterialManifestRequests.get(requestKey);
    if (!request) {
      request = fetch(apiUrl(`/api/v1/jobs/${job.id}/l32/material-snapshots`), { cache: "no-store" }).then(async (response) => {
        if (!response.ok) throw new Error("material snapshots unavailable");
        return response.json() as Promise<L32MaterialSnapshotManifest>;
      });
      l32MaterialManifestRequests.set(requestKey, request);
      void request.catch(() => l32MaterialManifestRequests.delete(requestKey));
    }
    void request
      .then((manifest) => {
        if (cancelled) return;
        setL32MaterialSnapshots({ key: requestKey, files: Object.fromEntries(manifest.operations.map((operation) => [
          operation.operation_id,
          operation.files.map((file) => apiUrl(`/api/v1/jobs/${job.id}/files/${encodeURIComponent(file)}`)),
        ])) });
        setL32MaterialSnapshotError(null);
      })
      .catch(() => {
        if (!cancelled) setL32MaterialSnapshotError({ key: requestKey, message: "实体材料逐帧预生成失败；当前零件显示不代表该工序的材料去除结果。" });
      });
    return () => { cancelled = true; };
  }, [isL32, job.id, job.machine_configuration_hash, job.plan, l32Rotational]);

  const l32ToolpathSegments = useMemo<ToolpathSegment[]>(() => {
    if (!l32Program || !l32Axis) return EMPTY_TOOLPATH_SEGMENTS;
    const axisLength = Math.hypot(l32Axis.direction.x, l32Axis.direction.y, l32Axis.direction.z) || 1;
    const axis = {
      x: l32Axis.direction.x / axisLength,
      y: l32Axis.direction.y / axisLength,
      z: l32Axis.direction.z / axisLength,
    };
    const helper = Math.abs(axis.z) < 0.9 ? { x: 0, y: 0, z: 1 } : { x: 0, y: 1, z: 0 };
    const cross = {
      x: axis.y * helper.z - axis.z * helper.y,
      y: axis.z * helper.x - axis.x * helper.z,
      z: axis.x * helper.y - axis.y * helper.x,
    };
    const crossLength = Math.hypot(cross.x, cross.y, cross.z) || 1;
    const radial = { x: cross.x / crossLength, y: cross.y / crossLength, z: cross.z / crossLength };
    const cutoffZ = l32Program.coordinate_frames.find((frame) => frame.channel_id === "sub")?.source_cutoff_z_mm ?? 0;
    const toMainZ = (channelId: string, z: number) => channelId === "sub" ? cutoffZ - z : z;
    const point = (channelId: string, x: number, z: number) => {
      const axial = toMainZ(channelId, z);
      const radius = Math.abs(x) / 2;
      return {
        x: l32Axis.origin.x + axis.x * axial + radial.x * radius,
        y: l32Axis.origin.y + axis.y * axial + radial.y * radius,
        z: l32Axis.origin.z + axis.z * axial + radial.z * radius,
      };
    };
    const segments: ToolpathSegment[] = [];
    for (const channel of l32Program.toolpath.channels) {
      const position: Record<string, number> = {};
      for (const command of channel.commands) {
        const previous = { ...position };
        for (const [name, value] of Object.entries(command.axes)) position[name.toUpperCase()] = value;
        if (["rapid_move", "feed_move", "arc_move"].includes(command.type)
          && previous.X !== undefined && previous.Z !== undefined
          && position.X !== undefined && position.Z !== undefined) {
          const start = point(channel.id, previous.X, previous.Z);
          const end = point(channel.id, position.X, position.Z);
          if (Math.hypot(end.x - start.x, end.y - start.y, end.z - start.z) > 1e-6) {
            segments.push({
              operation_id: command.operation_id,
              motion: command.type === "rapid_move" ? "rapid" : "cut",
              x1: start.x, y1: start.y, z1: start.z,
              x2: end.x, y2: end.y, z2: end.z,
              setup_id: channel.id,
              work_axis: radial,
            });
          }
        }
        if (["thread_cut", "drill_cycle", "tap_cycle"].includes(command.type)) {
          const parameters = command.parameters ?? {};
          const startZ = Number(parameters.start_z_mm);
          const endZ = Number(parameters.end_z_mm);
          if (!Number.isFinite(startZ) || !Number.isFinite(endZ)) continue;
          const diameter = command.type === "thread_cut"
            ? Number(parameters.target_diameter_mm ?? parameters.minor_diameter_mm ?? position.X ?? 0)
            : 0;
          const start = point(channel.id, diameter, startZ);
          const end = point(channel.id, diameter, endZ);
          segments.push({
            operation_id: command.operation_id,
            motion: "cut",
            x1: start.x, y1: start.y, z1: start.z,
            x2: end.x, y2: end.y, z2: end.z,
            setup_id: channel.id,
            work_axis: radial,
          });
        }
      }
    }
    return segments;
  }, [l32Axis, l32Program]);

  const l32OperationPreviewSegments = useMemo<ToolpathSegment[]>(() => {
    if (l32MillingPreview?.operationId === selectedOperation?.id) return l32MillingPreview?.segments ?? EMPTY_TOOLPATH_SEGMENTS;
    if (!selectedOperation) return EMPTY_TOOLPATH_SEGMENTS;
    const cachedMilling = l32MillingPreviews[selectedOperation.id];
    if (cachedMilling?.length) return cachedMilling;
    const groove = l32GrooveSegments[selectedOperation.id];
    if (groove?.length) return groove;
    const preview = l32OperationPreview?.operationId === selectedOperation.id
      ? l32OperationPreview : l32TurningPreviews[selectedOperation.id];
    return preview && l32Axis ? l32TurningPreviewToSegments(preview, l32Axis) : EMPTY_TOOLPATH_SEGMENTS;
  }, [l32Axis, l32GrooveSegments, l32MillingPreview, l32MillingPreviews, l32OperationPreview, l32TurningPreviews, selectedOperation]);

  const l32PreviewOperationId = l32OperationPreview?.operationId ?? l32MillingPreview?.operationId;

  const l32AvailableToolpathSegments = useMemo<ToolpathSegment[]>(() => {
    if (!isL32) return EMPTY_TOOLPATH_SEGMENTS;
    return operations.flatMap((operation) => {
      if (operation.enabled === false) return EMPTY_TOOLPATH_SEGMENTS;
      if (operation.id === selectedOperation?.id && l32PreviewOperationId === operation.id && l32OperationPreviewSegments.length) {
        return l32OperationPreviewSegments;
      }
      const wholeProgramSegments = l32ToolpathSegments.filter((segment) => segment.operation_id === operation.id);
      if (wholeProgramSegments.some((segment) => segment.motion === "cut")) return wholeProgramSegments;
      const milling = l32MillingPreviews[operation.id];
      if (milling?.some((segment) => segment.motion === "cut")) return milling;
      const groove = l32GrooveSegments[operation.id];
      if (groove?.some((segment) => segment.motion === "cut")) return groove;
      const turning = l32TurningPreviews[operation.id];
      return turning && l32Axis ? l32TurningPreviewToSegments(turning, l32Axis) : EMPTY_TOOLPATH_SEGMENTS;
    });
  }, [isL32, l32Axis, l32GrooveSegments, l32MillingPreviews, l32OperationPreviewSegments, l32PreviewOperationId, l32ToolpathSegments, l32TurningPreviews, operations, selectedOperation?.id]);

  const visibleTurningStage = useMemo<TurningStageView | null>(() => {
    if (!isL32 || activeMode !== "仿真" || !selectedOperation || !l32Axis) return null;
    const snapshot = !l32WholePartBlocked && l32Program
      ? l32Program.continuous_simulation.stage_snapshots?.find(
          (item) => item.operation_id === selectedOperation.id,
        )
      : null;
    if (snapshot && l32Program) {
      const cutoffZ = l32Program.coordinate_frames.find((frame) => frame.channel_id === "sub")?.source_cutoff_z_mm ?? 0;
      const transformSamples = (samples: typeof snapshot.before_samples) => samples.map((sample) => ({
        ...sample,
        z: snapshot.source_frame === "sub" ? cutoffZ - sample.z : sample.z,
      }));
      return {
        operation_id: snapshot.operation_id,
        channel_id: snapshot.channel_id,
        before_samples: transformSamples(snapshot.before_samples),
        after_samples: transformSamples(snapshot.after_samples),
        axis_origin: l32Axis.origin,
        axis_direction: l32Axis.direction,
      };
    }

    const selectedPreview = l32TurningPreviews[selectedOperation.id]
      ?? (l32OperationPreview?.operationId === selectedOperation.id ? l32OperationPreview : null);
    const selectedGroove = l32GroovePreviews[selectedOperation.id];
    const selectedIndex = operations.findIndex((operation) => operation.id === selectedOperation.id);
    const gridPreview = selectedPreview ?? operations
      .slice(0, Math.max(selectedIndex, 0))
      .reverse()
      .map((operation) => l32TurningPreviews[operation.id])
      .find((preview) => preview?.channelId === "main");
    if (!gridPreview) return null;
    const transformSamples = (preview: L32TurningPreview) => preview.draft.simulation.samples
      .map((sample) => ({
        ...sample,
        z: preview.channelId === "sub" ? preview.sourceCutoffZ - sample.z : sample.z,
      }))
      .sort((left, right) => left.z - right.z);
    const selectedSamples = transformSamples(gridPreview);
    if (!selectedSamples.length) return null;
    const nearestSample = (samples: typeof selectedSamples, z: number) => samples.reduce(
      (nearest, candidate) => Math.abs(candidate.z - z) < Math.abs(nearest.z - z) ? candidate : nearest,
      samples[0],
    );
    let cumulative = selectedSamples.map((sample) => ({
      z: sample.z,
      outer_radius: selectedPreview?.stockRadius ?? selectedGroove?.stockRadius ?? gridPreview.stockRadius,
      inner_radius: 0,
    }));
    const mergeRemoval = (preview: L32TurningPreview) => {
      const samples = transformSamples(preview);
      if (!samples.length) return;
      cumulative = cumulative.map((current) => {
        const machined = nearestSample(samples, current.z);
        return {
          z: current.z,
          outer_radius: Math.min(current.outer_radius, machined.outer_radius),
          inner_radius: Math.max(current.inner_radius, machined.inner_radius),
        };
      });
    };
    for (const operation of operations.slice(0, Math.max(selectedIndex, 0))) {
      const preview = l32TurningPreviews[operation.id];
      if (preview?.channelId === (selectedPreview?.channelId ?? "main")) mergeRemoval(preview);
      const groove = l32GroovePreviews[operation.id];
      if (groove) {
        cumulative = cumulative.map((sample) => {
          const strip = groove.strips.find((item) => sample.z >= item.z_min_mm - 1e-6 && sample.z <= item.z_max_mm + 1e-6);
          return strip ? { ...sample, outer_radius: Math.min(sample.outer_radius, strip.cut_to_radius_mm) } : sample;
        });
      }
    }
    const beforeSamples = cumulative.map((sample) => ({ ...sample }));
    if (selectedOperation.type === "turn_cutoff") {
      const cutoffZ = Number(selectedOperation.parameters.finished_back_datum_z_mm ?? selectedOperation.parameters.z_mm);
      if (Number.isFinite(cutoffZ)) {
        cumulative = cumulative.map((sample) => sample.z < cutoffZ
          ? { ...sample, outer_radius: 0, inner_radius: 0 }
          : sample);
      }
    } else if (selectedPreview) {
      mergeRemoval(selectedPreview);
    } else if (selectedGroove) {
      cumulative = cumulative.map((sample) => {
        const strip = selectedGroove.strips.find((item) => sample.z >= item.z_min_mm - 1e-6 && sample.z <= item.z_max_mm + 1e-6);
        return strip ? { ...sample, outer_radius: Math.min(sample.outer_radius, strip.cut_to_radius_mm) } : sample;
      });
    } else {
      return null;
    }
    return {
      operation_id: selectedOperation.id,
      channel_id: selectedPreview?.channelId ?? "main",
      before_samples: beforeSamples,
      after_samples: cumulative,
      axis_origin: l32Axis.origin,
      axis_direction: l32Axis.direction,
    };
  }, [activeMode, isL32, l32Axis, l32GroovePreviews, l32OperationPreview, l32Program, l32TurningPreviews, l32WholePartBlocked, operations, selectedOperation]);

  const visibleToolpathSegments = useMemo(() => {
    if (isL32 && (activeMode === "刀路" || activeMode === "仿真") && playbackMode === "single" && l32OperationPreviewSegments.some((segment) => segment.motion === "cut")) return l32OperationPreviewSegments;
    if (isL32 && l32WholePartBlocked && activeMode !== "仿真") return EMPTY_TOOLPATH_SEGMENTS;
    if (activeMode === "刀路") return isL32 ? l32ToolpathSegments : camResult?.preview_segments ?? [];
    if (activeMode !== "仿真" || !selectedOperation) return EMPTY_TOOLPATH_SEGMENTS;
    const selectedIndex = operations.findIndex((operation) => operation.id === selectedOperation.id);
    const visibleOperationIds = new Set(
      (playbackMode === "single"
        ? operations.slice(Math.max(selectedIndex, 0), Math.max(selectedIndex, 0) + 1)
        : operations.slice(0, Math.max(selectedIndex, 0) + 1)
      ).filter((operation) => operation.enabled !== false).map((operation) => operation.id),
    );
    const sourceSegments = isL32 ? l32AvailableToolpathSegments : camResult?.preview_segments ?? EMPTY_TOOLPATH_SEGMENTS;
    const operationOrder = new Map(operations.map((operation, index) => [operation.id, index]));
    const visibleSegments = sourceSegments.filter(
      (segment) => visibleOperationIds.has(segment.operation_id),
    ).sort((left, right) => (operationOrder.get(left.operation_id) ?? 0) - (operationOrder.get(right.operation_id) ?? 0));
    return visibleSegments.some((segment) => segment.motion === "cut") ? visibleSegments : EMPTY_TOOLPATH_SEGMENTS;
  },
    [activeMode, camResult?.preview_segments, isL32, l32AvailableToolpathSegments, l32OperationPreviewSegments, l32ToolpathSegments, l32WholePartBlocked, operations, playbackMode, selectedOperation],
  );
  const l32MaterialRequestKey = `${job.id}:${job.machine_configuration_hash ?? "unbound"}:${JSON.stringify(job.plan?.setups ?? [])}`;
  const visibleMaterialSnapshots = useMemo(() => {
    if (!isL32 || activeMode !== "仿真" || !selectedOperation || l32MaterialSnapshots?.key !== l32MaterialRequestKey) {
      return EMPTY_MATERIAL_SNAPSHOTS;
    }
    const urls: string[] = [];
    const stages: { operationId: string; start: number; count: number }[] = [];
    const selectedIndex = operations.findIndex((operation) => operation.id === selectedOperation.id);
    const included = playbackMode === "single"
      ? [selectedOperation]
      : operations.slice(0, selectedIndex + 1).filter((operation) => operation.enabled !== false);
    for (const operation of included) {
      const files = l32MaterialSnapshots.files[operation.id] ?? [];
      stages.push({ operationId: operation.id, start: urls.length, count: files.length });
      urls.push(...files);
    }
    return { urls, stages };
  }, [activeMode, isL32, l32MaterialRequestKey, l32MaterialSnapshots, operations, playbackMode, selectedOperation]);
  const missingMaterialStages = visibleMaterialSnapshots.stages
    .filter((stage) => stage.count === 0 && visibleToolpathSegments.some((segment) => segment.operation_id === stage.operationId && segment.motion === "cut"))
    .map((stage) => stage.operationId);
  const awaitingL32MaterialSnapshots = isL32 && activeMode === "仿真"
    && selectedOperation != null
    && selectedOperation.enabled !== false
    && ["turn_facing", "turn_od_roughing", "turn_od_finishing", "turn_grooving", "turn_cutoff", "live_tool_contour_roughing", "live_tool_contour_finishing", "pocket_roughing", "pocket_finishing"].includes(selectedOperation.type)
    && (l32MaterialSnapshots?.key !== l32MaterialRequestKey || !(l32MaterialSnapshots.files[selectedOperation.id]?.length));
  const initialToolpathSegments = useMemo(() => {
    if (activeMode !== "仿真" || playbackMode !== "single" || !selectedOperation) return EMPTY_TOOLPATH_SEGMENTS;
    const selectedIndex = operations.findIndex((operation) => operation.id === selectedOperation.id);
    const precedingIds = new Set(operations.slice(0, Math.max(selectedIndex, 0)).map((operation) => operation.id));
    return camResult?.preview_segments.filter((segment) => precedingIds.has(segment.operation_id))
      ?? EMPTY_TOOLPATH_SEGMENTS;
  }, [activeMode, camResult?.preview_segments, operations, playbackMode, selectedOperation]);
  const visibleProfileBoundaries = useMemo(
    () => {
      if (activeMode !== "仿真" || !selectedOperation) return EMPTY_PROFILE_BOUNDARIES;
      const selectedIndex = operations.findIndex((operation) => operation.id === selectedOperation.id);
      const cumulativeOperationIds = new Set(
        operations.slice(0, Math.max(selectedIndex, 0) + 1).map((operation) => operation.id),
      );
      return camResult?.profile_boundaries.filter((boundary) => cumulativeOperationIds.has(boundary.operation_id))
        ?? EMPTY_PROFILE_BOUNDARIES;
    },
    [activeMode, camResult?.profile_boundaries, operations, selectedOperation],
  );
  const selectedSetupId = useMemo(
    () => job.plan?.setups.find((setup) => setup.operations.some((operation) => operation.id === selectedOperation?.id))?.id,
    [job.plan?.setups, selectedOperation?.id],
  );
  const selectedLibraryDevice = useMemo(
    () => deviceLibrary?.devices.find((device) => device.id === selectedLibraryDeviceId) ?? deviceLibrary?.devices[0] ?? null,
    [deviceLibrary, selectedLibraryDeviceId],
  );
  const infoLibraryDevice = useMemo(
    () => deviceLibrary?.devices.find((device) => device.id === deviceInfoId) ?? null,
    [deviceInfoId, deviceLibrary],
  );
  const libraryOperations = useMemo(() => {
    if (!selectedLibraryDevice) return [];
    const bindings = selectedLibraryDevice.operation_bindings;
    if (bindings?.length) {
      const bindingIds = new Set(bindings.map((binding) => binding.operation_id));
      return (catalogs?.operations ?? []).filter((operation) => bindingIds.has(operation.id));
    }
    const compatibleGroups = new Set(selectedLibraryDevice.system_integration.compatible_operation_groups);
    return (catalogs?.operations ?? []).filter((operation) =>
      compatibleGroups.has(operation.category)
      || [...compatibleGroups].some((group) => DEVICE_OPERATION_GROUP_ALIASES[group]?.includes(operation.category)
        || DEVICE_OPERATION_GROUP_ALIASES[group]?.includes(operation.id)),
    );
  }, [catalogs?.operations, selectedLibraryDevice]);
  const simulationResult = camResult?.simulation;
  const usesCumulativeStock = Boolean(simulationResult?.surface.is_cumulative && (job.plan?.setups.length ?? 0) > 1);
  const visibleCamoticsSurface = useMemo(() => {
    if (activeMode !== "仿真" || !selectedSetupId || usesCumulativeStock) return null;
    const surface = camResult?.camotics_surfaces?.find((item) => item.setup_id === selectedSetupId);
    return surface ? { url: apiUrl(`/api/v1/jobs/${job.id}/files/${surface.file}`), frame: surface.frame } : null;
  }, [activeMode, camResult?.camotics_surfaces, job.id, selectedSetupId, usesCumulativeStock]);
  const visibleSimulation = useMemo(() => {
    if (activeMode !== "仿真" || !simulationResult || isSheetForming) return null;
    const surface = usesCumulativeStock
      ? simulationResult.surface
      : simulationResult.setup_surfaces?.find((item) => item.setup_id === selectedSetupId) ?? simulationResult.surface;
    const stockVolume = surface.stock_volume_mm3 ?? simulationResult.metrics.initial_stock_volume_mm3;
    const removedVolume = surface.removed_volume_mm3 ?? simulationResult.metrics.removed_volume_mm3;
    return {
      ...simulationResult,
      surface,
      metrics: {
        ...simulationResult.metrics,
        cut_segment_count: surface.cut_segment_count ?? simulationResult.metrics.cut_segment_count,
        removed_volume_mm3: removedVolume,
        remaining_volume_mm3: Math.max(stockVolume - removedVolume, 0),
        removed_percent: stockVolume ? Math.round(removedVolume / stockVolume * 10000) / 100 : 0,
      },
    };
  }, [activeMode, isSheetForming, selectedSetupId, simulationResult, usesCumulativeStock]);
  const visibleFixtureComponents = useMemo(
    () => activeMode === "仿真" ? job.plan?.safety?.fixture_components ?? [] : [],
    [activeMode, job.plan?.safety?.fixture_components],
  );
  const visibleTopologyEdges = useMemo(
    () => job.analysis?.visual_edges ?? [],
    [job.analysis?.visual_edges],
  );

  useEffect(() => {
    const needsCam = readOnly || activeMode === "刀路" || activeMode === "仿真";
    if (!needsCam || camResult || isL32) return;
    let cancelled = false;
    fetch(apiUrl(`/api/v1/jobs/${job.id}/cam`))
      .then((response) => response.ok ? response.json() : null)
      .then((payload: CamResult | null) => {
        if (payload && !cancelled) {
          setCamResult(payload);
          if (readOnly) {
            const ids = new Set(payload.preview_segments.filter((segment) => segment.motion === "cut").map((segment) => segment.operation_id));
            const playable = operations.find((operation) => ids.has(operation.id));
            if (playable) {
              setSelectedOperation(playable);
              setSelectedFeatureIds(playable.feature_ids);
            }
            setActiveMode("仿真");
          }
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setLoadingCam(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeMode, camResult, isL32, job.id, operations, readOnly]);

  const chooseOperation = (operation: Operation) => {
    if (activeMode === "仿真" && camResult && !cutOperationIds.has(operation.id)) return;
    setSelectedOperation(operation);
    setSelectedFeatureIds(operation.feature_ids);
    if (isL32) {
      setActiveMode("仿真");
      void previewL32Operation(operation);
    }
  };

  const openOperationDetails = (operation: Operation, anchor: HTMLElement) => {
    setSelectedOperation(operation);
    setSelectedFeatureIds(operation.feature_ids);
    setActiveMode("工艺");
    setEditingOperationDetails(false);
    setToolDraftId(operation.tool.id);
    setParameterEdits({});
    setClearance(job.plan?.safety?.clearance_mm ?? 3);
    setViseGripHeight(job.plan?.safety?.vise_grip_height_mm ?? 1.5);
    setSupportThickness(job.plan?.safety?.support_thickness_mm ?? 3);
    const anchorBounds = anchor.getBoundingClientRect();
    const panelWidth = Math.min(360, window.innerWidth - 24);
    const panelHeight = Math.min(500, window.innerHeight - 124);
    const anchorCenter = anchorBounds.top + anchorBounds.height / 2;
    const minimumTop = Math.min(102, Math.max(12, window.innerHeight - panelHeight - 12));
    const top = Math.min(
      Math.max(minimumTop, anchorCenter - panelHeight / 2),
      Math.max(minimumTop, window.innerHeight - panelHeight - 12),
    );
    setOperationPopoverPosition({
      top,
      left: Math.min(anchorBounds.right + 10, Math.max(12, window.innerWidth - panelWidth - 12)),
      anchorY: Math.min(panelHeight - 24, Math.max(24, anchorCenter - top)),
    });
    setShowOperationDetails(true);
  };

  const openOperationSimulation = (operation: Operation) => {
    if (operation.enabled === false) return;
    if (camResult && !isL32 && !cutOperationIds.has(operation.id)) return;
    setSelectedOperation(operation);
    setSelectedFeatureIds(operation.feature_ids);
    setEditingOperationDetails(false);
    setShowOperationDetails(false);
    setPlaybackResetToken((current) => current + 1);
    setLoadingCam(!camResult && !isL32);
    setActiveMode("仿真");
    if (isL32) void previewL32Operation(operation);
  };

  const cancelOperationEdit = () => {
    setEditingOperationDetails(false);
    setToolDraftId(selectedOperation?.tool.id ?? "");
    setParameterEdits({});
    setClearance(job.plan?.safety?.clearance_mm ?? 3);
    setViseGripHeight(job.plan?.safety?.vise_grip_height_mm ?? 1.5);
    setSupportThickness(job.plan?.safety?.support_thickness_mm ?? 3);
    setOperationMessage("");
  };

  const chooseMode = (mode: string) => {
    if (mode === "仿真" && camResult && selectedOperation && !cutOperationIds.has(selectedOperation.id)) {
      const playable = operations.find((operation) => cutOperationIds.has(operation.id));
      if (playable) {
        setSelectedOperation(playable);
        setSelectedFeatureIds(playable.feature_ids);
      }
    }
    setLoadingCam((mode === "刀路" || mode === "仿真") && !camResult && !isL32);
    setActiveMode(mode);
    if (mode === "仿真" && isL32 && selectedOperation) void previewL32Operation(selectedOperation);
  };

  const chooseFeature = (id: string) => {
    setSelectedFeatureIds([id]);
    const relatedOperation = operations.find((operation) => operation.feature_ids.includes(id));
    if (relatedOperation) setSelectedOperation(relatedOperation);
  };

  const chooseSpatialDefect = (region: SpatialDefectRegion) => {
    const operationId = region.attribution[0]?.operation_id;
    const operation = operations.find((item) => item.id === operationId);
    if (operation) {
      setSelectedOperation(operation);
      setSelectedFeatureIds(operation.feature_ids);
    }
    setShowSimulationChecks(true);
  };

  const openReviewQueue = () => {
    setInspectionPanel(null);
    setActiveMode("特征");
    setOperationPopoverPosition({ top: 110, left: Math.min(326, Math.max(12, window.innerWidth - 372)), anchorY: 28 });
    setShowOperationDetails(true);
    if (firstReviewFeature) chooseFeature(firstReviewFeature.id);
  };

  const generateCam = async () => {
    setGeneratingCam(true);
    setCamError("");
    setCamProgress({ stage: "connecting", message: "正在连接 CAM 生成器", percent: 0 });
    try {
      await new Promise<void>((resolve, reject) => {
        const stream = new EventSource(apiUrl(`/api/v1/jobs/${job.id}/cam/stream`));
        let settled = false;
        stream.onmessage = async (event) => {
          const progress = JSON.parse(event.data) as {
            stage: string; message: string; percent: number;
            setup_id?: string; operation_id?: string; current?: number; total?: number;
          };
          setCamProgress(progress);
          if (progress.operation_id) {
            const activeOperation = operations.find((operation) => operation.id === progress.operation_id);
            if (activeOperation) {
              setSelectedOperation(activeOperation);
              setSelectedFeatureIds(activeOperation.feature_ids);
            }
          }
          if (progress.stage === "error") {
            settled = true;
            stream.close();
            reject(new Error(progress.message));
            return;
          }
          if (progress.stage !== "completed") return;
          settled = true;
          stream.close();
          try {
            const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/cam`));
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.detail || "刀路结果加载失败");
            const generated = payload as CamResult;
            setCamResult(generated);
            if (isSheetForming) {
              setShowSimulationChecks(true);
              setActiveMode("仿真");
            } else if (generated.verification.status === "failed" || generated.collision.status === "failed") {
              setShowSimulationChecks(true);
              setActiveMode("仿真");
            } else {
              setActiveMode("刀路");
            }
            resolve();
          } catch (reason) {
            reject(reason);
          }
        };
        stream.onerror = () => {
          if (settled) return;
          settled = true;
          stream.close();
          reject(new Error("刀路进度连接中断"));
        };
      });
      const refreshedJob = await fetch(apiUrl(`/api/v1/jobs/${job.id}`));
      if (refreshedJob.ok) setJob(await refreshedJob.json() as Job);
    } catch (reason) {
      setCamError(reason instanceof Error ? reason.message : "刀路生成失败");
    } finally {
      setGeneratingCam(false);
    }
  };

  const reviewFeature = async (featureId: string, reviewState: "accepted" | "excluded") => {
    const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/features/${featureId}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_state: reviewState }),
    });
    if (!response.ok) return;
    const updatedJob = await response.json() as Job;
    setJob(updatedJob);
    setCamResult(null);
    setLoadingCam(activeMode === "刀路" || activeMode === "仿真");
    const updatedOperations = updatedJob.plan?.setups.flatMap((setup) => setup.operations) ?? [];
    const relatedOperation = updatedOperations.find((operation) => operation.feature_ids.includes(featureId));
    setSelectedOperation(relatedOperation ?? updatedOperations[0] ?? null);
    setSelectedFeatureIds(reviewState === "excluded" ? [] : [featureId]);
  };

  const applyUpdatedJob = (updatedJob: Job, preferredOperationId?: string) => {
    const updatedOperations = updatedJob.plan?.setups.flatMap((setup) => setup.operations) ?? [];
    const preferred = updatedOperations.find((operation) => operation.id === preferredOperationId) ?? updatedOperations[0] ?? null;
    setJob(updatedJob);
    setCamResult(null);
    setLoadingCam(activeMode === "刀路" || activeMode === "仿真");
    setSelectedOperation(preferred);
    setSelectedFeatureIds(preferred?.feature_ids ?? []);
    setParameterEdits({});
    setL32OperationPreview(null);
    setL32TurningPreviews({});
    setL32GroovePreviews({});
    setL32GrooveSegments({});
    setL32MillingPreviews({});
    setWarmingL32Previews(false);
    setL32MaterialSnapshots(null);
    setL32MaterialSnapshotError(null);
    setL32MillingPreview(null);
    l32PreviewResponseCacheRef.current.clear();
    l32PrewarmedOperationIdsRef.current.clear();
    l32PreviewRequestRef.current += 1;
  };

  const previewL32Operation = async (operation: Operation) => {
    const requestId = ++l32PreviewRequestRef.current;
    if (!isL32) return;
    const prewarmed = l32PrewarmedOperationIdsRef.current.has(operation.id);
    if (!prewarmed) {
      setOperationMessage(`正在生成 ${operation.id} 的真实刀路与材料效果…`);
      setPreviewingL32OperationId(operation.id);
    }
    setLoadingL32Program(true);
    try {
      let previewJob = job;
      let previewOperation = operation;
      let machineInstanceId = job.machine_instance_id;
      if (!machineInstanceId) {
        const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}`));
        const refreshed = await response.json() as Job & { detail?: string };
        if (!response.ok) throw new Error(refreshed.detail || "无法刷新任务设备绑定");
        if (requestId !== l32PreviewRequestRef.current) return;
        setJob(refreshed);
        previewJob = refreshed;
        previewOperation = refreshed.plan?.setups.flatMap((setup) => setup.operations).find((item) => item.id === operation.id) ?? operation;
        machineInstanceId = refreshed.machine_instance_id;
        if (!machineInstanceId) {
          const bindResponse = await fetch(apiUrl(`/api/v1/jobs/${job.id}/machine-instance/default`), { method: "POST" });
          const bound = await bindResponse.json() as Job & { detail?: string };
          if (!bindResponse.ok) throw new Error(bound.detail || "无法建立 L32 任务设备配置");
          if (requestId !== l32PreviewRequestRef.current) return;
          setJob(bound);
          previewJob = bound;
          previewOperation = bound.plan?.setups.flatMap((setup) => setup.operations).find((item) => item.id === operation.id) ?? previewOperation;
          machineInstanceId = bound.machine_instance_id;
        }
      }
      if (!machineInstanceId) throw new Error("当前任务尚未绑定 L32 设备实例");
      if (previewOperation.type === "turn_grooving") {
        if (!l32Axis) throw new Error("尚未取得切槽工序的回转轴坐标");
        const [geometryResult, sweepResult] = await Promise.all([
          fetchL32PreviewJson<{
            detail?: string;
            grooves: Array<{
              operation_id: string;
              draft: {
                actual_tool_fits_floor: boolean;
                stock_radius_mm: number;
                strips: Array<{ z_min_mm: number; z_max_mm: number; cut_to_radius_mm: number }>;
              };
            }>;
          }>(`${job.id}:front-groove-geometry`, `/api/v1/jobs/${job.id}/l32/front-groove-geometry`),
          fetchL32PreviewJson<{
            detail?: string; target_gouge_check_passed: boolean;
            check: { removed_volume_mm3: number };
          }>(`${job.id}:front-groove-sweep`, `/api/v1/jobs/${job.id}/l32/front-groove-sweep-check`),
        ]);
        const geometry = geometryResult.payload;
        const sweep = sweepResult.payload;
        if (!geometryResult.ok) throw new Error(geometry.detail || "切槽几何刀路生成失败");
        if (!sweepResult.ok) throw new Error(sweep.detail || "切槽材料扫掠验证失败");
        const groove = geometry.grooves.find((item) => item.operation_id === previewOperation.id);
        if (!groove) throw new Error("未找到当前切槽工序对应的精确轮廓");
        if (!groove.draft.actual_tool_fits_floor) throw new Error("当前切槽刀宽度大于槽底宽度");
        if (!sweep.target_gouge_check_passed) throw new Error("切槽刀路未通过原始 STEP 过切检查");
        const preview = { operationId: operation.id, stockRadius: groove.draft.stock_radius_mm, strips: groove.draft.strips };
        const segments = l32GroovePreviewToSegments(preview, l32Axis);
        setL32GroovePreviews((current) => ({ ...current, [operation.id]: preview }));
        setL32GrooveSegments((current) => ({ ...current, [operation.id]: segments }));
        if (requestId === l32PreviewRequestRef.current) {
          setL32OperationPreview(null);
          setL32MillingPreview({ operationId:operation.id, segments });
          setOperationMessage(`${operation.id} 精确槽形扫掠已更新；去除 ${sweep.check.removed_volume_mm3.toFixed(3)} mm³`);
        }
        return;
      }
      const geometricMillingTypes = new Set([
        "pocket_roughing", "pocket_finishing",
        "live_tool_contour_roughing", "live_tool_contour_finishing",
      ]);
      if (geometricMillingTypes.has(previewOperation.type)) {
        type GeometricMove = { kind: "rapid" | "feed"; point: { x: number; y: number; z: number } };
        type GeometricDraft = {
          moves: GeometricMove[];
          bound_machine_has_required_module?: boolean | null;
          access_direction?: { x: number; y: number; z: number };
          access_sign?: -1 | 1;
        };
        const toSegments = (drafts: GeometricDraft[]) => {
          const segments: ToolpathSegment[] = [];
          for (const [draftIndex, draft] of drafts.entries()) {
            let previous: GeometricMove | null = null;
            for (const move of draft.moves) {
              if (previous) segments.push({
                operation_id: previewOperation.id,
                motion: move.kind === "rapid" ? "rapid" : "cut",
                x1: previous.point.x, y1: previous.point.y, z1: previous.point.z,
                x2: move.point.x, y2: move.point.y, z2: move.point.z,
                setup_id: `${previewOperation.channel_id ?? "main"}-${draftIndex + 1}`,
                work_axis: draft.access_direction ?? { x: 0, y: draft.access_sign ?? 1, z: 0 },
              });
              previous = move;
            }
          }
          return segments.filter((segment) => Math.hypot(
            segment.x2 - segment.x1, segment.y2 - segment.y1, segment.z2 - segment.z1,
          ) > 1e-6);
        };
        let drafts: GeometricDraft[] = [];
        let resultDetail = "";
        if (previewOperation.type === "pocket_roughing" || previewOperation.type === "pocket_finishing") {
          const featureId = previewOperation.feature_ids.find((id) => id.startsWith("MF-"));
          if (!featureId) throw new Error("型腔工序没有绑定精确型腔特征");
          const [draftResult, sweepResult] = await Promise.all([
            fetchL32PreviewJson<GeometricDraft & { detail?: string }>(
              `${job.id}:pocket:${featureId}:draft`,
              `/api/v1/jobs/${job.id}/l32/catalog-back-pocket/${featureId}/draft`,
            ),
            fetchL32PreviewJson<{
              detail?: string; pocket_region_status: string;
              removed_pocket_region_mm3: number; remaining_pocket_region_mm3: number;
            }>(
              `${job.id}:pocket:${featureId}:sweep`,
              `/api/v1/jobs/${job.id}/l32/catalog-back-pocket/${featureId}/sweep-check`,
            ),
          ]);
          const draft = draftResult.payload;
          const sweep = sweepResult.payload;
          if (!draftResult.ok) throw new Error(draft.detail || "型腔刀路生成失败");
          if (!sweepResult.ok) throw new Error(sweep.detail || "型腔材料扫掠验证失败");
          if (draft.bound_machine_has_required_module === false) throw new Error("当前 L32 配置缺少 U151B 背面动力刀具模块");
          drafts = [draft];
          resultDetail = `去除 ${sweep.removed_pocket_region_mm3.toFixed(3)} mm³，剩余尖角材料 ${sweep.remaining_pocket_region_mm3.toFixed(3)} mm³`;
        } else {
          const roughing = previewOperation.type === "live_tool_contour_roughing";
          const draftPath = roughing ? "catalog-exterior-toolpaths" : "catalog-ear-toolpaths";
          const checkPath = roughing ? "catalog-exterior-sweep-check" : "catalog-ear-sweep-check";
          const [draftResult, sweepResult] = await Promise.all([
            fetchL32PreviewJson<{
              detail?: string; bound_machine_has_required_module?: boolean | null;
              exterior_drafts?: GeometricDraft[]; side_drafts?: GeometricDraft[];
            }>(`${job.id}:${draftPath}`, `/api/v1/jobs/${job.id}/l32/${draftPath}`),
            fetchL32PreviewJson<{ detail?: string; target_gouge_check_passed?: boolean }>(
              `${job.id}:${checkPath}`, `/api/v1/jobs/${job.id}/l32/${checkPath}`,
            ),
          ]);
          const payload = draftResult.payload;
          const sweep = sweepResult.payload;
          if (!draftResult.ok) throw new Error(payload.detail || "非回转外形刀路生成失败");
          if (!sweepResult.ok) throw new Error(sweep.detail || "非回转外形扫掠验证失败");
          if (payload.bound_machine_has_required_module === false) throw new Error("当前 L32 配置缺少 U30B 主轴侧动力刀具模块");
          if (sweep.target_gouge_check_passed !== true) throw new Error("非回转外形刀路未通过原始 STEP 过切检查");
          drafts = payload.exterior_drafts ?? payload.side_drafts ?? [];
          resultDetail = `${drafts.length} 个径向方向的刀具扫掠已通过目标实体过切检查`;
        }
        const segments = toSegments(drafts);
        if (!segments.some((segment) => segment.motion === "cut")) throw new Error("工序未生成有效切削段");
        setL32MillingPreviews((current) => ({ ...current, [operation.id]: segments }));
        if (requestId === l32PreviewRequestRef.current) {
          setL32OperationPreview(null);
          setL32MillingPreview({ operationId: operation.id, segments });
          setOperationMessage(`${operation.id} 真实刀路已更新；${resultDetail}`);
        }
        return;
      }
      const rotational: RotationalFeatureAnalysis = l32Rotational ?? await fetch(apiUrl(`/api/v1/jobs/${job.id}/turning/analyze`), { method: "POST" })
        .then(async (response) => {
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || "无法读取回转特征");
          return payload as RotationalFeatureAnalysis;
        });
      if (requestId !== l32PreviewRequestRef.current) return;
      if (!l32Rotational) setL32Rotational(rotational);
      const directProfile = rotational.profiles.find((profile) => operation.feature_ids.includes(profile.id));
      let sourceProfile = directProfile ?? rotational.profiles.find((profile) =>
        operation.feature_ids.includes(`${profile.id}-BACK`),
      );
      if (!sourceProfile) throw new Error("该工序没有绑定可用于刀路计算的已确认回转轮廓");
      if (sourceProfile.review_state !== "accepted") {
        const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/turning/profiles/${sourceProfile.id}`), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ review_state: "accepted" }),
        });
        const accepted = await response.json() as RotationalFeatureAnalysis & { detail?: string };
        if (!response.ok) throw new Error(accepted.detail || "无法确认工序使用的回转轮廓");
        if (requestId !== l32PreviewRequestRef.current) return;
        setL32Rotational(accepted);
        sourceProfile = accepted.profiles.find((profile) => profile.id === sourceProfile?.id);
        if (!sourceProfile || sourceProfile.review_state !== "accepted") {
          throw new Error("工序使用的回转轮廓确认失败");
        }
        const refreshedResponse = await fetch(apiUrl(`/api/v1/jobs/${job.id}`));
        const refreshed = await refreshedResponse.json() as Job & { detail?: string };
        if (!refreshedResponse.ok) throw new Error(refreshed.detail || "无法刷新已确认的工艺方案");
        if (requestId !== l32PreviewRequestRef.current) return;
        setJob(refreshed);
        previewJob = refreshed;
        previewOperation = refreshed.plan?.setups.flatMap((setup) => setup.operations).find((item) => item.id === operation.id) ?? previewOperation;
      }
      const stockRadius = Number(previewJob.plan?.stock.diameter_mm ?? 0) / 2
        || Math.max(...sourceProfile.points.map((point) => point.radius));
      const zValues = sourceProfile.points.map((point) => point.z);
      const cutoffOperation = previewJob.plan?.setups.flatMap((setup) => setup.operations)
        .find((item) => item.type === "turn_cutoff");
      const cutoffPlaneZ = Number(cutoffOperation?.parameters.finished_back_datum_z_mm ?? cutoffOperation?.parameters.z_mm);
      const stockZMin = Number.isFinite(cutoffPlaneZ)
        ? Math.min(Math.min(...zValues) - 2, cutoffPlaneZ - 0.5)
        : Math.min(...zValues) - 2;
      const channelId = operation.channel_id === "sub" ? "sub" : "main";
      let draft: TurningDraftResult;
      let sourceCutoffZ = 0;
      if (channelId === "sub") {
        const cutoffOperation = previewJob.plan?.setups.flatMap((setup) => setup.operations).find((item) => item.type === "turn_cutoff");
        sourceCutoffZ = Number(cutoffOperation?.parameters.finished_back_datum_z_mm ?? cutoffOperation?.parameters.z_mm ?? Math.min(...zValues));
        const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/turning/backside/draft`), {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            machine_instance_id: machineInstanceId,
            source_profile_id: sourceProfile.id,
            operation: previewOperation,
            source_cutoff_z_mm: sourceCutoffZ,
            stock_radius_mm: stockRadius,
            resolution_mm: 0.1,
          }),
        });
        const payload = await response.json() as BacksideDraftResult & { detail?: string };
        if (!response.ok) throw new Error(payload.detail || "背轴工序效果生成失败");
        draft = payload.draft;
        sourceCutoffZ = payload.transform.source_cutoff_z_mm;
      } else {
        const result = await fetchL32PreviewJson<TurningDraftResult & { detail?: string }>(
          `${job.id}:turning:${previewOperation.id}:${JSON.stringify(previewOperation)}`,
          `/api/v1/jobs/${job.id}/turning/draft`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            machine_instance_id: machineInstanceId,
            operation: previewOperation,
            profile: sourceProfile,
            stock_radius_mm: stockRadius,
            initial_bore_radius_mm: 0,
            z_min_mm: stockZMin,
            z_max_mm: Math.max(...zValues) + 2,
            resolution_mm: 0.1,
          }),
        });
        const payload = result.payload;
        if (!result.ok) throw new Error(payload.detail || "工序效果生成失败");
        draft = payload;
      }
      if (requestId === l32PreviewRequestRef.current) {
        const preview: L32TurningPreview = {
          operationId: operation.id,
          channelId,
          sourceCutoffZ,
          stockRadius,
          draft,
        };
        setL32MillingPreview(null);
        setL32OperationPreview(preview);
        setL32TurningPreviews((current) => ({ ...current, [operation.id]: preview }));
        setOperationMessage(`${operation.id} 真实刀路与材料效果已更新`);
      }
    } catch (reason) {
      if (requestId === l32PreviewRequestRef.current) {
        setL32OperationPreview(null);
        setL32MillingPreview(null);
        setOperationMessage(reason instanceof Error ? reason.message : "工序效果生成失败");
      }
    } finally {
      if (requestId === l32PreviewRequestRef.current) {
        setLoadingL32Program(false);
        setPreviewingL32OperationId(null);
      }
    }
  };

  const applyAutomaticRemediation = async () => {
    if (!camResult?.remediation?.can_auto_replan || applyingRemediation) return;
    setApplyingRemediation(true);
    setSafetyMessage("");
    setCamError("");
    setCamProgress({ stage: "remediation_loop", message: "正在启动自动纠错闭环", percent: 0 });
    try {
      const outcome = await new Promise<{ outcome?: string; iteration?: number }>((resolve, reject) => {
        const stream = new EventSource(apiUrl(`/api/v1/jobs/${job.id}/cam/remediation/stream`));
        let settled = false;
        stream.onmessage = (event) => {
          const progress = JSON.parse(event.data) as {
            stage: string; message: string; percent: number; outcome?: string; iteration?: number;
            operation_id?: string; setup_id?: string; current?: number; total?: number;
          };
          setCamProgress(progress);
          if (progress.operation_id) {
            const activeOperation = operations.find((operation) => operation.id === progress.operation_id);
            if (activeOperation) {
              setSelectedOperation(activeOperation);
              setSelectedFeatureIds(activeOperation.feature_ids);
            }
          }
          if (progress.stage === "error") {
            settled = true;
            stream.close();
            reject(new Error(progress.message));
          } else if (progress.stage === "completed") {
            settled = true;
            stream.close();
            resolve({ outcome: progress.outcome, iteration: progress.iteration });
          }
        };
        stream.onerror = () => {
          if (settled) return;
          settled = true;
          stream.close();
          reject(new Error("自动纠错进度连接中断"));
        };
      });
      const [camResponse, jobResponse] = await Promise.all([
        fetch(apiUrl(`/api/v1/jobs/${job.id}/cam`)),
        fetch(apiUrl(`/api/v1/jobs/${job.id}`)),
      ]);
      const camPayload = await camResponse.json();
      const jobPayload = await jobResponse.json();
      if (!camResponse.ok) throw new Error(camPayload.detail || "复验结果加载失败");
      if (!jobResponse.ok) throw new Error(jobPayload.detail || "工艺方案刷新失败");
      const updatedJob = jobPayload as Job;
      setJob(updatedJob);
      setCamResult(camPayload as CamResult);
      setClearance(updatedJob.plan?.safety?.clearance_mm ?? clearance);
      setViseGripHeight(updatedJob.plan?.safety?.vise_grip_height_mm ?? viseGripHeight);
      setSupportThickness(updatedJob.plan?.safety?.support_thickness_mm ?? supportThickness);
      setShowSimulationChecks(true);
      setActiveMode("仿真");
      const outcomeLabels: Record<string, string> = {
        passed: "全部复验已通过",
        blocked: "发现高风险问题，已停止自动修改",
        max_iterations: "达到最大迭代次数，问题尚未收敛",
        manual_review: "仍有需要工程师处理的问题",
      };
      setSafetyMessage(`自动纠错第 ${outcome.iteration ?? 1} 轮完成：${outcomeLabels[outcome.outcome ?? ""] ?? "请查看复验结果"}`);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "自动纠错失败";
      setCamError(message);
      setSafetyMessage(message);
    } finally {
      setApplyingRemediation(false);
    }
  };

  const selectSolid = async (solidIndex: number) => {
    if (solidIndex === selectedSolidIndex || solidBusy) return;
    setSolidBusy(true);
    setSafetyMessage("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/solid`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ solid_index: solidIndex }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "切换目标实体失败");
      applyUpdatedJob(payload as Job);
      setCamError("");
      setSafetyMessage(`已切换到实体 ${solidIndex}，特征和工艺已重新规划`);
    } catch (reason) {
      setSafetyMessage(reason instanceof Error ? reason.message : "切换目标实体失败");
    } finally {
      setSolidBusy(false);
    }
  };

  const saveOperationEdits = async () => {
    if (!selectedSetup || !selectedOperation) return;
    setOperationBusy(true);
    setOperationMessage("");
    setSafetyMessage("");
    const operationId = selectedOperation.id;
    try {
      const operationResponse = await fetch(apiUrl(`/api/v1/jobs/${job.id}/setups/${selectedSetup.id}/operations/${operationId}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parameters: parameterDraft, tool_id: toolDraftId }),
      });
      const operationPayload = await operationResponse.json();
      if (!operationResponse.ok) throw new Error(operationPayload.detail || "保存工序失败");

      const safetyResponse = await fetch(apiUrl(`/api/v1/jobs/${job.id}/safety`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clearance_mm: clearance, vise_grip_height_mm: viseGripHeight, support_thickness_mm: supportThickness }),
      });
      const safetyPayload = await safetyResponse.json();
      if (!safetyResponse.ok) throw new Error(safetyPayload.detail || "安全参数保存失败");

      applyUpdatedJob(safetyPayload as Job, operationId);
      setCamError("");
      setEditingOperationDetails(false);
      setOperationMessage("设置已保存并重新校核，原刀路结果已失效");
    } catch (reason) {
      setOperationMessage(reason instanceof Error ? reason.message : "保存并重新校核失败");
    } finally {
      setOperationBusy(false);
    }
  };

  const deleteOperation = async () => {
    if (!selectedSetup || !selectedOperation) return;
    setOperationBusy(true);
    setOperationMessage("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/setups/${selectedSetup.id}/operations/${selectedOperation.id}`), { method: "DELETE" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "删除工序失败");
      applyUpdatedJob(payload as Job);
      setOperationMessage("工序已删除");
    } catch (reason) {
      setOperationMessage(reason instanceof Error ? reason.message : "删除工序失败");
    } finally {
      setOperationBusy(false);
    }
  };

  const toggleOperationEnabled = async () => {
    if (!selectedSetup || !selectedOperation) return;
    const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/setups/${selectedSetup.id}/operations/${selectedOperation.id}`), {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: selectedOperation.enabled === false }),
    });
    const payload = await response.json();
    if (response.ok) {
      applyUpdatedJob(payload as Job, selectedOperation.id);
      setOperationMessage(selectedOperation.enabled === false ? "工序已启用" : "工序已抑制，不参与刀路生成");
    } else setOperationMessage(payload.detail || "更新工序状态失败");
  };

  const moveOperation = async (direction: -1 | 1) => {
    if (!selectedSetup || !selectedOperation) return;
    const ids = selectedSetup.operations.map((operation) => operation.id);
    const index = ids.indexOf(selectedOperation.id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/setups/${selectedSetup.id}/operations/reorder`), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ operation_ids: ids }),
    });
    const payload = await response.json();
    if (response.ok) {
      applyUpdatedJob(payload as Job, selectedOperation.id);
      setOperationMessage("工序顺序已调整");
    } else setOperationMessage(payload.detail || "调整顺序失败");
  };

  if (!job.plan || !job.analysis || !job.model_url) {
    return <div className="fatal-state"><AlertTriangle />{job.error || "任务没有产生可用结果"}<button onClick={onNew}>上传新零件</button></div>;
  }

  return (
    <main className="workbench">
      <header className="topbar app-header">
        <div className="brand compact"><span>{APP_LOGO_TEXT}</span>{APP_NAME}</div>
        <div className="project-title"><small>当前零件</small><strong>{job.filename}</strong></div>
        <div className="top-meta">
          {sourceSolids > 1 && solidCandidates.length > 0 && <label className="header-solid-selector">
            <select aria-label="选择目标加工实体" disabled={readOnly || solidBusy} value={selectedSolidIndex} onChange={(event) => selectSolid(Number(event.target.value))}>
              {solidCandidates.map((candidate) => <option key={candidate.index} value={candidate.index}>实体 {candidate.index} · {candidate.bounds.size.x.toFixed(1)}×{candidate.bounds.size.y.toFixed(1)}×{candidate.bounds.size.z.toFixed(1)} mm</option>)}
            </select>
          </label>}
          {readOnly && <span className="readonly-badge">只读模式</span>}
        </div>
        <div className="header-actions">
          <button className="header-command-button" onClick={onNew}><FileUp size={14} />新建任务</button>
          <button className="header-command-button" onClick={onHistory}><History size={14} />历史记录</button>
          <div className="admin-identity" title="当前用户"><UserRound size={14} /><strong>admin</strong><ChevronDown size={13} /></div>
        </div>
      </header>
      <nav className="compact-mode-toolbar" aria-label="工作模式">
        {["特征", "工艺", "刀路", "仿真"].map((mode) => <button key={mode} className={activeMode === mode ? "active" : ""} onClick={() => chooseMode(mode)}>{isSheetForming && mode === "刀路" ? "成形" : mode}</button>)}
      </nav>

      {showL32Program && <L32ProgramViewer
        jobId={job.id}
        operations={operations}
        stock={job.plan.stock}
        wholePartBlockers={l32WholePartBlockers}
        fallbackBounds={solidCandidates.find((item) => item.index === selectedSolidIndex)?.bounds ?? solidCandidates[0]?.bounds ?? null}
        onClose={() => setShowL32Program(false)}
        onOpenEngineering={() => {
          setShowL32Program(false);
          setShowL32Workbench(true);
        }}
      />}

      {showProcessDesigner && catalogs && <ProcessDesigner
        job={job}
        catalogs={catalogs}
        apiUrl={apiUrl}
        readOnly={readOnly}
        onUpdated={applyUpdatedJob}
        onPreview={async (operation) => {
          setShowProcessDesigner(false);
          setShowL32Program(false);
          chooseOperation(operation);
        }}
        onEngineeringReview={(operation) => {
          chooseOperation(operation);
          setShowProcessDesigner(false);
          setShowL32Workbench(true);
        }}
        onClose={() => setShowProcessDesigner(false)}
      />}

      {showL32Workbench && <L32Workbench
        jobId={job.id}
        catalogs={catalogs}
        plannedOperations={job.plan.setups.flatMap((setup) => setup.operations)}
        preferredOperationId={selectedOperation?.id}
        manufacturingRequirements={job.plan.manufacturing_requirements ?? null}
        boundMachineInstanceId={job.machine_instance_id}
        readOnly={readOnly}
        onPlanChanged={async () => {
          const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}`));
          if (response.ok) applyUpdatedJob(await response.json() as Job, selectedOperation?.id);
        }}
        onClose={() => setShowL32Workbench(false)}
      />}

      {showResourceLibrary && <div className="operation-library-backdrop" onMouseDown={() => setShowResourceLibrary(false)}>
        <section className={`operation-library-dialog resource-library-dialog ${infoLibraryDevice ? "has-device-info" : ""}`} onMouseDown={(event) => event.stopPropagation()}>
          <header><div><small>OPERATION LIBRARY</small><strong>工序库</strong></div><span>{libraryOperations.length} 道工序</span><button aria-label="关闭工序库" onClick={() => setShowResourceLibrary(false)}><X size={16} /></button></header>
          <div className="operation-library-body">
          <div className="operation-library-content">
            <section className="operation-device-picker" aria-label="选择设备">
              <div><strong>选择设备</strong></div>
              <div className="operation-device-dropdown">
                <select aria-label="工序库设备" value={selectedLibraryDevice?.id ?? ""} onChange={(event) => { setSelectedLibraryDeviceId(event.target.value); setDeviceInfoId(null); }}>
                  {!deviceLibrary && <option value="">正在加载设备…</option>}
                  {(deviceLibrary?.devices ?? []).map((device) => <option key={device.id} value={device.id}>{device.display_name} · {device.category_label}</option>)}
                </select>
                <button className={`operation-device-info ${deviceInfoId === selectedLibraryDevice?.id ? "active" : ""}`} aria-label={`查看 ${selectedLibraryDevice?.display_name ?? "当前设备"} 设备详情`} title="设备详情" disabled={!selectedLibraryDevice} onClick={() => setDeviceInfoId((current) => current === selectedLibraryDevice?.id ? null : selectedLibraryDevice?.id ?? null)}><Info size={14} strokeWidth={2} /></button>
              </div>
            </section>
            <div className="operation-library-grid">
              {libraryOperations.map((definition) => {
                return <article key={definition.id}>
                  <div><span>{definition.category}</span></div>
                  <strong>{definition.name}</strong>
                  <p>{definition.description}</p>
                  <small>{definition.engine.provider} / {definition.engine.operation}{definition.engine.modifiers.length ? ` + ${definition.engine.modifiers.join("+")}` : ""}</small>
                </article>;
              })}
              {selectedLibraryDevice && libraryOperations.length === 0 && <p className="device-library-empty">该设备暂未绑定可用工序。</p>}
            </div>
          </div>
          </div>

          {infoLibraryDevice && <aside className="device-info-popover" aria-label={`${infoLibraryDevice.model} 设备详情`}>
              <header><div><small>{infoLibraryDevice.manufacturer} · {infoLibraryDevice.record_kind === "virtual" ? "虚拟设备" : "实体设备"}</small><strong>{infoLibraryDevice.name}</strong><span>{infoLibraryDevice.category_label}</span></div><button aria-label="关闭设备详情" onClick={() => setDeviceInfoId(null)}><X size={14} /></button></header>
              <div className="device-profile-metrics">
                {infoLibraryDevice.record_kind === "virtual"
                  ? <div><small>工作空间</small><strong>{infoLibraryDevice.workpiece.working_envelope_mm?.join("×")}</strong><span>XYZ mm</span></div>
                  : <div><small>最大直径</small><strong>Ø{infoLibraryDevice.workpiece.maximum_diameter_mm}</strong><span>选配 Ø{infoLibraryDevice.workpiece.optional_maximum_diameter_mm ?? "—"} mm</span></div>}
                <div><small>主轴转速</small><strong>{infoLibraryDevice.spindles.find((spindle) => spindle.id === "main")?.maximum_rpm.toLocaleString()}</strong><span>rpm</span></div>
                {infoLibraryDevice.record_kind === "virtual"
                  ? <div><small>运动轴</small><strong>{infoLibraryDevice.axes.length}</strong><span>轴</span></div>
                  : <div><small>一次夹持</small><strong>{infoLibraryDevice.workpiece.maximum_length_per_chucking_mm}</strong><span>mm</span></div>}
              </div>
              <section className="device-profile-section"><h4>设备身份</h4><dl>
                <div><dt>型号范围</dt><dd>{infoLibraryDevice.variants.join(" / ")}</dd></div>
                <div><dt>控制系统</dt><dd>{infoLibraryDevice.controller.model}</dd></div>
                <div><dt>运动轴</dt><dd>{infoLibraryDevice.axes.map((axis) => axis.id).join(" / ")}</dd></div>
                <div><dt>实际配置</dt><dd className="pending">{infoLibraryDevice.configuration_status === "confirmed" ? "已确认" : "待确认"}</dd></div>
              </dl></section>
              <section className="device-profile-section"><h4>加工能力</h4><div className="device-capabilities">{infoLibraryDevice.capabilities.map((capability) => <span key={capability.code}>{capability.name}</span>)}</div></section>
              <section className="device-profile-section integration"><h4>系统接入</h4><dl>
                <div><dt>机床运动学</dt><dd className="pending">{infoLibraryDevice.system_integration.kinematics_adapter ?? "待适配"}</dd></div>
                <div><dt>NC 后处理</dt><dd className="pending">{infoLibraryDevice.system_integration.postprocessor ?? "待适配"}</dd></div>
                <div><dt>CAM 程序输出</dt><dd>{infoLibraryDevice.system_integration.direct_nc_output ? "可生成" : "未接入"}</dd></div>
                {infoLibraryDevice.system_integration.production_release_requires_physical_machine && <div><dt>生产放行</dt><dd className="pending">需绑定实体设备</dd></div>}
              </dl></section>
              {infoLibraryDevice.required_confirmation.length > 0 && <section className="device-profile-section required"><h4>建档待补充</h4><ul>{infoLibraryDevice.required_confirmation.map((item) => <li key={item}>{item}</li>)}</ul></section>}
              <footer>资料来源：{infoLibraryDevice.source.document} · {infoLibraryDevice.source.document_date}{infoLibraryDevice.source.pages.length > 0 ? ` · 第 ${infoLibraryDevice.source.pages.join("、")} 页` : ""}</footer>
          </aside>}
        </section>
      </div>}

      <section className="workspace">
        <aside className="workbench-sidebar">
          <section className={`feature-tree panel accordion-panel ${structureExpanded ? "expanded" : "collapsed"}`}>
            <button className="panel-heading accordion-trigger" aria-expanded={structureExpanded} onClick={() => setStructureExpanded((value) => !value)}><Layers3 size={16} /><span>工艺路线</span><small>{job.plan.setups.length} 装夹 · {operations.length} 工序</small><ChevronRight className="accordion-chevron" size={16} /></button>
            {structureExpanded && <div className="panel-content">
              <div className="tree-section"><strong><Box size={15} /> 毛坯</strong><small>{String((job.plan.stock.size_mm as number[])?.join(" × "))} mm</small></div>
              {job.plan.setups.map((setup) => (
                <div key={setup.id} className="setup-tree">
                  <div className="tree-section"><strong><Rotate3D size={15} /> {setup.name}</strong><small>{setup.fixture}</small></div>
                  {setup.operations.map((operation) => (
                    <div key={operation.id} className={`operation-tree-row ${selectedOperation?.id === operation.id ? "selected" : ""} ${operation.enabled === false ? "suppressed" : ""} ${generatingCam && camProgress?.operation_id === operation.id ? "stream-active" : ""}`}>
                      <button className="operation-tree-main" title={`查看 ${operation.id} 仿真`} disabled={operation.enabled === false || (Boolean(camResult) && !isL32 && !cutOperationIds.has(operation.id))} onClick={() => openOperationSimulation(operation)}>
                        <span>{operation.id}</span><div><strong>{operation.name}</strong><small>{operation.tool.name}</small></div>
                      </button>
                      <div className="operation-tree-actions">
                        <button aria-label={`查看 ${operation.id} 工序详情`} title="查看工序详情" onClick={(event) => openOperationDetails(operation, event.currentTarget)}><Info size={15} /></button>
                      </div>
                    </div>
                  ))}
                </div>
              ))}
              <div className="tree-summary"><CircleDot size={14} /> {holes.length} 孔 · {prismaticFeatures.length} 型腔/槽 · {internalProfiles.length} 内轮廓/雕刻 · {reviewCount} 待复核 · 排除 {excludedCount + prismaticExcludedCount + internalProfileExcludedCount}</div>
            </div>}
          </section>

          {showOperationDetails && selectedOperation && <section ref={operationPopoverRef} className="operation-popover inspector panel" style={{ top: operationPopoverPosition.top, left: operationPopoverPosition.left, "--operation-anchor-y": `${operationPopoverPosition.anchorY}px` } as CSSProperties}>
            <header className="operation-popover-heading"><div><Bot size={16} /><span>工序详情</span><small>{selectedOperation.id}</small></div><div className="operation-popover-actions">{!readOnly && !editingOperationDetails && <button className="edit-operation-button" aria-label="编辑工序" title="编辑" onClick={() => { setEditingOperationDetails(true); setToolDraftId(selectedOperation.tool.id); setOperationMessage(""); }}><Pencil size={13} /></button>}<button aria-label="关闭工序详情" title="关闭" onClick={() => { setEditingOperationDetails(false); setShowOperationDetails(false); }}><X size={15} /></button></div></header>
            <div className="panel-content">
              {selectedOperation ? (
                <>
                  <div className="inspector-block"><label>工序</label><h3>{selectedOperation.name}</h3><p>{selectedOperation.id} · {selectedOperation.type}</p></div>
                  <div className="inspector-block"><label>{isSheetForming ? "工艺装备" : "刀具"}</label><div className="tool-card"><Wrench size={18} /><div><strong>{displayedTool?.name}</strong><small>{isSheetForming ? displayedTool?.kind : `${displayedTool?.kind} · 伸出 ${displayedTool?.stickout_mm} mm · 刀柄 Ø${displayedTool?.holder_diameter_mm}`}</small></div></div>
                    {selectedDefinition && !readOnly && editingOperationDetails && <select className="tool-selector" disabled={operationBusy} value={toolDraftId} onChange={(event) => setToolDraftId(event.target.value)}>{(catalogs?.tools ?? []).filter((tool) => selectedDefinition.tool.accepts.includes(tool.kind)).map((tool) => <option key={tool.id} value={tool.id}>{tool.name}</option>)}</select>}
                  </div>
                  {!isSheetForming && <div className="inspector-block safety-editor">
                    <label>安全与夹具</label>
                    <div><span>安全间隙</span>{editingOperationDetails ? <input type="number" min="0.5" max="50" step="0.5" value={clearance} onChange={(event) => setClearance(Number(event.target.value))} /> : <strong>{clearance} mm</strong>}</div>
                    {job.plan.safety?.fixture_strategy === "sacrificial_plate"
                      ? <div><span>牺牲垫板厚度</span>{editingOperationDetails ? <input type="number" min="0.5" max="50" step="0.5" value={supportThickness} onChange={(event) => setSupportThickness(Number(event.target.value))} /> : <strong>{supportThickness} mm</strong>}</div>
                      : <div><span>平口钳夹持高度</span>{editingOperationDetails ? <input type="number" min="0.5" max="50" step="0.5" value={viseGripHeight} onChange={(event) => setViseGripHeight(Number(event.target.value))} /> : <strong>{viseGripHeight} mm</strong>}</div>}
                    {safetyMessage && <small>{safetyMessage}</small>}
                  </div>}
                  <div className="inspector-block"><label>规划依据</label><ul>{selectedOperation.rationale.map((item) => <li key={item}>{item}</li>)}</ul></div>
                  <div className="inspector-block operation-parameters"><label>工序参数</label>
                    {selectedDefinition ? <>
                      <p>{selectedDefinition.description} · {selectedDefinition.engine.operation} · {selectedDefinition.maturity}</p>
                      {editingOperationDetails ? <div className="parameter-fields">{selectedDefinition.parameters.map((parameter) => {
                        const value = parameterDraft[parameter.key] ?? parameter.default ?? "";
                        return <label key={parameter.key}><span>{parameter.label}{parameter.unit ? ` (${parameter.unit})` : ""}</span>
                          {parameter.type === "boolean"
                            ? <input type="checkbox" checked={Boolean(value)} onChange={(event) => selectedOperation && setParameterEdits((current) => ({ ...current, [selectedOperation.id]: { ...parameterDraft, [parameter.key]: event.target.checked } }))} />
                            : parameter.type === "enum"
                              ? <select value={String(value)} onChange={(event) => selectedOperation && setParameterEdits((current) => ({ ...current, [selectedOperation.id]: { ...parameterDraft, [parameter.key]: event.target.value } }))}>{parameter.choices.map((choice) => <option key={choice}>{choice}</option>)}</select>
                              : <input type="number" min={parameter.minimum ?? undefined} max={parameter.maximum ?? undefined} step={parameter.type === "integer" ? 1 : "any"} value={Number(value)} onChange={(event) => selectedOperation && setParameterEdits((current) => ({ ...current, [selectedOperation.id]: { ...parameterDraft, [parameter.key]: Number(event.target.value) } }))} />}
                        </label>;
                      })}</div> : <dl className="parameter-readonly-list">{selectedDefinition.parameters.map((parameter) => {
                        const value = parameterDraft[parameter.key] ?? parameter.default ?? "—";
                        return <div key={parameter.key}><dt>{parameter.label}</dt><dd>{parameter.type === "boolean" ? (value ? "是" : "否") : `${String(value)}${parameter.unit ? ` ${parameter.unit}` : ""}`}</dd></div>;
                      })}</dl>}
                      {!readOnly && editingOperationDetails && <div className="operation-editor-actions"><button disabled={operationBusy} onClick={() => moveOperation(-1)} title="上移"><ArrowUp size={13} />上移</button><button disabled={operationBusy} onClick={() => moveOperation(1)} title="下移"><ArrowDown size={13} />下移</button><button disabled={operationBusy} onClick={toggleOperationEnabled}>{selectedOperation.enabled === false ? "启用" : "抑制"}</button><button disabled={operationBusy} className="danger" onClick={deleteOperation}><Trash2 size={13} />删除</button></div>}
                    </> : <dl>{Object.entries(selectedOperation.parameters).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl>}
                    {operationMessage && <small className="operation-message">{operationMessage}</small>}
                  </div>
                  {selectedFeatures.map((feature: ManufacturingFeature) => (
                    <div className={`feature-card ${feature.review_state}`} key={feature.id}>
                      {"depth" in feature ? <>
                        <div><span>{feature.id}</span><small>{feature.kind === "pocket" ? "封闭型腔" : feature.kind === "slot" ? "贯通槽" : "machining_kind" in feature && feature.machining_kind === "engraving" ? "浅雕刻" : "machining_kind" in feature && feature.machining_kind === "blind_pocket" ? "异形盲型腔" : "异形贯通孔"}</small></div>
                        <div><strong>{feature.length.toFixed(2)} × {feature.width.toFixed(2)}</strong><small>深 {feature.depth.toFixed(2)} · {Math.round(feature.confidence * 100)}%</small></div>
                      </> : <>
                        <div><span>{feature.id}</span><small>{feature.end_type === "through" ? "通孔" : feature.end_type === "blind" ? "盲孔" : "孔端待确认"}</small></div>
                        <div><strong>Ø{feature.diameter.toFixed(2)} × {feature.length.toFixed(2)}</strong><small>{feature.segment_count} 个圆柱面 · {Math.round(feature.confidence * 100)}%</small></div>
                      </>}
                      {feature.review_reasons.map((reason) => <p key={reason}>{reason}</p>)}
                      {!readOnly && editingOperationDetails && <div className="feature-actions"><button onClick={() => reviewFeature(feature.id, "accepted")}><Check size={12} />确认特征</button><button onClick={() => reviewFeature(feature.id, "excluded")}><AlertTriangle size={12} />排除</button></div>}
                    </div>
                  ))}
                </>
              ) : <p className="empty-panel">选择一道工序查看规划依据。</p>}
            </div>
            {!readOnly && editingOperationDetails && <footer className="operation-edit-footer editing">
              <button onClick={cancelOperationEdit} disabled={operationBusy}>取消</button><button className="primary" onClick={saveOperationEdits} disabled={operationBusy}>{operationBusy ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}{operationBusy ? "保存并校核中…" : "保存并重新检验"}</button>
            </footer>}
          </section>}
        </aside>

        <section className={`viewport panel inspection-rail-host ${activeMode === "刀路" || activeMode === "仿真" ? "show-toolpath-legend" : ""}`}>
          {automationBlocked && <div className="capability-blocker">
            <div><AlertTriangle size={17} /><strong>已阻止生成不完整工艺</strong></div>
            {job.plan.blocking_reasons.map((reason) => <p key={reason}>{reason}</p>)}
          </div>}
          {isL32 && playbackMode === "cumulative" && (activeMode === "仿真" || activeMode === "刀路") && l32WholePartBlocked && l32PreviewOperationId !== selectedOperation?.id && previewingL32OperationId !== selectedOperation?.id && <div className="simulation-failure-banner l32-whole-part-blocker">
            <AlertTriangle size={18} />
            <div>
              <strong>逐工序真实刀路已生成，正在进行整件连续余料校核</strong>
              <span>{l32WholePartBlockers.join("；")}</span>
            </div>
          </div>}
          {isL32 && activeMode === "仿真" && selectedOperation && operationMessage && <div className={`l32-operation-preview-status ${l32PreviewOperationId === selectedOperation.id ? "complete" : ""}`}>
            {previewingL32OperationId === selectedOperation.id ? <LoaderCircle className="spin" size={15} /> : l32PreviewOperationId === selectedOperation.id ? <Check size={15} /> : <Info size={15} />}
            <span>{operationMessage}</span>
          </div>}
          {awaitingL32MaterialSnapshots && <div className={`l32-material-snapshot-status ${l32MaterialSnapshotError?.key === l32MaterialRequestKey || l32MaterialSnapshots?.key === l32MaterialRequestKey ? "failed" : ""}`}>
            {l32MaterialSnapshotError?.key === l32MaterialRequestKey || l32MaterialSnapshots?.key === l32MaterialRequestKey ? <AlertTriangle size={15} /> : <LoaderCircle className="spin" size={15} />}
            <span>{l32MaterialSnapshotError?.key === l32MaterialRequestKey ? l32MaterialSnapshotError.message : l32MaterialSnapshots?.key === l32MaterialRequestKey ? "当前工序没有可用的实体材料帧；画面仅供刀路参考。" : "正在预生成实体材料的逐帧变化；当前显示暂不代表加工结果。"}</span>
          </div>}
          {isL32 && activeMode === "仿真" && playbackMode === "cumulative" && missingMaterialStages.length > 0 && <div className="l32-material-snapshot-status failed">
            <AlertTriangle size={15} />
            <span>{missingMaterialStages.join("、")} 没有实体材料帧；累计播放在这些工序只展示刀路，不代表已验证的材料去除。</span>
          </div>}
          <ModelViewer
            modelUrl={`${apiUrl(job.model_url)}?solid=${selectedSolidIndex}`}
            features={viewerFeatures}
            selectedFeatureIds={selectedFeatureIds}
            onSelectFeature={chooseFeature}
            toolpathSegments={visibleToolpathSegments}
            materialSnapshotUrls={visibleMaterialSnapshots.urls}
            materialSnapshotStages={visibleMaterialSnapshots.stages}
            initialToolpathSegments={initialToolpathSegments}
            profileBoundaries={visibleProfileBoundaries}
            simulation={visibleSimulation}
            simulationBlocked={isL32 && playbackMode === "cumulative" && l32WholePartBlocked && visibleMaterialSnapshots.urls.length === 0 && l32PreviewOperationId !== selectedOperation?.id && previewingL32OperationId !== selectedOperation?.id && (activeMode === "仿真" || activeMode === "刀路")}
            turningStage={visibleMaterialSnapshots.urls.length ? null : visibleTurningStage}
            camoticsSurface={visibleCamoticsSurface}
            fixtureComponents={visibleFixtureComponents}
            animateToolpath={activeMode === "仿真" && (playbackMode === "single" || !l32WholePartBlocked || visibleMaterialSnapshots.urls.length > 0 || l32PreviewOperationId === selectedOperation?.id)}
            initialProgress={initialSimulationProgress}
            playbackResetToken={playbackResetToken}
            operationTools={operationTools}
            topologyEdges={visibleTopologyEdges}
            activeOperationId={selectedOperation?.id}
            isFinalOperation={selectedOperation?.id === operations[operations.length - 1]?.id}
            playbackMode={playbackMode}
            onPlaybackModeChange={setPlaybackMode}
            toolpathLoaded={isL32 ? !loadingL32Program && (l32Program !== null || !warmingL32Previews) : !loadingCam}
            formingPreview={activeMode === "仿真" ? camResult?.forming_preview ?? null : null}
            spatialDefects={activeMode === "仿真" ? spatialDefects : null}
            onSelectDefect={chooseSpatialDefect}
            viewMode={activeMode as "特征" | "工艺" | "刀路" | "仿真"}
            workAxis={selectedSetup?.work_axis}
            activeOperationLabel={selectedOperation?.name}
          />
          <nav className="inspection-rail" aria-label="工程检查与工艺工具">
            {reviewCount > 0 && <button className="inspection-card review" onClick={openReviewQueue} title="查看待人工复核的制造特征">
              <em>{reviewCount}</em>
              <AlertTriangle size={19} />
              <strong>待复核</strong>
              <small>{reviewCount} 项</small>
            </button>}
            <button className={`inspection-card tool ${showResourceLibrary ? "active" : ""}`} onClick={() => { setInspectionPanel(null); setDeviceInfoId(null); setShowResourceLibrary(true); }} title="打开工序库">
              <Library size={19} />
              <strong>工序库</strong>
              <small>{catalogs?.operations.length ?? 0} 项工序</small>
            </button>
            {operations.length > 0 && <button className={`inspection-card process-design ${showProcessDesigner ? "active" : ""}`} onClick={() => { setInspectionPanel(null); setShowProcessDesigner(true); }} title="逐步设计和调整工序">
              <Layers3 size={19} />
              <strong>工序设计</strong>
              <small>编辑与插入</small>
            </button>}
            {isL32 && <button className={`inspection-card tool ${inspectionPanel === "tools" ? "active" : ""}`} onPointerDown={(event) => event.stopPropagation()} onClick={() => setInspectionPanel((current) => current === "tools" ? null : "tools")} title="打开刀具库">
              <Wrench size={19} />
              <strong>刀具库</strong>
              <small>现场刀具</small>
            </button>}
          </nav>
          {inspectionPanel === "tools" && <section ref={inspectionPanelRef} className="tool-library-container"><ToolLibraryPanel machineInstanceId={job.machine_instance_id} catalogTools={catalogs?.tools ?? []} apiUrl={apiUrl} onClose={() => setInspectionPanel(null)} readOnly={readOnly} /></section>}
          {!readOnly && activeMode === "刀路" && isL32 && <button className="viewport-generate-button l32-draft-button" onClick={() => setShowL32Program(true)}><Play size={16} />打开 L32 真实刀路</button>}
          {!readOnly && activeMode === "刀路" && !isL32 && <button className="viewport-generate-button" disabled={automationBlocked || generatingCam || applyingRemediation} onClick={generateCam} title={automationBlocked ? "当前工艺不完整，暂时无法生成刀路" : undefined}>
            {generatingCam || applyingRemediation ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
            {generatingCam ? `生成中 ${Math.round(camProgress?.percent ?? 0)}%` : applyingRemediation ? `纠错中 ${Math.round(camProgress?.percent ?? 0)}%` : isSheetForming ? "生成成形仿真" : camResult ? "重新生成刀路" : "生成刀路"}
          </button>}
          {loadingCam && <div className="cam-loading-notice"><LoaderCircle className="spin" size={16} /><div><strong>{isSheetForming ? "正在加载成形仿真" : "正在加载刀路数据"}</strong><small>模型可以继续查看，结果就绪后会自动显示</small></div></div>}
          {(generatingCam || applyingRemediation) && camProgress && <div className="cam-loading-notice cam-stream-progress"><LoaderCircle className="spin" size={16} /><div><strong>{camProgress.message}</strong><small>{camProgress.operation_id ? `${camProgress.operation_id} · ` : ""}{camProgress.current != null && camProgress.total ? `${camProgress.current}/${camProgress.total} · ` : ""}{Math.round(camProgress.percent)}%</small><span><i style={{ width: `${camProgress.percent}%` }} /></span></div></div>}
          {activeMode === "仿真" && validationStatus === "failed" && camResult && !isSheetForming && <div className="simulation-failure-banner"><AlertTriangle size={18} /><div><strong>当前刀路未达到成品，禁止上机</strong><span>STEP 空间重合 {camResult.verification.metrics.target_overlap_percent?.toFixed(1) ?? "—"}% · 目标过切 {camResult.verification.metrics.missing_target_volume_mm3?.toFixed(2) ?? "—"} mm³ · 多余残料 {camResult.verification.metrics.excess_stock_volume_mm3?.toFixed(2) ?? "—"} mm³</span></div></div>}
          {activeMode === "仿真" && isSheetForming && camResult && <div className="forming-preview-banner"><Rotate3D size={18} /><div><strong>薄板成形工艺预览</strong><span>名义板厚 {camResult.forming_preview?.nominal_thickness_mm.toFixed(2)} mm · 成形深度 {camResult.forming_preview?.formed_depth_mm.toFixed(2)} mm · 不输出生产 NC</span></div></div>}
          {activeMode === "仿真" && <button className={`simulation-check-toggle ${validationStatus ?? ""}`} onClick={() => setShowSimulationChecks(!showSimulationChecks)}><ShieldCheck size={15} /> 检查结果 <span>{validationStatus ? validationStatus.toUpperCase() : "WAIT"}</span></button>}
          {activeMode === "仿真" && showSimulationChecks && <div className="simulation-panel">
            <div><ShieldCheck size={16} /><strong>碰撞与静态预检</strong><span className={validationStatus ?? undefined}>{validationStatus ? validationStatus.toUpperCase() : "等待生成刀路"}</span><button aria-label="关闭检查结果" onClick={() => setShowSimulationChecks(false)}><X size={14} /></button></div>
            {camResult ? <>
              {camResult.collision.checks.map((check) => <p key={check.id} className={check.status}><i />{check.message}</p>)}
              {camResult.verification.checks.map((check) => <p key={check.id} className={check.status}><i />{check.message}</p>)}
              {camResult.remediation && camResult.remediation.defects.length > 0 && <section className={`remediation-summary ${camResult.remediation.status}`}>
                <header>
                  <div><AlertTriangle size={14} /><strong>加工缺陷与补救建议</strong></div>
                  <span>{camResult.remediation.summary.defect_count} 项缺陷 · {camResult.remediation.summary.action_count} 项建议 · 第 {camResult.remediation.iteration}/{camResult.remediation.max_iterations} 轮</span>
                  {!readOnly && camResult.remediation.can_auto_replan && <button className="remediation-apply" onClick={applyAutomaticRemediation} disabled={applyingRemediation}>{applyingRemediation ? <LoaderCircle className="spin" size={12} /> : <Wrench size={12} />}{applyingRemediation ? "正在纠错复验" : `自动修复并复验（${camResult.remediation.summary.auto_action_count} 项）`}</button>}
                </header>
                {camResult.remediation.defects.slice(0, 6).map((defect) => <article key={defect.id} className={defect.severity}>
                  <div><b>{defect.title}</b><em>{defect.severity}</em></div>
                  <p>{defect.message}</p>
                  {(defect.operation_ids.length > 0 || defect.feature_ids.length > 0) && <small>{[...defect.operation_ids, ...defect.feature_ids].join(" · ")}</small>}
                  {defect.action_ids.map((actionId) => {
                    const action = camResult.remediation?.actions.find((item) => item.id === actionId);
                    return action ? <p className="remediation-action" key={actionId}><ChevronRight size={11} />{action.label}{action.auto_applicable ? <span>可自动执行</span> : null}</p> : null;
                  })}
                </article>)}
              </section>}
              {isSheetForming
                ? <div className="simulation-metrics"><strong>{camResult.forming_preview?.stages.length ?? 0}</strong><span>工艺阶段<br />完整覆盖</span><strong>{camResult.forming_preview?.nominal_thickness_mm.toFixed(2)}</strong><span>名义板厚<br />mm</span></div>
                : <div className="simulation-metrics"><strong>{camResult.simulation.metrics.removed_percent}%</strong><span>材料去除<br />{camResult.simulation.metrics.removed_volume_mm3.toLocaleString()} mm³</span><strong>{camResult.simulation.metrics.resolution_mm}</strong><span>网格精度<br />mm</span></div>}
              <small>{isSheetForming ? "概念动画不包含应变、减薄、起皱、破裂和回弹有限元计算，不能用于模具生产放行" : `刀路估算 ${camResult.verification.metrics.estimated_cycle_minutes} min · 绿色为牺牲垫板，红色为硬限位禁入区；包络校核不替代机床级仿真`}</small>
            </> : <p>生成刀路后执行机床行程、参数范围、刀具直径和工序覆盖校验。</p>}
          </div>}
        </section>

      </section>

    </main>
  );
}

export default function App() {
  const [job, setJob] = useState<Job | null>(null);
  const [loadingSession, setLoadingSession] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [showNewJob, setShowNewJob] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  useEffect(() => {
    document.title = PAGE_TITLE;
  }, []);

  useEffect(() => {
    const restoreFromLocation = async () => {
      const match = window.location.pathname.match(/^\/jobs\/([0-9a-f]{32})\/?$/);
      if (!match) {
        setJob(null);
        setReadOnly(false);
        setShowNewJob(false);
        setLoadingSession(false);
        return;
      }
      setLoadingSession(true);
      setSessionError("");
      try {
        const response = await fetch(apiUrl(`/api/v1/jobs/${match[1]}`), { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "任务不存在");
        setJob(payload as Job);
        setReadOnly(new URLSearchParams(window.location.search).get("view") === "1");
      } catch (reason) {
        setJob(null);
        setSessionError(reason instanceof Error ? reason.message : "无法加载任务会话");
      } finally {
        setLoadingSession(false);
      }
    };
    void restoreFromLocation();
    window.addEventListener("popstate", restoreFromLocation);
    return () => window.removeEventListener("popstate", restoreFromLocation);
  }, []);

  const openJob = (createdJob: Job) => {
    window.history.pushState({}, "", `/jobs/${createdJob.id}`);
    setReadOnly(false);
    setJob(createdJob);
    setShowNewJob(false);
    setShowHistory(false);
    setSessionError("");
  };

  if (loadingSession) return <div className="fatal-state"><LoaderCircle className="spin" />正在加载任务会话…</div>;
  return <>
    {job
      ? <Workbench key={job.id} initialJob={job} onNew={() => setShowNewJob(true)} onHistory={() => setShowHistory(true)} readOnly={readOnly} />
      : <EmptyWorkbench error={sessionError} onNew={() => setShowNewJob(true)} onHistory={() => setShowHistory(true)} />}
    <NewJobDialog open={showNewJob} canClose onClose={() => setShowNewJob(false)} onCreated={openJob} />
    {showHistory && <HistoryDialog activeJobId={job?.id} onClose={() => setShowHistory(false)} onSelected={openJob} />}
  </>;
}

function EmptyWorkbench({ error, onNew, onHistory }: { error: string; onNew: () => void; onHistory: () => void }) {
  const [catalogs, setCatalogs] = useState<Catalogs | null>(null);
  const [deviceLibrary, setDeviceLibrary] = useState<DeviceLibrary | null>(null);
  const [selectedDeviceId, setSelectedDeviceId] = useState("");
  const [showOperationLibrary, setShowOperationLibrary] = useState(false);
  const [showToolLibrary, setShowToolLibrary] = useState(false);

  useEffect(() => {
    fetch(apiUrl("/api/v1/catalogs"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: Catalogs) => setCatalogs(payload))
      .catch(() => setCatalogs(null));
    fetch(apiUrl("/api/v1/device-library"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: DeviceLibrary) => {
        setDeviceLibrary(payload);
        setSelectedDeviceId(payload.devices[0]?.id ?? "");
      })
      .catch(() => setDeviceLibrary({ schema_version: "1.0.0", devices: [] }));
  }, []);

  const selectedDevice = deviceLibrary?.devices.find((device) => device.id === selectedDeviceId) ?? deviceLibrary?.devices[0] ?? null;
  const libraryOperations = useMemo(() => {
    if (!selectedDevice) return catalogs?.operations ?? [];
    const bindingIds = new Set((selectedDevice.operation_bindings ?? []).map((binding) => binding.operation_id));
    if (bindingIds.size > 0) return (catalogs?.operations ?? []).filter((operation) => bindingIds.has(operation.id));
    const compatibleGroups = new Set(selectedDevice.system_integration.compatible_operation_groups);
    return (catalogs?.operations ?? []).filter((operation) =>
      compatibleGroups.has(operation.category)
      || [...compatibleGroups].some((group) => DEVICE_OPERATION_GROUP_ALIASES[group]?.includes(operation.category)
        || DEVICE_OPERATION_GROUP_ALIASES[group]?.includes(operation.id)),
    );
  }, [catalogs?.operations, selectedDevice]);

  return <main className="workbench empty-workbench">
    <header className="topbar app-header">
      <div className="brand compact"><span>{APP_LOGO_TEXT}</span>{APP_NAME}</div>
      <div className="project-title empty-project-title"><strong>尚未导入模型</strong></div>
      <div className="top-meta" />
      <div className="header-actions">
        <button className="header-command-button" onClick={onNew}><FileUp size={14} />新建任务</button>
        <button className="header-command-button" onClick={onHistory}><History size={14} />历史记录</button>
        <div className="admin-identity" title="当前用户"><UserRound size={14} /><strong>admin</strong><ChevronDown size={13} /></div>
      </div>
    </header>

    <section className="workspace empty-workspace-shell">
      <section className="viewport panel empty-viewport">
        <div className="empty-viewport-grid" />
        <div className="empty-part-state">
          {error ? <AlertTriangle size={27} /> : <Box size={27} />}
          <strong>{error || "尚未导入模型"}</strong>
          <small>新建任务并上传 STEP 模型后，这里将显示模型与工艺规划</small>
          <button onClick={onNew}><FileUp size={15} />新建任务</button>
        </div>
        <nav className="inspection-rail empty-inspection-rail" aria-label="工程检查与工艺工具">
          <button className={`inspection-card tool ${showOperationLibrary ? "active" : ""}`} onClick={() => { setShowToolLibrary(false); setShowOperationLibrary(true); }}><Library size={19} /><strong>工序库</strong><small>{catalogs?.operations.length ?? 0} 项工序</small></button>
          <button className={`inspection-card tool ${showToolLibrary ? "active" : ""}`} onClick={() => { setShowOperationLibrary(false); setShowToolLibrary((value) => !value); }}><Wrench size={19} /><strong>刀具库</strong><small>{catalogs?.tools.length ?? 0} 款刀具</small></button>
        </nav>
        {showToolLibrary && <section className="tool-library-container"><ToolLibraryPanel machineInstanceId={null} catalogTools={catalogs?.tools ?? []} apiUrl={apiUrl} onClose={() => setShowToolLibrary(false)} readOnly /></section>}
      </section>
    </section>

    {showOperationLibrary && <div className="operation-library-backdrop" onMouseDown={() => setShowOperationLibrary(false)}>
      <section className="operation-library-dialog resource-library-dialog" onMouseDown={(event) => event.stopPropagation()}>
        <header><div><small>OPERATION LIBRARY</small><strong>工序库</strong></div><span>{libraryOperations.length} 道工序</span><button aria-label="关闭工序库" onClick={() => setShowOperationLibrary(false)}><X size={16} /></button></header>
        <div className="operation-library-body"><div className="operation-library-content">
          <section className="operation-device-picker" aria-label="选择设备">
            <div><strong>选择设备</strong></div>
            <div className="operation-device-dropdown"><select aria-label="工序库设备" value={selectedDevice?.id ?? ""} onChange={(event) => setSelectedDeviceId(event.target.value)}>
              {!deviceLibrary && <option value="">正在加载设备…</option>}
              {(deviceLibrary?.devices ?? []).map((device) => <option key={device.id} value={device.id}>{device.display_name} · {device.category_label}</option>)}
            </select></div>
          </section>
          <div className="operation-library-grid">
            {libraryOperations.map((definition) => <article key={definition.id}><div><span>{definition.category}</span></div><strong>{definition.name}</strong><p>{definition.description}</p><small>{definition.engine.provider} / {definition.engine.operation}{definition.engine.modifiers.length ? ` + ${definition.engine.modifiers.join("+")}` : ""}</small></article>)}
            {selectedDevice && libraryOperations.length === 0 && <p className="device-library-empty">该设备暂未绑定可用工序。</p>}
          </div>
        </div></div>
      </section>
    </div>}
  </main>;
}

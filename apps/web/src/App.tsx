import { useEffect, useMemo, useRef, useState } from "react";
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
  Cog,
  FileUp,
  FolderOpen,
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
import { L32Workbench } from "./L32Workbench";
import type { CamResult, Catalogs, DeviceLibrary, Job, ManufacturingFeature, Operation, SpatialDefectRegion, ToolpathSegment, Vec3 } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const EMPTY_TOOLPATH_SEGMENTS: ToolpathSegment[] = [];
const EMPTY_PROFILE_BOUNDARIES: { operation_id: string; setup_id: string; work_axis: Vec3; points: Vec3[] }[] = [];
function apiUrl(path: string) {
  return `${API_BASE}${path}`;
}

type PlanningProgressEvent = {
  stage: string;
  message: string;
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

const PLANNING_STAGE_ORDER = [
  "uploading", "geometry_analysis", "draft_planning", "drawing_analysis", "ai_planning",
  "process_generation", "coverage_validation", "completed",
];
const DEVICE_OPERATION_GROUP_ALIASES: Record<string, string[]> = {
  "钻孔": ["孔加工"],
  "倒角": ["边加工"],
  "小型型腔": ["型腔加工"],
  "雕刻": ["engraving"],
};

function NewJobDialog({ open, onClose, onCreated, canClose = true }: {
  open: boolean;
  onClose: () => void;
  onCreated: (job: Job) => void;
  canClose?: boolean;
}) {
  const stepInputRef = useRef<HTMLInputElement>(null);
  const drawingInputRef = useRef<HTMLInputElement>(null);
  const planningStreamRef = useRef<EventSource | null>(null);
  const [stepFile, setStepFile] = useState<File | null>(null);
  const [drawingFile, setDrawingFile] = useState<File | null>(null);
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
        setNewJobDevices(payload.devices);
        setSelectedNewJobDeviceId((current) => current || payload.devices[0]?.id || "");
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
    if (!stepFile || !drawingFile || !selectedNewJobDeviceId) return;
    setBusy(true);
    setError("");
    setProgressEvents([{ stage: "uploading", message: "正在上传二维图纸和三维模型", percent: 2 }]);
    const form = new FormData();
    form.append("step", stepFile);
    form.append("drawing", drawingFile);
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
            setDrawingFile(null);
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
          <div><span className="dialog-icon"><FileUp size={18} /></span><div><strong id="new-job-title">新建工艺任务</strong><small>同时上传工程图和三维模型，自动规划工序</small></div></div>
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
              <span>{item.message}</span>
              {item.operation_count !== undefined && <small>{item.setup_count} 次装夹 · {item.operation_count} 道工序</small>}
            </div>)}
          </div>
        </div> : <><div className="upload-pair">
          <button
            className={`drop-zone upload-pdf ${drawingFile ? "has-file" : ""}`}
            onClick={() => drawingInputRef.current?.click()}
            onDragOver={(event) => { event.preventDefault(); event.currentTarget.classList.add("is-dragging"); }}
            onDragLeave={(event) => event.currentTarget.classList.remove("is-dragging")}
            onDrop={(event) => {
              event.preventDefault();
              event.currentTarget.classList.remove("is-dragging");
              const candidate = event.dataTransfer.files?.[0];
              if (!candidate) return;
              if (!/\.pdf$/i.test(candidate.name)) { setError("左侧请选择 PDF 工程图"); return; }
              setError("");
              setDrawingFile(candidate);
            }}
          >
            <input ref={drawingInputRef} type="file" accept=".pdf,application/pdf" hidden onChange={(event) => { setError(""); setDrawingFile(event.target.files?.[0] ?? null); }} />
            <span className="upload-type">PDF</span>
            <FileUp size={28} />
            <strong>{drawingFile ? drawingFile.name : "二维工程图"}</strong>
            <small>{drawingFile ? `${(drawingFile.size / 1024 / 1024).toFixed(2)} MB · 点击重新选择` : "尺寸、公差、粗糙度与技术要求"}</small>
          </button>
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
              if (!/\.(step|stp)$/i.test(candidate.name)) { setError("右侧请选择 STEP 或 STP 三维模型"); return; }
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
          <div>
            {newJobDevices.map((device) => <button className={selectedNewJobDeviceId === device.id ? "active" : ""} key={device.id} onClick={() => setSelectedNewJobDeviceId(device.id)}>
              <Cog size={16} />
              <span><strong>{device.display_name}</strong><small>{device.category_label}</small></span>
              <em className={device.library_status}>{device.library_status === "supported" ? "已支持" : "适配中"}</em>
            </button>)}
          </div>
        </section>
        {error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}
        <button className="primary-action" disabled={!stepFile || !drawingFile || !selectedNewJobDeviceId || busy} onClick={submit}>
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

function HistoryDialog({ activeJobId, onClose, onSelected }: { activeJobId: string; onClose: () => void; onSelected: (job: Job) => void }) {
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
        setItems((payload as JobHistoryItem[]).filter((item) => item.id !== activeJobId));
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
    if (!items.length || !window.confirm(`确定清空 ${items.length} 条历史记录吗？当前打开的任务会保留。`)) return;
    setClearing(true);
    setError("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs?preserve_job_id=${encodeURIComponent(activeJobId)}`), { method: "DELETE" });
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
  const fileMenuRef = useRef<HTMLDivElement>(null);
  const operationPopoverRef = useRef<HTMLElement>(null);
  const [showFileMenu, setShowFileMenu] = useState(false);
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
  const [inspectionPanel, setInspectionPanel] = useState<"drawing" | "coverage" | null>(null);
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
  const [showL32Workbench, setShowL32Workbench] = useState(false);

  useEffect(() => {
    if (!showFileMenu) return;
    const closeMenu = (event: MouseEvent) => {
      if (!fileMenuRef.current?.contains(event.target as Node)) setShowFileMenu(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => event.key === "Escape" && setShowFileMenu(false);
    window.addEventListener("mousedown", closeMenu);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("mousedown", closeMenu);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [showFileMenu]);

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
  const drawingRequirements = job.plan?.manufacturing_requirements;
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
        setDeviceLibrary(payload);
        setSelectedLibraryDeviceId((current) => current || payload.devices[0]?.id || "");
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

  const visibleToolpathSegments = useMemo(() => {
    if (activeMode === "刀路") return camResult?.preview_segments ?? [];
    if (activeMode !== "仿真" || !selectedOperation) return EMPTY_TOOLPATH_SEGMENTS;
    const selectedIndex = operations.findIndex((operation) => operation.id === selectedOperation.id);
    const visibleOperationIds = new Set(
      (playbackMode === "single"
        ? operations.slice(Math.max(selectedIndex, 0), Math.max(selectedIndex, 0) + 1)
        : operations.slice(0, Math.max(selectedIndex, 0) + 1)
      ).map((operation) => operation.id),
    );
    const visibleSegments = camResult?.preview_segments.filter(
      (segment) => visibleOperationIds.has(segment.operation_id),
    ) ?? EMPTY_TOOLPATH_SEGMENTS;
    return visibleSegments.some((segment) => segment.motion === "cut") ? visibleSegments : EMPTY_TOOLPATH_SEGMENTS;
  },
    [activeMode, camResult?.preview_segments, operations, playbackMode, selectedOperation],
  );
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
  };

  const openOperationDetails = (operation: Operation, anchor: HTMLElement) => {
    chooseOperation(operation);
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
        <div className="brand compact"><span>S</span> SEKSUN CNC</div>
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
          <div className="file-menu" ref={fileMenuRef}>
            <button className={`file-menu-trigger ${showFileMenu ? "open" : ""}`} aria-haspopup="menu" aria-expanded={showFileMenu} onClick={() => setShowFileMenu((value) => !value)}><FolderOpen size={14} />文件<ChevronDown size={13} /></button>
            {showFileMenu && <div className="file-menu-dropdown" role="menu">
              <button role="menuitem" onClick={() => { setShowFileMenu(false); onNew(); }}><FileUp size={15} />新建</button>
              <button role="menuitem" onClick={() => { setShowFileMenu(false); onHistory(); }}><History size={15} />历史记录</button>
            </div>}
          </div>
          <div className="admin-identity" title="当前用户"><UserRound size={14} /><strong>admin</strong><ChevronDown size={13} /></div>
        </div>
      </header>
      <nav className="compact-mode-toolbar" aria-label="工作模式">
        {["特征", "工艺", "刀路", "仿真"].map((mode) => <button key={mode} className={activeMode === mode ? "active" : ""} onClick={() => chooseMode(mode)}>{isSheetForming && mode === "刀路" ? "成形" : mode}</button>)}
        {isL32 && <button className="l32-mode-entry" onClick={() => setShowL32Workbench(true)}>L32 适配</button>}
      </nav>

      {showL32Workbench && <L32Workbench
        jobId={job.id}
        catalogs={catalogs}
        plannedOperations={job.plan.setups.flatMap((setup) => setup.operations)}
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
              <div><strong>选择设备</strong><small>左右滑动查看更多设备</small></div>
              <div className="operation-device-scroll">
                {(deviceLibrary?.devices ?? []).map((device) => <div className={`operation-device-option ${selectedLibraryDevice?.id === device.id ? "active" : ""}`} key={device.id}>
                  <button className="operation-device-select" onClick={() => setSelectedLibraryDeviceId(device.id)}>
                    <Cog size={15} /><span><strong>{device.display_name}</strong><small>{device.category_label}</small></span>
                  </button>
                  <button className={`operation-device-info ${deviceInfoId === device.id ? "active" : ""}`} aria-label={`查看 ${device.display_name} 设备详情`} title="设备详情" onClick={() => setDeviceInfoId((current) => current === device.id ? null : device.id)}><Info size={11} strokeWidth={2} /></button>
                </div>)}
              </div>
            </section>
            <div className="operation-library-grid">
              {libraryOperations.map((definition) => {
                const binding = selectedLibraryDevice?.operation_bindings?.find((item) => item.operation_id === definition.id);
                const adapting = binding?.status === "adapting" || definition.maturity === "planned" || selectedLibraryDevice?.library_status === "adapting";
                return <article key={definition.id}>
                  <div><span>{definition.category}</span><i className={adapting ? "adapting" : "supported"}>{adapting ? "适配中" : "已支持"}</i></div>
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
              <section className="device-profile-section"><h4>加工能力</h4><div className="device-capabilities">{infoLibraryDevice.capabilities.map((capability) => <span className={capability.status} key={capability.code}>{capability.name}<i>{capability.status === "supported" ? "支持" : capability.status === "conditional" ? "条件支持" : "待确认"}</i></span>)}</div></section>
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
                    <button key={operation.id} disabled={activeMode === "仿真" && Boolean(camResult) && !cutOperationIds.has(operation.id)} className={`${selectedOperation?.id === operation.id ? "selected" : ""} ${operation.enabled === false ? "suppressed" : ""} ${generatingCam && camProgress?.operation_id === operation.id ? "stream-active" : ""} ${activeMode === "仿真" && camResult && !cutOperationIds.has(operation.id) ? "unavailable" : ""}`} onClick={(event) => openOperationDetails(operation, event.currentTarget)}>
                      <span>{operation.id}</span><div><strong>{operation.name}</strong><small>{operation.tool.name}{activeMode === "仿真" && camResult && !cutOperationIds.has(operation.id) ? " · 无有效刀路" : ""}</small></div>
                    </button>
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
            <footer className={`operation-edit-footer ${!readOnly && editingOperationDetails ? "editing" : ""}`}>
              <span className="compact-confidence">置信度 <strong>{Math.round(selectedOperation.confidence * 100)}%</strong></span>
              {!readOnly && editingOperationDetails && <><button onClick={cancelOperationEdit} disabled={operationBusy}>取消</button><button className="primary" onClick={saveOperationEdits} disabled={operationBusy}>{operationBusy ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}{operationBusy ? "保存并校核中…" : "保存并重新检验"}</button></>}
            </footer>
          </section>}
        </aside>

        <section className={`viewport panel inspection-rail-host ${activeMode === "刀路" || activeMode === "仿真" ? "show-toolpath-legend" : ""}`}>
          {automationBlocked && <div className="capability-blocker">
            <div><AlertTriangle size={17} /><strong>已阻止生成不完整工艺</strong></div>
            {job.plan.blocking_reasons.map((reason) => <p key={reason}>{reason}</p>)}
          </div>}
          <ModelViewer
            modelUrl={`${apiUrl(job.model_url)}?solid=${selectedSolidIndex}`}
            features={viewerFeatures}
            selectedFeatureIds={selectedFeatureIds}
            onSelectFeature={chooseFeature}
            toolpathSegments={visibleToolpathSegments}
            initialToolpathSegments={initialToolpathSegments}
            profileBoundaries={visibleProfileBoundaries}
            simulation={visibleSimulation}
            camoticsSurface={visibleCamoticsSurface}
            fixtureComponents={visibleFixtureComponents}
            animateToolpath={activeMode === "仿真"}
            initialProgress={initialSimulationProgress}
            operationTools={operationTools}
            topologyEdges={visibleTopologyEdges}
            activeOperationId={selectedOperation?.id}
            isFinalOperation={selectedOperation?.id === operations[operations.length - 1]?.id}
            playbackMode={playbackMode}
            onPlaybackModeChange={setPlaybackMode}
            toolpathLoaded={!loadingCam}
            formingPreview={activeMode === "仿真" ? camResult?.forming_preview ?? null : null}
            spatialDefects={activeMode === "仿真" ? spatialDefects : null}
            onSelectDefect={chooseSpatialDefect}
            viewMode={activeMode as "特征" | "工艺" | "刀路" | "仿真"}
            workAxis={selectedSetup?.work_axis}
            activeOperationLabel={selectedOperation?.name}
          />
          <nav className="inspection-rail" aria-label="工程检查与工艺工具">
            {drawingRequirements && <button className={`inspection-card ${drawingRequirements.status} ${inspectionPanel === "drawing" ? "active" : ""}`} onPointerDown={(event) => event.stopPropagation()} onClick={() => setInspectionPanel((current) => current === "drawing" ? null : "drawing")} title="查看图纸要求绑定状态">
              {drawingRequirements.unresolved_requirement_ids.length > 0 && <em>{drawingRequirements.unresolved_requirement_ids.length}</em>}
              <FileUp size={19} />
              <strong>图纸要求</strong>
              <small>{drawingRequirements.summary.matched}/{drawingRequirements.summary.total}</small>
            </button>}
            {coverage && <button className={`inspection-card ${coverage.status} ${inspectionPanel === "coverage" ? "active" : ""}`} onPointerDown={(event) => event.stopPropagation()} onClick={() => setInspectionPanel((current) => current === "coverage" ? null : "coverage")} title="查看制造特征与工序覆盖状态">
              {coverage.unresolved_count > 0 && <em>{coverage.unresolved_count}</em>}
              <CircleDot size={19} />
              <strong>工艺覆盖</strong>
              <small>{Math.round(coverage.score * 100)}%</small>
            </button>}
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
            {isL32 && <button className="inspection-card l32" onClick={() => setShowL32Workbench(true)} title="打开 L32 车削适配工作台">
              <Cog size={19} />
              <strong>L32 适配</strong>
              <small>草案模式</small>
            </button>}
          </nav>
          {inspectionPanel && <section ref={inspectionPanelRef} className={`inspection-popover ${inspectionPanel}`} aria-label={inspectionPanel === "drawing" ? "图纸要求详情" : "工艺覆盖详情"}>
            <header>
              <div><small>{inspectionPanel === "drawing" ? "DRAWING REQUIREMENTS" : "PROCESS COVERAGE"}</small><strong>{inspectionPanel === "drawing" ? "图纸要求" : "工艺覆盖"}</strong></div>
              <span className={inspectionPanel === "drawing" ? drawingRequirements?.status : coverage?.status}>{inspectionPanel === "drawing" ? (drawingRequirements?.status === "complete" ? "已匹配" : "需处理") : `${Math.round((coverage?.score ?? 0) * 100)}%`}</span>
              <button aria-label="关闭检查面板" onClick={() => setInspectionPanel(null)}><X size={15} /></button>
            </header>
            {inspectionPanel === "drawing" && drawingRequirements ? <>
              <div className="inspection-overview">
                <div><small>要求总数</small><strong>{drawingRequirements.summary.total}</strong></div>
                <div><small>已绑定</small><strong>{drawingRequirements.summary.matched}</strong></div>
                <div><small>待处理</small><strong>{drawingRequirements.unresolved_requirement_ids.length}</strong></div>
              </div>
              <div className="inspection-popover-list">
                <article className={drawingRequirements.status}>
                  <div><span>绑定状态</span><em>{drawingRequirements.status === "complete" ? "完整" : "需复核"}</em></div>
                  <strong>二维要求与三维特征</strong>
                  <dl><div><dt>模糊匹配</dt><dd>{drawingRequirements.summary.ambiguous}</dd></div><div><dt>未映射</dt><dd>{drawingRequirements.summary.unmapped}</dd></div><div><dt>仅识别</dt><dd>{drawingRequirements.summary.recognized_only}</dd></div></dl>
                </article>
                {drawingRequirements.unresolved_requirement_ids.length > 0 && <article className="incomplete">
                  <div><span>决策限制</span><em>{drawingRequirements.unresolved_requirement_ids.length} 项</em></div>
                  <strong>尚未唯一绑定到三维特征</strong>
                  <p>这些要求仅作提示，不直接用于确定性工艺决策。</p>
                </article>}
                <article>
                  <div><span>来源</span><em>{drawingRequirements.source_system}</em></div>
                  <strong>{job.drawing_filename || "工程图"}</strong>
                  <p>{[drawingRequirements.drawing_number && `图号 ${drawingRequirements.drawing_number}`, drawingRequirements.revision && `版本 ${drawingRequirements.revision}`].filter(Boolean).join(" · ") || "未提供图号与版本"}</p>
                </article>
              </div>
            </> : coverage && <>
              <div className="inspection-overview">
                <div><small>加工特征</small><strong>{coverage.target_count}</strong></div>
                <div><small>已覆盖</small><strong>{coverage.covered_count}</strong></div>
                <div><small>待处理</small><strong>{coverage.unresolved_count + coverage.review_count}</strong></div>
              </div>
              <div className="inspection-popover-list">
                {coverage.targets.map((target) => <article key={target.id} className={target.state}>
                  <div><span>{target.kind}</span><em>{target.state === "covered" ? "已覆盖" : target.state === "review" ? "待复核" : "未覆盖"}</em></div>
                  <strong>{target.label}</strong>
                  <p>{target.covered_by.length ? `关联工序 ${target.covered_by.join("、")}` : `需要 ${target.required_operation_types.join("、")}`}</p>
                </article>)}
                {[...coverage.issues, ...coverage.capability_gaps].map((message, index) => <article className="incomplete" key={`${index}-${message}`}><div><span>检查提醒</span><em>注意</em></div><p>{message}</p></article>)}
              </div>
            </>}
          </section>}
          {!readOnly && activeMode === "刀路" && isL32 && <button className="viewport-generate-button l32-draft-button" onClick={() => setShowL32Workbench(true)}><Cog size={16} />打开 L32 车削草案</button>}
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
    const restoreFromLocation = async () => {
      const match = window.location.pathname.match(/^\/jobs\/([0-9a-f]{32})\/?$/);
      if (!match) {
        setJob(null);
        setReadOnly(false);
        setShowNewJob(true);
        setLoadingSession(false);
        return;
      }
      setLoadingSession(true);
      setSessionError("");
      try {
        const response = await fetch(apiUrl(`/api/v1/jobs/${match[1]}`));
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
      : <main className="empty-workspace">
          <header><div className="brand"><span>S</span> SEKSUN CNC</div></header>
          <section>{sessionError ? <AlertTriangle /> : <Box />}<strong>{sessionError || "尚未打开零件"}</strong><small>新任务将在当前工作台中创建</small><button onClick={() => setShowNewJob(true)}><FileUp size={15} />上传 STEP</button></section>
        </main>}
    <NewJobDialog open={showNewJob} canClose={Boolean(job)} onClose={() => setShowNewJob(false)} onCreated={openJob} />
    {showHistory && job && <HistoryDialog activeJobId={job.id} onClose={() => setShowHistory(false)} onSelected={openJob} />}
  </>;
}

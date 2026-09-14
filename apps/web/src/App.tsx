import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bot,
  Box,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Clock3,
  Download,
  FileUp,
  Layers3,
  Library,
  Link2,
  LoaderCircle,
  Pencil,
  Play,
  RefreshCw,
  Rotate3D,
  Settings2,
  ShieldCheck,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { ModelViewer } from "./ModelViewer";
import type { AIProcessReviewResult, CamResult, Catalogs, Job, ManufacturingFeature, Operation, OperationDefinition, SpatialDefectRegion, ToolpathSegment, Vec3 } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const EMPTY_TOOLPATH_SEGMENTS: ToolpathSegment[] = [];
const EMPTY_PROFILE_BOUNDARIES: { operation_id: string; setup_id: string; work_axis: Vec3; points: Vec3[] }[] = [];
const PROCESS_PHASES = ["基准", "粗加工", "孔加工", "精加工", "切断", "完成"] as const;

function processPhase(operationType: string): typeof PROCESS_PHASES[number] {
  if (operationType === "face_milling" || operationType.includes("preform")) return "基准";
  if (operationType.includes("roughing") || operationType === "adaptive_clearing") return "粗加工";
  if (["drilling", "helical_boring", "tapping", "reaming"].includes(operationType)) return "孔加工";
  if (operationType === "internal_profile_finishing") return "精加工";
  if (operationType.includes("profile") || operationType === "tab_removal") return "切断";
  if (["edge_chamfer", "deburring", "inspection"].includes(operationType)) return "完成";
  return "精加工";
}

function apiUrl(path: string) {
  return `${API_BASE}${path}`;
}

function NewJobDialog({ open, onClose, onCreated, canClose = true }: {
  open: boolean;
  onClose: () => void;
  onCreated: (job: Job) => void;
  canClose?: boolean;
}) {
  const stepInputRef = useRef<HTMLInputElement>(null);
  const drawingInputRef = useRef<HTMLInputElement>(null);
  const [stepFile, setStepFile] = useState<File | null>(null);
  const [drawingFile, setDrawingFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || !canClose) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, canClose, onClose, open]);

  const submit = async () => {
    if (!stepFile || !drawingFile) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.append("step", stepFile);
    form.append("drawing", drawingFile);
    try {
      const response = await fetch(apiUrl("/api/v1/jobs"), { method: "POST", body: form });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "分析失败");
      onCreated(payload as Job);
      setStepFile(null);
      setDrawingFile(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法连接分析服务");
    } finally {
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
        <div className="upload-pair">
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
            <strong>{stepFile ? stepFile.name : "三维零件模型"}</strong>
            <small>{stepFile ? `${(stepFile.size / 1024 / 1024).toFixed(2)} MB · 点击重新选择` : "精确 B-Rep 几何与制造特征"}</small>
          </button>
        </div>
        {error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}
        <button className="primary-action" disabled={!stepFile || !drawingFile || busy} onClick={submit}>
          {busy ? <><LoaderCircle className="spin" size={17} /> 正在分析并生成工艺…</> : <>分析并生成工艺 <ChevronRight size={17} /></>}
        </button>
      </section>
    </div>
  );
}

function Workbench({ initialJob, onNew, readOnly = false }: { initialJob: Job; onNew: () => void; readOnly?: boolean }) {
  const [job, setJob] = useState(initialJob);
  const [selectedOperation, setSelectedOperation] = useState<Operation | null>(job.plan?.setups[0]?.operations[0] ?? null);
  const [selectedFeatureIds, setSelectedFeatureIds] = useState<string[]>(selectedOperation?.feature_ids ?? []);
  const [activeMode, setActiveMode] = useState("工艺");
  const [approving, setApproving] = useState(false);
  const [generatingCam, setGeneratingCam] = useState(false);
  const [loadingCam, setLoadingCam] = useState(readOnly);
  const [camResult, setCamResult] = useState<CamResult | null>(null);
  const [camError, setCamError] = useState("");
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
  const [shareBaseUrl, setShareBaseUrl] = useState(window.location.origin);
  const [shareMessage, setShareMessage] = useState("");
  const [reanalyzing, setReanalyzing] = useState(false);
  const [showSimulationChecks, setShowSimulationChecks] = useState(false);
  const [applyingRemediation, setApplyingRemediation] = useState(false);
  const [showWarnings, setShowWarnings] = useState(false);
  const [catalogs, setCatalogs] = useState<Catalogs | null>(null);
  const [showOperationLibrary, setShowOperationLibrary] = useState(false);
  const [libraryFeatureIds, setLibraryFeatureIds] = useState<string[]>([]);
  const [operationMessage, setOperationMessage] = useState("");
  const [operationBusy, setOperationBusy] = useState(false);
  const [solidBusy, setSolidBusy] = useState(false);
  const [aiReview, setAiReview] = useState<AIProcessReviewResult | null>(null);
  const [aiReviewBusy, setAiReviewBusy] = useState(false);
  const [aiReviewError, setAiReviewError] = useState("");
  const [showAiReview, setShowAiReview] = useState(false);
  const [parameterEdits, setParameterEdits] = useState<Record<string, Record<string, string | number | boolean>>>({});
  const [structureExpanded, setStructureExpanded] = useState(true);
  const [showOperationDetails, setShowOperationDetails] = useState(false);
  const [editingOperationDetails, setEditingOperationDetails] = useState(false);
  const [toolDraftId, setToolDraftId] = useState(selectedOperation?.tool.id ?? "");
  const [operationPopoverPosition, setOperationPopoverPosition] = useState({ top: 110, left: 326, anchorY: 28 });
  const [playbackMode, setPlaybackMode] = useState<"single" | "cumulative">("single");
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
  const enabledOperations = operations.filter((operation) => operation.enabled !== false);
  const isSheetForming = job.plan?.process_kind === "sheet_forming";
  const planApproved = enabledOperations.length > 0 && enabledOperations.every((operation) => operation.status === "approved");
  const automationBlocked = job.plan?.automation_status === "unsupported";
  const aiApprovalBlocked = aiReview?.review.approval_blocked === true;
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
  const warningMessages = [
    ...(drawingRequirements?.unresolved_requirement_ids.length
      ? [`图纸要求：${drawingRequirements.unresolved_requirement_ids.length} 项尚未唯一绑定到三维特征，不得直接用于确定性工艺决策。`] : []),
    ...(coverage?.issues.map((item) => `覆盖检查：${item}`) ?? []),
    ...(coverage?.capability_gaps.map((item) => `能力缺口：${item}`) ?? []),
    ...(job.plan?.warnings ?? []),
  ];
  const warningCount = warningMessages.length;
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
  const processPhases = useMemo(
    () => PROCESS_PHASES.map((name) => ({
      name,
      operations: operations.filter((operation) => processPhase(operation.type) === name),
    })).filter((phase) => phase.operations.length > 0),
    [operations],
  );

  useEffect(() => {
    if (!showOperationDetails) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setEditingOperationDetails(false);
        setShowOperationDetails(false);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [showOperationDetails]);

  const selectedFeatures = useMemo(
    () => manufacturingFeatures.filter((feature) => selectedFeatureIds.includes(feature.id)),
    [manufacturingFeatures, selectedFeatureIds],
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
  const selectableGeometry = useMemo(() => [
    ...(job.analysis?.planar_features ?? []).map((feature) => ({ id: feature.id, type: "planar_face", label: `平面 ${feature.id} · ${feature.area.toFixed(1)} mm²` })),
    ...(job.analysis?.cylindrical_features ?? []).filter((feature) => feature.kind === "hole" && feature.review_state !== "excluded").map((feature) => ({ id: feature.id, type: "cylindrical_hole", label: `孔 ${feature.id} · Ø${feature.diameter.toFixed(2)} × ${feature.length.toFixed(2)}` })),
    ...(job.analysis?.prismatic_features ?? []).filter((feature) => feature.review_state !== "excluded").map((feature) => ({ id: feature.id, type: `prismatic_${feature.kind}`, label: `${feature.kind === "pocket" ? "型腔" : "槽"} ${feature.id} · ${feature.length.toFixed(1)} × ${feature.width.toFixed(1)}` })),
    ...(job.analysis?.internal_profile_features ?? []).filter((feature) => feature.review_state !== "excluded").map((feature) => ({
      id: feature.id,
      type: "internal_profile",
      label: `${feature.machining_kind === "engraving" ? "浅雕刻" : feature.machining_kind === "blind_pocket" ? "异形盲型腔" : "异形贯通孔"} ${feature.id} · ${feature.length.toFixed(1)} × ${feature.width.toFixed(1)}`,
    })),
  ], [job.analysis?.cylindrical_features, job.analysis?.internal_profile_features, job.analysis?.planar_features, job.analysis?.prismatic_features]);

  useEffect(() => {
    fetch(apiUrl("/api/v1/catalogs"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: Catalogs) => setCatalogs(payload))
      .catch(() => setOperationMessage("工序库暂时无法加载"));
  }, []);

  useEffect(() => {
    fetch(apiUrl(`/api/v1/jobs/${job.id}/ai/plan`))
      .then((response) => response.ok ? response.json() : null)
      .then((payload: AIProcessReviewResult | null) => {
        if (payload) setAiReview(payload);
      })
      .catch(() => undefined);
  }, [job.id]);

  const runAiReview = async () => {
    setAiReviewBusy(true);
    setAiReviewError("");
    setShowAiReview(true);
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/ai/plan`), { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "AI 工艺审查失败");
      setAiReview(payload as AIProcessReviewResult);
    } catch (reason) {
      setAiReviewError(reason instanceof Error ? reason.message : "无法连接 Qwen 工艺中枢");
    } finally {
      setAiReviewBusy(false);
    }
  };

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
    fetch(apiUrl("/api/v1/config"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((config: { public_base_url?: string }) => {
        if (config.public_base_url) setShareBaseUrl(config.public_base_url);
      })
      .catch(() => undefined);
  }, [job.id]);

  useEffect(() => {
    const needsCam = readOnly || activeMode === "刀路" || activeMode === "仿真";
    if (!needsCam || camResult) return;
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
  }, [activeMode, camResult, job.id, operations, readOnly]);

  const copyShareLink = async () => {
    const shareUrl = `${shareBaseUrl}/jobs/${job.id}?view=1`;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(shareUrl);
      } else {
        const input = document.createElement("textarea");
        input.value = shareUrl;
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.appendChild(input);
        input.select();
        document.execCommand("copy");
        input.remove();
      }
      setShareMessage(`已复制：${shareUrl}`);
    } catch {
      setShareMessage(`复制失败，请手动复制：${shareUrl}`);
    }
  };

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
    setLoadingCam((mode === "刀路" || mode === "仿真") && !camResult);
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
    setActiveMode("特征");
    setOperationPopoverPosition({ top: 110, left: Math.min(326, Math.max(12, window.innerWidth - 372)), anchorY: 28 });
    setShowOperationDetails(true);
    if (firstReviewFeature) chooseFeature(firstReviewFeature.id);
  };

  const navigateOperation = (direction: -1 | 1) => {
    if (!selectedOperation) return;
    const index = operations.findIndex((operation) => operation.id === selectedOperation.id);
    const target = operations[index + direction];
    if (target) chooseOperation(target);
  };

  const approve = async () => {
    setApproving(true);
    setSafetyMessage("");
    const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/approve`), { method: "POST" });
    const payload = await response.json();
    if (response.ok) setJob(payload as Job);
    else setSafetyMessage(payload.detail || "方案批准失败");
    setApproving(false);
  };

  const reanalyze = async () => {
    setReanalyzing(true);
    setSafetyMessage("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/reanalyze`), { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "重新分析失败");
      const updatedJob = payload as Job;
      const updatedOperations = updatedJob.plan?.setups.flatMap((setup) => setup.operations) ?? [];
      setJob(updatedJob);
      setAiReview(null);
      setCamResult(null);
      setLoadingCam(activeMode === "刀路" || activeMode === "仿真");
      setSelectedOperation(updatedOperations[0] ?? null);
      setSelectedFeatureIds(updatedOperations[0]?.feature_ids ?? []);
      setClearance(updatedJob.plan?.safety?.clearance_mm ?? 3);
      setViseGripHeight(updatedJob.plan?.safety?.vise_grip_height_mm ?? 1.5);
      setSupportThickness(updatedJob.plan?.safety?.support_thickness_mm ?? 3);
      setSafetyMessage("已使用最新识别规则重新分析");
    } catch (reason) {
      setSafetyMessage(reason instanceof Error ? reason.message : "重新分析失败");
    } finally {
      setReanalyzing(false);
    }
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
    setAiReview(null);
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
    setAiReview(null);
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
      setAiReview(null);
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

  const createLibraryOperation = async (definition: OperationDefinition) => {
    if (!selectedSetup || !definition.manual_enabled) return;
    setOperationBusy(true);
    setOperationMessage("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/setups/${selectedSetup.id}/operations`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ definition_id: definition.id, feature_ids: libraryFeatureIds }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "创建工序失败");
      const updatedJob = payload as Job;
      const created = updatedJob.plan?.setups
        .find((setup) => setup.id === selectedSetup.id)
        ?.operations.at(-1);
      applyUpdatedJob(updatedJob, created?.id);
      setShowOperationLibrary(false);
      setOperationMessage(`已创建${definition.name}，请检查几何、刀具和参数`);
    } catch (reason) {
      setOperationMessage(reason instanceof Error ? reason.message : "创建工序失败");
    } finally {
      setOperationBusy(false);
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
        <div className="top-meta"><span>{job.material}</span>{readOnly && <span className="readonly-badge">只读分享</span>}</div>
        <div className="header-actions">
          <button className="ghost share-button" onClick={copyShareLink}><Link2 size={14} />分享</button>
          {!readOnly && <button className="ghost reanalyze-button" onClick={reanalyze} disabled={reanalyzing}><RefreshCw className={reanalyzing ? "spin" : ""} size={14} />{reanalyzing ? "分析中" : "重新分析"}</button>}
          <button className="ghost new-job-button" onClick={onNew}><FileUp size={14} />新建</button>
          {!readOnly && <button className={`approve ${automationBlocked || aiApprovalBlocked ? "blocked" : ""}`} onClick={approve} disabled={approving || applyingRemediation || automationBlocked || aiApprovalBlocked || planApproved}>{automationBlocked || aiApprovalBlocked ? <AlertTriangle size={15} /> : <Check size={15} />}{automationBlocked ? "无法自动规划" : aiApprovalBlocked ? "AI 阻止批准" : applyingRemediation ? "自动纠错中" : approving ? "确认中" : planApproved ? "工艺已批准" : "批准方案"}</button>}
        </div>
      </header>
      <nav className="mode-tabs workspace-toolbar">
        <div className="mode-switcher">
          {["特征", "工艺", "刀路", "仿真"].map((mode) => <button key={mode} className={activeMode === mode ? "active" : ""} onClick={() => chooseMode(mode)}>{isSheetForming && mode === "刀路" ? "成形" : mode}</button>)}
        </div>
        <div className={`plan-state ${reviewCount > 0 ? "needs-review" : ""}`}>
          <i />
          {sourceSolids > 1 && solidCandidates.length > 0 ? <label className="solid-selector">
            <span>{sourceSolids} 个实体</span>
            <select
              aria-label="选择目标加工实体"
              disabled={readOnly || solidBusy}
              value={selectedSolidIndex}
              onChange={(event) => selectSolid(Number(event.target.value))}
            >
              {solidCandidates.map((candidate) => <option key={candidate.index} value={candidate.index}>
                实体 {candidate.index} · {candidate.bounds.size.x.toFixed(1)}×{candidate.bounds.size.y.toFixed(1)}×{candidate.bounds.size.z.toFixed(1)} mm · {candidate.volume.toFixed(0)} mm³
              </option>)}
            </select>
          </label> : null}
          <span>{solidBusy ? "重新规划中…" : `${job.plan.setups.length} 次装夹 · ${operations.length} 道工序`}</span>
        </div>
        <div className="context-actions">
          {drawingRequirements && <button className={`drawing-requirements-button ${drawingRequirements.status}`} onClick={() => setShowWarnings(true)} title="查看图纸要求绑定状态"><FileUp size={14} />图纸 {drawingRequirements.summary.matched}/{drawingRequirements.summary.total}</button>}
          {coverage && <button className={`coverage-button ${coverage.status}`} onClick={() => setShowWarnings(true)}><CircleDot size={14} />覆盖 {Math.round(coverage.score * 100)}%{coverage.unresolved_count > 0 ? ` · ${coverage.unresolved_count} 未识别` : ""}</button>}
          {!readOnly && <button className="ai-review-button" disabled={aiReviewBusy} onClick={runAiReview}><Bot className={aiReviewBusy ? "spin" : ""} size={15} />{aiReviewBusy ? "AI 审查中" : "AI 工艺审查"}</button>}
          {readOnly && aiReview && <button className="ai-review-button" onClick={() => setShowAiReview(true)}><Bot size={15} />AI 审查结果</button>}
          {reviewCount > 0 && <button className="review-queue-button" onClick={openReviewQueue}><AlertTriangle size={15} />{reviewCount} 项待复核</button>}
          {!readOnly && !isSheetForming && <button onClick={() => { setLibraryFeatureIds(selectedFeatureIds); setShowOperationLibrary(true); }}><Library size={15} />工序库</button>}
          {!readOnly && <button className="primary" disabled={automationBlocked || !planApproved || generatingCam || applyingRemediation} onClick={generateCam}><Play size={15} />{generatingCam ? `生成中 ${Math.round(camProgress?.percent ?? 0)}%` : applyingRemediation ? `纠错中 ${Math.round(camProgress?.percent ?? 0)}%` : isSheetForming ? "生成成形仿真" : "生成刀路"}</button>}
        </div>
      </nav>

      {showOperationLibrary && <div className="operation-library-backdrop" onMouseDown={() => setShowOperationLibrary(false)}>
        <section className="operation-library-dialog" onMouseDown={(event) => event.stopPropagation()}>
          <header><div><Library size={18} /><strong>工序库</strong><small>FREECAD CAM · 选择工序后使用当前几何创建</small></div><button onClick={() => setShowOperationLibrary(false)}><X size={16} /></button></header>
          <div className="operation-library-summary">
            <span>当前装夹 <strong>{selectedSetup?.id}</strong></span>
            <span>已选几何 <strong>{libraryFeatureIds.length}</strong></span>
            <span>可人工创建 <strong>{catalogs?.operations.filter((item) => item.manual_enabled).length ?? 0}</strong></span>
          </div>
          {operationMessage && <p className="operation-library-message"><AlertTriangle size={14} />{operationMessage}</p>}
          <div className="operation-library-geometry"><strong>加工几何</strong><span>先选择面、孔或型腔，再创建适用工序</span><div>{selectableGeometry.map((geometry) => <button key={geometry.id} className={libraryFeatureIds.includes(geometry.id) ? "selected" : ""} onClick={() => setLibraryFeatureIds((current) => current.includes(geometry.id) ? current.filter((id) => id !== geometry.id) : [...current, geometry.id])}><i>{geometry.type}</i>{geometry.label}</button>)}</div></div>
          <div className="operation-library-grid">
            {(catalogs?.operations ?? []).map((definition) => <button key={definition.id} disabled={!definition.manual_enabled || operationBusy} onClick={() => createLibraryOperation(definition)}>
              <div><span>{definition.category}</span><i className={definition.maturity}>{definition.maturity}</i></div>
              <strong>{definition.name}</strong>
              <p>{definition.description}</p>
              <small>{definition.engine.provider} / {definition.engine.operation}{definition.engine.modifiers.length ? ` + ${definition.engine.modifiers.join("+")}` : ""}</small>
              {!definition.manual_enabled && <em>尚未接入当前执行适配器</em>}
            </button>)}
          </div>
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

          {showOperationDetails && selectedOperation && <section className="operation-popover inspector panel" style={{ top: operationPopoverPosition.top, left: operationPopoverPosition.left, "--operation-anchor-y": `${operationPopoverPosition.anchorY}px` } as CSSProperties}>
            <header className="operation-popover-heading"><div><Bot size={16} /><span>工序详情</span><small>{selectedOperation.id}</small></div><div className="operation-popover-actions">{!readOnly && !editingOperationDetails && <button className="edit-operation-button" aria-label="编辑工序" title="编辑" onClick={() => { setEditingOperationDetails(true); setToolDraftId(selectedOperation.tool.id); setOperationMessage(""); }}><Pencil size={13} /></button>}<button aria-label="关闭工序详情" title="关闭" onClick={() => { setEditingOperationDetails(false); setShowOperationDetails(false); }}><X size={15} /></button></div></header>
            <div className="panel-content">
              {selectedOperation ? (
                <>
                  <div className="confidence"><span>建议置信度</span><strong>{Math.round(selectedOperation.confidence * 100)}%</strong><div><i style={{ width: `${selectedOperation.confidence * 100}%` }} /></div></div>
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
            {!readOnly && editingOperationDetails && <footer className="operation-edit-footer"><button onClick={cancelOperationEdit} disabled={operationBusy}>取消</button><button className="primary" onClick={saveOperationEdits} disabled={operationBusy}>{operationBusy ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}{operationBusy ? "保存并校核中…" : "保存并重新检验"}</button></footer>}
          </section>}
        </aside>

        <section className="viewport panel">
          {automationBlocked && <div className="capability-blocker">
            <div><AlertTriangle size={17} /><strong>已阻止生成不完整工艺</strong></div>
            {job.plan.blocking_reasons.map((reason) => <p key={reason}>{reason}</p>)}
          </div>}
          <ModelViewer
            modelUrl={`${apiUrl(job.model_url)}?solid=${selectedSolidIndex}`}
            features={manufacturingFeatures}
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
          />
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
            </> : <p>批准方案并生成刀路后执行机床行程、参数范围、刀具直径和工序覆盖校验。</p>}
          </div>}
        </section>

      </section>

      {showWarnings && <div className="warning-drawer">
          <header><div><AlertTriangle size={15} /><strong>任务提醒</strong><span>{warningCount}</span></div><button aria-label="关闭任务提醒" onClick={() => setShowWarnings(false)}><X size={14} /></button></header>
          <div>{warningMessages.map((warning, index) => <p key={`${index}-${warning}`}><span>{index + 1}</span>{warning}</p>)}</div>
        </div>}
      {showAiReview && <div className="ai-review-drawer">
        <header><div><Bot size={16} /><strong>Qwen 工艺中枢</strong>{aiReview && <span className={aiReview.review.approval_blocked ? "blocked" : "ready"}>{aiReview.review.approval_blocked ? "阻止批准" : "建议复核"}</span>}</div><button aria-label="关闭 AI 审查" onClick={() => setShowAiReview(false)}><X size={14} /></button></header>
        <div className="ai-review-content">
          {aiReviewBusy && <div className="ai-review-loading"><LoaderCircle className="spin" size={18} /><div><strong>正在综合几何、工艺与仿真结果</strong><small>通常需要 20–60 秒，请勿重复提交</small></div></div>}
          {aiReviewError && <div className="inline-error"><AlertTriangle size={15} />{aiReviewError}</div>}
          {aiReview && !aiReviewBusy && <>
            <div className="ai-review-summary"><div><small>制造意图</small><strong>{aiReview.review.manufacturing_intent}</strong></div><div><small>建议路线</small><strong>{aiReview.review.recommended_process_kind}</strong></div><div><small>置信度</small><strong>{Math.round(aiReview.review.confidence * 100)}%</strong></div><div><small>耗时</small><strong>{(aiReview.latency_ms / 1000).toFixed(1)}s</strong></div></div>
            <p className="ai-review-conclusion">{aiReview.review.summary}</p>
            <section><h4>装夹策略</h4>{aiReview.review.setup_strategy.map((item, index) => <p key={`${index}-${item}`}><span>{index + 1}</span>{item}</p>)}</section>
            <section><h4>工序建议</h4>{aiReview.review.operation_recommendations.length ? [...aiReview.review.operation_recommendations].sort((a, b) => a.priority - b.priority).map((item, index) => <p key={`${index}-${item.operation_id}-${item.action}`}><span>{item.action}</span><b>{item.operation_id || item.operation_type}</b>{item.reason}</p>) : <small>当前没有新增或修改建议</small>}</section>
            <section><h4>制造风险</h4>{aiReview.review.risks.map((risk) => <p className={`risk-${risk.severity}`} key={risk.code}><span>{risk.severity}</span><b>{risk.code}</b>{risk.description}；{risk.recommended_action}</p>)}</section>
            {aiReview.review.missing_information.length > 0 && <section><h4>缺失信息</h4>{aiReview.review.missing_information.map((item) => <p key={item}><AlertTriangle size={12} />{item}</p>)}</section>}
            <footer>{aiReview.model} · {aiReview.usage?.total_tokens?.toLocaleString() ?? "—"} tokens · AI 仅提供建议，仍需确定性校验和工程师批准</footer>
          </>}
        </div>
      </div>}
      <section className="operation-deck compact-operation-deck panel">
        <div className="compact-route">
          <div className="route-current"><Settings2 size={15} /><div><strong>{selectedOperation?.name ?? "未选择工序"}</strong><span>{selectedOperation ? `${selectedOperation.id} · ${operations.findIndex((operation) => operation.id === selectedOperation.id) + 1}/${operations.length}` : `0/${operations.length}`}</span></div></div>
          <div className="deck-actions route-navigation">
            <button aria-label="上一道工序" title="上一道工序" disabled={!selectedOperation || operations[0]?.id === selectedOperation.id} onClick={() => navigateOperation(-1)}><ChevronLeft size={14} /></button>
            <button aria-label="下一道工序" title="下一道工序" disabled={!selectedOperation || operations[operations.length - 1]?.id === selectedOperation.id} onClick={() => navigateOperation(1)}><ChevronRight size={14} /></button>
          </div>
          <div className="process-phase-strip" aria-label="加工阶段">
            {processPhases.map((phase) => {
              const selectedIndex = operations.findIndex((operation) => operation.id === selectedOperation?.id);
              const phaseIndices = phase.operations.map((operation) => operations.findIndex((item) => item.id === operation.id));
              const active = phase.operations.some((operation) => operation.id === selectedOperation?.id);
              const completed = selectedIndex >= 0 && Math.max(...phaseIndices) < selectedIndex;
              const streamActive = phase.operations.some((operation) => operation.id === camProgress?.operation_id);
              const target = activeMode === "仿真" && camResult
                ? phase.operations.find((operation) => cutOperationIds.has(operation.id))
                : phase.operations[0];
              return <button
                key={phase.name}
                className={`${active ? "active" : ""} ${completed ? "completed" : ""} ${streamActive ? "stream-active" : ""}`}
                disabled={!target}
                onClick={() => target && chooseOperation(target)}
                title={`${phase.name} · ${phase.operations.length} 道工序`}
              ><i /><span>{phase.name}</span><small>{phase.operations.length}</small></button>;
            })}
          </div>
          <span className="route-estimate"><Clock3 size={13} />{camResult ? "刀路" : "规划"} {camResult?.verification.metrics.estimated_cycle_minutes ?? job.plan.estimated_minutes} min</span>
        </div>
        <div className="warnings">
          <AlertTriangle size={15} />
          <span>{shareMessage || camError || safetyMessage || (generatingCam ? camProgress?.message : "") || (camResult ? isSheetForming ? `${camResult.engine} · ${camResult.generated_operations.length} 个阶段 · 工艺校验 ${validationStatus}` : `FreeCAD ${camResult.engine_version} 原生 CAM · ${camResult.path_command_count} 条指令 · 刀路校验 ${validationStatus}` : warningMessages[0])}</span>
          {warningCount > 0 && <button className="warning-count-button" onClick={() => setShowWarnings((value) => !value)}><AlertTriangle size={14} />{warningCount} 条提醒</button>}
          <button onClick={() => window.open(apiUrl(`/api/v1/jobs/${job.id}/files/plan.json`))}><Download size={14} />工艺 JSON</button>
          {camResult && !isSheetForming && <button disabled={validationStatus === "failed"} title={validationStatus === "failed" ? "空间成品校验未通过，禁止下载上机程序" : "下载 G-code 草案"} onClick={() => window.open(apiUrl(camResult.files.gcode))}><Download size={14} />{validationStatus === "failed" ? "G-code 已拦截" : "G-code 草案"}</button>}
          {camResult && !isSheetForming && <button onClick={() => window.open(apiUrl(camResult.files.freecad))}><Download size={14} />FreeCAD</button>}
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.verification))}><ShieldCheck size={14} />预检报告</button>}
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.collision))}><AlertTriangle size={14} />碰撞报告</button>}
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.simulation))}><Box size={14} />仿真数据</button>}
        </div>
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
    setSessionError("");
  };

  if (loadingSession) return <div className="fatal-state"><LoaderCircle className="spin" />正在加载任务会话…</div>;
  return <>
    {job
      ? <Workbench key={job.id} initialJob={job} onNew={() => setShowNewJob(true)} readOnly={readOnly} />
      : <main className="empty-workspace">
          <header><div className="brand"><span>S</span> SEKSUN CNC</div></header>
          <section>{sessionError ? <AlertTriangle /> : <Box />}<strong>{sessionError || "尚未打开零件"}</strong><small>新任务将在当前工作台中创建</small><button onClick={() => setShowNewJob(true)}><FileUp size={15} />上传 STEP</button></section>
        </main>}
    <NewJobDialog open={showNewJob} canClose={Boolean(job)} onClose={() => setShowNewJob(false)} onCreated={openJob} />
  </>;
}

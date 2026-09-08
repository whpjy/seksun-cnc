import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bot,
  Box,
  Check,
  ChevronRight,
  CircleDot,
  Clock3,
  Download,
  FileUp,
  Layers3,
  Library,
  Link2,
  LoaderCircle,
  Play,
  RefreshCw,
  Rotate3D,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { ModelViewer } from "./ModelViewer";
import type { CamResult, Catalogs, Job, ManufacturingFeature, Operation, OperationDefinition, ToolpathSegment, Vec3 } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const EMPTY_TOOLPATH_SEGMENTS: ToolpathSegment[] = [];
const EMPTY_PROFILE_BOUNDARIES: { operation_id: string; setup_id: string; work_axis: Vec3; points: Vec3[] }[] = [];

function apiUrl(path: string) {
  return `${API_BASE}${path}`;
}

function UploadScreen({ onCreated }: { onCreated: (job: Job) => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [material, setMaterial] = useState("6061-T6 铝合金");
  const [machine, setMachine] = useState("三轴立式加工中心");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [catalogs, setCatalogs] = useState<Catalogs | null>(null);

  useEffect(() => {
    fetch(apiUrl("/api/v1/catalogs"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: Catalogs) => setCatalogs(payload))
      .catch(() => setError("参数库暂不可用，已保留默认选项"));
  }, []);

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.append("step", file);
    form.append("material", material);
    form.append("machine", machine);
    try {
      const response = await fetch(apiUrl("/api/v1/jobs"), { method: "POST", body: form });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "分析失败");
      onCreated(payload as Job);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法连接分析服务");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="landing">
      <header className="landing-header">
        <div className="brand"><span>S</span> SEKSUN CNC</div>
        <div className="system-state"><i /> Geometry engine ready</div>
      </header>
      <section className="hero">
        <div className="eyebrow"><Sparkles size={14} /> INTELLIGENT PROCESS PLANNING</div>
        <h1>从几何事实到<br /><em>可审查的加工工艺</em></h1>
        <p>上传 STEP，自动识别制造特征、规划装夹与工序。每一步都有依据，每一个结果都由工程师确认。</p>
      </section>
      <section className="upload-card">
        <button className={`drop-zone ${file ? "has-file" : ""}`} onClick={() => inputRef.current?.click()}>
          <input
            ref={inputRef}
            type="file"
            accept=".step,.stp"
            hidden
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          {file ? <><Box size={28} /><strong>{file.name}</strong><small>{(file.size / 1024 / 1024).toFixed(2)} MB · STEP</small></> : <><FileUp size={28} /><strong>选择 STEP / STP 零件</strong><small>服务端使用 OCCT 精确解析 B-Rep</small></>}
        </button>
        <div className="form-row">
          <label>材料<select value={material} onChange={(event) => setMaterial(event.target.value)}>{(catalogs?.materials.map((item) => item.name) ?? ["6061-T6 铝合金", "7075-T6 铝合金", "S45C", "SUS304"]).map((name) => <option key={name}>{name}</option>)}</select></label>
          <label>目标设备<select value={machine} onChange={(event) => setMachine(event.target.value)}>{(catalogs?.machines.map((item) => item.name) ?? ["三轴立式加工中心", "四轴加工中心", "五轴加工中心"]).map((name) => <option key={name}>{name}</option>)}</select></label>
        </div>
        {error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}
        <button className="primary-action" disabled={!file || busy} onClick={submit}>
          {busy ? <><LoaderCircle className="spin" size={17} /> 正在识别几何与规划工艺…</> : <>开始智能规划 <ChevronRight size={17} /></>}
        </button>
        <div className="safety-note"><ShieldCheck size={15} /> 工艺输出为工程草案，不会未经审核生成上机指令</div>
      </section>
    </main>
  );
}

function Workbench({ initialJob, onReset, readOnly = false }: { initialJob: Job; onReset: () => void; readOnly?: boolean }) {
  const [job, setJob] = useState(initialJob);
  const [selectedOperation, setSelectedOperation] = useState<Operation | null>(job.plan?.setups[0]?.operations[0] ?? null);
  const [selectedFeatureIds, setSelectedFeatureIds] = useState<string[]>(selectedOperation?.feature_ids ?? []);
  const [activeMode, setActiveMode] = useState("工艺");
  const [approving, setApproving] = useState(false);
  const [generatingCam, setGeneratingCam] = useState(false);
  const [loadingCam, setLoadingCam] = useState(true);
  const [camResult, setCamResult] = useState<CamResult | null>(null);
  const [camError, setCamError] = useState("");
  const [clearance, setClearance] = useState(job.plan?.safety?.clearance_mm ?? 3);
  const [viseGripHeight, setViseGripHeight] = useState(job.plan?.safety?.vise_grip_height_mm ?? 1.5);
  const [supportThickness, setSupportThickness] = useState(job.plan?.safety?.support_thickness_mm ?? 3);
  const [savingSafety, setSavingSafety] = useState(false);
  const [safetyMessage, setSafetyMessage] = useState("");
  const [shareBaseUrl, setShareBaseUrl] = useState(window.location.origin);
  const [shareMessage, setShareMessage] = useState("");
  const [reanalyzing, setReanalyzing] = useState(false);
  const [showSimulationChecks, setShowSimulationChecks] = useState(false);
  const [catalogs, setCatalogs] = useState<Catalogs | null>(null);
  const [showOperationLibrary, setShowOperationLibrary] = useState(false);
  const [libraryFeatureIds, setLibraryFeatureIds] = useState<string[]>([]);
  const [operationMessage, setOperationMessage] = useState("");
  const [operationBusy, setOperationBusy] = useState(false);
  const [parameterEdits, setParameterEdits] = useState<Record<string, Record<string, string | number | boolean>>>({});
  const operations = useMemo(
    () => job.plan?.setups.flatMap((setup) => setup.operations) ?? [],
    [job.plan?.setups],
  );
  const operationTools = useMemo(
    () => Object.fromEntries(operations.map((operation) => [operation.id, {
      diameter_mm: operation.tool.diameter_mm,
      stickout_mm: operation.tool.stickout_mm,
      holder_diameter_mm: operation.tool.holder_diameter_mm,
      kind: operation.tool.kind,
      drill_point_angle_deg: Number(operation.parameters.drill_point_angle_deg ?? 118),
    }])),
    [operations],
  );
  const enabledOperations = operations.filter((operation) => operation.enabled !== false);
  const planApproved = enabledOperations.length > 0 && enabledOperations.every((operation) => operation.status === "approved");
  const automationBlocked = job.plan?.automation_status === "unsupported";
  const holes = useMemo(
    () => job.analysis?.cylindrical_features.filter((item) => item.kind === "hole" && item.review_state !== "excluded") ?? [],
    [job.analysis?.cylindrical_features],
  );
  const prismaticFeatures = useMemo(
    () => job.analysis?.prismatic_features?.filter((item) => item.review_state !== "excluded") ?? [],
    [job.analysis?.prismatic_features],
  );
  const manufacturingFeatures = useMemo<ManufacturingFeature[]>(
    () => [...holes, ...prismaticFeatures],
    [holes, prismaticFeatures],
  );
  const excludedCount = job.analysis?.cylindrical_features.filter((item) => item.kind === "hole" && item.review_state === "excluded").length ?? 0;
  const prismaticExcludedCount = job.analysis?.prismatic_features?.filter((item) => item.review_state === "excluded").length ?? 0;
  const reviewCount = manufacturingFeatures.filter((item) => item.review_state === "review").length;
  const sourceSolids = Number(job.analysis?.topology.source_solids ?? job.analysis?.topology.solids ?? 1);
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
    () => new Set(camResult?.preview_segments.filter((segment) => segment.motion === "cut").map((segment) => segment.operation_id) ?? []),
    [camResult?.preview_segments],
  );

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
  const parameterDraft = selectedOperation
    ? parameterEdits[selectedOperation.id] ?? selectedOperation.parameters
    : {};
  const selectableGeometry = useMemo(() => [
    ...(job.analysis?.planar_features ?? []).map((feature) => ({ id: feature.id, type: "planar_face", label: `平面 ${feature.id} · ${feature.area.toFixed(1)} mm²` })),
    ...(job.analysis?.cylindrical_features ?? []).filter((feature) => feature.kind === "hole" && feature.review_state !== "excluded").map((feature) => ({ id: feature.id, type: "cylindrical_hole", label: `孔 ${feature.id} · Ø${feature.diameter.toFixed(2)} × ${feature.length.toFixed(2)}` })),
    ...(job.analysis?.prismatic_features ?? []).filter((feature) => feature.review_state !== "excluded").map((feature) => ({ id: feature.id, type: `prismatic_${feature.kind}`, label: `${feature.kind === "pocket" ? "型腔" : "槽"} ${feature.id} · ${feature.length.toFixed(1)} × ${feature.width.toFixed(1)}` })),
  ], [job.analysis?.cylindrical_features, job.analysis?.planar_features, job.analysis?.prismatic_features]);

  useEffect(() => {
    fetch(apiUrl("/api/v1/catalogs"))
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((payload: Catalogs) => setCatalogs(payload))
      .catch(() => setOperationMessage("工序库暂时无法加载"));
  }, []);

  const visibleToolpathSegments = useMemo(() => {
    if (activeMode === "刀路") return camResult?.preview_segments ?? [];
    if (activeMode !== "仿真" || !selectedOperation) return EMPTY_TOOLPATH_SEGMENTS;
    const operationSegments = camResult?.preview_segments.filter(
      (segment) => segment.operation_id === selectedOperation.id,
    ) ?? EMPTY_TOOLPATH_SEGMENTS;
    return operationSegments.some((segment) => segment.motion === "cut") ? operationSegments : EMPTY_TOOLPATH_SEGMENTS;
  },
    [activeMode, camResult?.preview_segments, selectedOperation],
  );
  const visibleProfileBoundaries = useMemo(
    () => activeMode === "仿真" && selectedOperation
      ? camResult?.profile_boundaries.filter((boundary) => boundary.operation_id === selectedOperation.id) ?? EMPTY_PROFILE_BOUNDARIES
      : EMPTY_PROFILE_BOUNDARIES,
    [activeMode, camResult?.profile_boundaries, selectedOperation],
  );
  const selectedSetupId = useMemo(
    () => job.plan?.setups.find((setup) => setup.operations.some((operation) => operation.id === selectedOperation?.id))?.id,
    [job.plan?.setups, selectedOperation?.id],
  );
  const simulationResult = camResult?.simulation;
  const visibleCamoticsSurface = useMemo(() => {
    if (activeMode !== "仿真" || !selectedSetupId) return null;
    const surface = camResult?.camotics_surfaces?.find((item) => item.setup_id === selectedSetupId);
    return surface ? { url: apiUrl(`/api/v1/jobs/${job.id}/files/${surface.file}`), frame: surface.frame } : null;
  }, [activeMode, camResult?.camotics_surfaces, job.id, selectedSetupId]);
  const visibleSimulation = useMemo(() => {
    if (activeMode !== "仿真" || !simulationResult) return null;
    const surface = simulationResult.setup_surfaces?.find((item) => item.setup_id === selectedSetupId)
      ?? simulationResult.surface;
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
  }, [activeMode, selectedSetupId, simulationResult]);
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
    fetch(apiUrl(`/api/v1/jobs/${job.id}/cam`))
      .then((response) => response.ok ? response.json() : null)
      .then((payload: CamResult | null) => {
        if (payload) {
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
      .finally(() => setLoadingCam(false));
  }, [job.id, readOnly, operations]);

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

  const chooseMode = (mode: string) => {
    if (mode === "仿真" && camResult && selectedOperation && !cutOperationIds.has(selectedOperation.id)) {
      const playable = operations.find((operation) => cutOperationIds.has(operation.id));
      if (playable) {
        setSelectedOperation(playable);
        setSelectedFeatureIds(playable.feature_ids);
      }
    }
    setActiveMode(mode);
  };

  const chooseFeature = (id: string) => {
    setSelectedFeatureIds([id]);
    const relatedOperation = operations.find((operation) => operation.feature_ids.includes(id));
    if (relatedOperation) setSelectedOperation(relatedOperation);
  };

  const followPlaybackOperation = (operationId: string) => {
    const operation = operations.find((item) => item.id === operationId);
    if (operation && operation.id !== selectedOperation?.id) setSelectedOperation(operation);
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
      setCamResult(null);
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

  const saveSafety = async () => {
    setSavingSafety(true);
    setSafetyMessage("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/safety`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clearance_mm: clearance, vise_grip_height_mm: viseGripHeight, support_thickness_mm: supportThickness }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "安全参数保存失败");
      const updatedJob = payload as Job;
      setJob(updatedJob);
      setCamResult(null);
      setCamError("");
      setSelectedOperation(updatedJob.plan?.setups.flatMap((setup) => setup.operations)[0] ?? null);
      setSafetyMessage("安全参数已更新，工艺方案需要重新批准");
    } catch (reason) {
      setSafetyMessage(reason instanceof Error ? reason.message : "安全参数保存失败");
    } finally {
      setSavingSafety(false);
    }
  };

  const generateCam = async () => {
    setGeneratingCam(true);
    setCamError("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/cam`), { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "刀路生成失败");
      setCamResult(payload as CamResult);
      const refreshedJob = await fetch(apiUrl(`/api/v1/jobs/${job.id}`));
      if (refreshedJob.ok) setJob(await refreshedJob.json() as Job);
      setActiveMode("刀路");
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
    setSelectedOperation(preferred);
    setSelectedFeatureIds(preferred?.feature_ids ?? []);
    setParameterEdits({});
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

  const saveOperationParameters = async () => {
    if (!selectedSetup || !selectedOperation) return;
    setOperationBusy(true);
    setOperationMessage("");
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/setups/${selectedSetup.id}/operations/${selectedOperation.id}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parameters: parameterDraft }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "保存工序失败");
      applyUpdatedJob(payload as Job, selectedOperation.id);
      setOperationMessage("工序参数已保存，刀路需要重新生成");
    } catch (reason) {
      setOperationMessage(reason instanceof Error ? reason.message : "保存工序失败");
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

  const updateOperationTool = async (toolId: string) => {
    if (!selectedSetup || !selectedOperation) return;
    setOperationBusy(true);
    try {
      const response = await fetch(apiUrl(`/api/v1/jobs/${job.id}/setups/${selectedSetup.id}/operations/${selectedOperation.id}`), {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tool_id: toolId }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "更换刀具失败");
      applyUpdatedJob(payload as Job, selectedOperation.id);
      setOperationMessage("刀具已更换，切削参数已重新计算");
    } catch (reason) {
      setOperationMessage(reason instanceof Error ? reason.message : "更换刀具失败");
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
    return <div className="fatal-state"><AlertTriangle />{job.error || "任务没有产生可用结果"}<button onClick={onReset}>返回</button></div>;
  }

  return (
    <main className="workbench">
      <header className="topbar nx-menubar">
        <div className="brand compact"><span>S</span> SEKSUN CNC</div>
        <nav className="nx-menu" aria-label="主菜单">
          {["文件", "首页", "几何", "工艺", "刀路", "仿真", "分析", "工具"].map((item) => (
            <button key={item} className={item === "工艺" ? "active" : ""}>{item}</button>
          ))}
        </nav>
        <div className="project-title"><small>当前零件</small><strong>{job.filename}</strong></div>
        <div className="top-meta"><span>{job.material}</span><span>V0.9 NATIVE CAM</span>{readOnly && <span className="readonly-badge">只读分享</span>}</div>
        <button className="ghost share-button" onClick={copyShareLink}><Link2 size={14} />复制分享链接</button>
        {!readOnly && <button className="ghost reanalyze-button" onClick={reanalyze} disabled={reanalyzing}><RefreshCw className={reanalyzing ? "spin" : ""} size={14} />{reanalyzing ? "分析中" : "重新分析"}</button>}
        <button className="ghost" onClick={onReset}>新建任务</button>
        {!readOnly && <button className={`approve ${automationBlocked ? "blocked" : ""}`} onClick={approve} disabled={approving || automationBlocked}>{automationBlocked ? <AlertTriangle size={15} /> : <Check size={15} />}{automationBlocked ? "超出自动规划范围" : approving ? "确认中" : "批准方案"}</button>}
      </header>
      <nav className="mode-tabs nx-ribbon">
        <div className="ribbon-tools">
          <button title="导入零件"><FileUp size={17} /><span>导入</span></button>
          <button title="制造特征"><CircleDot size={17} /><span>特征</span></button>
          <button title="打开工序库" disabled={readOnly} onClick={() => { setLibraryFeatureIds(selectedFeatureIds); setShowOperationLibrary(true); }}><Library size={17} /><span>工序库</span></button>
          <button title="生成刀轨" disabled={automationBlocked || !planApproved || generatingCam} onClick={generateCam}><Play size={17} /><span>生成</span></button>
          <button title="安全检查" onClick={() => chooseMode("仿真")}><ShieldCheck size={17} /><span>检查</span></button>
        </div>
        <div className="mode-switcher">
          {["特征", "工艺", "刀路", "仿真"].map((mode) => <button key={mode} className={activeMode === mode ? "active" : ""} onClick={() => chooseMode(mode)}>{mode}</button>)}
        </div>
        <div className="plan-state"><i /> {sourceSolids > 1 ? `${sourceSolids} 实体 / 主体已筛选 · ` : ""}{job.plan.setups.length} 次装夹 · {operations.length} 道候选工序</div>
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
        <aside className="feature-tree panel">
          <div className="panel-heading"><Layers3 size={16} /> 制造结构</div>
          <div className="tree-section"><strong><Box size={15} /> 毛坯</strong><small>{String((job.plan.stock.size_mm as number[])?.join(" × "))} mm</small></div>
          {job.plan.setups.map((setup) => (
            <div key={setup.id} className="setup-tree">
              <div className="tree-section"><strong><Rotate3D size={15} /> {setup.name}</strong><small>{setup.fixture}</small></div>
              {setup.operations.map((operation) => (
                <button key={operation.id} disabled={activeMode === "仿真" && Boolean(camResult) && !cutOperationIds.has(operation.id)} className={`${selectedOperation?.id === operation.id ? "selected" : ""} ${operation.enabled === false ? "suppressed" : ""} ${activeMode === "仿真" && camResult && !cutOperationIds.has(operation.id) ? "unavailable" : ""}`} onClick={() => chooseOperation(operation)}>
                  <span>{operation.id}</span><div><strong>{operation.name}</strong><small>{operation.tool.name}{activeMode === "仿真" && camResult && !cutOperationIds.has(operation.id) ? " · 无有效刀路" : ""}</small></div>
                </button>
              ))}
            </div>
          ))}
          <div className="tree-summary"><CircleDot size={14} /> {holes.length} 孔 · {prismaticFeatures.length} 型腔/槽 · {reviewCount} 待复核 · 排除 {excludedCount + prismaticExcludedCount}</div>
        </aside>

        <section className="viewport panel">
          {automationBlocked && <div className="capability-blocker">
            <div><AlertTriangle size={17} /><strong>已阻止生成不完整工艺</strong></div>
            {job.plan.blocking_reasons.map((reason) => <p key={reason}>{reason}</p>)}
          </div>}
          <ModelViewer
            modelUrl={apiUrl(job.model_url)}
            features={manufacturingFeatures}
            selectedFeatureIds={selectedFeatureIds}
            onSelectFeature={chooseFeature}
            onActiveOperationChange={followPlaybackOperation}
            toolpathSegments={visibleToolpathSegments}
            profileBoundaries={visibleProfileBoundaries}
            simulation={visibleSimulation}
            camoticsSurface={visibleCamoticsSurface}
            fixtureComponents={visibleFixtureComponents}
            animateToolpath={activeMode === "仿真"}
            initialProgress={initialSimulationProgress}
            operationTools={operationTools}
            topologyEdges={visibleTopologyEdges}
            activeOperationId={selectedOperation?.id}
            toolpathLoaded={!loadingCam}
          />
          {activeMode === "仿真" && <button className={`simulation-check-toggle ${validationStatus ?? ""}`} onClick={() => setShowSimulationChecks(!showSimulationChecks)}><ShieldCheck size={15} /> 检查结果 <span>{validationStatus ? validationStatus.toUpperCase() : "WAIT"}</span></button>}
          {activeMode === "仿真" && showSimulationChecks && <div className="simulation-panel">
            <div><ShieldCheck size={16} /><strong>碰撞与静态预检</strong><span className={validationStatus ?? undefined}>{validationStatus ? validationStatus.toUpperCase() : "等待生成刀路"}</span><button aria-label="关闭检查结果" onClick={() => setShowSimulationChecks(false)}><X size={14} /></button></div>
            {camResult ? <>
              {camResult.collision.checks.map((check) => <p key={check.id} className={check.status}><i />{check.message}</p>)}
              {camResult.verification.checks.map((check) => <p key={check.id} className={check.status}><i />{check.message}</p>)}
              <div className="simulation-metrics"><strong>{camResult.simulation.metrics.removed_percent}%</strong><span>材料去除<br />{camResult.simulation.metrics.removed_volume_mm3.toLocaleString()} mm³</span><strong>{camResult.simulation.metrics.resolution_mm}</strong><span>网格精度<br />mm</span></div>
              <small>刀路估算 {camResult.verification.metrics.estimated_cycle_minutes} min · 绿色为牺牲垫板，红色为硬限位禁入区；包络校核不替代机床级仿真</small>
            </> : <p>批准方案并生成刀路后执行机床行程、参数范围、刀具直径和工序覆盖校验。</p>}
          </div>}
        </section>

        <aside className="inspector panel">
          <div className="panel-heading"><Bot size={16} /> 工艺依据</div>
          {selectedOperation ? (
            <>
              <div className="confidence"><span>建议置信度</span><strong>{Math.round(selectedOperation.confidence * 100)}%</strong><div><i style={{ width: `${selectedOperation.confidence * 100}%` }} /></div></div>
              <div className="inspector-block"><label>工序</label><h3>{selectedOperation.name}</h3><p>{selectedOperation.id} · {selectedOperation.type}</p></div>
              <div className="inspector-block"><label>刀具</label><div className="tool-card"><Wrench size={18} /><div><strong>{selectedOperation.tool.name}</strong><small>{selectedOperation.tool.kind} · 伸出 {selectedOperation.tool.stickout_mm} mm · 刀柄 Ø{selectedOperation.tool.holder_diameter_mm}</small></div></div>
                {selectedDefinition && !readOnly && <select className="tool-selector" disabled={operationBusy} value={selectedOperation.tool.id} onChange={(event) => updateOperationTool(event.target.value)}>{(catalogs?.tools ?? []).filter((tool) => selectedDefinition.tool.accepts.includes(tool.kind)).map((tool) => <option key={tool.id} value={tool.id}>{tool.name}</option>)}</select>}
              </div>
              <div className="inspector-block safety-editor">
                <label>安全与夹具</label>
                <div><span>安全间隙 mm</span><input disabled={readOnly} type="number" min="0.5" max="50" step="0.5" value={clearance} onChange={(event) => setClearance(Number(event.target.value))} /></div>
                {job.plan.safety?.fixture_strategy === "sacrificial_plate"
                  ? <div><span>牺牲垫板厚度 mm</span><input disabled={readOnly} type="number" min="0.5" max="50" step="0.5" value={supportThickness} onChange={(event) => setSupportThickness(Number(event.target.value))} /></div>
                  : <div><span>平口钳夹持高度 mm</span><input disabled={readOnly} type="number" min="0.5" max="50" step="0.5" value={viseGripHeight} onChange={(event) => setViseGripHeight(Number(event.target.value))} /></div>}
                {!readOnly && <button onClick={saveSafety} disabled={savingSafety}>{savingSafety ? "保存中…" : "保存并重新校核"}</button>}
                {safetyMessage && <small>{safetyMessage}</small>}
              </div>
              <div className="inspector-block"><label>推理依据</label><ul>{selectedOperation.rationale.map((item) => <li key={item}>{item}</li>)}</ul></div>
              <div className="inspector-block operation-parameters"><label>工序参数</label>
                {selectedDefinition ? <>
                  <p>{selectedDefinition.description} · {selectedDefinition.engine.operation} · {selectedDefinition.maturity}</p>
                  <div className="parameter-fields">{selectedDefinition.parameters.map((parameter) => {
                    const value = parameterDraft[parameter.key] ?? parameter.default ?? "";
                    return <label key={parameter.key}><span>{parameter.label}{parameter.unit ? ` (${parameter.unit})` : ""}</span>
                      {parameter.type === "boolean"
                        ? <input disabled={readOnly} type="checkbox" checked={Boolean(value)} onChange={(event) => selectedOperation && setParameterEdits((current) => ({ ...current, [selectedOperation.id]: { ...parameterDraft, [parameter.key]: event.target.checked } }))} />
                        : parameter.type === "enum"
                          ? <select disabled={readOnly} value={String(value)} onChange={(event) => selectedOperation && setParameterEdits((current) => ({ ...current, [selectedOperation.id]: { ...parameterDraft, [parameter.key]: event.target.value } }))}>{parameter.choices.map((choice) => <option key={choice}>{choice}</option>)}</select>
                          : <input disabled={readOnly} type="number" min={parameter.minimum ?? undefined} max={parameter.maximum ?? undefined} step={parameter.type === "integer" ? 1 : "any"} value={Number(value)} onChange={(event) => selectedOperation && setParameterEdits((current) => ({ ...current, [selectedOperation.id]: { ...parameterDraft, [parameter.key]: Number(event.target.value) } }))} />}
                    </label>;
                  })}</div>
                  {!readOnly && <div className="operation-editor-actions"><button disabled={operationBusy} onClick={saveOperationParameters}><Check size={13} />保存参数</button><button disabled={operationBusy} onClick={() => moveOperation(-1)} title="上移"><ArrowUp size={13} /></button><button disabled={operationBusy} onClick={() => moveOperation(1)} title="下移"><ArrowDown size={13} /></button><button disabled={operationBusy} onClick={toggleOperationEnabled}>{selectedOperation.enabled === false ? "启用" : "抑制"}</button><button disabled={operationBusy} className="danger" onClick={deleteOperation}><Trash2 size={13} />删除</button></div>}
                </> : <dl>{Object.entries(selectedOperation.parameters).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl>}
                {operationMessage && <small className="operation-message">{operationMessage}</small>}
              </div>
              {selectedFeatures.map((feature: ManufacturingFeature) => (
                <div className={`feature-card ${feature.review_state}`} key={feature.id}>
                  {"depth" in feature ? <>
                    <div><span>{feature.id}</span><small>{feature.kind === "pocket" ? "封闭型腔" : "贯通槽"}</small></div>
                    <div><strong>{feature.length.toFixed(2)} × {feature.width.toFixed(2)}</strong><small>深 {feature.depth.toFixed(2)} · {Math.round(feature.confidence * 100)}%</small></div>
                  </> : <>
                    <div><span>{feature.id}</span><small>{feature.end_type === "through" ? "通孔" : feature.end_type === "blind" ? "盲孔" : "孔端待确认"}</small></div>
                    <div><strong>Ø{feature.diameter.toFixed(2)} × {feature.length.toFixed(2)}</strong><small>{feature.segment_count} 个圆柱面 · {Math.round(feature.confidence * 100)}%</small></div>
                  </>}
                  {feature.review_reasons.map((reason) => <p key={reason}>{reason}</p>)}
                  {!readOnly && <div className="feature-actions"><button onClick={() => reviewFeature(feature.id, "accepted")}><Check size={12} />确认特征</button><button onClick={() => reviewFeature(feature.id, "excluded")}><AlertTriangle size={12} />排除</button></div>}
                </div>
              ))}
            </>
          ) : <p>选择一道工序查看规划依据。</p>}
        </aside>
      </section>

      <section className="operation-deck panel">
        <div className="deck-heading"><div><Settings2 size={16} /><strong>工序路线</strong></div><span><Clock3 size={14} />{camResult ? "刀路估算" : "规划估算"} {camResult?.verification.metrics.estimated_cycle_minutes ?? job.plan.estimated_minutes} min</span></div>
        <div className="operation-flow">
          {operations.map((operation, index) => (
            <div key={operation.id} className="flow-group">
              <button disabled={activeMode === "仿真" && Boolean(camResult) && !cutOperationIds.has(operation.id)} className={`${selectedOperation?.id === operation.id ? "active" : ""} ${operation.status} ${operation.enabled === false ? "suppressed" : ""} ${activeMode === "仿真" && camResult && !cutOperationIds.has(operation.id) ? "unavailable" : ""}`} onClick={() => chooseOperation(operation)}>
                <span>{operation.id}</span><strong>{operation.name}</strong><small>{activeMode === "仿真" && camResult && !cutOperationIds.has(operation.id) ? "无有效 FreeCAD 刀路" : operation.tool.name}</small><i>{Math.round(operation.confidence * 100)}%</i>
              </button>
              {index < operations.length - 1 && <ChevronRight className="flow-arrow" size={18} />}
            </div>
          ))}
        </div>
        <div className="warnings">
          <AlertTriangle size={15} />
          <span>{shareMessage || camError || (camResult ? `FreeCAD ${camResult.engine_version} 原生 CAM · ${camResult.path_command_count} 条指令 · 综合校验 ${validationStatus}` : safetyMessage || job.plan.warnings[0])}</span>
          <button onClick={() => window.open(apiUrl(`/api/v1/jobs/${job.id}/files/plan.json`))}><Download size={14} />工艺 JSON</button>
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.gcode))}><Download size={14} />G-code 草案</button>}
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.freecad))}><Download size={14} />FreeCAD</button>}
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.verification))}><ShieldCheck size={14} />预检报告</button>}
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.collision))}><AlertTriangle size={14} />碰撞报告</button>}
          {camResult && <button onClick={() => window.open(apiUrl(camResult.files.simulation))}><Box size={14} />仿真数据</button>}
          {!readOnly && <button disabled={automationBlocked || !planApproved || generatingCam} onClick={generateCam}><Play size={14} />{automationBlocked ? "刀路已阻止" : generatingCam ? "生成中…" : "生成刀路"}</button>}
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

  useEffect(() => {
    const restoreFromLocation = async () => {
      const match = window.location.pathname.match(/^\/jobs\/([0-9a-f]{32})\/?$/);
      if (!match) {
        setJob(null);
        setReadOnly(false);
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
  };

  const reset = () => {
    window.history.pushState({}, "", "/");
    setReadOnly(false);
    setJob(null);
    setSessionError("");
  };

  if (loadingSession) return <div className="fatal-state"><LoaderCircle className="spin" />正在加载任务会话…</div>;
  if (sessionError) return <div className="fatal-state"><AlertTriangle />{sessionError}<button onClick={reset}>返回新建任务</button></div>;
  return job ? <Workbench key={job.id} initialJob={job} onReset={reset} readOnly={readOnly} /> : <UploadScreen onCreated={openJob} />;
}

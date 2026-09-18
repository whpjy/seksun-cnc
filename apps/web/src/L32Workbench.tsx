import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Cog, LoaderCircle, Play, ShieldCheck, X } from "lucide-react";
import type {
  Catalogs,
  BacksideDraftResult,
  BacksideChainDraftResult,
  FrontChainDraftResult,
  L32MachineDefinition,
  L32MachineSnapshot,
  InnerBoreChainResult,
  ManufacturingRequirements,
  Operation,
  RotationalFeatureAnalysis,
  RotationalProfile,
  TurningDraftResult,
  TurningTransferDraftResult,
  WholePartDraftResult,
} from "./types";


const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const apiUrl = (path: string) => `${API_BASE}${path}`;

async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), init);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "L32 请求失败");
  return payload as T;
}

type L32DraftResult = TurningDraftResult | TurningTransferDraftResult;

function ProfileChart({ profile, result }: { profile: RotationalProfile; result: L32DraftResult | null }) {
  const target = profile.points;
  const resultSamples = result?.simulation.samples ?? [];
  const allZ = [...target.map((item) => item.z), ...resultSamples.map((item) => item.z)];
  const simulatedRadius = (item: TurningDraftResult["simulation"]["samples"][number]) => profile.side === "inner" ? item.inner_radius : item.outer_radius;
  const allR = [...target.map((item) => item.radius), ...resultSamples.map(simulatedRadius)];
  const minZ = Math.min(...allZ);
  const maxZ = Math.max(...allZ);
  const maxR = Math.max(...allR, 1);
  const x = (z: number) => 18 + (z - minZ) / Math.max(maxZ - minZ, 1e-6) * 284;
  const y = (radius: number) => 116 - radius / maxR * 96;
  const profilePoints = target.map((item) => `${x(item.z)},${y(item.radius)}`).join(" ");
  const stride = Math.max(1, Math.ceil(resultSamples.length / 140));
  const stockPoints = resultSamples.filter((_, index) => index % stride === 0 || index === resultSamples.length - 1)
    .map((item) => `${x(item.z)},${y(simulatedRadius(item))}`).join(" ");
  return <svg className="l32-profile-chart" viewBox="0 0 320 132" role="img" aria-label="Z-R 回转轮廓">
    <line x1="18" y1="116" x2="306" y2="116" />
    <line x1="18" y1="16" x2="18" y2="116" />
    {stockPoints && <polyline className="stock-profile" points={stockPoints} />}
    <polyline className="target-profile" points={profilePoints} />
    <text x="288" y="129">Z / mm</text><text x="4" y="12">R</text>
  </svg>;
}

export function L32Workbench({ jobId, catalogs, plannedOperations, manufacturingRequirements, boundMachineInstanceId, preferredOperationId, readOnly, onPlanChanged, onClose }: {
  jobId: string;
  catalogs: Catalogs | null;
  plannedOperations: Operation[];
  manufacturingRequirements: ManufacturingRequirements | null;
  boundMachineInstanceId?: string | null;
  preferredOperationId?: string | null;
  readOnly: boolean;
  onPlanChanged: () => void | Promise<void>;
  onClose: () => void;
}) {
  const [definition, setDefinition] = useState<L32MachineDefinition | null>(null);
  const [rotational, setRotational] = useState<RotationalFeatureAnalysis | null>(null);
  const [snapshot, setSnapshot] = useState<L32MachineSnapshot | null>(null);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [selectedThreadFeatureId, setSelectedThreadFeatureId] = useState("");
  const [selectedThreadRequirementId, setSelectedThreadRequirementId] = useState("");
  const [threadStartZ, setThreadStartZ] = useState(0);
  const [threadEndZ, setThreadEndZ] = useState(0);
  const [threadDepth, setThreadDepth] = useState(0);
  const [threadPassCount, setThreadPassCount] = useState(8);
  const [threadReliefStrategy, setThreadReliefStrategy] = useState<"groove" | "runout" | "thread_to_end">("groove");
  const [threadReliefWidth, setThreadReliefWidth] = useState(0);
  const [threadInsertId, setThreadInsertId] = useState("");
  const [threadControllerCycle, setThreadControllerCycle] = useState("");
  const [threadReviewer, setThreadReviewer] = useState("admin");
  const [initialBoreDiameter, setInitialBoreDiameter] = useState(0);
  const [boringStickout, setBoringStickout] = useState(0);
  const [boringClearance, setBoringClearance] = useState(0.2);
  const [boringToolId, setBoringToolId] = useState("");
  const [boringShoulderStrategy, setBoringShoulderStrategy] = useState<"not_required" | "small_nose_tool">("not_required");
  const [boringInventoryId, setBoringInventoryId] = useState("");
  const [boringReviewer, setBoringReviewer] = useState("admin");
  const [grooveWidth, setGrooveWidth] = useState(0);
  const [grooveFinalDiameter, setGrooveFinalDiameter] = useState(0);
  const [grooveDrawingDepth, setGrooveDrawingDepth] = useState(0);
  const [grooveRequirementId, setGrooveRequirementId] = useState("");
  const [grooveRequirementKind, setGrooveRequirementKind] = useState<"external_groove" | "internal_groove" | "seal_groove">("external_groove");
  const [grooveRawText, setGrooveRawText] = useState("");
  const [groovePeckDepth, setGroovePeckDepth] = useState(0.5);
  const [grooveToolId, setGrooveToolId] = useState("");
  const [grooveInventoryId, setGrooveInventoryId] = useState("");
  const [grooveStickout, setGrooveStickout] = useState(25);
  const [grooveClearance, setGrooveClearance] = useState(0.2);
  const [grooveFormConfirmed, setGrooveFormConfirmed] = useState(false);
  const [grooveReviewer, setGrooveReviewer] = useState("admin");
  const [drillToolId, setDrillToolId] = useState("");
  const [drillStickout, setDrillStickout] = useState(30);
  const [drillPointAngle, setDrillPointAngle] = useState(118);
  const [drillPeckDepth, setDrillPeckDepth] = useState(2);
  const [drillBottomCondition, setDrillBottomCondition] = useState<"through" | "blind_tip_allowance_confirmed">("blind_tip_allowance_confirmed");
  const [drillTipAllowance, setDrillTipAllowance] = useState(0);
  const [drillChipStrategy, setDrillChipStrategy] = useState<"standard_peck" | "deep_hole_peck">("standard_peck");
  const [drillCoolantConfirmed, setDrillCoolantConfirmed] = useState(false);
  const [drillInventoryId, setDrillInventoryId] = useState("");
  const [drillReviewer, setDrillReviewer] = useState("admin");
  const [variant, setVariant] = useState("VIII");
  const [operationMode, setOperationMode] = useState<"guide_bushing" | "guide_bushing_less">("guide_bushing");
  const [installedModules, setInstalledModules] = useState<string[]>([]);
  const [serialNumber, setSerialNumber] = useState("");
  const [controllerRevision, setControllerRevision] = useState("");
  const [barDiameter, setBarDiameter] = useState(32);
  const [operationType, setOperationType] = useState("turn_od_finishing");
  const [selectedPlannedOperationId, setSelectedPlannedOperationId] = useState(preferredOperationId ?? "");
  const [parameterValues, setParameterValues] = useState<Record<string, string | number | boolean>>({});
  const [stockRadius, setStockRadius] = useState(12);
  const [initialBoreRadius, setInitialBoreRadius] = useState(0);
  const [zMin, setZMin] = useState(-30);
  const [zMax, setZMax] = useState(2);
  const [result, setResult] = useState<L32DraftResult | null>(null);
  const [pickoffZ, setPickoffZ] = useState(-29);
  const [approachZ, setApproachZ] = useState(-26);
  const [gripLength, setGripLength] = useState(8);
  const [synchronizationRpm, setSynchronizationRpm] = useState(1200);
  const [subSpindleClampConfirmed, setSubSpindleClampConfirmed] = useState(false);
  const [backsideOperationId, setBacksideOperationId] = useState("OP50");
  const [backsideResult, setBacksideResult] = useState<BacksideDraftResult | null>(null);
  const [backsideChainResult, setBacksideChainResult] = useState<BacksideChainDraftResult | null>(null);
  const [frontChainResult, setFrontChainResult] = useState<FrontChainDraftResult | null>(null);
  const [wholePartResult, setWholePartResult] = useState<WholePartDraftResult | null>(null);
  const [innerBoreChainResult, setInnerBoreChainResult] = useState<InnerBoreChainResult | null>(null);
  const [busy, setBusy] = useState<"loading" | "machine" | "profile" | "thread" | "drilling" | "boring" | "groove-binding" | "grooving" | "draft" | null>("loading");
  const [message, setMessage] = useState("");
  const preferredOperation = plannedOperations.find((item) => item.id === preferredOperationId);
  const preferredOperationType = preferredOperation?.type ?? "";
  const preferredOperationFeatureIds = preferredOperation?.feature_ids.join("|") ?? "";

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiJson<{ definition: L32MachineDefinition }>("/api/v1/machines/l32/definitions"),
      apiJson<RotationalFeatureAnalysis>(`/api/v1/jobs/${jobId}/turning/analyze`, { method: "POST" }),
      boundMachineInstanceId
        ? apiJson<L32MachineSnapshot>(`/api/v1/jobs/${jobId}/machine-instance`).catch(() => null)
        : Promise.resolve(null),
    ]).then(([machine, features, boundSnapshot]) => {
      if (cancelled) return;
      setDefinition(machine.definition);
      setRotational(features);
      if (boundSnapshot) {
        setSnapshot(boundSnapshot);
        setVariant(boundSnapshot.instance.variant);
        setOperationMode(boundSnapshot.instance.operation_mode);
        setInstalledModules(boundSnapshot.instance.installed_modules);
        setSerialNumber(boundSnapshot.instance.serial_number ?? "");
        setControllerRevision(boundSnapshot.instance.controller_revision ?? "");
        setBarDiameter(boundSnapshot.instance.bar_diameter_mm);
      }
      const firstProfile = features.profiles[0];
      const preferredProfile = preferredOperationId
        ? features.profiles.find((item) => preferredOperationFeatureIds.split("|").includes(item.id) || preferredOperationFeatureIds.split("|").includes(`${item.id}-BACK`))
        : null;
      const firstThreadFeature = features.features.find((item) => item.kind === "thread_form_candidate");
      setSelectedProfileId(preferredProfile?.id ?? firstProfile?.id ?? "");
      if (preferredOperationId && preferredOperationType) {
        setOperationType(preferredOperationType);
        setSelectedPlannedOperationId(preferredOperationId);
      }
      setSelectedThreadFeatureId(firstThreadFeature?.id ?? "");
      if (firstProfile) {
        const radii = firstProfile.points.map((item) => item.radius);
        const zValues = firstProfile.points.map((item) => item.z);
        const suggestedStockRadius = Math.min(19, Math.max(...radii) + 1);
        setBarDiameter(suggestedStockRadius > 16 ? 38 : 32);
        setStockRadius(suggestedStockRadius);
        setInitialBoreRadius(firstProfile.side === "inner" ? Math.max(2.1, Math.min(...radii) - 1) : 0);
        setZMin(Math.min(...zValues) - 2);
        setZMax(Math.max(...zValues) + 2);
      }
      setMessage(features.status === "not_detected" ? "未识别到可用回转轮廓" : "请确认设备实例与回转轮廓");
    }).catch((reason) => !cancelled && setMessage(reason instanceof Error ? reason.message : "L32 数据加载失败"))
      .finally(() => !cancelled && setBusy(null));
    return () => { cancelled = true; };
  }, [jobId, boundMachineInstanceId, preferredOperationId, preferredOperationType, preferredOperationFeatureIds]);

  const profile = rotational?.profiles.find((item) => item.id === selectedProfileId) ?? rotational?.profiles[0] ?? null;
  const threadFeatures = rotational?.features.filter((item) => item.kind === "thread_form_candidate") ?? [];
  const threadRequirements = manufacturingRequirements?.requirements?.filter((item) => item.type === "thread" && item.thread) ?? [];
  const selectedThreadFeature = threadFeatures.find((item) => item.id === selectedThreadFeatureId) ?? threadFeatures[0] ?? null;
  const selectedThreadRequirement = threadRequirements.find((item) => item.id === selectedThreadRequirementId) ?? threadRequirements[0] ?? null;
  const selectedThread = selectedThreadRequirement?.thread ?? null;
  const modelledMajorDiameter = selectedThreadFeature
    ? (Math.max(selectedThreadFeature.radius_start, selectedThreadFeature.radius_end) + selectedThreadFeature.depth_mm) * 2
    : null;
  const pitchMatches = Boolean(selectedThreadFeature && selectedThread && selectedThreadFeature.pitch_candidates_mm.some(
    (candidate) => Math.abs(candidate - selectedThread.pitch_mm) <= Math.max(selectedThread.pitch_mm * 0.02, 0.02),
  ));
  const diameterMatches = Boolean(selectedThread && modelledMajorDiameter !== null
    && Math.abs(modelledMajorDiameter - selectedThread.major_diameter_mm) <= Math.max(selectedThread.pitch_mm * 0.75, 0.3));
  const selectedThreadDraft = selectedThreadFeature
    ? plannedOperations.find((item) => item.type === "turn_threading" && item.feature_ids.includes(selectedThreadFeature.id))
    : null;

  useEffect(() => {
    if (!selectedThreadRequirementId && threadRequirements[0]) {
      setSelectedThreadRequirementId(threadRequirements[0].id);
    }
  }, [selectedThreadRequirementId, threadRequirements]);

  useEffect(() => {
    if (!selectedThreadFeature || !selectedThreadDraft) return;
    const parameters = selectedThreadDraft.parameters;
    setThreadStartZ(Number(parameters.start_z_mm ?? Math.max(selectedThreadFeature.z_start, selectedThreadFeature.z_end)));
    setThreadEndZ(Number(parameters.end_z_mm ?? Math.min(selectedThreadFeature.z_start, selectedThreadFeature.z_end)));
    setThreadDepth(Number(parameters.thread_depth_mm ?? selectedThreadFeature.depth_mm));
    setThreadPassCount(Number(parameters.pass_count ?? 8));
    setThreadReliefStrategy((parameters.relief_strategy as typeof threadReliefStrategy) ?? "groove");
    setThreadReliefWidth(Number(parameters.relief_width_mm ?? selectedThreadFeature.resolved_pitch_mm ?? 0));
    setThreadInsertId(String(parameters.tool_insert_id ?? ""));
    setThreadControllerCycle(String(parameters.controller_cycle_id ?? ""));
    setThreadReviewer(String(parameters.engineering_reviewer ?? "admin"));
  }, [selectedThreadDraft?.id, selectedThreadFeature?.id]);

  const turningDefinitions = useMemo(() => (catalogs?.operations ?? []).filter((item) =>
    item.engine.provider === "turning" && (
      item.id === "turn_facing"
      || (profile?.side === "outer" && ["turn_od_roughing", "turn_od_finishing", "turn_grooving", "turn_cutoff"].includes(item.id))
      || (profile?.side === "outer" && item.id === "turn_threading" && selectedThreadDraft?.enabled)
      || (profile?.side === "inner" && ["axial_drilling", "turn_id_roughing", "turn_id_finishing", "turn_grooving"].includes(item.id))
    )), [catalogs?.operations, profile?.side, selectedThreadDraft?.enabled]);
  const operationDefinition = turningDefinitions.find((item) => item.id === operationType) ?? turningDefinitions[0] ?? null;
  const plannedOperationCandidates = plannedOperations.filter((item) =>
    item.type === operationDefinition?.id && (!profile || item.feature_ids.includes(profile.id)),
  );
  const plannedOperation = plannedOperationCandidates.find((item) => item.id === selectedPlannedOperationId)
    ?? plannedOperationCandidates[0];
  const regionalOperation = plannedOperation?.source === "automatic"
    && plannedOperation.parameters.profile_z_min_mm !== undefined
    && plannedOperation.parameters.profile_z_max_mm !== undefined;
  const backsideOperations = plannedOperations.filter((item) => item.workpiece_side === "back");
  const hasRegionalOperations = plannedOperations.some((item) =>
    item.parameters.profile_region_complete === false
    || item.parameters.source_region_z_min_mm !== undefined,
  );
  const cutoffOperation = plannedOperations.find((item) => item.id === "OP40" && item.type === "turn_cutoff");
  const cutoffKerfIntrusion = profile && cutoffOperation
    ? Math.max(
      Number(cutoffOperation.parameters.z_mm ?? 0)
      + Number(cutoffOperation.parameters.cutting_width_mm ?? cutoffOperation.tool.cutting_width_mm ?? 0) / 2
      - Math.min(...profile.points.map((item) => item.z)),
      0,
    )
    : 0;
  const hasCutoffKerfOverlap = cutoffKerfIntrusion > 0.05;
  const innerChainOperations = profile?.side === "inner" ? plannedOperations.filter((item) =>
    item.feature_ids.includes(profile.id)
    && (
      ["axial_drilling", "turn_id_roughing", "turn_id_finishing"].includes(item.type)
      || (item.type === "turn_grooving" && item.parameters.groove_side === "internal")
    ),
  ) : [];
  const innerChainReady = innerChainOperations.some((item) => item.type === "axial_drilling")
    && innerChainOperations.some((item) => item.type === "turn_id_roughing")
    && innerChainOperations.some((item) => item.type === "turn_id_finishing")
    && innerChainOperations.every((item) => item.enabled && item.parameters.engineering_review_status === "verified_engineer");
  const backsideOperation = backsideOperations.find((item) => item.id === backsideOperationId) ?? backsideOperations[0] ?? null;
  const backsideChainReady = ["OP55-BACK", "OP58-BACK"].every((id) =>
    backsideOperations.some((item) => item.id === id && item.enabled),
  );
  const hasFrontFormOperations = plannedOperations.some((item) => item.id === "OP21-FORM" && item.feature_ids.includes(profile?.id ?? ""));
  const frontChainReady = ["OP20", "OP21-FORM", "OP30", "OP31-FORM"].every((id) =>
    plannedOperations.some((item) => item.id === id && item.enabled && item.feature_ids.includes(profile?.id ?? "")),
  );
  const effectiveParameterValues = operationDefinition ? {
    ...Object.fromEntries(operationDefinition.parameters.filter((item) => item.default !== null).map((item) => [item.key, item.default as string | number | boolean])),
    ...plannedOperation?.parameters,
    ...parameterValues,
  } : {};
  const isBoringOperation = operationDefinition?.id === "turn_id_roughing" || operationDefinition?.id === "turn_id_finishing";
  const isAxialDrillingOperation = operationDefinition?.id === "axial_drilling";
  const isGroovingOperation = operationDefinition?.id === "turn_grooving";
  const isInternalGroovingOperation = isGroovingOperation && plannedOperation?.parameters.groove_side === "internal";
  const grooveBindingMatched = plannedOperation?.parameters.drawing_binding_status === "matched";
  const isFinishingBoringOperation = plannedOperation?.type === "turn_id_finishing";
  const sharpInnerShoulderCount = Number(plannedOperation?.parameters.sharp_inner_shoulder_count ?? 0);

  useEffect(() => {
    if (!plannedOperationCandidates.length) {
      setSelectedPlannedOperationId("");
      return;
    }
    if (!plannedOperationCandidates.some((item) => item.id === selectedPlannedOperationId)) {
      setSelectedPlannedOperationId(plannedOperationCandidates[0].id);
    }
  }, [operationDefinition?.id, profile?.id, plannedOperationCandidates.map((item) => item.id).join("|"), selectedPlannedOperationId]);

  useEffect(() => {
    if (!isBoringOperation || !plannedOperation) return;
    const requiredEntry = Number(plannedOperation.parameters.required_initial_bore_diameter_mm ?? 0);
    const reviewedPrebore = plannedOperations.find((item) => item.type === "axial_drilling" && item.enabled && item.feature_ids.some((id) => plannedOperation.feature_ids.includes(id)));
    setInitialBoreDiameter(Number(plannedOperation.parameters.initial_bore_diameter_mm ?? reviewedPrebore?.tool.diameter_mm ?? requiredEntry + 0.4));
    setBoringStickout(Number(plannedOperation.parameters.confirmed_stickout_mm ?? plannedOperation.tool.stickout_mm));
    setBoringClearance(Number(plannedOperation.parameters.assembly_clearance_mm ?? 0.2));
    setBoringToolId(String(plannedOperation.parameters.finishing_tool_id ?? plannedOperation.tool.id));
    setBoringShoulderStrategy((plannedOperation.parameters.shoulder_strategy as typeof boringShoulderStrategy) ?? (Number(plannedOperation.parameters.sharp_inner_shoulder_count ?? 0) > 0 ? "small_nose_tool" : "not_required"));
    setBoringInventoryId(String(plannedOperation.parameters.boring_bar_inventory_id ?? ""));
    setBoringReviewer(String(plannedOperation.parameters.engineering_reviewer ?? "admin"));
  }, [isBoringOperation, plannedOperation?.id, plannedOperations]);

  useEffect(() => {
    if (!isAxialDrillingOperation || !plannedOperation) return;
    setDrillToolId(plannedOperation.tool.id);
    setDrillStickout(Number(plannedOperation.parameters.confirmed_stickout_mm ?? plannedOperation.tool.stickout_mm));
    setDrillPointAngle(Number(plannedOperation.parameters.drill_point_angle_deg ?? 118));
    setDrillPeckDepth(Number(plannedOperation.parameters.peck_depth_mm ?? 2));
    setDrillBottomCondition((plannedOperation.parameters.bottom_condition as typeof drillBottomCondition) ?? "blind_tip_allowance_confirmed");
    const defaultTipLength = plannedOperation.tool.diameter_mm / 2 / Math.tan(118 / 2 * Math.PI / 180);
    setDrillTipAllowance(Number(plannedOperation.parameters.tip_overtravel_allowance_mm ?? defaultTipLength.toFixed(2)));
    setDrillChipStrategy((plannedOperation.parameters.chip_evacuation_strategy as typeof drillChipStrategy) ?? (plannedOperation.parameters.deep_hole_review_required ? "deep_hole_peck" : "standard_peck"));
    setDrillCoolantConfirmed(Boolean(plannedOperation.parameters.through_tool_coolant_confirmed ?? false));
    setDrillInventoryId(String(plannedOperation.parameters.drill_inventory_id ?? ""));
    setDrillReviewer(String(plannedOperation.parameters.engineering_reviewer ?? "admin"));
  }, [isAxialDrillingOperation, plannedOperation?.id]);

  useEffect(() => {
    if (!isGroovingOperation || !plannedOperation) return;
    setGrooveWidth(Number(plannedOperation.parameters.groove_width_mm ?? 0));
    setGrooveFinalDiameter(Number(plannedOperation.parameters.final_diameter_mm ?? 0));
    setGrooveDrawingDepth(Number(plannedOperation.parameters.groove_depth_mm ?? 0));
    setGrooveRequirementId(String(plannedOperation.parameters.drawing_requirement_id || `DRAWING-${plannedOperation.id}`));
    setGrooveRequirementKind(isInternalGroovingOperation ? "internal_groove" : "external_groove");
    setGroovePeckDepth(Number(plannedOperation.parameters.peck_depth_mm ?? 0.5));
    setGrooveToolId(plannedOperation.tool.id);
    setGrooveInventoryId(String(plannedOperation.parameters.groove_tool_inventory_id ?? ""));
    setGrooveStickout(Number(plannedOperation.parameters.confirmed_stickout_mm ?? plannedOperation.tool.stickout_mm));
    setGrooveClearance(Number(plannedOperation.parameters.assembly_clearance_mm ?? 0.2));
    setGrooveFormConfirmed(Boolean(plannedOperation.parameters.profile_form_confirmed ?? false));
    setGrooveReviewer(String(plannedOperation.parameters.engineering_reviewer ?? "admin"));
  }, [isGroovingOperation, isInternalGroovingOperation, plannedOperation?.id]);

  const selectProfile = (profileId: string) => {
    const selected = rotational?.profiles.find((item) => item.id === profileId);
    setSelectedProfileId(profileId);
    setResult(null);
    if (!selected) return;
    const radii = selected.points.map((item) => item.radius);
    const zValues = selected.points.map((item) => item.z);
    const suggestedStockRadius = Math.min(19, Math.max(...radii) + 1);
    setBarDiameter(suggestedStockRadius > 16 ? 38 : 32);
    setStockRadius(suggestedStockRadius);
    setInitialBoreRadius(selected.side === "inner" ? Math.max(2.1, Math.min(...radii) - 1) : 0);
    setZMin(Math.min(...zValues) - 2);
    setZMax(Math.max(...zValues) + 2);
  };

  const selectOperationType = (definitionId: string) => {
    setOperationType(definitionId);
    setSelectedPlannedOperationId("");
    setParameterValues({});
    setResult(null);
  };

  const saveMachine = async () => {
    if (!definition) return;
    setBusy("machine"); setMessage(""); setResult(null);
    try {
      const instanceId = `l32-${jobId.slice(0, 12)}`;
      const created = await apiJson<L32MachineSnapshot>("/api/v1/machines/l32/instances", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: instanceId, definition_id: definition.id, name: `L32 ${variant} · ${jobId.slice(0, 6)}`,
          serial_number: serialNumber || null, variant, controller_revision: controllerRevision || null,
          operation_mode: operationMode, installed_modules: installedModules, enabled_options: barDiameter > 32 ? ["bar_diameter_38mm"] : [],
          bar_diameter_mm: barDiameter, postprocessor_profile_id: null,
        }),
      });
      await apiJson<unknown>(`/api/v1/jobs/${jobId}/machine-instance`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ machine_instance_id: created.instance.id }),
      });
      setSnapshot(created);
      await onPlanChanged();
      setMessage(created.validation.valid ? "设备实例快照已保存" : "配置存在阻断项，请根据提示修正");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "设备实例保存失败"); }
    finally { setBusy(null); }
  };

  const reviewProfile = async (reviewState: "accepted" | "excluded") => {
    if (!profile) return;
    setBusy("profile"); setMessage(""); setResult(null);
    try {
      const updated = await apiJson<RotationalFeatureAnalysis>(`/api/v1/jobs/${jobId}/turning/profiles/${profile.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ review_state: reviewState }),
      });
      setRotational(updated);
      await onPlanChanged();
      setMessage(reviewState === "accepted" ? "回转轮廓已确认并保存" : "回转轮廓已排除");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "轮廓状态保存失败"); }
    finally { setBusy(null); }
  };

  const confirmThreadBinding = async () => {
    if (!selectedThreadFeature || !selectedThreadRequirement) return;
    setBusy("thread"); setMessage(""); setResult(null);
    try {
      await apiJson<unknown>(`/api/v1/jobs/${jobId}/turning/thread-bindings/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          thread_feature_id: selectedThreadFeature.id,
          requirement_id: selectedThreadRequirement.id,
        }),
      });
      const updated = await apiJson<RotationalFeatureAnalysis>(
        `/api/v1/jobs/${jobId}/turning/analyze`, { method: "POST" },
      );
      setRotational(updated);
      await onPlanChanged();
      setMessage("螺纹要求与周期牙形已确认；车螺纹草案已生成并保持禁用");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "螺纹绑定确认失败"); }
    finally { setBusy(null); }
  };

  const reviewThreadOperation = async () => {
    if (!selectedThreadDraft) return;
    setBusy("thread"); setMessage(""); setResult(null);
    try {
      await apiJson<unknown>(`/api/v1/jobs/${jobId}/turning/thread-operations/${selectedThreadDraft.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_z_mm: threadStartZ,
          end_z_mm: threadEndZ,
          thread_depth_mm: threadDepth,
          pass_count: threadPassCount,
          relief_strategy: threadReliefStrategy,
          relief_width_mm: threadReliefWidth,
          tool_insert_id: threadInsertId,
          controller_cycle_id: threadControllerCycle,
          reviewer: threadReviewer,
        }),
      });
      await onPlanChanged();
      setOperationType("turn_threading");
      setMessage("螺纹工序已通过 DRAFT 级工程审核，可生成控制器无关仿真草案");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "螺纹工序审核失败"); }
    finally { setBusy(null); }
  };

  const reviewBoringOperation = async () => {
    if (!plannedOperation || !isBoringOperation) return;
    setBusy("boring"); setMessage(""); setResult(null);
    try {
      await apiJson<unknown>(`/api/v1/jobs/${jobId}/turning/boring-operations/${plannedOperation.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          initial_bore_diameter_mm: initialBoreDiameter,
          confirmed_stickout_mm: boringStickout,
          assembly_clearance_mm: boringClearance,
          finishing_tool_id: isFinishingBoringOperation ? boringToolId : null,
          shoulder_strategy: boringShoulderStrategy,
          boring_bar_inventory_id: boringInventoryId,
          reviewer: boringReviewer,
        }),
      });
      await onPlanChanged();
      setInitialBoreRadius(initialBoreDiameter / 2);
      setMessage("内孔入口、镗杆空间、隐藏倒扣和轴向伸出已通过 DRAFT 级审核");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "内孔工序审核失败"); }
    finally { setBusy(null); }
  };

  const reviewGroovingOperation = async () => {
    if (!plannedOperation || !isGroovingOperation) return;
    setBusy("grooving"); setMessage(""); setResult(null);
    try {
      await apiJson<unknown>(`/api/v1/jobs/${jobId}/turning/grooving-operations/${plannedOperation.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirmed_groove_width_mm: grooveWidth,
          confirmed_final_diameter_mm: grooveFinalDiameter,
          peck_depth_mm: groovePeckDepth,
          groove_tool_id: grooveToolId,
          groove_tool_inventory_id: grooveInventoryId,
          confirmed_stickout_mm: isInternalGroovingOperation ? grooveStickout : null,
          assembly_clearance_mm: grooveClearance,
          profile_form_confirmed: grooveFormConfirmed,
          reviewer: grooveReviewer,
        }),
      });
      await onPlanChanged();
      setMessage(`${isInternalGroovingOperation ? "内" : "外"}槽尺寸、槽形、分层切入及现场切槽刀已通过 DRAFT 级审核`);
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "槽加工工序审核失败"); }
    finally { setBusy(null); }
  };

  const confirmGrooveBinding = async () => {
    if (!plannedOperation || !isGroovingOperation) return;
    const grooveFeatureId = plannedOperation.feature_ids.find((id) => id.startsWith("TPF-"));
    if (!grooveFeatureId) { setMessage("当前工序缺少可追溯的 STEP 槽候选"); return; }
    setBusy("groove-binding"); setMessage(""); setResult(null);
    try {
      await apiJson<unknown>(`/api/v1/jobs/${jobId}/turning/groove-bindings/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          groove_feature_id: grooveFeatureId,
          requirement_id: grooveRequirementId,
          requirement_kind: grooveRequirementKind,
          confirmed_groove_width_mm: grooveWidth,
          confirmed_groove_depth_mm: grooveDrawingDepth,
          confirmed_bottom_diameter_mm: grooveFinalDiameter,
          raw_text: grooveRawText || null,
          reviewer: grooveReviewer,
        }),
      });
      await onPlanChanged();
      setMessage("图纸槽尺寸已与唯一 STEP 候选绑定；仍需完成刀具、槽形与切削参数审核");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "图纸槽尺寸绑定失败"); }
    finally { setBusy(null); }
  };

  const reviewAxialDrillingOperation = async () => {
    if (!plannedOperation || !isAxialDrillingOperation) return;
    setBusy("drilling"); setMessage(""); setResult(null);
    try {
      await apiJson<unknown>(`/api/v1/jobs/${jobId}/turning/axial-drilling-operations/${plannedOperation.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          drill_tool_id: drillToolId,
          confirmed_stickout_mm: drillStickout,
          drill_point_angle_deg: drillPointAngle,
          peck_depth_mm: drillPeckDepth,
          bottom_condition: drillBottomCondition,
          tip_overtravel_allowance_mm: drillTipAllowance,
          chip_evacuation_strategy: drillChipStrategy,
          through_tool_coolant_confirmed: drillCoolantConfirmed,
          drill_inventory_id: drillInventoryId,
          reviewer: drillReviewer,
        }),
      });
      await onPlanChanged();
      setMessage("预孔钻头、伸出、排屑和钻尖越程已通过 DRAFT 级审核");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "预孔工序审核失败"); }
    finally { setBusy(null); }
  };

  const generateDraft = async () => {
    if (!snapshot || !profile || !operationDefinition || !catalogs) return;
    const tool = catalogs.tools.find((item) => item.id === operationDefinition.tool.default_tool_id);
    if (!tool) { setMessage("默认车削刀具未找到"); return; }
    const generatedOperation: Operation = {
      id: `L32-${operationDefinition.id.toUpperCase()}`, sequence: 10, type: operationDefinition.id,
      name: operationDefinition.name, feature_ids: [profile.id], tool, parameters: effectiveParameterValues,
      rationale: ["由已确认的回转轮廓生成 L32 适配草案"], confidence: profile.confidence,
      status: "proposed", definition_id: operationDefinition.id, definition_version: operationDefinition.version,
      source: "manual", enabled: true, generation_state: "dirty",
      channel_id: "main", spindle_id: "main", workpiece_side: "front", synchronization_group: null,
    };
    const operation: Operation = plannedOperation
      ? (regionalOperation || ["turn_threading", "axial_drilling", "turn_id_roughing", "turn_id_finishing", "turn_grooving"].includes(operationDefinition.id))
        ? plannedOperation
        : {
          ...plannedOperation,
          feature_ids: [profile.id],
          parameters: effectiveParameterValues,
          status: "proposed",
          generation_state: "dirty",
        }
      : generatedOperation;
    setBusy("draft"); setMessage(""); setResult(null);
    try {
      const transferMode = operation.type === "turn_cutoff";
      const generated = await apiJson<L32DraftResult>(
        transferMode
          ? `/api/v1/jobs/${jobId}/turning/transfer/draft`
          : `/api/v1/jobs/${jobId}/turning/draft`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          machine_instance_id: snapshot.instance.id, operation, profile,
          stock_radius_mm: stockRadius, initial_bore_radius_mm: initialBoreRadius,
          z_min_mm: zMin, z_max_mm: zMax, resolution_mm: 0.2,
          ...(transferMode ? {
            approach_z_mm: approachZ,
            pickoff_z_mm: pickoffZ,
            grip_length_mm: gripLength,
            synchronization_rpm: synchronizationRpm,
            sub_spindle_clamp_confirmed: subSpindleClampConfirmed,
          } : {}),
        }),
      });
      setResult(generated);
      setMessage(transferMode ? "主轴/背轴同步接料与切断草案已生成" : "车削刀路 IR 与 Z-R 仿真草案已生成");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "车削草案生成失败"); }
    finally { setBusy(null); }
  };

  const generateInnerBoreChain = async () => {
    if (!snapshot || !profile || profile.side !== "inner") return;
    setBusy("draft"); setMessage(""); setInnerBoreChainResult(null);
    try {
      const generated = await apiJson<InnerBoreChainResult>(`/api/v1/jobs/${jobId}/turning/inner-bore-chain/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          machine_instance_id: snapshot.instance.id,
          profile_id: profile.id,
          stock_radius_mm: stockRadius,
          z_min_mm: zMin,
          z_max_mm: zMax,
          resolution_mm: 0.2,
        }),
      });
      setInnerBoreChainResult(generated);
      setMessage(generated.status === "passed" ? "内孔连续余料仿真已通过" : "内孔连续余料仿真发现阻断项");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "内孔连续余料仿真失败"); }
    finally { setBusy(null); }
  };

  const generateBacksideDraft = async () => {
    if (!snapshot || !profile || !backsideOperation) return;
    setBusy("draft"); setMessage(""); setBacksideResult(null);
    try {
      const generated = await apiJson<BacksideDraftResult>(`/api/v1/jobs/${jobId}/turning/backside/draft`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          machine_instance_id: snapshot.instance.id,
          source_profile_id: profile.id,
          operation: backsideOperation,
          source_cutoff_z_mm: Math.min(...profile.points.map((item) => item.z)),
          stock_radius_mm: stockRadius,
          resolution_mm: 0.2,
        }),
      });
      setBacksideResult(generated);
      setMessage(`${backsideOperation.id} 背轴加工草案已生成`);
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "背轴加工草案生成失败"); }
    finally { setBusy(null); }
  };

  const generateBacksideChainDraft = async () => {
    if (!snapshot || !profile || profile.side !== "outer" || !backsideChainReady) return;
    setBusy("draft"); setMessage(""); setBacksideChainResult(null);
    try {
      const generated = await apiJson<BacksideChainDraftResult>(`/api/v1/jobs/${jobId}/turning/backside-chain/draft`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          machine_instance_id: snapshot.instance.id,
          source_profile_id: profile.id,
          stock_radius_mm: stockRadius,
          resolution_mm: 0.2,
        }),
      });
      setBacksideChainResult(generated);
      setMessage(generated.status === "passed" ? "背面粗车→精车连续余料 DRAFT 已通过" : "背面连续余料 DRAFT 发现阻断项");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "背面连续余料草案生成失败"); }
    finally { setBusy(null); }
  };

  const generateFrontChainDraft = async () => {
    if (!snapshot || !profile || profile.side !== "outer" || !frontChainReady) return;
    setBusy("draft"); setMessage(""); setFrontChainResult(null);
    try {
      const generated = await apiJson<FrontChainDraftResult>(`/api/v1/jobs/${jobId}/turning/front-chain/draft`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          machine_instance_id: snapshot.instance.id,
          source_profile_id: profile.id,
          stock_radius_mm: stockRadius,
          resolution_mm: 0.2,
        }),
      });
      setFrontChainResult(generated);
      setMessage(generated.status === "passed" ? "前端四工序连续余料 DRAFT 已通过" : "前端连续余料 DRAFT 发现阻断项");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "前端连续余料草案生成失败"); }
    finally { setBusy(null); }
  };

  const generateWholePartDraft = async () => {
    if (!snapshot || !profile) return;
    setBusy("draft"); setMessage(""); setWholePartResult(null);
    try {
      const generated = await apiJson<WholePartDraftResult>(`/api/v1/jobs/${jobId}/turning/whole-program/draft`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          machine_instance_id: snapshot.instance.id,
          source_profile_id: profile.id,
          stock_radius_mm: stockRadius,
          initial_bore_radius_mm: initialBoreRadius,
          resolution_mm: 0.2,
          approach_z_mm: approachZ,
          pickoff_z_mm: pickoffZ,
          grip_length_mm: gripLength,
          synchronization_rpm: synchronizationRpm,
          sub_spindle_clamp_confirmed: subSpindleClampConfirmed,
        }),
      });
      setWholePartResult(generated);
      setMessage("OP10–OP60 双通道整件程序草案已生成");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "整件程序草案生成失败"); }
    finally { setBusy(null); }
  };

  return <div className="l32-workbench-backdrop" onMouseDown={onClose}>
    <section className="l32-workbench" role="dialog" aria-modal="true" aria-labelledby="l32-workbench-title" onMouseDown={(event) => event.stopPropagation()}>
      <header><div><Cog size={18} /><span><small>L32 TURNING ADAPTER</small><strong id="l32-workbench-title">L32 车削适配工作台</strong></span></div><em>DRAFT ONLY</em><button aria-label="关闭 L32 工作台" onClick={onClose}><X size={17} /></button></header>
      {busy === "loading" ? <div className="l32-loading"><LoaderCircle className="spin" />正在加载机床定义与回转特征…</div> : <div className="l32-workbench-body">
        <section className="l32-card machine-config">
          <h3><span>01</span>设备实例</h3>
          <div className="l32-form-grid">
            <label>机型<select disabled={readOnly} value={variant} onChange={(event) => { setVariant(event.target.value); setInstalledModules([]); setSnapshot(null); }}>{definition?.variants.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.model_code}</option>)}</select></label>
            <label>运行方式<select disabled={readOnly} value={operationMode} onChange={(event) => { setOperationMode(event.target.value as typeof operationMode); setSnapshot(null); }}><option value="guide_bushing">导套式</option><option value="guide_bushing_less">无导套式</option></select></label>
            <label>棒料直径<select disabled={readOnly} value={barDiameter} onChange={(event) => { const diameter = Number(event.target.value); setBarDiameter(diameter); setStockRadius((current) => Math.min(current, diameter / 2)); setSnapshot(null); }}><option value={32}>Ø32 mm</option><option value={38}>Ø38 mm（选件）</option></select></label>
            <label>设备编号<input disabled={readOnly} value={serialNumber} placeholder="建议填写" onChange={(event) => { setSerialNumber(event.target.value); setSnapshot(null); }} /></label>
            <label className="wide">控制器版本<input disabled={readOnly} value={controllerRevision} placeholder="例如 M70LPC-VU 现场版本" onChange={(event) => { setControllerRevision(event.target.value); setSnapshot(null); }} /></label>
          </div>
          <div className="l32-modules"><strong>已安装刀具模块</strong><div>{definition?.modules.filter((item) => item.compatible_variants.includes(variant)).map((item) => <label key={item.id}><input type="checkbox" disabled={readOnly} checked={installedModules.includes(item.id)} onChange={(event) => { setInstalledModules((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id)); setSnapshot(null); }} /><span>{item.id}<small>{item.name}</small></span></label>)}</div></div>
          {!readOnly && <button className="l32-primary" disabled={busy !== null || !definition} onClick={saveMachine}>{busy === "machine" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}保存并校验设备</button>}
          {snapshot && <div className={`l32-validation ${snapshot.validation.valid ? "valid" : "invalid"}`}><strong>{snapshot.validation.valid ? "配置可用于草案" : "配置被阻断"}</strong><small>{snapshot.instance.variant} · {snapshot.validation.enabled_axes.join(" / ")} · {snapshot.configuration_hash.slice(0, 12)}</small>{snapshot.validation.issues.map((issue) => <p key={issue.code}><AlertTriangle size={11} />{issue.message}</p>)}</div>}
        </section>

        <section className="l32-card profile-review">
          <h3><span>02</span>回转轮廓</h3>
          {rotational?.profiles.length ? <>
            <select value={profile?.id} onChange={(event) => selectProfile(event.target.value)}>{rotational.profiles.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.side === "outer" ? "外轮廓" : "内轮廓"} · {item.extraction_method}</option>)}</select>
            {profile && <><ProfileChart profile={profile} result={result} /><div className="l32-profile-meta"><span className={profile.review_state}>{profile.review_state === "accepted" ? "已确认" : profile.review_state === "excluded" ? "已排除" : "待确认"}</span><small>{profile.points.length} 点 · 置信度 {Math.round(profile.confidence * 100)}%</small></div>{profile.review_reasons.map((reason) => <p key={reason}>{reason}</p>)}{!readOnly && <div className="l32-review-actions"><button disabled={busy !== null} onClick={() => reviewProfile("excluded")}><X size={13} />排除</button><button disabled={busy !== null} onClick={() => reviewProfile("accepted")}><Check size={13} />确认轮廓</button></div>}</>}
          </> : <div className="l32-empty"><AlertTriangle size={18} />没有可审查的回转轮廓</div>}
          {(threadFeatures.length > 0 || threadRequirements.length > 0) && <div className="l32-thread-binding">
            <div className="l32-thread-heading"><strong>螺纹证据绑定</strong><span className={selectedThreadFeature?.binding_state ?? "unbound"}>{selectedThreadFeature?.binding_state === "matched" ? "已确认" : selectedThreadFeature?.binding_state === "ambiguous" ? "待工程师确认" : "未绑定"}</span></div>
            {threadFeatures.length > 0 && threadRequirements.length > 0 ? <>
              <label>STEP 周期牙形<select value={selectedThreadFeature?.id ?? ""} onChange={(event) => setSelectedThreadFeatureId(event.target.value)}>{threadFeatures.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.repeat_count} 个重复</option>)}</select></label>
              <label>图纸螺纹<select value={selectedThreadRequirement?.id ?? ""} onChange={(event) => setSelectedThreadRequirementId(event.target.value)}>{threadRequirements.map((item) => <option key={item.id} value={item.id}>{item.thread?.designation ?? item.raw_text ?? item.id}</option>)}</select></label>
              <dl>
                <div><dt>图纸螺距</dt><dd>{selectedThread?.pitch_mm.toFixed(4) ?? "—"} mm</dd></div>
                <div><dt>STEP 候选</dt><dd>{selectedThreadFeature?.pitch_candidates_mm.map((item) => item.toFixed(4)).join(" / ") ?? "—"} mm</dd></div>
                <div><dt>图纸大径</dt><dd>{selectedThread?.major_diameter_mm.toFixed(3) ?? "—"} mm</dd></div>
                <div><dt>STEP 推算大径</dt><dd>{modelledMajorDiameter?.toFixed(3) ?? "—"} mm</dd></div>
                <div><dt>螺距校验</dt><dd className={pitchMatches ? "passed" : "failed"}>{pitchMatches ? "匹配" : "不匹配"}</dd></div>
                <div><dt>大径校验</dt><dd className={diameterMatches ? "passed" : "failed"}>{diameterMatches ? "匹配" : "不匹配"}</dd></div>
              </dl>
              {selectedThreadFeature?.binding_state === "matched" ? <div className="l32-thread-confirmed"><ShieldCheck size={14} /><span><strong>工程师绑定已保存</strong><small>{selectedThreadDraft ? `${selectedThreadDraft.id} · ${selectedThreadDraft.enabled ? "已通过 DRAFT 工序审核" : "草案保持禁用"}` : "正在等待计划刷新"}</small></span></div> : !readOnly && <button className="l32-primary" disabled={busy !== null || !pitchMatches || !diameterMatches || selectedThread?.side === "internal"} onClick={confirmThreadBinding}>{busy === "thread" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}确认图纸与 STEP 绑定</button>}
              {selectedThreadFeature?.binding_state === "matched" && selectedThreadDraft && <div className="l32-thread-review-form">
                <strong>螺纹 DRAFT 工序审核</strong>
                <div><label>起点 Z / mm<input disabled={readOnly} type="number" step="0.001" value={threadStartZ} onChange={(event) => setThreadStartZ(Number(event.target.value))} /></label><label>终点 Z / mm<input disabled={readOnly} type="number" step="0.001" value={threadEndZ} onChange={(event) => setThreadEndZ(Number(event.target.value))} /></label></div>
                <div><label>径向牙深 / mm<input disabled={readOnly} type="number" min="0.001" step="0.001" value={threadDepth} onChange={(event) => setThreadDepth(Number(event.target.value))} /></label><label>切削次数<input disabled={readOnly} type="number" min="1" max="20" step="1" value={threadPassCount} onChange={(event) => setThreadPassCount(Number(event.target.value))} /></label></div>
                <div><label>退刀策略<select disabled={readOnly} value={threadReliefStrategy} onChange={(event) => setThreadReliefStrategy(event.target.value as typeof threadReliefStrategy)}><option value="groove">退刀槽</option><option value="runout">受控退刀</option><option value="thread_to_end">车至端部</option></select></label><label>退刀宽度 / mm<input disabled={readOnly || threadReliefStrategy !== "groove"} type="number" min="0" step="0.001" value={threadReliefWidth} onChange={(event) => setThreadReliefWidth(Number(event.target.value))} /></label></div>
                <label>刀片/刀具实物编号<input disabled={readOnly} value={threadInsertId} placeholder="例如 16ER-16UN" onChange={(event) => setThreadInsertId(event.target.value)} /></label>
                <label>控制器循环标识<input disabled={readOnly} value={threadControllerCycle} placeholder="例如 L32-G92-DRAFT" onChange={(event) => setThreadControllerCycle(event.target.value)} /></label>
                <label>审核人<input disabled={readOnly} value={threadReviewer} onChange={(event) => setThreadReviewer(event.target.value)} /></label>
                {!readOnly && <button className="l32-primary generate" disabled={busy !== null || threadStartZ <= threadEndZ || threadDepth <= 0 || threadPassCount < 1 || !threadInsertId.trim() || !threadControllerCycle.trim() || !threadReviewer.trim() || (threadReliefStrategy === "groove" && threadReliefWidth < (selectedThreadFeature.resolved_pitch_mm ?? 0) * 0.5)} onClick={reviewThreadOperation}>{busy === "thread" ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{selectedThreadDraft.enabled ? "重新审核 DRAFT 工序" : "审核并启用 DRAFT 仿真"}</button>}
              </div>}
              <p className="l32-thread-warning"><AlertTriangle size={12} />{selectedThreadDraft?.enabled ? "仅允许生成控制器无关 DRAFT；未经后处理认证、空运行和试切仍禁止生产。" : "确认只生成禁用草案；起止位置、退刀槽、刀片及控制器循环仍需另行审核。"}</p>
            </> : <div className="l32-thread-empty"><AlertTriangle size={14} />{threadFeatures.length === 0 ? "STEP 中未识别到周期牙形" : "图纸中没有可用的结构化螺纹要求"}</div>}
          </div>}
        </section>

        <section className="l32-card draft-generator">
          <h3><span>03</span>刀路与仿真草案</h3>
          <label>车削工序<select disabled={readOnly || !operationDefinition} value={operationDefinition?.id ?? ""} onChange={(event) => selectOperationType(event.target.value)}>{turningDefinitions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          {plannedOperationCandidates.length > 1 && <label>计划工序<select disabled={readOnly} value={plannedOperation?.id ?? ""} onChange={(event) => { setSelectedPlannedOperationId(event.target.value); setParameterValues({}); setResult(null); }}>{plannedOperationCandidates.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.name} · {item.parameters.profile_z_min_mm !== undefined ? `Z ${Number(item.parameters.profile_z_min_mm).toFixed(3)}～${Number(item.parameters.profile_z_max_mm).toFixed(3)} mm` : `Ø${item.tool.diameter_mm.toFixed(1)} × ${Number(item.parameters.profile_depth_mm ?? item.parameters.depth_mm ?? 0).toFixed(2)} mm`}</option>)}</select></label>}
          {regionalOperation && <div className="l32-validation"><strong>受控轮廓分区 · {plannedOperation.id}</strong><p><ShieldCheck size={11} />{plannedOperation.parameters.cut_direction === "positive_z" ? "正 Z 反向" : "负 Z 纵向"} · Z {Number(plannedOperation.parameters.profile_z_min_mm).toFixed(3)}～{Number(plannedOperation.parameters.profile_z_max_mm).toFixed(3)} mm · {plannedOperation.tool.id}</p><p><AlertTriangle size={11} />只按已保存工序生成 DRAFT；区域外的背面/槽形不计入整件覆盖，刀具实物与机床干涉仍待认证。</p></div>}
          <div className="l32-parameter-grid">{operationDefinition?.parameters.map((parameter) => <label key={parameter.key}><span>{parameter.label}{parameter.unit ? ` / ${parameter.unit}` : ""}</span>{parameter.type === "enum" ? <select disabled={readOnly || regionalOperation} value={String(effectiveParameterValues[parameter.key] ?? "")} onChange={(event) => setParameterValues((current) => ({ ...current, [parameter.key]: event.target.value }))}>{parameter.choices.map((choice) => <option key={choice}>{choice}</option>)}</select> : <input disabled={readOnly || regionalOperation} type="number" min={parameter.minimum ?? undefined} max={parameter.maximum ?? undefined} value={Number(effectiveParameterValues[parameter.key] ?? 0)} onChange={(event) => setParameterValues((current) => ({ ...current, [parameter.key]: Number(event.target.value) }))} />}</label>)}</div>
          {isAxialDrillingOperation && plannedOperation && <div className="l32-boring-review">
            <div><strong>镗削预孔工程审核</strong><span className={plannedOperation.enabled ? "passed" : "warning"}>{plannedOperation.enabled ? "已通过" : "草案禁用"}</span></div>
            <p><ShieldCheck size={11} />孔级 {Number(plannedOperation.parameters.drilling_stage_index ?? 1)}/{Number(plannedOperation.parameters.planned_drilling_stage_count ?? 1)} · 目标 Ø{Number(plannedOperation.parameters.target_bore_diameter_mm ?? 0).toFixed(2)} × {Number(plannedOperation.parameters.profile_depth_mm ?? 0).toFixed(2)} mm · L/D {Number(plannedOperation.parameters.stage_length_to_diameter_ratio ?? plannedOperation.parameters.maximum_bore_length_to_diameter_ratio ?? 0).toFixed(2)} · {plannedOperation.parameters.deep_hole_review_required ? "深孔门禁生效" : "标准孔深范围"}</p>
            <label>现场钻头<select disabled={readOnly} value={drillToolId} onChange={(event) => setDrillToolId(event.target.value)}>{catalogs?.tools.filter((tool) => tool.kind === "drill").map((tool) => <option key={tool.id} value={tool.id}>{tool.name}</option>)}</select></label>
            <label>确认伸出 / mm<input disabled={readOnly} type="number" min="0.1" step="0.1" value={drillStickout} onChange={(event) => setDrillStickout(Number(event.target.value))} /></label>
            <label>钻尖角 / °<input disabled={readOnly} type="number" min="90" max="150" step="1" value={drillPointAngle} onChange={(event) => setDrillPointAngle(Number(event.target.value))} /></label>
            <label>啄钻深度 / mm<input disabled={readOnly} type="number" min="0.1" step="0.1" value={drillPeckDepth} onChange={(event) => setDrillPeckDepth(Number(event.target.value))} /></label>
            <label>孔底条件<select disabled={readOnly} value={drillBottomCondition} onChange={(event) => setDrillBottomCondition(event.target.value as typeof drillBottomCondition)}><option value="blind_tip_allowance_confirmed">盲孔钻尖余量已确认</option><option value="through">通孔</option></select></label>
            <label>允许钻尖越程 / mm<input disabled={readOnly || drillBottomCondition === "through"} type="number" min="0" step="0.1" value={drillTipAllowance} onChange={(event) => setDrillTipAllowance(Number(event.target.value))} /></label>
            <label>排屑策略<select disabled={readOnly} value={drillChipStrategy} onChange={(event) => setDrillChipStrategy(event.target.value as typeof drillChipStrategy)}><option value="standard_peck">标准啄钻</option><option value="deep_hole_peck">深孔分级退刀</option></select></label>
            <label><span>刀具内冷</span><input disabled={readOnly || drillChipStrategy !== "deep_hole_peck"} type="checkbox" checked={drillCoolantConfirmed} onChange={(event) => setDrillCoolantConfirmed(event.target.checked)} /> 已确认现场内冷</label>
            <label>钻头实物编号<input disabled={readOnly} value={drillInventoryId} placeholder="现场刀具编号" onChange={(event) => setDrillInventoryId(event.target.value)} /></label>
            <label>审核人<input disabled={readOnly} value={drillReviewer} onChange={(event) => setDrillReviewer(event.target.value)} /></label>
            {!readOnly && <button className="l32-primary" disabled={busy !== null || !drillToolId || drillStickout <= 0 || drillPeckDepth <= 0 || !drillInventoryId.trim() || !drillReviewer.trim() || (drillChipStrategy === "deep_hole_peck" && !drillCoolantConfirmed)} onClick={reviewAxialDrillingOperation}>{busy === "drilling" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}{plannedOperation.enabled ? "重新审核预孔工序" : "审核并启用预孔 DRAFT"}</button>}
            <p><AlertTriangle size={11} />系统会校验镗杆入口下限、成品孔余量、刃长、伸出和钻尖越程；这里只生成控制器无关循环。</p>
          </div>}
          {isBoringOperation && plannedOperation && <div className="l32-boring-review">
            <div><strong>内孔可达性审核</strong><span className={plannedOperation.enabled ? "passed" : "warning"}>{plannedOperation.enabled ? "已通过" : "草案禁用"}</span></div>
            {isFinishingBoringOperation && <label>现场精镗刀<select disabled={readOnly} value={boringToolId} onChange={(event) => setBoringToolId(event.target.value)}>{catalogs?.tools.filter((tool) => tool.kind === "turning_id").map((tool) => <option key={tool.id} value={tool.id}>{tool.name} · R{Number(tool.nose_radius_mm ?? 0).toFixed(2)} · 杆径 Ø{tool.holder_diameter_mm.toFixed(1)}</option>)}</select></label>}
            {isFinishingBoringOperation && sharpInnerShoulderCount > 0 && <><p><AlertTriangle size={11} />检测到 {sharpInnerShoulderCount} 个尖锐内肩，刀尖半径必须不大于 {Number(plannedOperation.parameters.maximum_finish_nose_radius_mm ?? 0.05).toFixed(2)} mm。</p><label>内肩处理策略<select disabled={readOnly} value={boringShoulderStrategy} onChange={(event) => setBoringShoulderStrategy(event.target.value as typeof boringShoulderStrategy)}><option value="small_nose_tool">小刀尖精镗刀</option><option value="not_required">无需处理（将被门禁拦截）</option></select></label></>}
            <label>实测初始孔直径 / mm<input disabled={readOnly} type="number" min="0.1" step="0.1" value={initialBoreDiameter} onChange={(event) => setInitialBoreDiameter(Number(event.target.value))} /></label>
            <label>确认伸出长度 / mm<input disabled={readOnly} type="number" min="0.1" step="0.1" value={boringStickout} onChange={(event) => setBoringStickout(Number(event.target.value))} /></label>
            <label>装配间隙 / mm<input disabled={readOnly} type="number" min="0" max="5" step="0.05" value={boringClearance} onChange={(event) => setBoringClearance(Number(event.target.value))} /></label>
            <label>镗杆实物编号<input disabled={readOnly} value={boringInventoryId} placeholder="现场刀具编号" onChange={(event) => setBoringInventoryId(event.target.value)} /></label>
            <label>审核人<input disabled={readOnly} value={boringReviewer} onChange={(event) => setBoringReviewer(event.target.value)} /></label>
            {!readOnly && <button className="l32-primary" disabled={busy !== null || initialBoreDiameter <= 0 || boringStickout <= 0 || !boringInventoryId.trim() || !boringReviewer.trim() || (isFinishingBoringOperation && !boringToolId) || (isFinishingBoringOperation && sharpInnerShoulderCount > 0 && boringShoulderStrategy !== "small_nose_tool")} onClick={reviewBoringOperation}>{busy === "boring" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}{plannedOperation.enabled ? "重新审核内孔工序" : "审核并启用内孔 DRAFT"}</button>}
            <p><AlertTriangle size={11} />只审核当前镗杆和初始孔条件；更换刀杆、伸出或入口孔后必须重新审核。</p>
          </div>}
          {isGroovingOperation && plannedOperation && <div className="l32-boring-review">
            <div><strong>{isInternalGroovingOperation ? "内槽工程审核" : "外槽工程审核"}</strong><span className={plannedOperation.enabled ? "passed" : "warning"}>{plannedOperation.enabled ? "已通过" : "草案禁用"}</span></div>
            <p><ShieldCheck size={11} />候选范围 Z {Number(plannedOperation.parameters.groove_start_z_mm ?? 0).toFixed(3)} ～ {Number(plannedOperation.parameters.groove_end_z_mm ?? 0).toFixed(3)} mm · 径向深度 {Number(plannedOperation.parameters.groove_depth_mm ?? 0).toFixed(3)} mm</p>
            <div><strong>图纸—STEP 槽绑定</strong><span className={grooveBindingMatched ? "passed" : "warning"}>{grooveBindingMatched ? "唯一匹配" : "待确认"}</span></div>
            <label>图纸要求编号<input disabled={readOnly || grooveBindingMatched} value={grooveRequirementId} onChange={(event) => setGrooveRequirementId(event.target.value)} /></label>
            <label>槽要求类型<select disabled={readOnly || grooveBindingMatched} value={grooveRequirementKind} onChange={(event) => setGrooveRequirementKind(event.target.value as typeof grooveRequirementKind)}><option value="external_groove">外槽</option><option value="internal_groove">内槽</option><option value="seal_groove">密封槽</option></select></label>
            <label>确认槽宽 / mm<input disabled={readOnly} type="number" min="0.01" step="0.01" value={grooveWidth} onChange={(event) => setGrooveWidth(Number(event.target.value))} /></label>
            <label>确认径向槽深 / mm<input disabled={readOnly || grooveBindingMatched} type="number" min="0.01" step="0.01" value={grooveDrawingDepth} onChange={(event) => setGrooveDrawingDepth(Number(event.target.value))} /></label>
            <label>确认槽底直径 / mm<input disabled={readOnly} type="number" min="0.01" step="0.01" value={grooveFinalDiameter} onChange={(event) => setGrooveFinalDiameter(Number(event.target.value))} /></label>
            <label>图纸原文/标注<input disabled={readOnly || grooveBindingMatched} value={grooveRawText} placeholder="例如：密封槽 1.00，槽底 Ø17.00" onChange={(event) => setGrooveRawText(event.target.value)} /></label>
            {!readOnly && !grooveBindingMatched && <button className="l32-primary" disabled={busy !== null || !grooveRequirementId.trim() || grooveWidth <= 0 || grooveDrawingDepth <= 0 || grooveFinalDiameter <= 0 || !grooveReviewer.trim()} onClick={confirmGrooveBinding}>{busy === "groove-binding" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}确认图纸尺寸与 STEP 唯一绑定</button>}
            <label>径向分层切入 / mm<input disabled={readOnly} type="number" min="0.01" step="0.05" value={groovePeckDepth} onChange={(event) => setGroovePeckDepth(Number(event.target.value))} /></label>
            <label>现场切槽刀<select disabled={readOnly} value={grooveToolId} onChange={(event) => setGrooveToolId(event.target.value)}>{catalogs?.tools.filter((tool) => tool.kind === (isInternalGroovingOperation ? "internal_grooving" : "grooving")).map((tool) => <option key={tool.id} value={tool.id}>{tool.name} · 刀宽 {Number(tool.cutting_width_mm ?? 0).toFixed(2)} mm</option>)}</select></label>
            {isInternalGroovingOperation && <><label>确认刀杆伸出 / mm<input disabled={readOnly} type="number" min="0.1" step="0.1" value={grooveStickout} onChange={(event) => setGrooveStickout(Number(event.target.value))} /></label><label>刀杆装配间隙 / mm<input disabled={readOnly} type="number" min="0" max="5" step="0.05" value={grooveClearance} onChange={(event) => setGrooveClearance(Number(event.target.value))} /></label></>}
            <label>切槽刀实物编号<input disabled={readOnly} value={grooveInventoryId} placeholder="现场刀具编号" onChange={(event) => setGrooveInventoryId(event.target.value)} /></label>
            <label><span>槽形确认</span><input disabled={readOnly} type="checkbox" checked={grooveFormConfirmed} onChange={(event) => setGrooveFormConfirmed(event.target.checked)} /> 已根据图纸/CAD确认槽形与槽底</label>
            <label>审核人<input disabled={readOnly} value={grooveReviewer} onChange={(event) => setGrooveReviewer(event.target.value)} /></label>
            {!readOnly && <button className="l32-primary" disabled={busy !== null || !grooveBindingMatched || grooveWidth <= 0 || grooveFinalDiameter <= 0 || groovePeckDepth <= 0 || !grooveToolId || !grooveInventoryId.trim() || !grooveFormConfirmed || !grooveReviewer.trim() || (isInternalGroovingOperation && grooveStickout <= 0)} onClick={reviewGroovingOperation}>{busy === "grooving" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}{plannedOperation.enabled ? `重新审核${isInternalGroovingOperation ? "内" : "外"}槽工序` : `审核并启用${isInternalGroovingOperation ? "内" : "外"}槽 DRAFT`}</button>}
            <p><AlertTriangle size={11} />宽槽将按实际刀宽自动拆分轴向刀位，并按确认切入量分层；圆角、倒角和异形槽尚不自动放行。{isInternalGroovingOperation ? "内槽必须先完成基孔精镗审核。" : ""}</p>
          </div>}
          <div className="l32-stock-grid"><label>棒料半径 / mm<input disabled={readOnly} type="number" min="0.1" max={barDiameter / 2} step="0.1" value={stockRadius} onChange={(event) => setStockRadius(Number(event.target.value))} /></label>{profile?.side === "inner" && <label>初始孔半径 / mm<input disabled={readOnly} type="number" min="2.1" max={stockRadius - 0.1} step="0.1" value={initialBoreRadius} onChange={(event) => setInitialBoreRadius(Number(event.target.value))} /></label>}<label>Z 最小 / mm<input disabled={readOnly} type="number" step="0.1" value={zMin} onChange={(event) => setZMin(Number(event.target.value))} /></label><label>Z 最大 / mm<input disabled={readOnly} type="number" step="0.1" value={zMax} onChange={(event) => setZMax(Number(event.target.value))} /></label></div>
          {operationDefinition?.id === "turn_cutoff" && <div className="l32-stock-grid"><label>背轴接近 Z / mm<input disabled={readOnly} type="number" step="0.1" value={approachZ} onChange={(event) => setApproachZ(Number(event.target.value))} /></label><label>接料位置 Z / mm<input disabled={readOnly} type="number" step="0.1" value={pickoffZ} onChange={(event) => setPickoffZ(Number(event.target.value))} /></label><label>夹持长度 / mm<input disabled={readOnly} type="number" min="0.1" max="100" step="0.1" value={gripLength} onChange={(event) => setGripLength(Number(event.target.value))} /></label><label>同步转速 / rpm<input disabled={readOnly} type="number" min="1" max="8000" step="100" value={synchronizationRpm} onChange={(event) => setSynchronizationRpm(Number(event.target.value))} /></label><label className="wide"><span>背轴夹紧条件</span><input disabled={readOnly} type="checkbox" checked={subSpindleClampConfirmed} onChange={(event) => setSubSpindleClampConfirmed(event.target.checked)} /> 已确认夹紧压力/夹持力</label></div>}
          {!readOnly && <button className="l32-primary generate" disabled={busy !== null || !snapshot?.validation.valid || profile?.review_state !== "accepted" || !operationDefinition || ((isAxialDrillingOperation || isBoringOperation || isGroovingOperation) && !plannedOperation?.enabled) || (operationDefinition.id === "turn_cutoff" && !subSpindleClampConfirmed)} onClick={generateDraft}>{busy === "draft" ? <LoaderCircle className="spin" size={14} /> : <Play size={14} />}{operationDefinition?.id === "turn_cutoff" ? "生成同步接料 DRAFT" : "生成 DRAFT"}</button>}
          {profile?.side === "inner" && <div className="l32-validation">
            <strong>内孔连续余料链</strong>
            <p>{innerChainReady ? `${innerChainOperations.length} 道预孔/镗孔工序已完成审核` : "必须先逐项审核全部预孔、粗镗和精镗工序"}</p>
            {!readOnly && <button className="l32-primary generate" disabled={busy !== null || !snapshot?.validation.valid || !innerChainReady} onClick={generateInnerBoreChain}><Play size={14} />生成内孔链连续 DRAFT</button>}
            {innerBoreChainResult && <div className={`l32-result ${innerBoreChainResult.status}`}><div><span>BORE CHAIN</span><strong>{innerBoreChainResult.stages.length}</strong><small>连续工序</small></div><dl><div><dt>连续材料</dt><dd className={innerBoreChainResult.status}>{innerBoreChainResult.status === "passed" ? "通过" : "阻断"}</dd></div><div><dt>最终过切</dt><dd>{innerBoreChainResult.final_verification.metrics.maximum_overcut_mm.toFixed(3)} mm</dd></div><div><dt>最终残料</dt><dd>{innerBoreChainResult.final_verification.metrics.maximum_excess_stock_mm.toFixed(3)} mm</dd></div><div><dt>程序哈希</dt><dd>{innerBoreChainResult.program_hash.slice(0, 12)}</dd></div></dl>{innerBoreChainResult.stages.map((stage) => <p key={stage.operation_id}>{stage.verification_status === "failed" ? <AlertTriangle size={11} /> : <Check size={11} />}{stage.operation_id} · {stage.operation_type} · 去除 {stage.removed_volume_mm3.toFixed(1)} mm³ · {stage.verification_status}</p>)}{innerBoreChainResult.checks.map((check) => <p key={check.id}>{check.status === "passed" ? <Check size={11} /> : <AlertTriangle size={11} />}{check.message}</p>)}</div>}
          </div>}
          {result && <div className={`l32-result ${result.thread_verification?.status ?? result.verification?.status ?? "warning"}`}><div><span>DRAFT</span><strong>{result.simulation.metrics.removal_percent.toFixed(2)}%</strong><small>材料去除率</small></div><dl><div><dt>刀具可达性</dt><dd className={result.reachability?.status}>{result.reachability?.status === "passed" ? "通过" : result.reachability?.status === "failed" ? "阻断" : "需复核"}</dd></div><div><dt>通道数量</dt><dd>{result.toolpath.channels.length}</dd></div><div><dt>{result.thread_verification ? "螺纹校核" : "轮廓校核"}</dt><dd className={result.thread_verification?.status ?? result.verification?.status}>{(result.thread_verification?.status ?? result.verification?.status) === "passed" ? "通过" : (result.thread_verification?.status ?? result.verification?.status) === "failed" ? "失败" : "警告"}</dd></div><div><dt>{result.thread_verification ? "刀次" : "最大过切"}</dt><dd>{result.thread_verification ? `${result.thread_verification.metrics.emitted_pass_count}/${result.thread_verification.metrics.pass_count}` : `${result.verification?.metrics.maximum_overcut_mm.toFixed(3) ?? "—"} mm`}</dd></div><div><dt>{result.thread_verification ? "牙底小径" : "最大残料"}</dt><dd>{result.thread_verification ? `${result.thread_verification.metrics.minor_diameter_mm.toFixed(3)} mm` : `${result.verification?.metrics.maximum_excess_stock_mm.toFixed(3) ?? "—"} mm`}</dd></div><div><dt>状态迁移</dt><dd>{"state_transitions" in result ? result.state_transitions.length : 0}</dd></div><div><dt>IR 指令</dt><dd>{result.toolpath.channels.reduce((sum, channel) => sum + channel.commands.length, 0)}</dd></div><div><dt>去除体积</dt><dd>{result.simulation.metrics.removed_volume_mm3.toFixed(1)} mm³</dd></div><div><dt>NC 输出</dt><dd>否</dd></div></dl>{"state_transitions" in result && result.state_transitions.map((state) => <p key={state.sequence}><Check size={11} />{state.state} · {state.holding_spindles.join("+")}{state.barrier_id ? ` · ${state.barrier_id}` : ""}</p>)}{result.thread_verification?.checks.map((check) => <p key={check.id}>{check.status === "passed" ? <Check size={11} /> : <AlertTriangle size={11} />}{check.message}</p>)}{result.reachability?.warnings.map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}{result.thread_verification?.warnings.map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}{result.verification?.warnings.map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}{result.warnings.map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}</div>}
          {backsideOperations.length > 0 && <div className="l32-validation"><strong>接料后背面工序</strong><label>背轴工序<select disabled={readOnly} value={backsideOperation?.id ?? ""} onChange={(event) => { setBacksideOperationId(event.target.value); setBacksideResult(null); }}>{backsideOperations.map((operation) => <option key={operation.id} value={operation.id}>{operation.id} · {operation.name}{operation.enabled ? "" : "（设备能力未启用）"}</option>)}</select></label>{!readOnly && <button className="l32-primary" disabled={busy !== null || !snapshot?.validation.valid || profile?.review_state !== "accepted" || !backsideOperation?.enabled} onClick={generateBacksideDraft}><Play size={14} />生成 {backsideOperation?.id ?? "背面"} DRAFT</button>}{backsideResult && <div className={`l32-result ${backsideResult.draft.verification?.status ?? "warning"}`}><div><span>SUB</span><strong>{backsideResult.derived_profile.points.length}</strong><small>背轴轮廓点</small></div><dl><div><dt>坐标换向</dt><dd>Z × {backsideResult.transform.z_scale}</dd></div><div><dt>背轴通道</dt><dd>{backsideResult.draft.toolpath.channels[0]?.id}</dd></div><div><dt>IR 指令</dt><dd>{backsideResult.draft.toolpath.channels[0]?.commands.length ?? 0}</dd></div><div><dt>NC 输出</dt><dd>否</dd></div></dl>{backsideResult.warnings.map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}</div>}</div>}
          {profile?.side === "outer" && hasFrontFormOperations && <div className="l32-validation">
            <strong>前端纵车＋成形连续余料链</strong>
            <p>{frontChainReady ? "OP20 → OP21-FORM → OP30 → OP31-FORM 已可连续验证" : "前端区域工序尚未全部启用"}</p>
            {!readOnly && <button className="l32-primary generate" disabled={busy !== null || !snapshot?.validation.valid || profile.review_state !== "accepted" || !frontChainReady} onClick={generateFrontChainDraft}><Play size={14} />生成前端连续 DRAFT</button>}
            {frontChainResult && <div className={`l32-result ${frontChainResult.status}`}>
              <div><span>FRONT CHAIN</span><strong>{frontChainResult.stages.length}</strong><small>连续工序</small></div>
              <dl><div><dt>主纵车过切</dt><dd>{frontChainResult.longitudinal_verification.metrics.maximum_overcut_mm.toFixed(3)} mm</dd></div><div><dt>前端成形过切</dt><dd>{frontChainResult.front_form_verification.metrics.maximum_overcut_mm.toFixed(3)} mm</dd></div><div><dt>连续材料</dt><dd className={frontChainResult.status}>{frontChainResult.status === "passed" ? "通过" : "阻断"}</dd></div><div><dt>NC 输出</dt><dd>否</dd></div></dl>
              {frontChainResult.stages.map((stage) => <p key={stage.operation_id}>{stage.verification_status === "failed" ? <AlertTriangle size={11} /> : <Check size={11} />}{stage.operation_id} · 去除 {stage.removed_volume_mm3.toFixed(1)} mm³ · {stage.verification_status}</p>)}
              {frontChainResult.checks.map((check) => <p key={check.id}>{check.status === "passed" ? <Check size={11} /> : <AlertTriangle size={11} />}{check.message}</p>)}
              {frontChainResult.warnings.map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}
            </div>}
          </div>}
          {profile?.side === "outer" && backsideOperations.some((item) => item.id === "OP55-BACK") && <div className="l32-validation">
            <strong>背面粗精车连续余料链</strong>
            <p>{backsideChainReady ? "OP55-BACK 与 OP58-BACK 已由设备配置启用" : "需先确认实机背面刀具模块，当前工序保持禁用"}</p>
            {!readOnly && <button className="l32-primary generate" disabled={busy !== null || !snapshot?.validation.valid || profile.review_state !== "accepted" || !backsideChainReady} onClick={generateBacksideChainDraft}><Play size={14} />生成背面连续 DRAFT</button>}
            {backsideChainResult && <div className={`l32-result ${backsideChainResult.status}`}>
              <div><span>BACK CHAIN</span><strong>{backsideChainResult.stages.length}</strong><small>连续工序</small></div>
              <dl><div><dt>连续材料</dt><dd className={backsideChainResult.status}>{backsideChainResult.status === "passed" ? "通过" : "阻断"}</dd></div><div><dt>最终过切</dt><dd>{backsideChainResult.final_verification.metrics.maximum_overcut_mm.toFixed(3)} mm</dd></div><div><dt>最终残料</dt><dd>{backsideChainResult.final_verification.metrics.maximum_excess_stock_mm.toFixed(3)} mm</dd></div><div><dt>NC 输出</dt><dd>否</dd></div></dl>
              {backsideChainResult.stages.map((stage) => <p key={stage.operation_id}>{stage.verification_status === "failed" ? <AlertTriangle size={11} /> : <Check size={11} />}{stage.operation_id} · 去除 {stage.removed_volume_mm3.toFixed(1)} mm³ · {stage.verification_status}</p>)}
              {backsideChainResult.checks.map((check) => <p key={check.id}>{check.status === "passed" ? <Check size={11} /> : <AlertTriangle size={11} />}{check.message}</p>)}
              {backsideChainResult.warnings.map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}
            </div>}
          </div>}
          {backsideOperations.length > 0 && <div className="l32-validation"><strong>OP10–OP60 整件程序</strong>{hasRegionalOperations && <p><Check size={11} />整件编排将包含前端及背面分区工序，并执行跨坐标最终轮廓校核。</p>}{hasCutoffKerfOverlap && <p><AlertTriangle size={11} />切断刀缝预计侵入成品 {cutoffKerfIntrusion.toFixed(3)} mm；需增加牺牲余料并重设背轴基准。</p>}<label><input disabled={readOnly} type="checkbox" checked={subSpindleClampConfirmed} onChange={(event) => setSubSpindleClampConfirmed(event.target.checked)} /> 已确认背轴夹紧条件</label>{!readOnly && <button className="l32-primary generate" disabled={busy !== null || hasCutoffKerfOverlap || !snapshot?.validation.valid || profile?.review_state !== "accepted" || !subSpindleClampConfirmed || backsideOperations.some((operation) => !operation.enabled)} onClick={generateWholePartDraft}><Play size={14} />生成整件双通道 DRAFT</button>}{wholePartResult && <div className={`l32-result ${wholePartResult.continuous_simulation.status === "passed" ? "passed" : "failed"}`}><div><span>WHOLE</span><strong>{wholePartResult.stages.length}</strong><small>工序阶段</small></div><dl><div><dt>预计节拍</dt><dd>{wholePartResult.timeline.estimated_cycle_seconds.toFixed(1)} s</dd></div><div><dt>同步屏障</dt><dd>{wholePartResult.timeline.barrier_order.length}</dd></div><div><dt>连续材料</dt><dd className={wholePartResult.continuous_simulation.status}>{wholePartResult.continuous_simulation.status === "passed" ? "通过" : "失败"}</dd></div><div><dt>总去除体积</dt><dd>{wholePartResult.continuous_simulation.total_removed_volume_mm3.toFixed(1)} mm³</dd></div><div><dt>程序哈希</dt><dd>{wholePartResult.program_hash.slice(0, 12)}</dd></div><div><dt>NC 输出</dt><dd>否</dd></div></dl>{wholePartResult.stages.map((stage) => <p key={stage.operation_id}><Check size={11} />{stage.operation_id} · {stage.channel_id} · {stage.command_count} 指令 · {stage.verification_status}</p>)}{wholePartResult.timeline.barrier_order.map((barrier) => { const waits = wholePartResult.timeline.events.filter((event) => event.barrier_id === barrier); return <p key={barrier}><ShieldCheck size={11} />{barrier} · 等待 {Math.max(...waits.map((event) => event.wait_seconds), 0).toFixed(2)} s</p>; })}{wholePartResult.continuous_simulation.checks.map((check) => <p key={check.id}>{check.status === "passed" ? <Check size={11} /> : <AlertTriangle size={11} />}{check.message}</p>)}{[...wholePartResult.timeline.warnings, ...wholePartResult.continuous_simulation.warnings, ...wholePartResult.warnings].map((warning) => <p key={warning}><AlertTriangle size={11} />{warning}</p>)}</div>}</div>}
        </section>
      </div>}
      {message && <footer className="l32-status">{busy && <LoaderCircle className="spin" size={13} />}{message}</footer>}
    </section>
  </div>;
}

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, ChevronRight, CirclePause, Gauge, LoaderCircle, Play, RotateCcw, Route, X } from "lucide-react";
import type { Bounds, Job, Operation, RotationalFeatureAnalysis, RotationalProfile } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const apiUrl = (path: string) => `${API_BASE}${path}`;
type StockSample = { z: number; outer_radius: number; inner_radius: number };
type ProgramCommand = { sequence: number; type: string; operation_id: string; axes: Record<string, number>; parameters: Record<string, string | number | boolean> };
type ProgramArtifact = {
  program_hash: string;
  toolpath: { channels: Array<{ id: string; commands: ProgramCommand[] }> };
  coordinate_frames: Array<{ channel_id: string; source_cutoff_z_mm?: number | null }>;
  stages: Array<{ sequence: number; operation_id: string; operation_name: string; channel_id: string; phase: string; command_count: number; verification_status: string }>;
  continuous_simulation: {
    status: "passed" | "failed";
    main_frame_after_cutoff: { samples: StockSample[] };
    sub_frame_final: { samples: StockSample[] };
    initial_volume_mm3: number; final_volume_mm3: number; total_removed_volume_mm3: number;
  };
};
type ProgramStage = {
  operation: Operation; channelId: string; phase: string; commandCount: number; verificationStatus: string;
  kind: "outer" | "inner" | "radial" | "handling";
  points: Array<{ z: number; radius: number; motion: string }>;
};

function operationKind(operation: Operation): ProgramStage["kind"] {
  if (["axial_drilling", "turn_id_roughing", "turn_id_finishing"].includes(operation.type)
    || (operation.type === "turn_grooving" && operation.parameters.groove_side === "internal")) return "inner";
  if (["turn_facing", "turn_cutoff", "turn_grooving"].includes(operation.type)) return "radial";
  if (operation.type.startsWith("turn_") || operation.type.includes("forming")) return "outer";
  return "handling";
}

function actualProgramStages(operations: Operation[], program: ProgramArtifact): ProgramStage[] {
  const operationsById = new Map(operations.map((operation) => [operation.id, operation]));
  const cutoffDatum = Number(program.coordinate_frames.find((frame) => frame.channel_id === "sub")?.source_cutoff_z_mm ?? 0);
  const paths = new Map<string, Array<{ z: number; radius: number; motion: string }>>();
  for (const channel of program.toolpath.channels) {
    const position: Record<string, number> = {};
    for (const command of channel.commands) {
      const previous = { ...position };
      Object.assign(position, command.axes);
      if (!["rapid_move", "feed_move", "arc_move", "cutoff"].includes(command.type)) continue;
      if (!Number.isFinite(position.X) || !Number.isFinite(position.Z)) continue;
      const convertZ = (value: number) => channel.id === "sub" ? cutoffDatum - value : value;
      const points = paths.get(command.operation_id) ?? [];
      if (Number.isFinite(previous.X) && Number.isFinite(previous.Z)) {
        const start = { z: convertZ(previous.Z), radius: Math.abs(previous.X) / 2, motion: command.type };
        const last = points.at(-1);
        if (!last || Math.abs(last.z - start.z) > 1e-8 || Math.abs(last.radius - start.radius) > 1e-8) points.push(start);
      }
      points.push({ z: convertZ(position.Z), radius: Math.abs(position.X) / 2, motion: command.type });
      paths.set(command.operation_id, points);
    }
  }
  return program.stages.map((stage) => {
    const operation = operationsById.get(stage.operation_id) ?? ({
      id: stage.operation_id, sequence: stage.sequence * 10, type: "machine_event", name: stage.operation_name,
      feature_ids: [], tool: { id: "machine", name: "机床动作", kind: "machine", diameter_mm: 0, flute_count: 0, max_rpm: 0, catalog_match: false, flute_length_mm: 0, stickout_mm: 0, holder_diameter_mm: 0 },
      parameters: {}, rationale: [], confidence: 1, status: "proposed", enabled: true, generation_state: "generated",
      definition_id: null, definition_version: 1, source: "automatic",
    } satisfies Operation);
    return { operation, channelId: stage.channel_id, phase: stage.phase, commandCount: stage.command_count, verificationStatus: stage.verification_status, kind: operationKind(operation), points: paths.get(stage.operation_id) ?? [] };
  });
}

function ProgramChart({ outer, inner, stockRadius, stages, currentStage, samples }: {
  outer: RotationalProfile; inner: RotationalProfile | null; stockRadius: number; stages: ProgramStage[]; currentStage: number; samples: StockSample[];
}) {
  const completed = stages.slice(0, currentStage + 1);
  const active = stages[currentStage];
  const allZ = [...outer.points.map((point) => point.z), ...samples.map((sample) => sample.z), ...completed.flatMap((stage) => stage.points.map((point) => point.z))];
  const minZ = Math.min(...allZ), maxZ = Math.max(...allZ);
  const maxRadius = Math.max(stockRadius, ...samples.map((sample) => sample.outer_radius), ...completed.flatMap((stage) => stage.points.map((point) => point.radius)), 1);
  const x = (z: number) => 44 + (z - minZ) / Math.max(maxZ - minZ, 0.001) * 664;
  const y = (radius: number) => 176 - radius / (maxRadius * 1.08) * 135;
  const mirrorY = (radius: number) => 176 + radius / (maxRadius * 1.08) * 135;
  const linePoints = (points: Array<{ z: number; radius: number }>, mirror = false) => points.map((point) => `${x(point.z)},${mirror ? mirrorY(point.radius) : y(point.radius)}`).join(" ");
  const outerStock = samples.map((sample) => ({ z: sample.z, radius: sample.outer_radius }));
  const innerStock = samples.map((sample) => ({ z: sample.z, radius: sample.inner_radius }));
  const materialPolygon = [...outerStock.map((point) => `${x(point.z)},${y(point.radius)}`), ...[...outerStock].reverse().map((point) => `${x(point.z)},${mirrorY(point.radius)}`)].join(" ");
  const borePolygon = [...innerStock.map((point) => `${x(point.z)},${y(point.radius)}`), ...[...innerStock].reverse().map((point) => `${x(point.z)},${mirrorY(point.radius)}`)].join(" ");
  return <svg className="l32-quick-chart" viewBox="0 0 752 352" role="img" aria-label="L32 真实刀路和连续材料仿真">
    <defs><linearGradient id="l32-stock" x1="0" y1="0" x2="0" y2="1"><stop stopColor="#405b73" /><stop offset="1" stopColor="#20394f" /></linearGradient><pattern id="l32-grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M 24 0 L 0 0 0 24" fill="none" stroke="#dbe5ec" strokeWidth="0.6" /></pattern></defs>
    <rect width="752" height="352" fill="url(#l32-grid)" /><line className="axis" x1="28" y1="176" x2="728" y2="176" />
    <rect className="raw-stock" x={x(minZ)} y={y(stockRadius)} width={Math.max(x(maxZ) - x(minZ), 1)} height={mirrorY(stockRadius) - y(stockRadius)} />
    {samples.length > 1 && <polygon className="finished-stock" points={materialPolygon} />}
    {samples.some((sample) => sample.inner_radius > 0) && <polygon className="bore" points={borePolygon} />}
    <polyline className="target" points={linePoints(outer.points)} /><polyline className="target mirror" points={linePoints(outer.points, true)} />
    {inner && <><polyline className="inner-target" points={linePoints(inner.points)} /><polyline className="inner-target mirror" points={linePoints(inner.points, true)} /></>}
    {completed.flatMap((stage) => stage.points.length > 1 ? [<polyline key={`${stage.operation.id}-top`} className={`toolpath ${stage.kind} ${stage === active ? "active" : ""}`} points={linePoints(stage.points)} />, stage.kind !== "inner" ? <polyline key={`${stage.operation.id}-bottom`} className={`toolpath ${stage.kind} mirror ${stage === active ? "active" : ""}`} points={linePoints(stage.points, true)} /> : null] : [])}
    {active?.points.at(-1) && <g className="tool-marker" transform={`translate(${x(active.points.at(-1)!.z)} ${y(active.points.at(-1)!.radius)})`}><circle r="7" /><path d="M-3,-3 L5,0 L-3,3 Z" /></g>}
    <text x="43" y="24">L32 TOOLPATH IR · CONTINUOUS MATERIAL</text><text x="640" y="338">Z / mm</text><text x="15" y="167">R</text>
  </svg>;
}

export function L32ProgramViewer({ jobId, operations, stock, fallbackBounds, wholePartBlockers = [], onClose, onOpenEngineering }: {
  jobId: string; operations: Operation[]; stock: Record<string, unknown>; fallbackBounds?: Bounds | null; wholePartBlockers?: string[]; onClose: () => void; onOpenEngineering: () => void;
}) {
  const [analysis, setAnalysis] = useState<RotationalFeatureAnalysis | null>(null);
  const [program, setProgram] = useState<ProgramArtifact | null>(null);
  const [loading, setLoading] = useState(true), [error, setError] = useState(""), [generating, setGenerating] = useState(false), [playing, setPlaying] = useState(false), [currentStage, setCurrentStage] = useState(0);
  useEffect(() => {
    if (wholePartBlockers.length) return undefined;
    let cancelled = false;
    Promise.all([
      fetch(apiUrl(`/api/v1/jobs/${jobId}/turning/analyze`), { method: "POST" }).then(async (response) => { const payload = await response.json(); if (!response.ok) throw new Error(payload.detail || "无法读取回转特征"); return payload as RotationalFeatureAnalysis; }),
      fetch(apiUrl(`/api/v1/jobs/${jobId}/files/turning-whole-program-draft.json`)).then((response) => response.ok ? response.json() as Promise<ProgramArtifact> : null),
    ]).then(([rotational, artifact]) => { if (!cancelled) { setAnalysis(rotational); setProgram(artifact); setCurrentStage(0); } })
      .catch((reason) => !cancelled && setError(reason instanceof Error ? reason.message : "L32 CAM 数据加载失败"))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [jobId, wholePartBlockers]);
  const fallbackProfile: RotationalProfile = (() => {
    const size = Array.isArray(stock.size_mm) ? stock.size_mm.map(Number) : [];
    const length = Number(stock.length_mm ?? Math.max(...size, fallbackBounds?.size.z ?? 30, 30));
    const diameter = Number(stock.diameter_mm ?? Math.min(...size.filter((item) => item > 0), fallbackBounds?.size.x ?? 20, 20));
    return { id: "CAM-ENVELOPE", axis_id: "CAM-AXIS", side: "outer", extraction_method: "bounding_cylinder", points: [{ z: -length, radius: Math.max(diameter / 2 - 1, 1) }, { z: 0, radius: Math.max(diameter / 2 - 1, 1) }], confidence: 0.25, review_state: "review", review_reasons: [] };
  })();
  const outer = analysis?.profiles.filter((profile) => profile.side === "outer" && profile.review_state !== "excluded").sort((left, right) => Number(right.extraction_method === "exact_section") - Number(left.extraction_method === "exact_section"))[0] ?? fallbackProfile;
  const inner = analysis?.profiles.find((profile) => profile.side === "inner" && profile.review_state !== "excluded") ?? null;
  const stockRadius = Math.max(Number(stock.diameter_mm ?? 0) / 2, ...outer.points.map((point) => point.radius), 1);
  const stages = useMemo(() => program ? actualProgramStages(operations, program) : [], [operations, program]);
  const activeStage = stages[currentStage];
  const cutoffDatum = Number(program?.coordinate_frames.find((frame) => frame.channel_id === "sub")?.source_cutoff_z_mm ?? 0);
  const samples = useMemo(() => !program ? [] : activeStage?.channelId === "sub" ? program.continuous_simulation.sub_frame_final.samples.map((sample) => ({ ...sample, z: cutoffDatum - sample.z })).sort((left, right) => left.z - right.z) : program.continuous_simulation.main_frame_after_cutoff.samples, [activeStage?.channelId, cutoffDatum, program]);
  useEffect(() => {
    if (!playing || stages.length === 0) return undefined;
    const timer = window.setInterval(() => setCurrentStage((current) => { if (current >= stages.length - 1) { setPlaying(false); return current; } return current + 1; }), 850);
    return () => window.clearInterval(timer);
  }, [playing, stages.length]);
  const generate = async () => {
    setGenerating(true); setError("");
    try {
      const jobResponse = await fetch(apiUrl(`/api/v1/jobs/${jobId}`));
      const jobPayload = await jobResponse.json() as Job & { detail?: string };
      if (!jobResponse.ok || !jobPayload.machine_instance_id) throw new Error(jobPayload.detail || "请先绑定 L32 设备实例");
      const zValues = outer.points.map((point) => point.z), zMin = Math.min(...zValues), zMax = Math.max(...zValues), length = Math.max(zMax - zMin, 0.5);
      const cutoff = operations.find((operation) => operation.type === "turn_cutoff");
      const cutoffZ = Number(cutoff?.parameters.finished_back_datum_z_mm ?? cutoff?.parameters.z_mm ?? zMin);
      const response = await fetch(apiUrl(`/api/v1/jobs/${jobId}/turning/whole-program/draft`), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine_instance_id: jobPayload.machine_instance_id, source_profile_id: outer.id, stock_radius_mm: stockRadius, initial_bore_radius_mm: 0, resolution_mm: 0.05, approach_z_mm: zMax + 2, pickoff_z_mm: Math.min(zMax - 0.2, cutoffZ + length * 0.6), grip_length_mm: Math.min(Math.max(length * 0.3, 0.5), 8), synchronization_rpm: 1200, sub_spindle_clamp_confirmed: true }) });
      const payload = await response.json(); if (!response.ok) throw new Error(payload.detail || "L32 CAM 编译失败");
      setProgram(payload as ProgramArtifact); setCurrentStage(0); setPlaying(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "L32 CAM 编译失败"); } finally { setGenerating(false); }
  };
  if (wholePartBlockers.length) return <div className="l32-quick-backdrop" onMouseDown={onClose}>
    <section className="l32-quick l32-quick-blocked" onMouseDown={(event) => event.stopPropagation()}>
      <header><div><span><AlertTriangle size={19} /></span><div><small>L32 CAM</small><strong>整件程序未通过几何和工序检查</strong></div></div><button aria-label="关闭" onClick={onClose}><X size={18} /></button></header>
      <div className="l32-quick-blocked-body"><p>当前任务已有的回转刀路仅覆盖局部区域，不能作为完整零件的累计仿真或成品结果。</p>
        <ul>{wholePartBlockers.map((reason) => <li key={reason}>{reason}</li>)}</ul>
        <p>请补齐非回转特征的加工工序、核实设备能力和整件三维结果，再编译整件程序。</p>
      </div>
      <footer><button className="engineering" onClick={onOpenEngineering}><Gauge size={15} />查看工程参数</button><button onClick={onClose}>返回原始零件</button></footer>
    </section>
  </div>;
  return <div className="l32-quick-backdrop" onMouseDown={onClose}><section className="l32-quick" onMouseDown={(event) => event.stopPropagation()}>
    <header><div><span><Route size={19} /></span><div><small>L32 CAM PROGRAM</small><strong>L32 工序、刀路与连续材料仿真</strong></div></div><div className="l32-quick-header-actions"><button aria-label="关闭" onClick={onClose}><X size={18} /></button></div></header>
    {loading ? <div className="l32-quick-loading"><LoaderCircle className="spin" />正在读取 L32 CAM 程序…</div> : <div className="l32-quick-body"><aside>
      <div className="l32-quick-summary"><div><span>工序</span><strong>{stages.length}</strong><small>道已编译</small></div><div><span>指令</span><strong>{program?.toolpath.channels.reduce((count, channel) => count + channel.commands.length, 0) ?? 0}</strong><small>Toolpath IR</small></div><div><span>材料去除</span><strong>{program?.continuous_simulation.total_removed_volume_mm3.toFixed(2) ?? "—"}</strong><small>mm³</small></div></div>
      <div className="l32-quick-stage-list">{stages.map((stage, index) => <button key={`${stage.operation.id}-${index}`} className={`${index === currentStage ? "active" : ""} ${index < currentStage ? "done" : ""}`} onClick={() => { setPlaying(false); setCurrentStage(index); }}><span>{index < currentStage ? <Check size={12} /> : String(index + 1).padStart(2, "0")}</span><div><strong>{stage.operation.name}</strong><small>{stage.operation.id} · {stage.channelId.toUpperCase()} · {stage.commandCount} 指令</small></div><em className={stage.points.length > 1 ? "preview" : "event"}>{stage.points.length > 1 ? "刀路" : "机床动作"}</em><ChevronRight size={13} /></button>)}</div>
    </aside><main><div className="l32-quick-view-head"><div><small>当前工序</small><strong>{activeStage ? `${activeStage.operation.id} · ${activeStage.operation.name}` : "等待 CAM 编译"}</strong></div><div><span><i className="target-key" />目标轮廓</span><span><i className="path-key" />真实刀路</span><span><i className="stock-key" />仿真余料</span></div></div>
      <ProgramChart outer={outer} inner={inner} stockRadius={stockRadius} stages={stages} currentStage={currentStage} samples={samples} />
      <div className="l32-quick-timeline"><div><span style={{ width: `${stages.length ? (currentStage + 1) / stages.length * 100 : 0}%` }} /></div><input aria-label="仿真阶段" type="range" min="0" max={Math.max(stages.length - 1, 0)} value={currentStage} onChange={(event) => { setPlaying(false); setCurrentStage(Number(event.target.value)); }} /><small>{stages.length ? currentStage + 1 : 0} / {stages.length}</small></div>
      {error && <div className="l32-quick-warning"><AlertTriangle size={15} />{error}</div>}{program && <div className="l32-program-proof"><Check size={15} /><span>连续材料仿真 {program.continuous_simulation.status === "passed" ? "通过" : "失败"} · 程序哈希 {program.program_hash.slice(0, 16)}</span></div>}
    </main></div>}
    <footer><button className="engineering" onClick={onOpenEngineering}><Gauge size={15} />工程参数</button><div>{program && <button className="playback" onClick={() => setPlaying((value) => !value)}>{playing ? <CirclePause size={15} /> : <Play size={15} />}{playing ? "暂停" : "播放"}</button>}<button className="generate" disabled={loading || generating} onClick={generate}>{generating ? <LoaderCircle className="spin" size={15} /> : program ? <RotateCcw size={15} /> : <Play size={15} />}{generating ? "正在编译…" : program ? "重新编译真实刀路" : "编译 L32 真实刀路"}</button></div></footer>
  </section></div>;
}

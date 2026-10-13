import type { ToolpathSegment, TurningStageView, TurningStockSample, Vec3 } from "../types";

export type MachiningPreview = {
  operation_id: string; revision: string; status: string; reason?: string | null; can_accept: boolean;
  evidence: { removed_volume_mm3?: number; verification_status?: string; profile_protection_status?: string;
    material_domain?:string;stock_mesh_url?:string;
    diagnostic_hints?: string[];
    reachability_checks?: { id: string; status: string; message: string; measured_value?: number | string; limit_value?: number | string }[] };
  axis: { origin: Vec3; direction: Vec3 } | null;
  channel_id: "main" | "sub"; source_cutoff_z: number | null;
  coordinate_frame?: "source_model" | "sub_local";
  toolpath?: { channels: { id: string; commands: { type: string; axes: Record<string, number>; parameters?: Record<string, unknown> }[] }[] } | null;
  before_samples: TurningStockSample[]; after_samples: TurningStockSample[];
  material_frames?: { command_index: number; fraction: number; changes: [number, number, number][] }[];
};

/** Convert emitted diameter-X/Z moves to model coordinates; non-motion IR stays hidden. */
export function previewSegments(preview: MachiningPreview): ToolpathSegment[] {
  if (!preview.axis || !preview.toolpath) return [];
  const { origin, direction } = preview.axis;
  const length = Math.hypot(direction.x, direction.y, direction.z);
  if (!length) return [];
  const axis = { x: direction.x / length, y: direction.y / length, z: direction.z / length };
  const helper = Math.abs(axis.z) < 0.9 ? { x: 0, y: 0, z: 1 } : { x: 0, y: 1, z: 0 };
  const cross = { x: axis.y * helper.z - axis.z * helper.y, y: axis.z * helper.x - axis.x * helper.z, z: axis.x * helper.y - axis.y * helper.x };
  const norm = Math.hypot(cross.x, cross.y, cross.z);
  const radial = { x: cross.x / norm, y: cross.y / norm, z: cross.z / norm };
  const point = (x: number, z: number) => {
    const axial = subLocal(preview) ? preview.source_cutoff_z! - z : z;
    return { x: origin.x + axis.x * axial + radial.x * Math.abs(x) / 2,
      y: origin.y + axis.y * axial + radial.y * Math.abs(x) / 2,
      z: origin.z + axis.z * axial + radial.z * Math.abs(x) / 2 };
  };
  const segments: ToolpathSegment[] = [];
  for (const channel of preview.toolpath.channels) {
    const position: Record<string, number> = {};
    for (const command of channel.commands) {
      const previous = { ...position };
      for (const [key, value] of Object.entries(command.axes)) position[key.toUpperCase()] = value;
      // Arc IR needs controller-specific interpolation; never draw a chord as a cutting arc.
      if (!["rapid_move", "feed_move"].includes(command.type)
        || ![previous.X, previous.Z, position.X, position.Z].every(Number.isFinite)) continue;
      const start = point(previous.X, previous.Z), end = point(position.X, position.Z);
      if (Math.hypot(end.x - start.x, end.y - start.y, end.z - start.z) < 1e-6) continue;
      segments.push({ operation_id: preview.operation_id, setup_id: channel.id, motion: command.type === "rapid_move" ? "rapid" : "cut",
        x1: start.x, y1: start.y, z1: start.z, x2: end.x, y2: end.y, z2: end.z, work_axis: radial });
    }
  }
  return segments;
}

export function previewMaterial(preview: MachiningPreview): TurningStageView | null {
  if (!preview.axis || !preview.before_samples.length || !preview.after_samples.length) return null;
  const transform = (samples: TurningStockSample[]) => subLocal(preview)
    ? samples.map(sample => ({ ...sample, z: preview.source_cutoff_z! - sample.z })).sort((a, b) => a.z - b.z) : samples;
  return { operation_id: preview.operation_id, channel_id: preview.channel_id,
    axis_origin: preview.axis.origin, axis_direction: preview.axis.direction,
    before_samples: transform(preview.before_samples), after_samples: transform(preview.after_samples),
    material_frames: preview.material_frames?.length
      ? materialFrames(preview).map(frame => ({ ...frame, samples: transform(frame.samples) })) : undefined };
}

function subLocal(preview: MachiningPreview): boolean {
  return preview.channel_id === "sub" && preview.coordinate_frame !== "source_model"
    && typeof preview.source_cutoff_z === "number" && Number.isFinite(preview.source_cutoff_z);
}

/** Match solver checkpoints to the same distance-weighted IR movement used by the viewer. */
function commandTimeline(preview: MachiningPreview) {
  let total = 0;
  const timeline: { start: number; end: number }[] = [];
  for (const channel of preview.toolpath?.channels || []) {
    const position: Record<string, number> = {};
    for (const command of channel.commands) {
      const previous = { ...position };
      for (const [key, value] of Object.entries(command.axes)) position[key.toUpperCase()] = value;
      const distance = [previous.X, previous.Z, position.X, position.Z].every(Number.isFinite)
        && ["rapid_move", "feed_move"].includes(command.type)
        ? Math.hypot((position.X - previous.X) / 2, position.Z - previous.Z) : 0;
      const weight = distance > 1e-6 ? Math.max(.01, distance / (command.type === "rapid_move" ? 4 : 1)) : 0;
      timeline.push({ start: total, end: total + weight }); total += weight;
    }
  }
  return { timeline, total };
}

function materialFrames(preview: MachiningPreview) {
  const { timeline, total } = commandTimeline(preview);
  let samples = preview.before_samples.map(sample => ({ ...sample }));
  const frames = [{ progress: 0, samples, operation_id: preview.operation_id }];
  for (const frame of preview.material_frames || []) {
    samples = samples.map(sample => ({ ...sample }));
    for (const [index, outer_radius, inner_radius] of frame.changes) {
      if (samples[index]) samples[index] = { ...samples[index], outer_radius, inner_radius };
    }
    const command = timeline[frame.command_index];
    const progress = total && command ? (command.start + frame.fraction * (command.end - command.start)) / total : 1;
    frames.push({ progress, samples, operation_id: preview.operation_id });
  }
  return frames;
}

/** Stitch recorded material states; use the first stock and the last computed remainder. */
export function cumulativeMaterial(previews: MachiningPreview[]): TurningStageView | null {
  const stages = previews.map(previewMaterial);
  if (!previews.length || stages.some(stage => !stage?.material_frames?.length)) return null;
  const weights = previews.map(preview => commandTimeline(preview).total || 1);
  const total = weights.reduce((sum, value) => sum + value, 0);
  let offset = 0;
  const frames: NonNullable<TurningStageView["material_frames"]> = [];
  stages.forEach((stage, index) => {
    for (const frame of stage!.material_frames!) frames.push({ ...frame, progress: (offset + frame.progress * weights[index]) / total });
    offset += weights[index];
  });
  return { ...stages[0]!, operation_id: previews.at(-1)!.operation_id,
    after_samples: stages.at(-1)!.after_samples, material_frames: frames };
}

export function machiningEventStage(event: { stage: string; subgraph?: string; viewer?: { mode?: string; kind?: string } | null }) {
  if (event.viewer?.mode === "toolpath" || event.viewer?.mode === "刀路") return "toolpath";
  if (event.viewer?.mode === "simulation" || event.viewer?.mode === "仿真") return "simulation";
  if (/trial|validation|execution|candidate_sandbox|finalize|cam/.test(event.stage)) return "simulation";
  if (/toolpath|compile/.test(event.stage)) return "toolpath";
  if (/repair|plan/.test(event.stage) || event.viewer?.kind === "operation" || event.subgraph === "planning") return "process";
  return "features";
}

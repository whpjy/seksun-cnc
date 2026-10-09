import type { AgentWorkspace, Job } from "../types";
import type { JournalEvent } from "./harness";

export type TrialState = { accepted_operation_ids: string[]; formally_accepted_operation_ids?: string[]; provisional_operation_ids?: string[]; production_ready?: boolean };
export type Delivery = { path: string; label: string };
export function finalText(events: JournalEvent[]): string {
  const lastEnd = events.filter(e => e.type === "turn/end").at(-1);
  if (!lastEnd) return "";
  const message = events.filter(e => e.type === "assistant/message" && e.seq < lastEnd.seq).at(-1);
  const content = (message?.data.message as { content?: {type: string; text?: string}[] })?.content;
  return (content || []).filter(b => b.type === "text").map(b => b.text || "").join("\n");
}
export function deliveriesIn(events: JournalEvent[], text: string): Delivery[] {
  const files = new Map<string, Delivery>();
  for (const event of events) if (event.type === "deliverables/presented" && Array.isArray(event.data.files)) {
    for (const file of event.data.files as {path: string; description?: string}[]) if (typeof file.path === "string") files.set(file.path, {path:file.path,label:file.description || file.path.split(/[\\/]/).at(-1)!});
  }
  for (const match of text.matchAll(/\[([^\]]+)\]\((\/workspace\/[^)]+)\)/g)) if (!files.has(match[2])) files.set(match[2], {path:match[2],label:match[1]});
  return [...files.values()];
}
export function outcome(job: Job | null, workspace: AgentWorkspace | null, trials: TrialState | null) {
  const coverage = job?.plan?.coverage;
  const accepted = trials?.accepted_operation_ids || [];
  const last = workspace?.events.filter(e => e.stage === "harness_plan_finalize").at(-1);
  const ready = coverage?.production_ready === true && trials?.production_ready === true;
  return { label: ready ? "验证通过" : last?.status === "blocked" || coverage?.status === "incomplete" || coverage?.status === "review" ? "部分验证通过" : accepted.length ? "工序试算已完成" : "工艺草案待验证", ready, accepted, coverage };
}

export function operationState(id: string, trials: TrialState | null, workspace: AgentWorkspace | null): string {
  if (trials?.provisional_operation_ids?.includes(id)) return "临时接受";
  if (trials?.accepted_operation_ids.includes(id)) return "试算已接受";
  const event = workspace?.events.filter(e => (e as typeof e & {operation_id?:string}).operation_id === id && e.kind === "validation").at(-1);
  return event?.status === "blocked" || event?.status === "failed" ? "验证受阻" : "待验证";
}
